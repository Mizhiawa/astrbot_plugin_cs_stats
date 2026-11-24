"""插件配置封装：将 AstrBotConfig（dict）映射为类型安全的配置对象，支持热重载与落盘。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_cs_stats"
QUERY_FEATURE = "cs_stats:query"
ROAST_FEATURE = "cs_stats:roast"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _to_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _to_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in ("1", "true", "yes", "on"):
            return True
        if lower in ("0", "false", "no", "off"):
            return False
    return default


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class LLMSettings:
    mode: str = "astrbot"
    astrbot_provider_id: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = 120.0
    proxy: str | None = None
    max_tokens: int | None = 4096
    thinking_enabled: bool = False
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderSettings:
    backend: str = "auto"
    chromium_executable: str = ""
    image_timeout_ms: int = 15_000
    scale: float = 2.0
    font_dir: str = ""
    playwright_browsers_path: str = ""
    watermark_text: str = "Designed by Hakuchumu\nPowered by AstrBot"


@dataclass
class CSStatsConfig:
    pw_token: str = ""
    pw_steam_id: str = ""
    pw_appversion: str = "4.2.1.232"
    http_timeout: float = 20.0
    steam_avatar_timeout: float = 3.0
    steam_proxy: str | None = None
    max_accounts_per_platform: int = 3
    pvp_season_lookback: int = 4
    command_prefix: str = ""
    allow_private: bool = True
    enabled_groups: list[str] = field(default_factory=list)
    disabled_groups: list[str] = field(default_factory=list)
    query_cooldown_seconds: int = 10
    roast_cooldown_seconds: int = 30
    session_timeout_seconds: int = 60
    llm: LLMSettings = field(default_factory=LLMSettings)
    render: RenderSettings = field(default_factory=RenderSettings)

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "CSStatsConfig":
        data = raw if isinstance(raw, dict) else {}
        llm_raw = data.get("llm") if isinstance(data.get("llm"), dict) else {}
        render_raw = data.get("render") if isinstance(data.get("render"), dict) else {}

        llm_mode = _to_str(llm_raw.get("mode"), "astrbot").lower()
        if llm_mode not in ("astrbot", "custom"):
            llm_mode = "astrbot"

        max_tokens_val = llm_raw.get("max_tokens", 4096)
        max_tokens = (
            _to_int(max_tokens_val, 4096, minimum=0) if max_tokens_val not in (None, "") else None
        )
        if max_tokens == 0:
            max_tokens = None

        proxy = _to_str(llm_raw.get("proxy"), "") or None
        reasoning_effort = _to_str(llm_raw.get("reasoning_effort"), "") or None
        extra_body = llm_raw.get("extra_body") if isinstance(llm_raw.get("extra_body"), dict) else {}

        # 兼容顶层直接写 watermark_text 的旧配置
        watermark = render_raw.get("watermark_text")
        if watermark is None:
            watermark = data.get("watermark_text")
        if watermark is None:
            watermark = "Designed by Hakuchumu\nPowered by AstrBot"

        render_backend = _to_str(render_raw.get("backend"), "auto").lower()
        if render_backend not in ("auto", "playwright", "astrbot"):
            render_backend = "auto"

        return cls(
            pw_token=_to_str(data.get("pw_token"), ""),
            pw_steam_id=_to_str(data.get("pw_steam_id"), ""),
            pw_appversion=_to_str(data.get("pw_appversion"), "4.2.1.232") or "4.2.1.232",
            http_timeout=_to_float(data.get("http_timeout"), 20.0, minimum=1.0),
            steam_avatar_timeout=_to_float(data.get("steam_avatar_timeout"), 3.0, minimum=0.0),
            steam_proxy=_to_str(data.get("steam_proxy"), "") or None,
            max_accounts_per_platform=_to_int(
                data.get("max_accounts_per_platform"), 3, minimum=1
            ),
            pvp_season_lookback=_to_int(data.get("pvp_season_lookback"), 4, minimum=0),
            command_prefix=str(data.get("command_prefix") or ""),
            allow_private=_to_bool(data.get("allow_private"), True),
            enabled_groups=_to_str_list(data.get("enabled_groups")),
            disabled_groups=_to_str_list(data.get("disabled_groups")),
            query_cooldown_seconds=_to_int(data.get("query_cooldown_seconds"), 10, minimum=0),
            roast_cooldown_seconds=_to_int(data.get("roast_cooldown_seconds"), 30, minimum=0),
            session_timeout_seconds=_to_int(data.get("session_timeout_seconds"), 60, minimum=10),
            llm=LLMSettings(
                mode=llm_mode,
                astrbot_provider_id=_to_str(llm_raw.get("astrbot_provider_id"), ""),
                api_key=_to_str(llm_raw.get("api_key"), ""),
                base_url=_to_str(llm_raw.get("base_url"), ""),
                model=_to_str(llm_raw.get("model"), ""),
                timeout=_to_float(llm_raw.get("timeout"), 120.0, minimum=5.0),
                proxy=proxy,
                max_tokens=max_tokens,
                thinking_enabled=_to_bool(llm_raw.get("thinking_enabled"), False),
                reasoning_effort=reasoning_effort,
                extra_body=dict(extra_body),
            ),
            render=RenderSettings(
                backend=render_backend,
                chromium_executable=_to_str(render_raw.get("chromium_executable"), ""),
                image_timeout_ms=_to_int(render_raw.get("image_timeout_ms"), 15_000, minimum=2000),
                scale=_to_float(render_raw.get("scale"), 2.0, minimum=1.0),
                font_dir=_to_str(render_raw.get("font_dir"), ""),
                playwright_browsers_path=_to_str(render_raw.get("playwright_browsers_path"), ""),
                watermark_text=str(watermark),
            ),
        )
