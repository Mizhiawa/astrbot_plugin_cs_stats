"""astrbot_plugin_cs_stats 单元测试（不依赖外部网络或本地 AstrBot 安装）。"""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cs_stats.config import CSStatsConfig
from cs_stats.cooldown import CooldownManager
from cs_stats.fivee_api import avatar_url, level_icon, level_name
from cs_stats.json_io import atomic_write_json, load_json
from cs_stats.parser import (
    QueryError,
    account_index,
    choose_candidate,
    choose_hint,
    format_account_line,
    parse_message_command,
    parse_query_args,
)
from cs_stats import render as render_mod
from cs_stats import roast as roast_mod
from cs_stats.store import CSStatsStore


class FakePlain:
    type = "Plain"

    def __init__(self, text: str):
        self.text = text


class FakeAt:
    type = "At"

    def __init__(self, qq: str | int):
        self.qq = str(qq)


class FakeReply:
    type = "Reply"

    def __init__(self, msg_id: str | int, sender_id: str | int | None = None):
        self.id = str(msg_id)
        self.sender_id = str(sender_id) if sender_id is not None else None


class TestConfigAndSchema(unittest.TestCase):
    def test_schema_valid_json(self):
        schema_path = ROOT / "_conf_schema.json"
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("pw_token", data)
        self.assertIn("llm", data)
        self.assertIn("render", data)
        self.assertEqual(data["llm"]["type"], "object")
        self.assertEqual(data["enabled_groups"]["type"], "list")

    def test_config_defaults_and_overrides(self):
        cfg = CSStatsConfig.from_raw({})
        self.assertEqual(cfg.pw_appversion, "4.2.3.234")
        self.assertEqual(cfg.llm.mode, "astrbot")
        self.assertEqual(cfg.max_accounts_per_platform, 3)

        custom = CSStatsConfig.from_raw({
            "pw_token": "tok123",
            "pw_steam_id": "76561198000000000",
            "max_accounts_per_platform": 0,
            "llm": {"mode": "custom", "api_key": "sk-x", "model": "gpt-4o", "max_tokens": 0},
            "render": {"scale": 1.5, "watermark_text": "Custom"},
        })
        self.assertEqual(custom.pw_token, "tok123")
        self.assertEqual(custom.max_accounts_per_platform, 1)  # minimum enforced
        self.assertEqual(custom.llm.mode, "custom")
        self.assertIsNone(custom.llm.max_tokens)
        self.assertEqual(custom.render.watermark_text, "Custom")


