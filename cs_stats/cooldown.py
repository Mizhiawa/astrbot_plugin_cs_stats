"""群聊功能冷却管理（私聊和管理员不受限制，仅成功执行后扣 CD）。"""

import time


class CooldownManager:
    def __init__(self) -> None:
        # (feature, group_id, user_id) -> last_timestamp
        self._last_used: dict[tuple[str, str, str], float] = {}

    @staticmethod
    def _key(
        feature: str, group_id: str | None, user_id: str, *, is_admin: bool
    ) -> tuple[str, str, str] | None:
        if not group_id or is_admin:
            return None
        return feature, str(group_id), str(user_id)

    def remaining(
        self,
        feature: str,
        group_id: str | None,
        user_id: str,
        duration_seconds: int,
        *,
        is_admin: bool,
    ) -> int:
        if duration_seconds <= 0:
            return 0
        key = self._key(feature, group_id, user_id, is_admin=is_admin)
        if key is None:
            return 0
        passed = time.time() - self._last_used.get(key, 0.0)
        if passed >= duration_seconds:
            return 0
        return max(1, int(duration_seconds - passed))

    def mark(
        self,
        feature: str,
        group_id: str | None,
        user_id: str,
        duration_seconds: int,
        *,
        is_admin: bool,
    ) -> None:
        if duration_seconds <= 0:
            return
        key = self._key(feature, group_id, user_id, is_admin=is_admin)
        if key is not None:
            self._last_used[key] = time.time()
