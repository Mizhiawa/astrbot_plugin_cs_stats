"""LLM 调用封装：支持 AstrBot 原生 Provider 与独立 OpenAI 兼容接口两种模式。"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import LLMSettings
from .logging_util import get_exc_desc, logger


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None = None
    elapsed: float = 0.0


_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _is_retryable(exc: Exception) -> bool:
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    if isinstance(
        exc,
        (
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
            InternalServerError,
            httpx.TimeoutException,
            httpx.NetworkError,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return False


def _extract_openai_content(resp: Any) -> str:
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return str(content or "")


async def _call_custom_openai(
    settings: LLMSettings, system_prompt: str, user_prompt: str
) -> ChatResult:
    from openai import AsyncOpenAI

    if not settings.api_key or not settings.model:
        raise ValueError("custom 模式下必须配置 llm.api_key 与 llm.model")

    extra_body: dict[str, Any] = dict(settings.extra_body or {})
    if settings.thinking_enabled:
        extra_body["thinking"] = {"type": "enabled"}

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if settings.max_tokens and settings.max_tokens > 0:
        kwargs["max_tokens"] = int(settings.max_tokens)
    if settings.reasoning_effort:
        kwargs["reasoning_effort"] = settings.reasoning_effort
    if extra_body:
        kwargs["extra_body"] = extra_body

    http_client = httpx.AsyncClient(proxy=settings.proxy, timeout=settings.timeout)
    client = AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url or None,
        timeout=settings.timeout,
        max_retries=0,
        http_client=http_client,
    )
    t0 = time.time()
    max_retries = 2
    try:
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.chat.completions.create(**kwargs)
                break
            except Exception as exc:
                if not _is_retryable(exc) or attempt >= max_retries:
                    raise
                delay = 2**attempt
                logger.info(
                    f"[cs_stats] LLM 请求失败 (model={settings.model})，{delay}s 后重试 "
                    f"({attempt + 1}/{max_retries}): {get_exc_desc(exc)}"
                )
                await asyncio.sleep(delay)
    finally:
        await client.close()

    finish_reason = resp.choices[0].finish_reason if resp and resp.choices else None
    return ChatResult(
        content=_extract_openai_content(resp),
        finish_reason=finish_reason,
        elapsed=max(0.0, time.time() - t0),
    )


async def _call_astrbot_llm(
    context: Any,
    umo: str | None,
    provider_id: str,
    system_prompt: str,
    user_prompt: str,
) -> ChatResult:
    """通过 AstrBot 4.x 原生 LLM 接口生成文本。"""
    t0 = time.time()
    target_provider_id = provider_id.strip()

    # 优先使用 AstrBot 4.x 标准的 llm_generate
    if hasattr(context, "llm_generate"):
        if not target_provider_id and hasattr(context, "get_current_chat_provider_id"):
            target_provider_id = await context.get_current_chat_provider_id(umo=umo)
        if target_provider_id:
            resp = await context.llm_generate(
                chat_provider_id=target_provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = getattr(resp, "completion_text", None) or getattr(resp, "completion", None) or ""
            finish_reason = None
            raw = getattr(resp, "raw_completion", None)
            if raw is not None and getattr(raw, "choices", None):
                finish_reason = getattr(raw.choices[0], "finish_reason", None)
            return ChatResult(
                content=str(text),
                finish_reason=finish_reason,
                elapsed=max(0.0, time.time() - t0),
            )

    # 兼容退回：get_using_provider_async / get_using_provider -> text_chat
    provider = None
    if hasattr(context, "get_using_provider_async"):
        provider = await context.get_using_provider_async(umo=umo)
    elif hasattr(context, "get_using_provider"):
        provider = context.get_using_provider(umo=umo)

    if provider is None:
        raise RuntimeError("AstrBot 当前未配置可用的大语言模型 Provider")

    resp = await provider.text_chat(
        prompt=user_prompt,
        system_prompt=system_prompt,
    )
    text = getattr(resp, "completion_text", None) or getattr(resp, "completion", None) or str(resp)
    return ChatResult(content=str(text), finish_reason=None, elapsed=max(0.0, time.time() - t0))


async def chat_completion(
    settings: LLMSettings,
    system_prompt: str,
    user_prompt: str,
    *,
    context: Any = None,
    umo: str | None = None,
) -> ChatResult:
    if settings.mode == "custom":
        return await _call_custom_openai(settings, system_prompt, user_prompt)
    if context is None:
        raise RuntimeError("astrbot 模式下缺少 context 实例")
    return await _call_astrbot_llm(
        context, umo, settings.astrbot_provider_id, system_prompt, user_prompt
    )
