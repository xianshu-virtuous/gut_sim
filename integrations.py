"""轻量联动：把肠胃事件写进别的插件。

目前只做一个：**向日程系统（daily_schedule）注入一条约十分钟的事件**。
比如"去了一下洗手间（约 3 分钟）""肚子不舒服，跑了一趟厕所（约 10 分钟）"。

设计上刻意做成**软依赖**：

- 不在 manifest 里声明 daily_schedule（声明了会让插件被静默剔除）；
- 运行时 try import，拿不到就静默跳过，绝不因为对方不存在/改版而报错；
- 写入失败也只记 debug 日志——这只是锦上添花的东西，不能反过来影响对话。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from src.kernel.logger import get_logger

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

#: 写入频率限制：同一类事件多久内只写一次（秒）
_MIN_INTERVAL = 120.0

#: 上次写入时间：event_key -> 时间戳
_LAST_WRITE: dict[str, float] = {}


def _too_soon(key: str) -> bool:
    last = _LAST_WRITE.get(key, 0.0)
    return (time.time() - last) < _MIN_INTERVAL


def _mark(key: str) -> None:
    _LAST_WRITE[key] = time.time()


async def note_event(
    config: "GutSimConfig",
    text: str,
    minutes: float,
    *,
    key: str = "",
) -> bool:
    """往日程日志里追加一条事件（软依赖）。

    Args:
        config: 插件配置（读 ``[integrations].daily_schedule``）
        text: 事件描述，例如"去了一下洗手间"
        minutes: 这件事大约占了多久
        key: 去重/限流用的键，默认取 text

    Returns:
        真的写进去了返回 True；对方不存在、关闭、失败都返回 False
    """

    if not config.integrations.daily_schedule:
        return False

    dedupe = key or text
    if _too_soon(dedupe):
        return False

    try:
        from daily_schedule.generator import append_log_event  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - 对方没装/改版：静默跳过
        logger.debug(f"gut_sim: daily_schedule 不可用，跳过事件注入：{exc!r}")
        return False

    try:
        day = datetime.now().date().isoformat()
        line = f"{text}（约 {max(1, int(round(minutes)))} 分钟）"
        await append_log_event(day, line)
        _mark(dedupe)
        logger.info(f"gut_sim: 已向日程注入事件「{line}」")
        return True
    except Exception as exc:  # noqa: BLE001 - 写入失败不影响主流程
        logger.debug(f"gut_sim: 日程事件写入失败：{exc!r}")
        return False


async def note_toilet(config: "GutSimConfig", seconds: float) -> bool:
    """如厕回来：写一条"去了洗手间"。"""

    minutes = max(1.0, float(seconds) / 60.0)
    return await note_event(config, "去了一下洗手间", minutes, key="toilet")


async def note_wetting(config: "GutSimConfig", place: str = "") -> bool:
    """失禁：写一条"肚子不舒服、收拾了一阵"。"""

    where = f"（{place}）" if place else ""
    return await note_event(
        config, f"肚子不舒服，跑了一趟厕所{where}", 10.0, key="wetting"
    )


async def note_diarrhea(config: "GutSimConfig") -> bool:
    """闹肚子事件：一条十分钟左右的厕所。"""

    return await note_event(config, "肠胃不舒服，在洗手间待了一阵", 10.0, key="diarrhea")


def reset_throttle() -> None:
    """清掉限流记录（测试用）。"""

    _LAST_WRITE.clear()


__all__ = [
    "note_diarrhea",
    "note_event",
    "note_toilet",
    "note_wetting",
    "reset_throttle",
]