class TestParser(unittest.TestCase):
    def test_command_boundary_and_longest_match(self):
        # 避免普通聊天误触发
        self.assertIsNone(parse_message_command([FakePlain("完美战绩真不错")], self_id="999"))
        self.assertIsNone(parse_message_command([FakePlain("5e单局打得很烂")], self_id="999"))

        # 最长匹配优先：完美单局锐评 不能误命中 完美单局
        cmd = parse_message_command([FakePlain("完美单局锐评 3 -r")], self_id="999")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.spec.action, "match_roast")
        self.assertEqual(cmd.spec.mode, "pvp")
        self.assertEqual(cmd.arg_text, "3 -r")

        # 大小写不敏感 + 斜杠前缀兼容
        cmd_5e = parse_message_command([FakePlain("/5E战绩 #2")], self_id="999")
        self.assertIsNotNone(cmd_5e)
        self.assertEqual(cmd_5e.spec.action, "profile")
        self.assertEqual(cmd_5e.spec.mode, "5e")
        self.assertEqual(cmd_5e.arg_text, "#2")

        # 自定义前缀严格校验
        self.assertIsNone(
            parse_message_command([FakePlain("完美战绩")], self_id="999", command_prefix="/")
        )
        self.assertIsNotNone(
            parse_message_command([FakePlain("/完美战绩")], self_id="999", command_prefix="/")
        )

    def test_at_and_reply_precedence(self):
        # 紧跟 @某人（无空格）
        cmd = parse_message_command([FakePlain("完美战绩"), FakeAt("12345")], self_id="999")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.arg_mentions, ["12345"])

        # 回复 Alice 时 QQ 会前置 At(Alice)，此时应查 Alice
        cmd2 = parse_message_command(
            [FakeReply("m1", "111"), FakeAt("111"), FakePlain("完美战绩")], self_id="999"
        )
        self.assertIsNotNone(cmd2)
        self.assertEqual(cmd2.reply_sender_id, "111")
        self.assertEqual(cmd2.arg_mentions, [])
        qargs = parse_query_args(
            cmd2.arg_text,
            cmd2.arg_mentions,
            sender_id="222",
            self_id="999",
            reply_sender_id=cmd2.reply_sender_id,
            allow_nth=False,
            allow_refresh=False,
        )
        self.assertEqual(qargs.target, "111")
        self.assertFalse(qargs.is_self)

        # 回复 Alice 但显式 @Bob -> 以 @Bob 为准
        cmd3 = parse_message_command(
            [FakeReply("m1", "111"), FakeAt("111"), FakePlain("完美战绩 "), FakeAt("333")],
            self_id="999",
        )
        qargs3 = parse_query_args(
            cmd3.arg_text,
            cmd3.arg_mentions,
            sender_id="222",
            self_id="999",
            reply_sender_id=cmd3.reply_sender_id,
            allow_nth=False,
            allow_refresh=False,
        )
        self.assertEqual(qargs3.target, "333")

        # @bot 与回复 bot 都查自己
        cmd4 = parse_message_command(
            [FakeReply("m2", "999"), FakeAt("999"), FakePlain(" 官匹单局 5 #2")],
            self_id="999",
        )
        qargs4 = parse_query_args(
            cmd4.arg_text,
            cmd4.arg_mentions,
            sender_id="222",
            self_id="999",
            reply_sender_id=cmd4.reply_sender_id,
            allow_nth=True,
            allow_refresh=False,
        )
        self.assertEqual(qargs4.target, "222")
        self.assertTrue(qargs4.is_self)
        self.assertEqual(qargs4.nth, 5)
        self.assertEqual(qargs4.index, 1)

    def test_invalid_query_args_and_helpers(self):
        with self.assertRaises(QueryError):
            parse_query_args(
                "25", [], sender_id="1", self_id="9", reply_sender_id=None, allow_nth=True, allow_refresh=False
            )
        self.assertEqual(account_index("#1"), 0)
        self.assertEqual(account_index("#3"), 2)
        self.assertIsNone(account_index("#0"))
        self.assertIsNone(account_index("abc"))
        self.assertEqual(choose_candidate([{"id": "a"}], "确认"), {"id": "a"})
        self.assertEqual(choose_candidate([{"id": "a"}, {"id": "b"}], "2"), {"id": "b"})
        self.assertIsNone(choose_candidate([{"id": "a"}], "取消"))
        self.assertIn("确认", choose_hint([{"id": "a"}]))
        self.assertIn("…7890", format_account_line("pw", 0, {"id": "76561198123457890", "nickname": "Niko"}))


class TestStoreAndCooldown(unittest.TestCase):
    def test_store_crud_and_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # 先写一个损坏的 bindings.json 测试自动备份恢复
            bad_file = tmp_path / "bindings.json"
            bad_file.write_text("{broken json", encoding="utf-8")

            store = CSStatsStore(tmp_path)
            self.assertEqual(store.accounts("100", "pw"), [])
            backups = list(tmp_path.glob("bindings.json.corrupt.*"))
            self.assertEqual(len(backups), 1)

            store.add_account("100", "pw", {"id": "111", "nickname": "A"})
            store.add_account("100", "pw", {"id": "222", "nickname": "B"})
            self.assertEqual([a["id"] for a in store.accounts("100", "pw")], ["111", "222"])

            store.set_main("100", "pw", 1)
            self.assertEqual([a["id"] for a in store.accounts("100", "pw")], ["222", "111"])

            removed = store.remove_account("100", "pw", 0)
            self.assertEqual(removed["id"], "222")

            store.save_roast("career", "pw:111:pvp", {"title": "干拉之王", "roast": "冲就完了", "watermark": "S20:5:x"})
            self.assertEqual(store.get_roast("career", "pw:111:pvp")["title"], "干拉之王")

            # 重新加载验证落盘
            reloaded = CSStatsStore(tmp_path)
            self.assertEqual(len(reloaded.accounts("100", "pw")), 1)
            self.assertEqual(reloaded.get_roast("career", "pw:111:pvp")["watermark"], "S20:5:x")

    def test_cooldown_manager(self):
        cd = CooldownManager()
        self.assertEqual(cd.remaining("q", "g1", "u1", 10, is_admin=False), 0)
        cd.mark("q", "g1", "u1", 10, is_admin=False)
        self.assertGreater(cd.remaining("q", "g1", "u1", 10, is_admin=False), 0)
        # 管理员和私聊免 CD
        self.assertEqual(cd.remaining("q", "g1", "u1", 10, is_admin=True), 0)
        self.assertEqual(cd.remaining("q", None, "u1", 10, is_admin=False), 0)


