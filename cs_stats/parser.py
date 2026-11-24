"""命令分发与参数解析（纯逻辑模块，便于单元测试与跨适配器复用）。"""

from dataclasses import dataclass, field
from typing import Any

# 第一个是主命令词，其余是别名
MODE_WORDS: dict[str, tuple[str, ...]] = {
    "pvp": ("完美",),
    "mm": ("官匹",),
    "5e": ("5e", "5E"),
}
PLATFORM_WORDS: dict[str, tuple[str, ...]] = {
    "pw": ("完美", "官匹"),
    "5e": ("5e", "5E"),
}
MODE_PLATFORM: dict[str, str] = {"pvp": "pw", "mm": "pw", "5e": "5e"}
SU_MODES: dict[str, str] = {
    word.lower(): mode for mode, words in MODE_WORDS.items() for word in words
}

MAX_CANDIDATES = 5
# 最近第 N 局的 N 上限（完美对局列表一页实测能拉 50 场）
MAX_NTH = 20


class QueryError(Exception):
    """参数、绑定或数据不足，文案直接回给用户。"""


@dataclass(frozen=True)
class CommandSpec:
    action: str  # profile / single / career_roast / match_roast / bind / unbind / list / main / su_query / pw_reload / pw_login / help
    mode: str | None = None
    platform: str | None = None
    admin_only: bool = False


@dataclass
class ParsedCommand:
    spec: CommandSpec
    matched_word: str
    arg_text: str
    arg_mentions: list[str] = field(default_factory=list)
    reply_sender_id: str | None = None
    reply_message_id: str | None = None


@dataclass
class QueryArgs:
    target: str
    is_self: bool
    index: int
    nth: int
    refresh: bool


def _build_command_table() -> list[tuple[str, CommandSpec]]:
    table: dict[str, CommandSpec] = {}
    for mode, words in MODE_WORDS.items():
        platform = MODE_PLATFORM[mode]
        for word in words:
            table[(word + "战绩").lower()] = CommandSpec("profile", mode=mode, platform=platform)
            table[(word + "单局").lower()] = CommandSpec("single", mode=mode, platform=platform)
            table[(word + "锐评").lower()] = CommandSpec(
                "career_roast", mode=mode, platform=platform
            )
            table[(word + "单局锐评").lower()] = CommandSpec(
                "match_roast", mode=mode, platform=platform
            )
    for platform, words in PLATFORM_WORDS.items():
        for word in words:
            table[(word + "绑定").lower()] = CommandSpec("bind", platform=platform)
            table[(word + "解绑").lower()] = CommandSpec("unbind", platform=platform)
            table[(word + "账号").lower()] = CommandSpec("list", platform=platform)
            table[(word + "主号").lower()] = CommandSpec("main", platform=platform)

    table["cs查询"] = CommandSpec("su_query", admin_only=True)
    table["完美重载"] = CommandSpec("pw_reload", admin_only=True)
    table["完美登录"] = CommandSpec("pw_login", admin_only=True)
    table["cs帮助"] = CommandSpec("help")
    table["csstats"] = CommandSpec("help")
    table["cs_stats"] = CommandSpec("help")

    # 按长度倒序排列，确保「完美单局锐评」优先于「完美单局」匹配
    return sorted(table.items(), key=lambda item: -len(item[0]))


COMMAND_TABLE: list[tuple[str, CommandSpec]] = _build_command_table()


def _segment_kind(seg: Any) -> str:
    if isinstance(seg, dict):
        return str(seg.get("type") or "").lower()
    seg_type = getattr(seg, "type", None)
    if seg_type is not None:
        val = getattr(seg_type, "value", seg_type)
        return str(val).lower()
    return type(seg).__name__.lower()


def _segment_text(seg: Any) -> str:
    if isinstance(seg, dict):
        return str(seg.get("text") or (seg.get("data") or {}).get("text") or "")
    return str(getattr(seg, "text", "") or "")


def _segment_at_qq(seg: Any) -> str:
    if isinstance(seg, dict):
        qq = seg.get("qq") or (seg.get("data") or {}).get("qq") or ""
    else:
        qq = getattr(seg, "qq", "") or ""
    qq_str = str(qq).strip()
    return qq_str if qq_str.isdigit() else ""


def _segment_reply_info(seg: Any) -> tuple[str | None, str | None]:
    if isinstance(seg, dict):
        data = seg.get("data") if isinstance(seg.get("data"), dict) else seg
        sender = data.get("sender_id")
        msg_id = data.get("id") or data.get("message_id")
    else:
        sender = getattr(seg, "sender_id", None)
        msg_id = getattr(seg, "id", None) or getattr(seg, "message_id", None)
    sender_str = str(sender).strip() if sender not in (None, "") else None
    msg_id_str = str(msg_id).strip() if msg_id not in (None, "") else None
    return sender_str, msg_id_str


