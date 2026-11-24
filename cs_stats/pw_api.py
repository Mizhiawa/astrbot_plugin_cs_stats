"""完美电竞 App 的私有接口。战绩类接口用管理员登录的 token 访问，搜索不用登录。

dataSource：3 是完美天梯，1 是官匹（完美同步的 Steam 官方匹配）。
头像不用完美的，取 Steam 资料页上的，见 steam_avatar。
"""

import time
import xml.etree.ElementTree as ET

import httpx

from .config import CSStatsConfig
from .logging_util import get_exc_desc, logger

API = "https://api.wmpvp.com/api"
SEARCH_URL = "https://appengine.wmpvp.com/steamcn/app/search/user"
LOGIN_URL = "https://passport.pwesports.cn/account/login"
SOURCE = {"pvp": 3, "mm": 1}
# 无效 token 实测返回 statusCode 4001「登录失效，请重新登录」
TOKEN_INVALID_CODE = 4001


class PWError(Exception):
    """文案直接回给用户。"""


class PWTokenInvalid(PWError):
    def __init__(self, newly: bool):
        super().__init__("完美查询暂时不可用（登录态失效，已通知管理员）")
        self.newly = newly


class PWClient:
    def __init__(self, config: CSStatsConfig):
        self.config = config
        self.token_invalid = False

    def update_config(self, config: CSStatsConfig) -> None:
        self.config = config

    def reset_token_state(self) -> None:
        self.token_invalid = False

    def _require_auth_config(self) -> None:
        if not self.config.pw_token or not self.config.pw_steam_id:
            raise PWError(
                "尚未配置完美登录凭据（pw_token / pw_steam_id），"
                "请管理员在 WebUI 配置或私聊发送「完美登录 <手机号> <验证码>」"
            )
        if not str(self.config.pw_steam_id).isdigit():
            raise PWError("配置的 pw_steam_id 格式不正确，应为纯数字 SteamID64")

    async def _post(self, url: str, payload: dict, *, auth: bool = True) -> dict:
        if auth:
            self._require_auth_config()
            if self.token_invalid:
                raise PWTokenInvalid(newly=False)
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "okhttp/4.11.0",
            "appversion": self.config.pw_appversion,
            "platform": "android",
        }
        if auth:
            headers["token"] = self.config.pw_token
        async with httpx.AsyncClient(timeout=self.config.http_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _unwrap(self, data: dict, what: str, *, ctx: str = ""):
        if data.get("statusCode") == 0:
            return data.get("data")
        if data.get("statusCode") == TOKEN_INVALID_CODE:
            tail = self.config.pw_token[-4:] if len(self.config.pw_token) >= 4 else ""
            logger.warning(
                f"[cs_stats] 完美接口 {what} 返回 4001: "
                f"errorMessage={data.get('errorMessage')!r}, "
                f"token_len={len(self.config.pw_token)}, token_tail=…{tail}, "
                f"appversion={self.config.pw_appversion!r}"
            )
            newly = not self.token_invalid
            self.token_invalid = True
            raise PWTokenInvalid(newly=newly)
        logger.warning(
            f"[cs_stats] 完美接口 {what} 返回异常: "
            f"statusCode={data.get('statusCode')}, errorMessage={data.get('errorMessage')!r}, "
            f"appversion={self.config.pw_appversion!r}, {ctx}"
        )
        raise PWError(f"{what}失败：{data.get('errorMessage') or data.get('statusCode')}")

    def _ids(self, steam_id: str) -> dict:
        self._require_auth_config()
        return {"mySteamId": int(self.config.pw_steam_id), "toSteamId": int(steam_id)}

    async def verify_token(self, token: str, steam_id: str) -> tuple[bool, str]:
        """用刚获取的 token 立即请求一次完美天梯接口，验证是否可用。"""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "okhttp/4.11.0",
            "appversion": self.config.pw_appversion,
            "platform": "android",
            "token": token,
        }
        payload = {
            "mySteamId": int(steam_id),
            "toSteamId": int(steam_id),
            "accessToken": "",
            "csgoSeasonId": "",
        }
        async with httpx.AsyncClient(timeout=self.config.http_timeout) as client:
            resp = await client.post(
                f"{API}/csgo/home/pvp/detailStats", json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
        code = body.get("statusCode")
        if code == 0:
            return True, "ok"
        return False, f"statusCode={code}, errorMessage={body.get('errorMessage')!r}"

    async def diagnose_token(self) -> list[str]:
        """用当前已保存的 pw_token / pw_steam_id 探测不同请求头与参数组合（不泄露 token）。"""
        self._require_auth_config()
        token = self.config.pw_token
        sid = int(self.config.pw_steam_id)
        tail = token[-4:] if len(token) >= 4 else ""
        ts = str(int(time.time()))
        base_headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "okhttp/4.11.0",
            "appversion": self.config.pw_appversion,
            "platform": "android",
            "token": token,
        }
        base_payload = {
            "mySteamId": sid,
            "toSteamId": sid,
            "accessToken": "",
            "csgoSeasonId": "",
        }
        cases: list[tuple[str, str, dict, dict]] = [
            ("A_默认android", f"{API}/csgo/home/pvp/detailStats", base_headers, base_payload),
            (
                "B_body带accessToken",
                f"{API}/csgo/home/pvp/detailStats",
                base_headers,
                {**base_payload, "accessToken": token},
            ),
            (
                "C_带t+h5_android",
                f"{API}/csgo/home/pvp/detailStats",
                {**base_headers, "platform": "h5_android", "t": ts},
                base_payload,
            ),
            (
                "D_platform=ios",
                f"{API}/csgo/home/pvp/detailStats",
                {**base_headers, "platform": "ios"},
                base_payload,
            ),
            (
                "E_3.5.4无platform",
                f"{API}/csgo/home/pvp/detailStats",
                {
                    "Content-Type": "application/json;charset=UTF-8",
                    "appversion": "3.5.4.172",
                    "token": token,
                },
                base_payload,
            ),
            (
                "F_官匹detailStats",
                f"{API}/csgo/home/official/detailStats",
                base_headers,
                base_payload,
            ),
        ]
        lines = [
            f"当前配置: steamId=…{str(sid)[-4:]} (len={len(str(sid))}), "
            f"token=…{tail} (len={len(token)}), appversion={self.config.pw_appversion}"
        ]
        async with httpx.AsyncClient(timeout=self.config.http_timeout) as client:
            for name, url, hdrs, body in cases:
                try:
                    resp = await client.post(url, json=body, headers=hdrs)
                    data = resp.json()
                    sc = data.get("statusCode")
                    msg = data.get("errorMessage")
                    extra = ""
                    if sc == 0 and isinstance(data.get("data"), dict):
                        d = data["data"]
                        extra = f", season={d.get('seasonId')}, cnt={d.get('cnt')}, name={d.get('name') or d.get('nickName')}"
                    lines.append(f"- {name}: HTTP {resp.status_code}, statusCode={sc}, msg={msg!r}{extra}")
                except Exception as exc:
                    lines.append(f"- {name}: 异常 {get_exc_desc(exc)}")
        return lines

    async def login_with_sms(self, mobile: str, code: str) -> tuple[str, str]:
        """使用完美 App 短信验证码登录，返回 (token, steam_id)。"""
        data = await self._post(
            LOGIN_URL,
            {"appId": 2, "mobilePhone": mobile, "securityCode": code},
            auth=False,
        )
        if str(data.get("code")) != "0":
            raise PWError(f"完美登录失败：{data.get('description') or data}")
        result_obj = data.get("result") or {}
        login_res = result_obj.get("loginResult") or {}
        account = login_res.get("accountInfo") or {}
        token = str(account.get("token") or "").strip()
        steam_id = str(account.get("steamId") or "").strip()
        logger.info(
            f"[cs_stats] 完美登录响应字段: loginResult_keys={sorted(login_res.keys())}, "
            f"accountInfo_keys={sorted(account.keys())}, "
            f"token_len={len(token)}, token_tail=…{token[-4:] if len(token) >= 4 else ''}"
        )
        if not token or not steam_id:
            raise PWError("完美登录返回数据缺少 token 或 steamId")
        return token, steam_id

    async def search(self, keyword: str) -> list[dict]:
        """昵称或 SteamID64 都能搜。"""
        data = await self._post(SEARCH_URL, {"keyword": keyword, "page": 1}, auth=False)
        if data.get("code") != 1:
            raise PWError(f"完美搜索失败：{data.get('message') or data.get('code')}")
        return [
            {
                "id": str(item["steamId"]),
                "nickname": item.get("pvpNickName")
                or item.get("appNickName")
                or str(item["steamId"]),
                "score": item.get("pvpScore") or 0,
            }
            for item in data.get("result") or []
            if item.get("steamId")
        ]

    async def _pvp_season(self, steam_id: str, season: str) -> dict:
        payload = {**self._ids(steam_id), "accessToken": "", "csgoSeasonId": season}
        ctx = (
            f"mySteamId={payload['mySteamId']}, toSteamId={payload['toSteamId']}, "
            f"csgoSeasonId={season!r}"
        )
        resp = await self._post(f"{API}/csgo/home/pvp/detailStats", payload)
        data = self._unwrap(resp, "完美天梯生涯", ctx=ctx)
        if isinstance(data, dict):
            logger.info(
                f"[cs_stats] 完美天梯生涯成功: {ctx}, seasonId={data.get('seasonId')!r}, "
                f"cnt={data.get('cnt')!r}, name={data.get('name')!r}"
            )
        return data

    async def pvp_stats(self, steam_id: str) -> dict:
        """本赛季没打就往前找有数据的赛季；都没有时返回本赛季（cnt 为 0）。"""
        current = await self._pvp_season(steam_id, "")
        if int(current.get("cnt") or 0) > 0:
            return current
        raw_season = str(current.get("seasonId") or "").lstrip("S")
        if not raw_season.isdigit():
            return current
        season_no = int(raw_season)
        for back in range(1, self.config.pvp_season_lookback + 1):
            if season_no - back <= 0:
                break
            season_id = f"S{season_no - back}"
            try:
                data = await self._pvp_season(steam_id, season_id)
            except PWTokenInvalid:
                raise
            except PWError as exc:
                logger.info(
                    f"[cs_stats] 回溯完美天梯赛季 {season_id} 无数据/跳过 ({steam_id}): {exc}"
                )
                continue
            if int(data.get("cnt") or 0) > 0:
                return data
        return current

    async def official_stats(self, steam_id: str) -> dict:
        """官匹最近 100 局汇总。"""
        payload = {**self._ids(steam_id), "accessToken": "", "csgoSeasonId": ""}
        return self._unwrap(
            await self._post(f"{API}/csgo/home/official/detailStats", payload), "官匹生涯"
        )

    async def match_list(
        self, steam_id: str, mode: str, size: int = 10
    ) -> tuple[bool, list[dict]]:
        """返回（对方是否公开数据, 最近对局）。"""
        payload = {
            **self._ids(steam_id),
            "dataSource": SOURCE[mode],
            "page": 1,
            "pageSize": size,
            "csgoSeasonId": "recent",
            "pvpType": -1,
        }
        ctx = (
            f"mySteamId={payload['mySteamId']}, toSteamId={payload['toSteamId']}, "
            f"dataSource={payload['dataSource']}"
        )
        data = self._unwrap(
            await self._post(f"{API}/csgo/home/match/list", payload), "对局列表", ctx=ctx
        )
        return bool(data.get("dataPublic")), data.get("matchList") or []

    async def match_detail(self, match_id: str, mode: str) -> dict:
        # 原来的 home/match/detailStats 已经 404
        payload = {"matchId": match_id, "platform": "admin", "dataSource": str(SOURCE[mode])}
        return self._unwrap(await self._post(f"{API}/v1/csgo/match", payload), "单局详情")

    async def steam_avatar(self, steam_id: str) -> str:
        """完美的两个图床（img/cdn.wmpvp.com）连续请求就会挂起或 403，所以头像改取 Steam 资料页。
        资料页一人一次请求，单局卡十个人就是十次，所以单局卡不放头像。
        取不到或超时（默认 3 秒）就返回空串，卡片显示首字母。"""
        if self.config.steam_avatar_timeout <= 0:
            return ""
        url = f"https://steamcommunity.com/profiles/{steam_id}?xml=1"
        try:
            async with httpx.AsyncClient(
                timeout=self.config.steam_avatar_timeout,
                proxy=self.config.steam_proxy,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            return ET.fromstring(resp.text).findtext("avatarFull") or ""
        except (httpx.HTTPError, ET.ParseError) as exc:
            logger.warning(f"[cs_stats] Steam 头像获取失败 {steam_id}: {get_exc_desc(exc)}")
            return ""
