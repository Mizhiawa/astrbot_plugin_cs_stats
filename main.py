"""AstrBot 4.29.4 + SnowLuma 2.4.2 (OneBot v11) CS2 战绩查询与锐评插件。

移植自 https://github.com/HakuchumuHYX/HakuBot 的 cs_stats 插件：
- 平台：完美世界竞技平台（天梯 pvp / 官匹 mm）与 5E 对战平台（优先排位 5e）
- 功能：生涯战绩卡片、最近第 N 局战绩卡片、AI 毒舌锐评（支持缓存水位与管理员强制刷新）、
  多账号绑定/解绑/切换主号、管理员免绑定查询（cs查询）、完美登录/热重载。
"""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.api.util import SessionController, SessionFilter, session_waiter
except ImportError:
    from astrbot.core.utils.session_waiter import (
        SessionController,
        SessionFilter,
        session_waiter,
    )

from .cs_stats import render as render_mod
from .cs_stats import roast as roast_mod
from .cs_stats.browser import BrowserPool
from .cs_stats.config import (
    PLUGIN_NAME,
    QUERY_FEATURE,
    ROAST_FEATURE,
    CSStatsConfig,
)
from .cs_stats.cooldown import CooldownManager
from .cs_stats.fivee_api import FiveEClient, FiveEError
from .cs_stats.logging_util import get_exc_desc
from .cs_stats.parser import (
    MAX_CANDIDATES,
    MODE_PLATFORM,
    MODE_WORDS,
    PLATFORM_WORDS,
    SU_MODES,
    ParsedCommand,
    QueryArgs,
    QueryError,
    account_index,
    choose_candidate,
    choose_hint,
    format_account_line,
    parse_message_command,
    parse_query_args,
)
from .cs_stats.pw_api import PWClient, PWError, PWTokenInvalid
from .cs_stats.render import CardRenderer
from .cs_stats.store import CSStatsStore

HELP_TEXT = """CS2 战绩查询与锐评（完美 / 官匹 / 5E）
【账号绑定】
- 完美绑定 / 官匹绑定 <昵称或SteamID64>
- 5e绑定 <昵称或5E个人主页ID>
- 完美账号 / 5e账号（查看已绑定序号）
- 完美主号 / 5e主号 #序号
- 完美解绑 / 5e解绑 [#序号]

【战绩与锐评】
- 完美战绩 / 官匹战绩 / 5e战绩 [@某人|回复] [#序号]
- 完美单局 / 官匹单局 / 5e单局 [1~20] [@某人|回复] [#序号]
- 完美锐评 / 官匹锐评 / 5e锐评 [@某人|回复] [#序号]
- 完美单局锐评 / 官匹单局锐评 / 5e单局锐评 [1~20] [@某人|回复] [#序号]
（管理员加 -r 或 刷新 可强制重新生成锐评）

【管理员命令】
- cs查询 <完美|官匹|5e> <昵称或ID>
- 完美登录 <手机号> <验证码>（仅限私聊）
- 完美重载"""


