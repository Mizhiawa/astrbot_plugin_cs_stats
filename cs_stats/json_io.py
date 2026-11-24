"""跨平台原子 JSON 读写工具（兼容 Windows 与 Linux）。"""

import copy
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging_util import logger


@dataclass(frozen=True)
class JsonLoadResult:
    success: bool
    data: Any
    error: Exception | None = None
    backup_path: Path | None = None
    missing: bool = False


def load_json(
    path: str | Path,
    *,
    expected_type: type = dict,
    default: Any = None,
    missing_ok: bool = False,
    backup_on_error: bool = False,
) -> JsonLoadResult:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, expected_type):
            raise TypeError(
                f"JSON 顶层应为 {expected_type.__name__}，实际是 {type(data).__name__}"
            )
        return JsonLoadResult(True, data)
    except FileNotFoundError as exc:
        return JsonLoadResult(
            missing_ok,
            copy.deepcopy(default),
            None if missing_ok else exc,
            missing=True,
        )
    except (OSError, ValueError, TypeError) as exc:
        backup = None
        if backup_on_error and target.is_file():
            try:
                backup = target.with_name(f"{target.name}.corrupt.{time.time_ns()}")
                shutil.copy2(target, backup)
            except OSError as backup_error:
                logger.warning(f"[cs_stats] 损坏的 JSON 备份失败 {target}: {backup_error}")
                backup = None
        return JsonLoadResult(False, copy.deepcopy(default), exc, backup)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        # Linux/POSIX 下尝试 fsync 父目录；Windows 不支持对目录使用 os.open
        if os.name != "nt":
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    expected_type: type | None = None,
) -> None:
    """原子写 JSON 文件，避免写入中途异常导致文件截断。"""
    if expected_type is not None and not isinstance(data, expected_type):
        raise TypeError(f"JSON 顶层应为 {expected_type.__name__}")
    atomic_write_bytes(
        path, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent).encode("utf-8")
    )
