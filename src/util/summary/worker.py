from PySide6.QtCore import QThread, Signal, Qt

from ..common.enum import DownloadStatus, ToastNotificationCategory, TranscriptSource
from ..common.config import config
from ..common.translator import Translator
from ..common.signal_bus import signal_bus
from ..common._json import json_loads
from ..download.task.info import TaskInfo
from ..download.task.manager import task_manager

from .client import LLMClient, LLMError, SummaryCancelled

from pathlib import Path
from threading import Lock
import logging
import re

logger = logging.getLogger(__name__)

# B 站字幕可下载为多种格式，作为总结输入时按可读性选择格式
_CC_SUBTITLE_EXT_PRIORITY = ("srt", "txt", "lrc", "ass", "json")

def get_transcript_path(task_info: TaskInfo, source: TranscriptSource = None) -> Path | None:
    """定位任务的转写文本文件，不存在时返回 None。

    source 为 None 时按 ASR 转写 → B 站字幕的顺序自动匹配；指定来源时仅匹配该来源的文件。
    """
    cwd = Path(task_info.File.download_path, task_info.File.folder)

    if source is None or source == TranscriptSource.ASR:
        transcript_path = _find_asr_transcript_path(task_info, cwd)

        if transcript_path is not None:
            return transcript_path

    if source is None or source == TranscriptSource.CC:
        transcript_path = _find_cc_transcript_path(task_info, cwd)

        if transcript_path is not None:
            return transcript_path

    return None

def _find_asr_transcript_path(task_info: TaskInfo, cwd: Path) -> Path | None:
    # 语音转文字结果，优先使用纯文本（txt），其次使用字幕（srt）
    base_name = f"{task_info.File.name}.{Translator.ADDITIONAL_FILES_QUALIFIER('ASR')}"

    for ext in ("txt", "srt"):
        transcript_path = cwd / f"{base_name}.{ext}"

        if transcript_path.exists():
            return transcript_path

    return None

def _find_cc_transcript_path(task_info: TaskInfo, cwd: Path) -> Path | None:
    # B 站字幕文件名带语言代码（{name}.{qualifier}.{language}.{ext}），可能存在多个语言/格式，
    # 按格式可读性与文件名排序后取第一个，保证结果稳定
    prefix = f"{task_info.File.name}.{Translator.ADDITIONAL_FILES_QUALIFIER('SUBTITLES')}."

    if not cwd.exists():
        return None

    candidates = [path for path in cwd.iterdir() if path.is_file() and path.name.startswith(prefix)]

    if not candidates:
        return None

    def sort_key(path: Path):
        ext = path.suffix.lower().lstrip(".")

        priority = _CC_SUBTITLE_EXT_PRIORITY.index(ext) if ext in _CC_SUBTITLE_EXT_PRIORITY else len(_CC_SUBTITLE_EXT_PRIORITY)

        return (priority, path.name)

    return sorted(candidates, key = sort_key)[0]

def get_summary_path(task_info: TaskInfo) -> Path:
    """返回任务的 AI 总结文件路径（不保证存在）。"""
    cwd = Path(task_info.File.download_path, task_info.File.folder)

    return cwd / f"{task_info.File.name}.{Translator.ADDITIONAL_FILES_QUALIFIER('SUMMARY')}.md"