class TestViewsAndRoast(unittest.TestCase):
    def test_pvp_profile_and_roast_input(self):
        stats = {
            "name": "s1mple",
            "seasonId": "S22",
            "cnt": 42,
            "pvpScore": 2401,
            "stars": 18,
            "scoreList": [{"score": 2650, "slevel": 1}],
            "summary": "突破手",
            "pwRating": 1.28,
            "rating": 1.24,
            "adr": 88.5,
            "rws": 12.6,
            "kd": 1.35,
            "winRate": 0.58,
            "headShotRatio": 0.49,
            "entryKillRatio": 0.61,
            "kills": 800,
            "deaths": 590,
            "assists": 150,
            "mvpCount": 90,
            "k2": 100,
            "k3": 40,
            "k4": 10,
            "k5": 2,
            "vs1": 20,
            "vs2": 8,
            "vs3": 2,
            "vs4": 0,
            "vs5": 0,
            "vs1WinRate": 0.65,
            "avgWe": 11.2,
            "shot": 8.8,
            "victory": 8.1,
            "breach": 7.9,
            "snipe": 9.2,
            "prop": 6.5,
            "historyPwRatings": [1.3, 1.1, 0.9, 1.4],
            "historyScores": [2650, 2630, 2610, 2590],
            "hotMaps": [
                {"mapName": "荒漠迷城", "totalMatch": 20, "winCount": 14},
                {"mapName": "炼狱小镇", "totalMatch": 10, "winCount": 3},
            ],
            "hotWeapons": [
                {"weaponName": "AK-47", "weaponKill": 400, "weaponHeadShot": 220},
                {"weaponName": "AWP", "weaponKill": 200, "weaponHeadShot": 40},
            ],
        }
        recent = [
            {
                "team": 1,
                "winTeam": 1,
                "score1": 13,
                "score2": 9,
                "kill": 24,
                "death": 14,
                "assist": 5,
                "pwRating": 1.45,
                "rating": 1.40,
                "pvpScore": 2401,
                "pvpStars": 18,
                "mapName": "荒漠迷城",
                "startTime": "2026-11-20 21:00:00",
            },
            {
                "team": 2,
                "winTeam": 1,
                "score1": 13,
                "score2": 10,
                "kill": 15,
                "death": 18,
                "assist": 4,
                "pwRating": 0.95,
                "rating": 0.92,
                "pvpScore": 2401,
                "pvpStars": 17,
                "mapName": "炼狱小镇",
                "startTime": "2026-11-20 20:00:00",
            },
        ]
        view = render_mod.pvp_profile_view(stats, recent, {"id": "76561198", "nickname": "s1mple"}, "")
        self.assertEqual(view["score"]["name"], "黄金 S")
        self.assertEqual(view["score"]["stars"], 18)
        self.assertEqual(view["recent"][0]["change"], 1)
        self.assertIsNone(view["recent"][1]["change"])

        payload = roast_mod.career_input(view)
        self.assertIn("黄金 S 18 星", payload["段位"])
        self.assertTrue(any("最强图 荒漠迷城" in d for d in payload["派生"]))

    def test_fivee_helpers_and_match_roast_filter(self):
        self.assertEqual(level_name(2, 52), "S")
        self.assertEqual(level_name(1, 28), "A+")
        self.assertTrue(level_icon(2, None).endswith("ques.avif"))
        self.assertTrue(avatar_url("abc.png").startswith("https://oss-arena.5eplay.com/"))

        detail = {
            "base": {
                "matchId": "m1",
                "map": "Mirage",
                "mode": "官匹",
                "startTime": "2026-11-20 22:00:00",
                "duration": 35,
                "score1": 13,
                "score2": 11,
                "halfScore1": 7,
                "halfScore2": 5,
                "winTeam": 1,
                "team1round": "1101",
                "team2round": "0010",
            },
            "players": [
                {
                    "playerId": "1001",
                    "nickName": "Me",
                    "team": 1,
                    "kill": 20,
                    "death": 15,
                    "assist": 3,
                    "adpr": 82,
                    "rating": 1.18,
                    "headShotRatio": 55.0,
                    "entryKill": 4,
                    "firstDeath": 2,
                    "kast": 72,
                    "mvp": 1,
                    "threeKill": 2,
                    "fourKill": 0,
                    "fiveKill": 0,
                    "first": ["flashTeammate", "entryKill"],
                    "csMatchPlayerInterestList": [
                        {"name": "友军克星", "message": "闪光弹致盲队友次数", "value": 8},
                        {"name": "爆头机器", "message": "爆头击杀", "value": 11},
                    ],
                },
                {
                    "playerId": "2001",
                    "nickName": "Enemy",
                    "team": 2,
                    "kill": 18,
                    "death": 18,
                    "assist": 2,
                    "adpr": 75,
                    "rating": 1.02,
                    "headShotRatio": 40.0,
                    "entryKill": 2,
                    "firstDeath": 3,
                    "kast": 65,
                    "mvp": 0,
                    "killMap": {"1001": 6},
                },
            ],
        }
        view = render_mod.pw_match_view(detail, "1001", "mm")
        self.assertEqual(view["top_killer"], ("Enemy", 6))
        payload = roast_mod.match_input(view)
        self.assertEqual(payload["本局在队内排第一的指标"], ["首杀"])
        self.assertEqual(len(payload["平台趣味标签"]), 1)
        self.assertIn("爆头机器", payload["平台趣味标签"][0])

    def test_jinja_templates_render_html(self):
        from cs_stats.browser import BrowserPool

        cfg = CSStatsConfig.from_raw({})
        renderer = render_mod.CardRenderer(cfg, BrowserPool())

        profile_view = render_mod.pvp_profile_view(
            {
                "name": "Donk",
                "seasonId": "S22",
                "cnt": 10,
                "pvpScore": 2100,
                "pwRating": 1.42,
                "rating": 1.38,
                "adr": 99.1,
                "rws": 14.2,
                "kd": 1.5,
                "winRate": 0.7,
                "historyPwRatings": [1.2, 1.5],
                "historyScores": [2050, 2100],
            },
            [],
            {"id": "76561198000000001", "nickname": "Donk"},
            "",
        )
        html_profile = asyncio.run(
            renderer.render_html(
                "profile.html",
                v=profile_view,
                roast={"title": "突破怪物", "roast": "见人就秒"},
                show_images=False,
            )
        )
        self.assertIn("Donk", html_profile)
        self.assertIn("突破怪物", html_profile)

        html_bind = asyncio.run(
            renderer.render_html(
                "bind_confirm.html",
                platform="pw",
                candidates=[
                    {
                        "id": "76561198000000001",
                        "nickname": "Donk",
                        "avatar": "",
                        "score_label": "本赛季天梯分",
                        "score": 2100,
                    }
                ],
                purpose="绑定",
                show_images=False,
            )
        )
        self.assertIn("绑定哪个完美账号", html_bind)

        detail = {
            "base": {
                "matchId": "m1",
                "map": "Mirage",
                "mode": "天梯",
                "startTime": "2026-11-20 22:00:00",
                "duration": 30,
                "score1": 13,
                "score2": 8,
                "halfScore1": 8,
                "halfScore2": 4,
                "winTeam": 1,
            },
            "players": [
                {
                    "playerId": "1001",
                    "nickName": "Donk",
                    "team": 1,
                    "kill": 25,
                    "death": 12,
                    "assist": 4,
                    "adpr": 105,
                    "pwRating": 1.65,
                    "rating": 1.60,
                    "headShotRatio": 0.64,
                    "entryKill": 5,
                    "firstDeath": 1,
                    "mvp": 1,
                    "pvpScore": 2100,
                    "pvpScoreChange": 25,
                },
                {
                    "playerId": "2001",
                    "nickName": "Bot",
                    "team": 2,
                    "kill": 10,
                    "death": 18,
                    "assist": 2,
                    "adpr": 55,
                    "pwRating": 0.72,
                    "rating": 0.70,
                    "headShotRatio": 0.30,
                    "entryKill": 1,
                    "firstDeath": 4,
                    "mvp": 0,
                    "pvpScore": 2050,
                    "pvpScoreChange": -20,
                },
            ],
        }
        match_view = render_mod.pw_match_view(detail, "1001", "pvp")
        html_match = asyncio.run(
            renderer.render_html("match.html", v=match_view, roast=None, show_images=False)
        )
        self.assertIn("Mirage", html_match)
        self.assertIn("13:8", html_match)

    def test_astrbot_html_render_fallback_and_steam_skip(self):
        from cs_stats.browser import BrowserPool
        from cs_stats.pw_api import PWClient

        # steam_avatar_timeout=0 时直接跳过请求
        cfg_skip = CSStatsConfig.from_raw({"steam_avatar_timeout": 0, "pvp_season_lookback": 2})
        pw = PWClient(cfg_skip)
        self.assertEqual(asyncio.run(pw.steam_avatar("76561199058490569")), "")

        # pvp_stats 回溯历史赛季遇到「无效的账号」应跳过而不是中断查询
        from cs_stats.pw_api import PWError

        async def fake_pvp_season(steam_id: str, season: str):
            if season == "":
                return {"seasonId": "S23", "cnt": 0}
            if season == "S22":
                raise PWError("完美天梯生涯失败：无效的账号")
            if season == "S21":
                return {"seasonId": "S21", "cnt": 12}
            return {"seasonId": season, "cnt": 0}

        pw._pvp_season = fake_pvp_season  # type: ignore[method-assign]
        res = asyncio.run(pw.pvp_stats("76561199058490569"))
        self.assertEqual(res["seasonId"], "S21")
        self.assertEqual(res["cnt"], 12)

        # Playwright 不可用时自动回退到 AstrBot html_render，且内联 BarlowCondensed 字体
        with tempfile.TemporaryDirectory() as tmp:
            fake_png = Path(tmp) / "out.png"
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            captured: dict = {}

            async def fake_html_render(tmpl, data, return_url=True, options=None):
                captured["tmpl"] = tmpl
                captured["html"] = data["_html"]
                captured["options"] = options
                return str(fake_png)

            cfg_auto = CSStatsConfig.from_raw({"render": {"backend": "astrbot"}})
            renderer = render_mod.CardRenderer(
                cfg_auto, BrowserPool(), astrbot_html_render=fake_html_render
            )
            png_bytes = asyncio.run(
                renderer.render_candidates(
                    "pw",
                    [
                        {
                            "id": "76561199058490569",
                            "nickname": "Player{{x}}",
                            "avatar": "",
                            "score_label": "本赛季天梯分",
                            "score": 1800,
                        }
                    ],
                    "绑定",
                )
            )
            self.assertEqual(png_bytes, b"\x89PNG\r\n\x1a\nfake")
            self.assertEqual(captured["tmpl"], "{{ _html | safe }}")
            self.assertIn("data:font/ttf;base64,", captured["html"])
            self.assertIn("Player{{x}}", captured["html"])


