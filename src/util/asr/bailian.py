from ..common._json import json_loads
from ..common.config import config
from ..network.request import get_mounts
from ..network.proxy import Proxy

from pathlib import Path
from typing import Optional
import httpx
import time
import logging

logger = logging.getLogger(__name__)

class DashScopeError(RuntimeError):
    """DashScope 接口调用异常"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)

        self.status_code = status_code

class ASRCancelled(RuntimeError):
    """语音转写被用户取消"""
    pass

class DashScopeASRClient:
    """阿里云百炼（DashScope）非实时语音识别客户端。

    本地音频文件无法直接提交识别，需要先通过百炼临时存储空间上传获得 oss:// URL（48 小时有效），
    再提交异步转写任务并轮询结果。参考文档：
    - 上传本地文件获取临时 URL：https://help.aliyun.com/zh/model-studio/get-temporary-file-url
    - 非实时语音识别：https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide
    """

    base_url = "https://dashscope.aliyuncs.com"

    # 轮询间隔、重试策略与总超时（秒）
    poll_interval = 3.0
    poll_retry_interval = 5.0
    poll_max_retries = 10
    poll_timeout = 3600.0

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

        self.client = httpx.Client(
            mounts = get_mounts(Proxy().get_proxies()),
            timeout = httpx.Timeout(connect = 30, read = 600, write = 600, pool = 60),
            follow_redirects = True
        )

    def _auth_headers(self, extra: dict = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        if extra:
            headers.update(extra)

        return headers

    def _request_json(self, response: httpx.Response) -> dict:
        try:
            data = json_loads(response.text) if response.content else {}
        except Exception:
            response.raise_for_status()
            raise DashScopeError(f"HTTP {response.status_code}: {response.text[:200]}", response.status_code)

        # DashScope 出错时返回非 200 状态码，响应体带 code 与 message 字段
        if response.status_code >= 400 or ("code" in data and "message" in data and "output" not in data):
            code = data.get("code", response.status_code)
            message = data.get("message", response.text[:200])

            raise DashScopeError(f"[{code}] {message}", response.status_code)

        return data

    def get_upload_policy(self) -> dict:
        # 获取百炼临时存储空间的上传凭证
        response = self.client.get(
            f"{self.base_url}/api/v1/uploads",
            params = {"action": "getPolicy", "model": self.model},
            headers = self._auth_headers()
        )

        data = self._request_json(response)

        if "data" not in data:
            raise DashScopeError(f"上传凭证响应格式异常：{data}")

        return data["data"]

    def upload_file(self, file_path: Path) -> str:
        # 上传本地文件至百炼临时存储空间，返回 oss:// URL（48 小时有效）
        policy = self.get_upload_policy()

        key = f"{policy['upload_dir']}/{file_path.name}"

        # OSS PostObject 表单上传，file 域必须位于最后，httpx 会将 data 中的字段排在 files 之前
        form_fields = {
            "OSSAccessKeyId": policy["oss_access_key_id"],
            "Signature": policy["signature"],
            "policy": policy["policy"],
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
            "key": key,
            "success_action_status": "200"
        }

        with open(file_path, "rb") as f:
            response = self.client.post(
                policy["upload_host"],
                data = form_fields,
                files = {"file": (file_path.name, f)}
            )

        if response.status_code != 200:
            raise DashScopeError(f"音频上传失败：HTTP {response.status_code}: {response.text[:200]}")

        logger.info(f"音频已上传至百炼临时存储空间：oss://{key}")

        return f"oss://{key}"

    def submit_transcription(self, file_url: str) -> str:
        # 提交异步转写任务，返回任务 ID
        # qwen3-asr 系列模型使用 input.file_url，fun-asr / paraformer 等模型使用 input.file_urls，失败时自动回退
        headers = self._auth_headers({
            "X-DashScope-Async": "enable",
            "X-DashScope-OssResourceResolve": "enable"    # 使用 oss:// 临时 URL 时必须携带
        })

        endpoint = f"{self.base_url}/api/v1/services/audio/asr/transcription"

        for input_body in ({"file_url": file_url}, {"file_urls": [file_url]}):
            payload = {
                "model": self.model,
                "input": input_body,
                "parameters": {
                    "enable_itn": True
                }
            }

            response = self.client.post(endpoint, json = payload, headers = headers)

            if response.status_code == 400 and "file_url" in input_body:
                logger.warning("使用 file_url 参数提交转写任务失败，尝试 file_urls 参数")
                continue

            data = self._request_json(response)

            task_id = data.get("output", {}).get("task_id")

            if not task_id:
                raise DashScopeError(f"转写任务提交响应格式异常：{data}")

            logger.info(f"转写任务已提交，task_id: {task_id}")

            return task_id

        raise DashScopeError("转写任务提交失败")

    def wait_for_result(self, task_id: str, cancel_check = None) -> dict:
        # 轮询任务状态直至完成，返回转写结果 JSON。
        # 单次轮询的网络错误或服务端 5xx 属于瞬时故障，做有限次重试，避免长时间转写因一次抖动而前功尽弃；
        # cancel_check 返回 True 表示用户已取消，抛出 ASRCancelled
        start_time = time.monotonic()
        failure_count = 0

        while True:
            if self._is_cancelled(cancel_check):
                raise ASRCancelled()

            try:
                response = self.client.get(
                    f"{self.base_url}/api/v1/tasks/{task_id}",
                    headers = self._auth_headers()
                )

                data = self._request_json(response)

            except DashScopeError as e:
                # 4xx 属于永久性错误（鉴权失败、任务不存在等），重试没有意义，直接抛出
                if e.status_code is None or e.status_code < 500:
                    raise

                failure_count = self._log_poll_failure(failure_count, e)

            except httpx.HTTPError as e:
                failure_count = self._log_poll_failure(failure_count, e)

            else:
                failure_count = 0

                output = data.get("output", {})
                status = str(output.get("task_status", "")).upper()

                if status == "SUCCEEDED":
                    return self._download_transcription(output)

                if status in ("FAILED", "UNKNOWN", "CANCELED"):
                    message = output.get("message", "")

                    for result in output.get("results") or []:
                        if result.get("message"):
                            message = f"{result.get('code', '')} {result.get('message', '')}".strip()

                    raise DashScopeError(f"转写任务{status}：{message}")

                if time.monotonic() - start_time > self.poll_timeout:
                    raise DashScopeError(f"转写任务超时（超过 {int(self.poll_timeout)} 秒）")

            if failure_count > self.poll_max_retries:
                raise DashScopeError(f"查询转写任务状态连续失败 {failure_count} 次，已中止转写")

            if self._interruptible_sleep(self.poll_retry_interval if failure_count else self.poll_interval, cancel_check):
                raise ASRCancelled()

    def _log_poll_failure(self, failure_count: int, e: Exception) -> int:
        failure_count += 1

        logger.warning(f"查询转写任务状态失败（第 {failure_count}/{self.poll_max_retries} 次），即将重试：{e}")

        return failure_count

    @staticmethod
    def _is_cancelled(cancel_check) -> bool:
        return cancel_check is not None and cancel_check()

    @staticmethod
    def _interruptible_sleep(duration: float, cancel_check) -> bool:
        # 分段睡眠以便及时响应取消请求，返回 True 表示已取消
        deadline = time.monotonic() + duration

        while True:
            if DashScopeASRClient._is_cancelled(cancel_check):
                return True

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return False

            time.sleep(min(0.2, remaining))

    def _download_transcription(self, output: dict) -> dict:
        # 获取结果下载地址。qwen3-asr 系列位于 output.result，fun-asr / paraformer 系列位于 output.results
        transcription_url = None

        for result in output.get("results") or []:
            if result.get("transcription_url"):
                transcription_url = result["transcription_url"]
                break

        if not transcription_url:
            transcription_url = (output.get("result") or {}).get("transcription_url")

        if not transcription_url:
            raise DashScopeError(f"转写结果响应中缺少 transcription_url：{output}")

        # 转写已完成，结果下载的瞬时网络错误同样重试，否则长时间转写会前功尽弃
        for attempt in range(self.poll_max_retries + 1):
            try:
                response = self.client.get(transcription_url)
                response.raise_for_status()

                return json_loads(response.text)

            except httpx.HTTPError as e:
                if attempt == self.poll_max_retries:
                    raise DashScopeError(f"下载转写结果失败：{e}") from e

                logger.warning(f"下载转写结果失败（第 {attempt + 1}/{self.poll_max_retries} 次重试）：{e}")

                time.sleep(self.poll_retry_interval)

    @staticmethod
    def extract_sentences(result: dict) -> list[dict]:
        # 从转写结果中提取句级文本（含毫秒级起止时间）
        sentences = []

        for transcript in result.get("transcripts", []):
            sentences.extend(transcript.get("sentences", []))

        return sentences

    def close(self):
        self.client.close()
