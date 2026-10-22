"""与 sisp 的联动：只读它的状态，判断当前是不是私密模式。

为什么只读：
- 三种 sisp（市场版 / deep_neo_sisp / 本地改造版）都把「局内状态」落在
  ``storage_api`` 的 JSON 里，key 就是 ``stream_id``，字段都叫 ``in_sex_mode``；
- 差异只有 store 名（deep_neo_sisp 用 ``deep_neo_sisp``，市场版与本地版都用 ``sisp``）；
- 所以适配层只需要按顺序探测 store，不需要 import 对方模块、更不需要改对方代码。

约定：探测失败 = 普通模式，绝不抛错打断主流程。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

#: 各个 sisp 变体里表示「私密模式」的字段名（实测三版一致）
PRIVATE_FIELD = "in_sex_mode"

#: 探测结果缓存：stream_id -> (时间戳, 是否私密, 命中的 store)
_CACHE: dict[str, tuple[float, bool, str]] = {}


def _key(stream_id: str, config: "GutSimConfig") -> str:
    suffix = str(config.sisp.state_key_suffix or "").strip()
    return f"{stream_id}{suffix}" if suffix else stream_id


def _candidate_stores(config: "GutSimConfig") -> list[str]:
    """按优先级列出要探测的 store 名。"""

    stores: list[str] = []
    extra = str(config.sisp.extra_store or "").strip()
    if extra:
        stores.append(extra)
    for name in config.sisp.store_order or []:
        text = str(name or "").strip()
        if text and text not in stores:
            stores.append(text)
    return stores


async def detect(stream_id: str, config: "GutSimConfig") -> tuple[bool, str]:
    """返回 (是否私密模式, 命中的 store 名)。

    Args:
        stream_id: 聊天流 ID
        config: 插件配置

    Returns:
        (private, store) —— 没探测到时返回 (False, "")
    """

    if not config.sisp.enabled or not stream_id:
        return False, ""

    now = time.time()
    cached = _CACHE.get(stream_id)
    ttl = float(config.sisp.cache_seconds)
    if cached and ttl > 0 and now - cached[0] <= ttl:
        return cached[1], cached[2]

    key = _key(stream_id, config)
    for store in _candidate_stores(config):
        try:
            data = await storage_api.load_json(store, key)
        except Exception as exc:  # noqa: BLE001 - 探测失败按普通模式处理
            logger.debug(f"gut_sim: 读取 {store} 状态失败（按普通模式）：{exc!r}")
            continue

        if isinstance(data, dict) and PRIVATE_FIELD in data:
            private = bool(data.get(PRIVATE_FIELD))
            _CACHE[stream_id] = (now, private, store)
            return private, store

    _CACHE[stream_id] = (now, False, "")
    return False, ""


async def private_mode(stream_id: str, config: "GutSimConfig") -> bool:
    """只要布尔值的简写。"""

    private, _store = await detect(stream_id, config)
    return private


async def read_state(stream_id: str, config: "GutSimConfig") -> tuple[dict, str]:
    """读 sisp 的整份局内状态（只读）。返回 (数据, 命中的 store)。"""

    if not config.sisp.enabled or not stream_id:
        return {}, ""

    key = _key(stream_id, config)
    for store in _candidate_stores(config):
        try:
            data = await storage_api.load_json(store, key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: 读取 {store} 状态失败：{exc!r}")
            continue
        if isinstance(data, dict) and PRIVATE_FIELD in data:
            return data, store
    return {}, ""


#: 场景标签缓存：stream_id -> (时间戳, scene_id, route)
_SCENE_CACHE: dict[str, tuple[float, str, str]] = {}


async def current_scene(stream_id: str, config: "GutSimConfig") -> str:
    """sisp 当前生效的场景标签（如 ``laxative``）；没有就空串。

    只读它的 ``scene`` / ``scene_until``，用来点亮我们同名的卡。
    """

    if not config.scene.enabled or not config.scene.follow_sisp:
        return ""

    now = time.time()
    cached = _SCENE_CACHE.get(stream_id)
    ttl = float(config.scene.sync_interval_seconds)
    if cached and ttl > 0 and now - cached[0] <= ttl:
        return cached[1]

    data, _store = await read_state(stream_id, config)
    scene = str(data.get("scene") or "").strip()
    until = float(data.get("scene_until") or 0.0)
    if scene and until > 0 and until < now:
        scene = ""

    _SCENE_CACHE[stream_id] = (now, scene, str(data.get("route") or ""))
    return scene


async def current_route(stream_id: str, config: "GutSimConfig") -> str:
    """sisp 当前路线（default / gas / excrete / intentional）。"""

    if not config.scene.enabled or not config.scene.follow_sisp:
        return ""

    now = time.time()
    cached = _SCENE_CACHE.get(stream_id)
    ttl = float(config.scene.sync_interval_seconds)
    if cached and ttl > 0 and now - cached[0] <= ttl:
        return cached[2]

    data, _store = await read_state(stream_id, config)
    route = str(data.get("route") or "").strip()
    scene = str(data.get("scene") or "").strip()
    until = float(data.get("scene_until") or 0.0)
    if scene and until > 0 and until < now:
        scene = ""
    _SCENE_CACHE[stream_id] = (now, scene, route)
    return route


def invalidate(stream_id: str | None = None) -> None:
    """清掉缓存（sisp 状态刚变、或插件卸载时调用）。"""

    if stream_id is None:
        _CACHE.clear()
        _SCENE_CACHE.clear()
    else:
        _CACHE.pop(stream_id, None)
        _SCENE_CACHE.pop(stream_id, None)


__all__ = [
    "PRIVATE_FIELD",
    "current_route",
    "current_scene",
    "detect",
    "invalidate",
    "private_mode",
    "read_state",
]
