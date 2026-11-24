"""日志与异常辅助工具。"""

import logging

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger("astrbot_plugin_cs_stats")


def get_exc_desc(exc: Exception) -> str:
    """获取异常的简短描述。"""
    return f"{type(exc).__name__}: {exc}"
