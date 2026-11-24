"""绑定表和锐评表持久化管理。

bindings.json：{QQ: {"pw": [账号...], "5e": [账号...]}}，按 QQ 全局生效，列表第一个是主号。
账号的 id 在完美是 SteamID64，在 5E 是 domain；5E 另存 uuid 省一次转换。
一个游戏账号可以被多个 QQ 绑定，否则先绑的人能占住别人的号。

roasts.json：{"career": {key: 锐评}, "match": {key: 锐评}}。
生涯锐评每个 key 只留最新一份，水位变了才重新生成；单局锐评按局存，对局不会变。
"""

from datetime import datetime
from pathlib import Path

from .json_io import atomic_write_json, load_json
from .logging_util import logger


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class CSStatsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bindings_file = self.data_dir / "bindings.json"
        self.roasts_file = self.data_dir / "roasts.json"
        self.bindings: dict[str, dict[str, list[dict]]] = self._load(self.bindings_file, {})
        self.roasts: dict[str, dict[str, dict]] = self._load(
            self.roasts_file, {"career": {}, "match": {}}
        )
        self.roasts.setdefault("career", {})
        self.roasts.setdefault("match", {})

    def _load(self, path: Path, default: dict) -> dict:
        result = load_json(path, default=default, missing_ok=True, backup_on_error=True)
        if not result.success:
            logger.error(
                f"[cs_stats] {path.name} 读取失败，已备份为 {result.backup_path}: {result.error}"
            )
        return result.data

    def accounts(self, qq: str, platform: str) -> list[dict]:
        return list(self.bindings.get(qq, {}).get(platform, []))

    def add_account(self, qq: str, platform: str, account: dict) -> None:
        self.bindings.setdefault(qq, {}).setdefault(platform, []).append(
            {**account, "bound_at": _now()}
        )
        atomic_write_json(self.bindings_file, self.bindings)

    def remove_account(self, qq: str, platform: str, index: int) -> dict:
        removed = self.bindings[qq][platform].pop(index)
        atomic_write_json(self.bindings_file, self.bindings)
        return removed

    def set_main(self, qq: str, platform: str, index: int) -> None:
        items = self.bindings[qq][platform]
        items.insert(0, items.pop(index))
        atomic_write_json(self.bindings_file, self.bindings)

    def get_roast(self, kind: str, key: str) -> dict | None:
        return self.roasts.setdefault(kind, {}).get(key)

    def save_roast(self, kind: str, key: str, roast: dict) -> None:
        self.roasts.setdefault(kind, {})[key] = {**roast, "created_at": _now()}
        atomic_write_json(self.roasts_file, self.roasts)
