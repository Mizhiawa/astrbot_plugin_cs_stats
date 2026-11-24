"""完美账号命令行登录脚本：将新获取的 token 与 steamId 写入 AstrBot 插件配置。

先在完美世界电竞 App 登录页给手机发送验证码（不要在 App 里提交），然后在终端运行：
    python scripts/pw_login.py
或指定 AstrBot 插件配置文件路径：
    python scripts/pw_login.py --config /path/to/AstrBot/data/config/astrbot_plugin_cs_stats_config.json

写入后在群聊或私聊发送管理员命令「完美重载」即可生效，无需重启 AstrBot。
（也可以直接私聊机器人发送「完美登录 <手机号> <验证码>」完成相同操作。）
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cs_stats.json_io import atomic_write_json, load_json  # noqa: E402

DEFAULT_APPVERSION = "4.2.1.232"
CONFIG_FILENAME = "astrbot_plugin_cs_stats_config.json"


def _guess_config_path() -> Path:
    plugin_root = Path(__file__).resolve().parents[1]
    # 常见部署路径：AstrBot/data/plugins/astrbot_plugin_cs_stats/scripts/pw_login.py
    # 对应配置路径：AstrBot/data/config/astrbot_plugin_cs_stats_config.json
    candidates = [
        plugin_root.parents[1] / "config" / CONFIG_FILENAME,
        Path.cwd() / "data" / "config" / CONFIG_FILENAME,
        plugin_root / CONFIG_FILENAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="登录完美世界电竞并写入 AstrBot cs_stats 配置")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="AstrBot 插件配置文件路径（默认自动推测 data/config/astrbot_plugin_cs_stats_config.json）",
    )
    args = parser.parse_args()

    config_file: Path = args.config or _guess_config_path()
    loaded = load_json(config_file, default={}, missing_ok=True)
    cfg = loaded.data if isinstance(loaded.data, dict) else {}
    appversion = str(cfg.get("pw_appversion") or DEFAULT_APPVERSION)

    mobile = input("手机号: ").strip()
    code = input("验证码: ").strip()
    if not mobile or not code:
        sys.exit("手机号和验证码不能为空")

    request = urllib.request.Request(
        "https://passport.pwesports.cn/account/login",
        json.dumps({"appId": 2, "mobilePhone": mobile, "securityCode": code}).encode("utf-8"),
        {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "okhttp/4.11.0",
            "appversion": appversion,
            "platform": "android",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if str(data.get("code")) != "0":
        sys.exit(f"登录失败: {data.get('description') or data}")

    account = data["result"]["loginResult"]["accountInfo"]
    cfg["pw_token"] = str(account["token"])
    cfg["pw_steam_id"] = str(account["steamId"])
    cfg.setdefault("pw_appversion", appversion)
    atomic_write_json(config_file, cfg)
    print(f"已写入 {config_file}，SteamID {account['steamId']}。向机器人发送「完美重载」生效。")


if __name__ == "__main__":
    main()