class SummaryWorker(QThread):
    """对语音转文字结果进行 AI 总结的工作线程。

    流程：读取转写文本（优先 txt，srt 则剥离时间轴）→ 按 Prompt 模板调用 OpenAI 兼容接口 → 写出 Markdown 总结文件。
    总结失败不影响任务完成，仅通过 Toast 提示。

    与 ASRWorker 相同，必须继承 QThread 并重写 run()（AsyncTask 的 moveToThread 模式会把 run() 投递回主线程阻塞界面）；
    线程对象不设置 parent，生命周期由类级注册表 _active_workers 管理。
    """

    success = Signal()
    error = Signal(str)

    # 运行中的总结线程（task_id → worker），供取消任务时定位并中断对应的线程
    _active_workers: dict[str, "SummaryWorker"] = {}

    def __init__(self, task_info: TaskInfo, update_download_status: bool = True):
        """
        update_download_status: 是否在下载列表中更新任务状态（下载流程内的自动总结为 True；
        已完成任务在查看器中手动重新生成时为 False，避免将已完成任务的状态改回处理中）。
        """
        super().__init__()

        self.task_info = task_info
        self.update_download_status = update_download_status

        self._client = None
        self._client_lock = Lock()

        SummaryWorker._active_workers[task_info.Basic.task_id] = self

        # 线程结束后从注册表移除并自毁
        self.finished.connect(self._on_finished)

    def run(self):
        try:
            self._check_cancelled()

            api_key = config.get(config.summary_api_key)

            if not api_key:
                raise LLMError(Translator.ERROR_MESSAGES("SUMMARY_NOT_CONFIGURED"))

            transcript = self._load_transcript()

            self._check_cancelled()

            self._client = LLMClient(
                config.get(config.summary_base_url),
                api_key,
                config.get(config.summary_model)
            )

            self._update_status(Translator.TIP_MESSAGES("GENERATING_SUMMARY"))

            summary = self._client.chat(self._build_prompt(transcript), cancel_check = self.isInterruptionRequested)

            self._check_cancelled()

            if not summary:
                raise LLMError(Translator.ERROR_MESSAGES("SUMMARY_EMPTY_RESULT"))

            self._write_output(summary)

            self.success.emit()

        except SummaryCancelled:
            logger.info(f"任务 {self.task_info.Basic.task_id} 的 AI 总结已取消")

        except Exception as e:
            if self.isInterruptionRequested():
                # 中断请求引发的连锁异常（如 HTTP 连接被 interrupt 关闭），按取消处理，不再提示错误
                logger.info(f"任务 {self.task_info.Basic.task_id} 的 AI 总结已取消")
            else:
                logger.exception("AI 总结失败")

                self.error.emit(str(e))

        finally:
            self._cleanup()

    def interrupt(self, on_finished = None):
        """请求中断总结，总结线程退出后回调 on_finished（排队至主线程执行）。"""
        self.requestInterruption()

        # 关闭 HTTP 客户端以打断进行中的请求，由此在 run() 内引发的异常按取消处理
        with self._client_lock:
            client = self._client

        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("关闭 AI 总结 HTTP 客户端时出错", exc_info = True)

        if on_finished is not None:
            self.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)

    @classmethod
    def stop_for_task(cls, task_id: str, on_finished = None):
        """中断指定任务的总结线程，线程退出后回调 on_finished；无运行中的线程时立即回调。"""
        worker = cls._active_workers.get(task_id)

        if worker is None or worker.isFinished():
            if on_finished is not None:
                on_finished()
        else:
            worker.interrupt(on_finished)

    @classmethod
    def is_running_for_task(cls, task_id: str) -> bool:
        worker = cls._active_workers.get(task_id)

        return worker is not None and not worker.isFinished()

    def _on_finished(self):
        SummaryWorker._active_workers.pop(self.task_info.Basic.task_id, None)

        self.deleteLater()

    def _check_cancelled(self):
        if self.isInterruptionRequested():
            raise SummaryCancelled()

    def _load_transcript(self) -> str:
        # 优先使用配置的字幕来源，对应文件不存在时回退到另一种来源
        source = config.get(config.summary_transcript_source)

        if not isinstance(source, TranscriptSource):
            source = TranscriptSource.ASR

        transcript_path = get_transcript_path(self.task_info, source) or get_transcript_path(self.task_info)

        if transcript_path is None:
            raise LLMError(Translator.ERROR_MESSAGES("SUMMARY_NO_TRANSCRIPT"))

        contents = transcript_path.read_text(encoding = "utf-8", errors = "replace")

        suffix = transcript_path.suffix.lower()

        if suffix == ".srt":
            contents = self._strip_srt_timeline(contents)

        elif suffix == ".lrc":
            contents = self._strip_lrc_timeline(contents)

        elif suffix == ".json":
            contents = self._extract_json_subtitle(contents)

        if not contents.strip():
            raise LLMError(Translator.ERROR_MESSAGES("SUMMARY_NO_TRANSCRIPT"))

        logger.info(f"任务 {self.task_info.Basic.task_id} 的 AI 总结使用转写文件：{transcript_path.name}")

        return contents

    @staticmethod
    def _strip_srt_timeline(contents: str) -> str:
        # 剥离 SRT 的序号与时间轴，仅保留字幕正文
        lines = []

        for line in contents.splitlines():
            line = line.strip()

            if not line or line.isdigit() or re.match(r"\d{2}:\d{2}:\d{2}[,.]\d+\s*-->", line):
                continue

            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _strip_lrc_timeline(contents: str) -> str:
        # 剥离 LRC 的时间标签与元数据标签，仅保留歌词正文
        lines = []

        for line in contents.splitlines():
            line = re.sub(r"^(\[[^\]]*\])+", "", line.strip())

            if line:
                lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _extract_json_subtitle(contents: str) -> str:
        # B 站字幕 JSON：提取 body 中的正文，解析失败时按原文返回
        try:
            body = json_loads(contents).get("body", [])
        except Exception:
            return contents

        return "\n".join(item.get("content", "") for item in body if isinstance(item, dict))

    def _build_prompt(self, transcript: str) -> str:
        prompt_template = config.get(config.summary_prompt) or ""

        if "{text}" in prompt_template:
            return prompt_template.replace("{text}", transcript)
        else:
            # 模板中缺少占位符时，将字幕内容追加到末尾，避免丢失转写文本
            return f"{prompt_template}\n\n{transcript}"

    def _write_output(self, summary: str):
        summary_path = get_summary_path(self.task_info)

        summary_path.parent.mkdir(parents = True, exist_ok = True)

        with open(summary_path, "w", encoding = "utf-8") as f:
            f.write(summary + "\n")

        # 记录生成的文件，供取消任务时清理；首次生成时累计任务大小（重新生成不重复累计）
        if summary_path.name not in self.task_info.File.relative_files:
            self.task_info.File.relative_files.append(summary_path.name)

            file_size = summary_path.stat().st_size
            self.task_info.Download.downloaded_size += file_size
            self.task_info.Download.total_size += file_size

        task_manager.update(self.task_info)

        logger.info(f"AI 总结完成，已生成文件：{summary_path.name}")

    def _update_status(self, label: str):
        if not self.update_download_status:
            return

        self.task_info.Download.status = DownloadStatus.ADDITIONAL_PROCESSING
        self.task_info.Download.status_label = label

        signal_bus.download.update_downloading_item.emit(self.task_info)

    def _cleanup(self):
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    logger.warning("关闭 AI 总结 HTTP 客户端时出错", exc_info = True)

                self._client = None