def parse_message_command(
    segments: list[Any],
    *,
    self_id: str,
    command_prefix: str = "",
) -> ParsedCommand | None:
    """从 AstrBot 消息组件列表中解析命令与参数。

    兼容 QQ/OneBot v11/SnowLuma 特性：
    - 跳过开头自带的 Reply 及 @bot / 回复附带的前置 @；
    - 支持无前缀与可选「/」前缀（或严格匹配自定义 command_prefix）；
    - 强制 command_boundary：命令词后必须是空白、非文本段（如 @某人）或消息结尾，
      避免「完美战绩真不错」之类普通聊天误触发。
    """
    reply_sender_id: str | None = None
    reply_message_id: str | None = None
    first_text_idx: int | None = None

    for idx, seg in enumerate(segments):
        kind = _segment_kind(seg)
        if kind == "reply":
            s_id, m_id = _segment_reply_info(seg)
            if reply_sender_id is None and s_id:
                reply_sender_id = s_id
            if reply_message_id is None and m_id:
                reply_message_id = m_id
            continue
        if kind == "at":
            # 命令前的 @（通常是 @bot 或 QQ 回复自动带的 @被回复人），跳过继续找首段文本
            continue
        if kind in ("plain", "text"):
            if _segment_text(seg).strip():
                first_text_idx = idx
                break
            continue
        # 首段是图片/表情等非文本，肯定不是本插件命令
        return None

    if first_text_idx is None:
        return None

    first_text = _segment_text(segments[first_text_idx]).lstrip()
    if command_prefix:
        if not first_text.startswith(command_prefix):
            return None
        body = first_text[len(command_prefix) :]
    else:
        # 留空时，同时支持无前缀（「完美战绩」）和 AstrBot 默认斜杠前缀（「/完美战绩」）
        body = first_text[1:] if first_text.startswith("/") else first_text

    body_lower = body.lower()
    matched_word: str | None = None
    matched_spec: CommandSpec | None = None
    rest_of_first_text = ""

    for word, spec in COMMAND_TABLE:
        if body_lower.startswith(word):
            tail = body[len(word) :]
            # command_boundary：命令后必须是空（结尾或紧跟 @等非文本段）或空白字符
            if tail == "" or tail[0].isspace():
                matched_word = word
                matched_spec = spec
                rest_of_first_text = tail
                break

    if matched_spec is None or matched_word is None:
        return None

    text_parts: list[str] = []
    if rest_of_first_text:
        text_parts.append(rest_of_first_text)

    arg_mentions: list[str] = []
    for seg in segments[first_text_idx + 1 :]:
        kind = _segment_kind(seg)
        if kind in ("plain", "text"):
            text_parts.append(_segment_text(seg))
        elif kind == "at":
            qq = _segment_at_qq(seg)
            if qq and qq != str(self_id):
                arg_mentions.append(qq)

    return ParsedCommand(
        spec=matched_spec,
        matched_word=matched_word,
        arg_text="".join(text_parts).strip(),
        arg_mentions=arg_mentions,
        reply_sender_id=reply_sender_id,
        reply_message_id=reply_message_id,
    )


def account_index(token: str) -> int | None:
    """「#2」→ 1；不是 #序号 返回 None。是否越界由调用方按绑定数判断。"""
    if token.startswith("#") and token[1:].isdecimal() and int(token[1:]) >= 1:
        return int(token[1:]) - 1
    return None


def parse_query_args(
    arg_text: str,
    arg_mentions: list[str],
    *,
    sender_id: str,
    self_id: str,
    reply_sender_id: str | None,
    allow_nth: bool,
    allow_refresh: bool,
) -> QueryArgs:
    index, nth, refresh = 0, 1, False
    for token in arg_text.split():
        if (parsed_idx := account_index(token)) is not None:
            index = parsed_idx
        elif allow_refresh and token in ("-r", "刷新"):
            refresh = True
        elif allow_nth and token.isdecimal() and 1 <= int(token) <= MAX_NTH:
            nth = int(token)
        else:
            raise QueryError(f"看不懂参数「{token}」")

    # @bot 和回复 bot 的消息都不算指定了对象
    mentioned = [qq for qq in arg_mentions if str(qq) != str(self_id)]
    if mentioned:
        target = str(mentioned[0])
    elif reply_sender_id and str(reply_sender_id) != str(self_id):
        target = str(reply_sender_id)
    else:
        target = str(sender_id)
    return QueryArgs(target, target == str(sender_id), index, nth, refresh)


def choose_candidate(candidates: list[dict], reply: str) -> dict | None:
    cleaned = reply.strip()
    if len(candidates) == 1 and cleaned == "确认":
        return candidates[0]
    if cleaned.isdecimal() and 1 <= int(cleaned) <= len(candidates):
        return candidates[int(cleaned) - 1]
    return None


def choose_hint(candidates: list[dict]) -> str:
    action = "回复「确认」" if len(candidates) == 1 else f"回复序号 1～{len(candidates)}"
    return f"{action}选择，回复其他内容取消"


def format_account_line(platform: str, index: int, account: dict) -> str:
    tag = f"SteamID …{account['id'][-4:]}" if platform == "pw" else f"5E ID {account['id']}"
    return f"#{index + 1} {account['nickname']}（{tag}）" + ("　主号" if index == 0 else "")
