"""Small, asynchronous client for OpenAI-compatible vision endpoints."""
from __future__ import annotations

import json
from typing import Any

import httpx


class ModelError(RuntimeError):
    """A model request failed without producing a usable answer."""


class VisionModel:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 90):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def complete(self, messages: list[dict[str, Any]], max_tokens: int = 768,
                       json_mode: bool = False) -> str:
        if not self.configured:
            raise ModelError("尚未配置视觉模型服务地址和模型名称。")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                trust_env=False,
            )
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.15, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Do not copy upstream request bodies, prompts or credentials into SSE.
            raise ModelError(f"视觉模型服务返回 HTTP {exc.response.status_code}。请检查模型部署日志。") from exc
        except httpx.TimeoutException as exc:
            raise ModelError(f"视觉模型请求超过 {self.timeout:g} 秒，后续观察会继续重试。") from exc
        except httpx.HTTPError as exc:
            raise ModelError("无法连接视觉模型服务，请检查服务地址和运行状态。") from exc
        try:
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("模型输出达到长度上限，结果被截断；本次不会记为成功观察或完整答案。")
            content = choice["message"]["content"]
            if isinstance(content, list):
                content = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return content.strip()
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError("视觉模型没有返回有效 content，检查输出长度与非思考模式配置。") from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