class TestMainPluginMockedAstrBot(unittest.TestCase):
    def test_main_import_and_snowluma_reply_fallback(self):
        # 注入最小化 astrbot mock，验证 main.py 在 AstrBot 4.29.4 导入与运行无语法/API 错误
        astrbot_mod = types.ModuleType("astrbot")
        api_mod = types.ModuleType("astrbot.api")
        event_mod = types.ModuleType("astrbot.api.event")
        star_mod = types.ModuleType("astrbot.api.star")
        comp_mod = types.ModuleType("astrbot.api.message_components")
        util_mod = types.ModuleType("astrbot.api.util")

        class DummyConfig(dict):
            def save_config(self):
                self.saved = True

        class DummyLogger:
            info = warning = error = debug = exception = staticmethod(lambda *a, **kw: None)

        class DummyStar:
            def __init__(self, context):
                self.context = context

        class DummyStarTools:
            _dir = tempfile.mkdtemp()

            @classmethod
            def get_data_dir(cls, name=None):
                return Path(cls._dir)

        class DummyFilter:
            class EventMessageType:
                ALL = "ALL"

            @staticmethod
            def event_message_type(*args, **kwargs):
                return lambda fn: fn

        class DummyImage:
            @staticmethod
            def fromBytes(data: bytes):
                return ("image_bytes", len(data))

        class DummyPlain:
            def __init__(self, text: str):
                self.text = text

        class DummySessionFilter:
            pass

        class DummySessionController:
            pass

        def dummy_session_waiter(**kwargs):
            return lambda fn: fn

        api_mod.AstrBotConfig = DummyConfig
        api_mod.logger = DummyLogger
        event_mod.AstrMessageEvent = object
        event_mod.MessageChain = list
        event_mod.filter = DummyFilter
        star_mod.Context = object
        star_mod.Star = DummyStar
        star_mod.StarTools = DummyStarTools
        comp_mod.Image = DummyImage
        comp_mod.Plain = DummyPlain
        util_mod.SessionController = DummySessionController
        util_mod.SessionFilter = DummySessionFilter
        util_mod.session_waiter = dummy_session_waiter

        injected = {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.message_components": comp_mod,
            "astrbot.api.util": util_mod,
        }
        old = {k: sys.modules.get(k) for k in injected}
        sys.modules.update(injected)
        try:
            # 以包形式加载插件
            pkg = types.ModuleType("astrbot_plugin_cs_stats")
            pkg.__path__ = [str(ROOT)]
            sys.modules["astrbot_plugin_cs_stats"] = pkg
            import importlib

            main_mod = importlib.import_module("astrbot_plugin_cs_stats.main")

            class FakeContext:
                def get_config(self):
                    return {"admins_id": ["astrbot", "888888"]}

            plugin = main_mod.CSStatsPlugin(FakeContext(), DummyConfig())

            # 测试 SnowLuma get_msg 补齐 Reply.sender_id
            class FakeApi:
                call_action = AsyncMock(return_value={"status": "ok", "data": {"sender": {"user_id": 55555}}})

            class FakeEvent:
                bot = types.SimpleNamespace(api=FakeApi())
                stopped = False

                def get_messages(self):
                    return [FakePlain("cs帮助")]

                def get_self_id(self):
                    return "999"

                def get_sender_id(self):
                    return "12345"

                def get_group_id(self):
                    return "77777"

                def is_admin(self):
                    return False

                def plain_result(self, text):
                    return ("plain", text)

                def stop_event(self):
                    self.stopped = True

            ev = FakeEvent()
            parsed = parse_message_command([FakeReply("10086", None), FakePlain("完美战绩")], self_id="999")
            sender = asyncio.run(plugin._resolve_reply_sender(ev, parsed))
            self.assertEqual(sender, "55555")

            # 测试 help 分发与 stop_event
            async def run_help():
                out = []
                async for item in plugin.on_message(ev):
                    out.append(item)
                return out

            results = asyncio.run(run_help())
            self.assertEqual(len(results), 1)
            self.assertIn("完美战绩", results[0][1])
            self.assertTrue(ev.stopped)

            # 测试通知管理员 send_private_msg
            asyncio.run(plugin._notify_token_invalid(ev))
            ev.bot.api.call_action.assert_any_await(
                "send_private_msg",
                user_id=888888,
                message=unittest.mock.ANY,
            )

            # 测试 terminate
            asyncio.run(plugin.terminate())
        finally:
            for k, v in old.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            sys.modules.pop("astrbot_plugin_cs_stats.main", None)
            sys.modules.pop("astrbot_plugin_cs_stats", None)


if __name__ == "__main__":
    unittest.main()