class _UserSessionFilter(SessionFilter):
    """按「会话 + 用户」隔离选号交互，防止同群多人同时绑定时串线。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


def _make_image_component(image_bytes: bytes) -> Comp.Image:
    """使用内存字节/Base64 构造图片组件，确保 SnowLuma 2.4.2 跨容器或远程连接稳定发图。"""
    if hasattr(Comp.Image, "fromBytes"):
        return Comp.Image.fromBytes(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return Comp.Image.fromBase64(b64)


class CSStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.raw_config = config
        self.cfg = CSStatsConfig.from_raw(config)

        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = CSStatsStore(data_dir)
        self.cooldown = CooldownManager()
        self.pw = PWClient(self.cfg)
        self.fivee = FiveEClient(self.cfg)
        self.browser_pool = BrowserPool()
        self.renderer = CardRenderer(
            self.cfg,
            self.browser_pool,
            astrbot_html_render=getattr(self, "html_render", None),
        )

    async def terminate(self) -> None:
        """卸载或重载插件时关闭 Playwright 浏览器，避免残留进程。"""
        await self.browser_pool.close()

    def _reload_config(self, *, from_disk: bool = True) -> None:
        if from_disk:
            load_fn = getattr(self.raw_config, "load_config", None)
            if callable(load_fn):
                try:
                    load_fn()
                except Exception as exc:
                    logger.warning(f"[cs_stats] 重新加载配置文件失败: {get_exc_desc(exc)}")
            else:
                config_path = getattr(self.raw_config, "config_path", None)
                if config_path and Path(config_path).is_file():
                    try:
                        disk_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
                        if isinstance(disk_data, dict):
                            self.raw_config.update(disk_data)
                    except Exception as exc:
                        logger.warning(f"[cs_stats] 从磁盘重载配置失败: {get_exc_desc(exc)}")
        self.cfg = CSStatsConfig.from_raw(self.raw_config)
        self.pw.update_config(self.cfg)
        self.fivee.update_config(self.cfg)
        self.renderer.update_config(self.cfg)

    def _group_allowed(self, group_id: str | None) -> bool:
        if not group_id:
            return True
        gid = str(group_id)
        if self.cfg.disabled_groups and gid in self.cfg.disabled_groups:
            return False
        if self.cfg.enabled_groups and gid not in self.cfg.enabled_groups:
            return False
        return True

    @staticmethod
    def _is_group_event(event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    @staticmethod
    async def _resolve_reply_sender(
        event: AstrMessageEvent, parsed: ParsedCommand
    ) -> str | None:
        """优先读取 Reply.sender_id；缺失时调用 SnowLuma/OneBot v11 的 get_msg 补齐。"""
        if parsed.reply_sender_id:
            return parsed.reply_sender_id
        if not parsed.reply_message_id:
            return None
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot is not None else None
        if api is None or not hasattr(api, "call_action"):
            return None
        try:
            msg_id: int | str = (
                int(parsed.reply_message_id)
                if parsed.reply_message_id.isdigit()
                else parsed.reply_message_id
            )
            resp = await api.call_action("get_msg", message_id=msg_id)
            if isinstance(resp, dict):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
                sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
                user_id = sender.get("user_id") or data.get("user_id")
                if user_id not in (None, ""):
                    return str(user_id)
        except Exception as exc:
            logger.debug(f"[cs_stats] get_msg 获取被回复人失败: {get_exc_desc(exc)}")
        return None

    async def _notify_token_invalid(self, event: AstrMessageEvent) -> None:
        logger.warning("[cs_stats] 完美登录态失效，完美查询已暂停")
        text = (
            "cs_stats：完美登录态失效，完美查询已暂停。\n"
            "请先在完美 App 登录页获取短信验证码（不要在 App 内提交），然后：\n"
            "1. 私聊机器人发送「完美登录 <手机号> <验证码>」；或\n"
            "2. 在 AstrBot WebUI 更新 pw_token/pw_steam_id 后发送「完美重载」。"
        )
        global_cfg = self.context.get_config() if hasattr(self.context, "get_config") else {}
        admins = global_cfg.get("admins_id", []) if isinstance(global_cfg, dict) else []
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot is not None else None
        platform_id = event.get_platform_id() if hasattr(event, "get_platform_id") else ""

        for admin_id in admins:
            admin_str = str(admin_id).strip()
            if not admin_str or admin_str == "astrbot":
                continue
            try:
                if api is not None and hasattr(api, "call_action") and admin_str.isdigit():
                    await api.call_action(
                        "send_private_msg", user_id=int(admin_str), message=text
                    )
                elif platform_id and hasattr(self.context, "send_message"):
                    umo = f"{platform_id}:FriendMessage:{admin_str}"
                    await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:
                logger.warning(f"[cs_stats] 通知管理员 {admin_str} 失败: {get_exc_desc(exc)}")

    def _pick_account(self, args: QueryArgs, platform: str) -> dict:
        word = PLATFORM_WORDS[platform][0]
        accounts = self.store.accounts(args.target, platform)
        if not accounts:
            if args.is_self:
                raise QueryError(f"你还没有绑定{word}账号，发「{word}绑定 昵称」绑定")
            raise QueryError(f"对方还没有绑定{word}账号")
        if args.index >= len(accounts):
            raise QueryError(f"只绑定了 {len(accounts)} 个{word}账号")
        return accounts[args.index]

    async def _fetch(self, event: AstrMessageEvent, coro) -> tuple[Any, str | None]:
        """执行外部平台接口调用，失败时返回 (None, 错误提示文案)。"""
        try:
            return await coro, None
        except QueryError as exc:
            return None, str(exc)
        except PWTokenInvalid as exc:
            if exc.newly:
                await self._notify_token_invalid(event)
            return None, str(exc)
        except (PWError, FiveEError) as exc:
            logger.warning(f"[cs_stats] 查询失败: {exc}")
            return None, str(exc)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.warning(f"[cs_stats] 接口请求失败: {get_exc_desc(exc)}")
            return None, "平台接口暂时连不上，稍后再试"

    async def _render_image(self, coro) -> tuple[bytes | None, str | None]:
        try:
            return await coro, None
        except Exception:
            logger.exception("[cs_stats] 图片渲染失败")
            return None, (
                "图片生成失败：当前环境未安装 Playwright Chromium 且内置文转图失败。\n"
                "请在 AstrBot 容器内执行 `playwright install chromium`，或检查 AstrBot 文转图配置。"
            )

    # ============== 数据加载：战绩、锐评、管理员查询共用 ==============

    async def _load_profile(self, mode: str, account: dict) -> tuple[dict, str]:
        """返回 (生涯 view, 生涯锐评水位)。"""
        if mode == "5e":
            career, home, recent, arena = await asyncio.gather(
                self.fivee.career(account["uuid"]),
                self.fivee.home(account["uuid"]),
                self.fivee.matches(account["uuid"]),
                self.fivee.arena(account["id"]),
            )
            # gate 列表只有 5 场；arena 有 10 场的对局号，但它的 Rating 是 1.0 版，缺的几场另调 gate 单局详情
            known = {m["match_id"] for m in recent}
            older_ids = [
                m["match_code"]
                for m in arena.get("match") or []
                if m.get("match_code") and m["match_code"] not in known
            ]
            older = await asyncio.gather(
                *(self.fivee.match_detail(match_id) for match_id in older_ids)
            )
            view = render_mod.fivee_profile_view(career, home, recent, arena, list(older), account)
            latest = recent[0]["match_id"] if recent else ""
            return view, f"{career.get('match_total')}:{latest}"

        stats_call = self.pw.pvp_stats if mode == "pvp" else self.pw.official_stats
        stats, (public, recent), avatar = await asyncio.gather(
            stats_call(account["id"]),
            self.pw.match_list(account["id"], mode, size=render_mod.HISTORY_MATCHES),
            self.pw.steam_avatar(account["id"]),
        )
        if not public:
            raise QueryError("对方没有公开完美战绩")
        latest = (stats.get("historyDates") or [""])[0]
        if mode == "pvp":
            if not int(stats.get("cnt") or 0):
                raise QueryError(
                    f"近 {self.cfg.pvp_season_lookback + 1} 个赛季都没有完美天梯记录"
                )
            return (
                render_mod.pvp_profile_view(stats, recent, account, avatar),
                f"{stats['seasonId']}:{stats['cnt']}:{latest}",
            )
        if not int(stats.get("cnt") or 0):
            raise QueryError("完美上没有官匹记录")
        return (
            render_mod.mm_profile_view(stats, recent, account, avatar),
            f"{stats['cnt']}:{latest}",
        )

    async def _load_match(self, mode: str, account: dict, nth: int) -> dict:
        if mode == "5e":
            items = await self.fivee.matches(account["uuid"])
            if nth > len(items):
                raise QueryError(f"5E 只能查到最近 {len(items)} 局")
            detail = await self.fivee.match_detail(items[nth - 1]["match_id"])
            return render_mod.fivee_match_view(detail, account["uuid"])

        public, items = await self.pw.match_list(account["id"], mode, size=nth)
        if not public:
            raise QueryError("对方没有公开完美战绩")
        if nth > len(items):
            raise QueryError(f"最近只有 {len(items)} 局{MODE_WORDS[mode][0]}记录")
        detail = await self.pw.match_detail(str(items[nth - 1]["matchId"]), mode)
        return render_mod.pw_match_view(detail, account["id"], mode)

    async def _search_candidates(self, platform: str, keyword: str) -> list[dict]:
        """搜索结果补齐卡片要显示的分数；5E 顺便把 uuid 转好，绑定时存下。"""
        if platform == "pw":
            found = (await self.pw.search(keyword))[:MAX_CANDIDATES]
            avatars = await asyncio.gather(*(self.pw.steam_avatar(c["id"]) for c in found))
            return [
                {**c, "avatar": avatar, "score_label": "本赛季天梯分", "score": c["score"] or "-"}
                for c, avatar in zip(found, avatars)
            ]

        found = (await self.fivee.search(keyword))[:MAX_CANDIDATES]
        uuids = await asyncio.gather(*(self.fivee.uuid_of(c["id"]) for c in found))
        homes = await asyncio.gather(*(self.fivee.home(uuid) for uuid in uuids))
        candidates = []
        for c, uuid, home in zip(found, uuids, homes):
            current = render_mod.fivee_current(home, [])
            candidates.append({
                **c,
                "uuid": uuid,
                "score_label": "本赛季 ELO",
                "score": current["value"] or current["name"],
                "extra": f"生涯 {(home.get('career') or {}).get('match_total') or 0} 场",
            })
        return candidates

    @staticmethod
    def _roast_key(mode: str, account: dict) -> str:
        return f"5e:{account['uuid']}" if mode == "5e" else f"pw:{account['id']}:{mode}"

    async def _get_or_generate_roast(
        self,
        event: AstrMessageEvent,
        kind: str,
        key: str,
        watermark: str | None,
        view: dict,
        refresh: bool,
    ) -> tuple[dict | None, str | None]:
        """返回 (roast_dict_or_None, error_message_if_blocked)。"""
        stored = self.store.get_roast(kind, key)
        if stored and (watermark is None or stored.get("watermark") == watermark) and not refresh:
            return stored, None

        group_id = event.get_group_id()
        user_id = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        remaining = self.cooldown.remaining(
            ROAST_FEATURE,
            group_id,
            user_id,
            self.cfg.roast_cooldown_seconds,
            is_admin=is_admin,
        )
        if remaining:
            return None, f"锐评冷却中，还剩 {remaining} 秒"

        await event.send(event.plain_result("锐评生成中…"))
        roast = await roast_mod.generate(
            kind,
            view,
            self.cfg,
            context=self.context,
            umo=event.unified_msg_origin,
        )
        if roast:
            payload = {**roast, "watermark": watermark} if watermark else roast
            self.store.save_roast(kind, key, payload)
            self.cooldown.mark(
                ROAST_FEATURE,
                group_id,
                user_id,
                self.cfg.roast_cooldown_seconds,
                is_admin=is_admin,
            )
        return roast, None

    # ============== 命令处理入口 ==============

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        parsed = parse_message_command(
            event.get_messages(),
            self_id=str(event.get_self_id()),
            command_prefix=self.cfg.command_prefix,
        )
        if parsed is None:
            return

        spec = parsed.spec
        group_id = event.get_group_id()

        # 群黑白名单检查（管理员命令除外）
        if not spec.admin_only and not self._group_allowed(group_id):
            return

        # 私聊开关检查（管理员命令及帮助在私聊始终可用）
        if not group_id and not spec.admin_only and spec.action != "help":
            if not self.cfg.allow_private:
                return

        # 命中插件命令后，在回复发送完毕通过 finally 阻止后续 LLM 及其他处理器
        try:
            if spec.action == "help":
                yield event.plain_result(HELP_TEXT)
                return

            if spec.admin_only and not event.is_admin():
                yield event.plain_result("仅管理员可执行此命令")
                return

            if spec.action in ("profile", "single", "career_roast", "match_roast"):
                async for result in self._handle_query(event, parsed):
                    yield result
            elif spec.action == "bind":
                async for result in self._handle_bind(event, parsed):
                    yield result
            elif spec.action == "unbind":
                yield self._handle_unbind(event, parsed)
            elif spec.action == "list":
                yield self._handle_list(event, parsed)
            elif spec.action == "main":
                yield self._handle_main(event, parsed)
            elif spec.action == "su_query":
                async for result in self._handle_su_query(event, parsed):
                    yield result
            elif spec.action == "pw_reload":
                self._reload_config()
                self.pw.reset_token_state()
                yield event.plain_result("已重新读取 cs_stats 配置，完美查询恢复")
            elif spec.action == "pw_login":
                async for result in self._handle_pw_login(event, parsed):
                    yield result
        finally:
            event.stop_event()

    async def _handle_query(self, event: AstrMessageEvent, parsed: ParsedCommand):
        action = parsed.spec.action
        mode = parsed.spec.mode
        platform = parsed.spec.platform
        assert mode is not None and platform is not None

        allow_nth = action in ("single", "match_roast")
        allow_refresh = action in ("career_roast", "match_roast")
        reply_sender = await self._resolve_reply_sender(event, parsed)

        try:
            args = parse_query_args(
                parsed.arg_text,
                parsed.arg_mentions,
                sender_id=str(event.get_sender_id()),
                self_id=str(event.get_self_id()),
                reply_sender_id=reply_sender,
                allow_nth=allow_nth,
                allow_refresh=allow_refresh,
            )
            account = self._pick_account(args, platform)
        except QueryError as exc:
            yield event.plain_result(str(exc))
            return

        if args.refresh and not event.is_admin():
            yield event.plain_result("只有管理员能强制重新生成锐评")
            return

        group_id = event.get_group_id()
        user_id = str(event.get_sender_id())
        is_admin = bool(event.is_admin())

        if action in ("profile", "single"):
            remaining = self.cooldown.remaining(
                QUERY_FEATURE,
                group_id,
                user_id,
                self.cfg.query_cooldown_seconds,
                is_admin=is_admin,
            )
            if remaining:
                yield event.plain_result(f"查询冷却中，还剩 {remaining} 秒")
                return

        if action == "profile":
            data, err = await self._fetch(event, self._load_profile(mode, account))
            if err:
                yield event.plain_result(err)
                return
            view, _watermark = data
            image, err = await self._render_image(self.renderer.render_profile(view))
            if err or image is None:
                yield event.plain_result(err or "图片生成失败")
                return
            yield event.chain_result([_make_image_component(image)])
            self.cooldown.mark(
                QUERY_FEATURE,
                group_id,
                user_id,
                self.cfg.query_cooldown_seconds,
                is_admin=is_admin,
            )
            return

        if action == "single":
            view, err = await self._fetch(event, self._load_match(mode, account, args.nth))
            if err:
                yield event.plain_result(err)
                return
            image, err = await self._render_image(self.renderer.render_match(view))
            if err or image is None:
                yield event.plain_result(err or "图片生成失败")
                return
            yield event.chain_result([_make_image_component(image)])
            self.cooldown.mark(
                QUERY_FEATURE,
                group_id,
                user_id,
                self.cfg.query_cooldown_seconds,
                is_admin=is_admin,
            )
            return

        if action == "career_roast":
            data, err = await self._fetch(event, self._load_profile(mode, account))
            if err:
                yield event.plain_result(err)
                return
            view, watermark = data
            key = self._roast_key(mode, account)
            roast, cd_err = await self._get_or_generate_roast(
                event, "career", key, watermark, view, args.refresh
            )
            if cd_err:
                yield event.plain_result(cd_err)
                return
            image, err = await self._render_image(self.renderer.render_profile(view, roast))
            if err or image is None:
                yield event.plain_result(err or "图片生成失败")
                return
            chain: list[Any] = [_make_image_component(image)]
            if not roast:
                chain.append(Comp.Plain("锐评生成失败，先看数据吧"))
            yield event.chain_result(chain)
            return

        if action == "match_roast":
            view, err = await self._fetch(event, self._load_match(mode, account, args.nth))
            if err:
                yield event.plain_result(err)
                return
            key = f"{self._roast_key(mode, account)}:{view['match_id']}"
            roast, cd_err = await self._get_or_generate_roast(
                event, "match", key, None, view, args.refresh
            )
            if cd_err:
                yield event.plain_result(cd_err)
                return
            image, err = await self._render_image(self.renderer.render_match(view, roast))
            if err or image is None:
                yield event.plain_result(err or "图片生成失败")
                return
            chain = [_make_image_component(image)]
            if not roast:
                chain.append(Comp.Plain("锐评生成失败，先看数据吧"))
            yield event.chain_result(chain)

    async def _handle_bind(self, event: AstrMessageEvent, parsed: ParsedCommand):
        platform = parsed.spec.platform
        assert platform is not None
        word = PLATFORM_WORDS[platform][0]
        id_hint = "SteamID64" if platform == "pw" else "5E 个人主页 ID"

        keyword = parsed.arg_text.strip()
        if not keyword:
            yield event.plain_result(f"用法：{word}绑定 <昵称或{id_hint}>")
            return

        qq = str(event.get_sender_id())
        bound = self.store.accounts(qq, platform)
        if len(bound) >= self.cfg.max_accounts_per_platform:
            yield event.plain_result(
                f"每个人最多绑定 {self.cfg.max_accounts_per_platform} 个{word}账号，"
                f"先用「{word}解绑 #序号」解绑一个"
            )
            return

        found, err = await self._fetch(event, self._search_candidates(platform, keyword))
        if err:
            yield event.plain_result(err)
            return

        bound_ids = {a["id"] for a in bound}
        candidates = [c for c in found if c["id"] not in bound_ids]
        if not candidates:
            yield event.plain_result(
                "搜到的号你都已经绑定了" if found else f"没有搜到{word}玩家「{keyword}」"
            )
            return

        image, err = await self._render_image(
            self.renderer.render_candidates(platform, candidates, "绑定")
        )
        if err or image is None:
            yield event.plain_result(err or "图片生成失败")
            return

        await event.send(
            event.chain_result([_make_image_component(image), Comp.Plain(choose_hint(candidates))])
        )

        @session_waiter(
            timeout=self.cfg.session_timeout_seconds, record_history_chains=False
        )
        async def bind_waiter(controller: SessionController, reply_event: AstrMessageEvent):
            try:
                picked = choose_candidate(candidates, reply_event.message_str)
                controller.stop()
                if picked is None:
                    await reply_event.send(reply_event.plain_result("已取消绑定"))
                    return
                # 完美头像每次现取 Steam 的，不存；5E 生涯接口不带头像，绑定时存下
                keys = ("id", "nickname") if platform == "pw" else ("id", "nickname", "avatar", "uuid")
                account = {k: picked[k] for k in keys}
                self.store.add_account(qq, platform, account)
                count = len(self.store.accounts(qq, platform))
                await reply_event.send(
                    reply_event.plain_result(
                        f"已绑定{word}账号 {picked['nickname']}（#{count}{'，主号' if count == 1 else ''}）"
                    )
                )
            finally:
                reply_event.stop_event()

        try:
            await bind_waiter(event, session_filter=_UserSessionFilter())
        except TimeoutError:
            yield event.plain_result("选择超时，已取消绑定")

    def _handle_unbind(self, event: AstrMessageEvent, parsed: ParsedCommand):
        platform = parsed.spec.platform
        assert platform is not None
        word = PLATFORM_WORDS[platform][0]
        idx = account_index(parsed.arg_text.strip() or "#1")
        qq = str(event.get_sender_id())
        bound = self.store.accounts(qq, platform)
        if idx is None or idx >= len(bound):
            msg = (
                f"用法：{word}解绑 [#序号]，发「{word}账号」查看序号"
                if bound
                else f"你没有绑定{word}账号"
            )
            return event.plain_result(msg)
        removed = self.store.remove_account(qq, platform, idx)
        rest = self.store.accounts(qq, platform)
        tail = f"，现在的主号是 {rest[0]['nickname']}" if rest and idx == 0 else ""
        return event.plain_result(f"已解绑 {removed['nickname']}{tail}")

    def _handle_list(self, event: AstrMessageEvent, parsed: ParsedCommand):
        platform = parsed.spec.platform
        assert platform is not None
        word = PLATFORM_WORDS[platform][0]
        bound = self.store.accounts(str(event.get_sender_id()), platform)
        if not bound:
            return event.plain_result(f"你还没有绑定{word}账号，发「{word}绑定 昵称」绑定")
        lines = [format_account_line(platform, i, a) for i, a in enumerate(bound)]
        return event.plain_result(f"你绑定的{word}账号：\n" + "\n".join(lines))

    def _handle_main(self, event: AstrMessageEvent, parsed: ParsedCommand):
        platform = parsed.spec.platform
        assert platform is not None
        word = PLATFORM_WORDS[platform][0]
        idx = account_index(parsed.arg_text.strip())
        qq = str(event.get_sender_id())
        bound = self.store.accounts(qq, platform)
        if idx is None or idx >= len(bound):
            return event.plain_result(f"用法：{word}主号 #序号，发「{word}账号」查看序号")
        self.store.set_main(qq, platform, idx)
        return event.plain_result(f"主号已设为 {self.store.accounts(qq, platform)[0]['nickname']}")

    async def _su_show(self, event: AstrMessageEvent, mode: str, account: dict):
        data, err = await self._fetch(event, self._load_profile(mode, account))
        if err:
            await event.send(event.plain_result(err))
            return
        view, _watermark = data
        image, err = await self._render_image(self.renderer.render_profile(view))
        if err or image is None:
            await event.send(event.plain_result(err or "图片生成失败"))
            return
        await event.send(event.chain_result([_make_image_component(image)]))

    async def _handle_su_query(self, event: AstrMessageEvent, parsed: ParsedCommand):
        parts = parsed.arg_text.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() not in SU_MODES:
            yield event.plain_result("用法：cs查询 <完美|官匹|5e> <昵称或ID>")
            return

        mode = SU_MODES[parts[0].lower()]
        platform = MODE_PLATFORM[mode]
        candidates, err = await self._fetch(
            event, self._search_candidates(platform, parts[1].strip())
        )
        if err:
            yield event.plain_result(err)
            return
        if not candidates:
            yield event.plain_result("没有搜到")
            return
        if len(candidates) == 1:
            await self._su_show(event, mode, candidates[0])
            return

        image, err = await self._render_image(
            self.renderer.render_candidates(platform, candidates, "查询")
        )
        if err or image is None:
            yield event.plain_result(err or "图片生成失败")
            return

        await event.send(
            event.chain_result([_make_image_component(image), Comp.Plain(choose_hint(candidates))])
        )

        @session_waiter(
            timeout=self.cfg.session_timeout_seconds, record_history_chains=False
        )
        async def su_waiter(controller: SessionController, reply_event: AstrMessageEvent):
            try:
                picked = choose_candidate(candidates, reply_event.message_str)
                controller.stop()
                if picked is None:
                    await reply_event.send(reply_event.plain_result("已取消"))
                    return
                await self._su_show(reply_event, mode, picked)
            finally:
                reply_event.stop_event()

        try:
            await su_waiter(event, session_filter=_UserSessionFilter())
        except TimeoutError:
            yield event.plain_result("选择超时，已取消查询")

    async def _handle_pw_login(self, event: AstrMessageEvent, parsed: ParsedCommand):
        if self._is_group_event(event):
            yield event.plain_result("为保护隐私，请私聊机器人使用「完美登录 <手机号> <验证码>」")
            return

        parts = parsed.arg_text.split()
        if len(parts) != 2:
            yield event.plain_result(
                "用法：完美登录 <手机号> <验证码>\n"
                "提示：先在完美世界电竞 App 登录页点击发送验证码（不要在 App 内提交），再发送此命令。"
            )
            return

        mobile, code = parts[0].strip(), parts[1].strip()
        result, err = await self._fetch(event, self.pw.login_with_sms(mobile, code))
        if err or not result:
            yield event.plain_result(err or "完美登录失败")
            return

        token, steam_id = result
        self.raw_config["pw_token"] = token
        self.raw_config["pw_steam_id"] = steam_id
        save_fn = getattr(self.raw_config, "save_config", None)
        if callable(save_fn):
            save_fn()
        self._reload_config(from_disk=False)
        self.pw.reset_token_state()

        try:
            ok, detail = await self.pw.verify_token(token, steam_id)
        except Exception as exc:
            ok, detail = False, get_exc_desc(exc)
        if ok:
            yield event.plain_result(
                f"完美登录成功且接口校验通过（SteamID …{steam_id[-4:]}），完美查询已就绪。"
                "注意：完美电竞 Android 端为单设备登录，请勿再用同一账号登录手机端完美 App。"
            )
        else:
            logger.warning(f"[cs_stats] 刚登录获得的 token 立即校验失败: {detail}")
            yield event.plain_result(
                f"已获取登录 token（SteamID …{steam_id[-4:]}），但立即校验战绩接口失败（{detail}）。"
                "请查看控制台日志中 `完美登录响应字段` 与 `4001` 信息，或尝试手动抓包填写 pw_token。"
            )
