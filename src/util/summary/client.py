from ..common._json import json_loads
from ..network.request import get_mounts
from ..network.proxy import Proxy

from typing import Optional
import httpx
import logging

logger = logging.getLogger(__name__)

class LLMError(RuntimeError):
    """LLM 接口调用异常"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)

        self.status_code = status_code

class SummaryCancelled(RuntimeError):
    """AI 总结被用户取消"""
    pass

class LLMClient:
    """OpenAI 兼容接口（chat/completions）客户端，用于生成视频内容总结。

    任何实现了 OpenAI 兼容协议的服务均可使用（DeepSeek、通义千问兼容模式、OpenAI 等），
    通过自定义 base_url 与 model 切换服务商。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

        self.client = httpx.Client(
            mounts = get_mounts(Proxy().get_proxies()),
            timeout = httpx.Timeout(connect = 30, read = 300, write = 60, pool = 60),
            follow_redirects = True
        )

    def chat(self, user_content: str, cancel_check = None) -> str:
        # 发起一次对话补全请求，返回助手的文本回复
        if cancel_check is not None and cancel_check():
            raise SummaryCancelled()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "stream": False
        }

        response = self.client.post(
            f"{self.base_url}/chat/completions",
            json = payload,
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
        )

        return self._extract_content(response)

    def _extract_content(self, response: httpx.Response) -> str:
        try:
            data = json_loads(response.text) if response.content else {}
        except Exception:
            response.raise_for_status()
            raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}", response.status_code)

        # OpenAI 兼容接口出错时响应体带 error.message 字段
        if response.status_code >= 400:
            error = data.get("error") or {}
            message = error.get("message") or data.get("message") or response.text[:200]

            raise LLMError(f"HTTP {response.status_code}: {message}", response.status_code)

        choices = data.get("choices") or []

        if not choices:
            raise LLMError(f"总结接口响应格式异常：{data}")

        content = (choices[0].get("message") or {}).get("content") or ""

        # 部分推理模型会将思考过程放在 reasoning_content 中，content 才是正式回复
        return content.strip()

    def close(self):
        self.client.close()
