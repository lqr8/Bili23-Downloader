from PySide6.QtCore import QThread, Signal, Qt

from ..common.enum import DownloadStatus, DownloadType, AsrOutputFormat, ToastNotificationCategory
from ..common.config import config
from ..common.translator import Translator
from ..common.signal_bus import signal_bus
from ..common.io.file import safe_remove
from ..download.task.info import TaskInfo
from ..download.task.manager import task_manager
from ..format.time import Time

from .bailian import DashScopeASRClient, DashScopeError, ASRCancelled

from pathlib import Path
from threading import Lock
import subprocess
import shutil
import logging
import os

logger = logging.getLogger(__name__)

class ASRWorker(QThread):
    """下载完成后对音频进行语音转文字的工作线程。

    流程：定位或提取音频文件 → 上传至百炼临时存储空间 → 提交异步转写任务 → 轮询结果 → 生成字幕或文本文件。
    转写失败不影响任务完成，仅通过 Toast 提示。

    必须继承 QThread 并重写 run()（与 FFmpegRunner 相同的模式）：
    AsyncTask 的 moveToThread + started.connect 组合在本应用中会把 run() 投递回主线程执行，
    长时间的转写轮询会阻塞界面。

    线程对象不设置 parent，生命周期由类级注册表 _active_workers 管理：
    任务完成或取消时 Downloader/Merger 对象树会被销毁，若本线程挂在对象树下会被连带销毁，
    导致 "QThread: Destroyed while thread is still running" 崩溃。
    """

    success = Signal()
    error = Signal(str)

    # 运行中的转写线程（task_id → worker），供取消任务时定位并中断对应的线程
    _active_workers: dict[str, "ASRWorker"] = {}

    def __init__(self, task_info: TaskInfo):
        super().__init__()

        self.task_info = task_info

        self._client = None
        self._extracted_audio: Path = None
        self._cleanup_lock = Lock()

        ASRWorker._active_workers[task_info.Basic.task_id] = self

        # 线程结束后从注册表移除并自毁
        self.finished.connect(self._on_finished)

    def run(self):
        try:
            self._check_cancelled()

            api_key = config.get(config.asr_api_key)

            if not api_key:
                raise DashScopeError(Translator.ERROR_MESSAGES("ASR_NOT_CONFIGURED"))

            audio_file = self._prepare_audio()

            if audio_file is None:
                logger.warning(f"任务 {self.task_info.Basic.task_id} 没有可用的音频文件，已跳过语音转文字")

                signal_bus.toast.show_long_message.emit(
                    ToastNotificationCategory.WARNING,
                    Translator.TIP_MESSAGES("ASR"),
                    Translator.TIP_MESSAGES("ASR_SKIPPED_NO_AUDIO")
                )

                self.success.emit()
                return

            self._check_cancelled()

            self._client = DashScopeASRClient(api_key, config.get(config.asr_model))

            self._update_status(Translator.TIP_MESSAGES("UPLOADING_AUDIO"))
            file_url = self._client.upload_file(audio_file)

            self._check_cancelled()

            self._update_status(Translator.TIP_MESSAGES("TRANSCRIBING_SPEECH"))
            task_id = self._client.submit_transcription(file_url)
            result = self._client.wait_for_result(task_id, cancel_check = self.isInterruptionRequested)

            self._check_cancelled()

            self._write_output(result)

            self.success.emit()

        except ASRCancelled:
            logger.info(f"任务 {self.task_info.Basic.task_id} 的语音转文字已取消")

        except Exception as e:
            if self.isInterruptionRequested():
                # 中断请求引发的连锁异常（如 HTTP 连接被 interrupt 关闭），按取消处理，不再提示错误
                logger.info(f"任务 {self.task_info.Basic.task_id} 的语音转文字已取消")
            else:
                logger.exception("语音转文字失败")

                self.error.emit(str(e))

        finally:
            self._cleanup()

    def interrupt(self, on_finished = None):
        """请求中断转写，转写线程退出后回调 on_finished（排队至主线程执行）。"""
        self.requestInterruption()

        # 关闭 HTTP 客户端以打断进行中的上传 / 轮询请求，由此在 run() 内引发的异常按取消处理
        client = self._client

        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("关闭语音转写 HTTP 客户端时出错", exc_info = True)

        if on_finished is not None:
            self.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)

    @classmethod
    def stop_for_task(cls, task_id: str, on_finished = None):
        """中断指定任务的转写线程，线程退出后回调 on_finished；无运行中的线程时立即回调。"""
        worker = cls._active_workers.get(task_id)

        if worker is None or worker.isFinished():
            if on_finished is not None:
                on_finished()
        else:
            worker.interrupt(on_finished)

    def _on_finished(self):
        ASRWorker._active_workers.pop(self.task_info.Basic.task_id, None)

        self.deleteLater()

    def _check_cancelled(self):
        if self.isInterruptionRequested():
            raise ASRCancelled()

    def _prepare_audio(self) -> Path:
        """定位用于转写的音频文件。

        优先直接使用已下载的音频流文件；否则从合并输出（或独立视频流）中用 FFmpeg 无损提取音轨。
        无可用音频时返回 None。
        """
        cwd = Path(self.task_info.File.download_path, self.task_info.File.folder)

        has_audio_stream = self.task_info.Download.type & DownloadType.AUDIO != 0

        if has_audio_stream and self.task_info.File.audio_file_ext:
            audio_file = cwd / f"{self.task_info.File.name}.{self.task_info.File.audio_file_ext}"

            if audio_file.exists():
                return audio_file

        for file_ext in (self.task_info.File.merge_file_ext, self.task_info.File.video_file_ext):
            if not file_ext:
                continue

            media_file = cwd / f"{self.task_info.File.name}.{file_ext}"

            if media_file.exists():
                if not self._has_audio_stream(media_file):
                    # 媒体文件不含音轨（如仅下载了 DASH 纯视频流），跳过提取
                    logger.warning(f"媒体文件 {media_file.name} 不含音轨，跳过语音转文字")
                    continue

                return self._extract_audio(media_file)

        return None

    def _has_audio_stream(self, media_file: Path) -> bool:
        """用 ffprobe 探测媒体文件是否包含音轨。

        ffprobe 不可用或探测失败时保守返回 True，交由后续的 ffmpeg 提取流程处理。
        """
        ffprobe = shutil.which("ffprobe.exe" if os.name == "nt" else "ffprobe")

        if ffprobe is None:
            logger.warning("未找到 ffprobe，跳过音轨探测，按存在音轨处理")

            return True

        # -select_streams a 只选择音频流，无音轨时输出为空
        command = [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", media_file.name]

        try:
            completed = subprocess.run(
                command,
                cwd = media_file.parent,
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                text = True,
                encoding = "utf-8",
                errors = "replace",
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})
            )

        except OSError as e:
            logger.warning(f"ffprobe 音轨探测失败：{e}")

            return True

        if completed.returncode != 0:
            logger.warning(f"ffprobe 音轨探测失败（return code: {completed.returncode}）：{completed.stderr[-500:]}")

            return True

        return completed.stdout.strip() != ""

    def _extract_audio(self, media_file: Path) -> Path:
        self._update_status(Translator.TIP_MESSAGES("EXTRACTING_AUDIO"))

        task_id = self.task_info.Basic.task_id

        # 优先无损提取音轨；容器不支持该编码时回退为转码 MP3（两种格式均在百炼支持列表内）
        extract_attempts = [
            (f"asr_{task_id}.m4a", ["-c:a", "copy"]),
            (f"asr_{task_id}.mp3", ["-c:a", "libmp3lame", "-q:a", "4"])
        ]

        for output_name, codec_args in extract_attempts:
            self._check_cancelled()

            output_file = media_file.parent / output_name

            command = ["ffmpeg", "-y", "-i", media_file.name, "-vn", *codec_args, output_file.name]

            try:
                completed = subprocess.run(
                    command,
                    cwd = media_file.parent,
                    stdout = subprocess.PIPE,
                    stderr = subprocess.PIPE,
                    text = True,
                    encoding = "utf-8",
                    errors = "replace",
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})
                )

            except FileNotFoundError as e:
                raise DashScopeError(Translator.ERROR_MESSAGES("FFMPEG_FAILED")) from e

            if completed.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                self._extracted_audio = output_file

                return output_file

            logger.warning(f"FFmpeg 提取音频失败（return code: {completed.returncode}）：{completed.stderr[-500:]}")

        raise DashScopeError(Translator.ERROR_MESSAGES("FFMPEG_FAILED"))

    def _write_output(self, result: dict):
        sentences = DashScopeASRClient.extract_sentences(result)

        if not sentences:
            raise DashScopeError(Translator.ERROR_MESSAGES("ASR_EMPTY_RESULT"))

        output_format = config.get(config.asr_output_format)

        base_name = f"{self.task_info.File.name}.{Translator.ADDITIONAL_FILES_QUALIFIER('ASR')}"
        cwd = Path(self.task_info.File.download_path, self.task_info.File.folder)

        generated_files = []

        if output_format in (AsrOutputFormat.SRT, AsrOutputFormat.BOTH):
            srt_contents = self._to_srt(sentences)
            srt_path = cwd / f"{base_name}.srt"

            srt_path.parent.mkdir(parents = True, exist_ok = True)

            with open(srt_path, "w", encoding = "utf-8") as f:
                f.write(srt_contents)

            generated_files.append(srt_path)

        if output_format in (AsrOutputFormat.TXT, AsrOutputFormat.BOTH):
            txt_contents = self._to_txt(sentences)
            txt_path = cwd / f"{base_name}.txt"

            txt_path.parent.mkdir(parents = True, exist_ok = True)

            with open(txt_path, "w", encoding = "utf-8") as f:
                f.write(txt_contents)

            generated_files.append(txt_path)

        # 记录生成的文件，供取消任务时清理；同时更新任务大小统计
        for path in generated_files:
            if path.name not in self.task_info.File.relative_files:
                self.task_info.File.relative_files.append(path.name)

            file_size = path.stat().st_size
            self.task_info.Download.downloaded_size += file_size
            self.task_info.Download.total_size += file_size

        task_manager.update(self.task_info)

        logger.info(f"语音转文字完成，已生成 {len(generated_files)} 个文件：{[path.name for path in generated_files]}")

    def _to_srt(self, sentences: list[dict]) -> str:
        srt_lines = []

        for i, sentence in enumerate(sentences):
            begin_time = sentence.get("begin_time", 0) / 1000
            end_time = sentence.get("end_time", 0) / 1000

            srt_lines.append(f"{i + 1}")
            srt_lines.append(f"{Time.format_srt_time(begin_time)} --> {Time.format_srt_time(end_time)}")
            srt_lines.append(str(sentence.get("text", "")).strip())
            srt_lines.append("")

        return "\n".join(srt_lines).strip()

    def _to_txt(self, sentences: list[dict]) -> str:
        return "\n".join(str(sentence.get("text", "")).strip() for sentence in sentences).strip()

    def _update_status(self, label: str):
        self.task_info.Download.status = DownloadStatus.ADDITIONAL_PROCESSING
        self.task_info.Download.status_label = label

        signal_bus.download.update_downloading_item.emit(self.task_info)

    def _cleanup(self):
        # 删除临时提取的音频文件（直接下载的音频流文件不删除）
        with self._cleanup_lock:
            if self._extracted_audio is not None:
                safe_remove(self._extracted_audio.parent, self._extracted_audio.name)
                self._extracted_audio = None

        if self._client is not None:
            self._client.close()
            self._client = None
