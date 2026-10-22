"""如厕：离线行为 + 被消息撞见时的"拦一下"。

机制照搬上下文归档的巡检思路：

1. **安静够久 → 溜去洗手间**：后台每 N 秒看一眼各流，本流安静超过
   ``idle_seconds`` 就按概率把她送去洗手间，待 ``stay_min~max`` 秒。
2. **来消息时拦一下**：如果她正"在洗手间"，这条消息会被压住 3~4 秒再处理，
   模拟"她伸手拦了一下、让你等一下"。
3. **回来后如实交代**：清空两条数值，并且让角色知道"自己刚去过洗手间"
   （知道不等于一定会说；被问起就如实说）。

作用域（主人要求）：
- 默认只认私聊；私聊再按白名单筛（``private_whitelist_only``）
- 群聊默认不触发；真开了也用隐晦说法（``group_direct = false``）
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

from src.app.plugin_system.api import stream_api
from src.app.plugin_system.api.log_api import get_logger

from . import state as gut

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

#: 记录"刚去过洗手间"的效果名（注入层用，存活 1 轮）
EFFECT_JUST_TOILET = "just_toilet"


# ── 作用域 ────────────────────────────────────────────────────────────────


def in_scope(config: "GutSimConfig", *, chat_type: str, sender_id: str) -> bool:
    """这条流/这个人，能不能撞见她去洗手间。"""

    toilet = config.toilet
    if not toilet.auto_enabled:
        return False

    if chat_type == "group":
        return bool(toilet.scope_group)

    if not toilet.scope_private:
        return False

    if toilet.private_whitelist_only:
        allowed = {str(item).strip() for item in (toilet.whitelist or []) if str(item).strip()}
        return bool(sender_id) and sender_id in allowed

    return True


def is_away(state: "gut.GutState", now: float | None = None) -> bool:
    """她现在是不是还在洗手间。"""

    moment = time.time() if now is None else now
    return state.toilet_until > moment


# ── 巡检：决定要不要去 ───────────────────────────────────────────────────


async def tick(config: "GutSimConfig") -> int:
    """跑一次巡检，返回这一轮"送进洗手间"的人数。

    只处理已经安静够久、且当前不在洗手间的流。
    """

    toilet = config.toilet
    if not toilet.auto_enabled:
        return 0

    try:
        stream_ids = stream_api.get_all_stream_ids()
    except Exception as exc:  # noqa: BLE001 - 拿不到流列表就跳过这轮
        logger.debug(f"gut_sim: 取流列表失败，跳过如厕巡检：{exc!r}")
        return 0

    now = time.time()
    started = 0

    for stream_id in stream_ids:
        try:
            state = await gut.load_state(stream_id, config)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: 巡检读状态失败（{stream_id[:8]}）：{exc!r}")
            continue

        # 还在洗手间 / 从没聊过 → 跳过
        if state.toilet_until > 0 or state.last_at <= 0:
            continue

        idle = now - state.last_at
        if idle < float(toilet.idle_seconds):
            continue

        if random.random() > float(toilet.chance):
            continue

        state.toilet_until = now + random.uniform(
            float(toilet.stay_min_seconds), float(toilet.stay_max_seconds)
        )
        state.toilet_leave_at = now
        await gut.save_state(stream_id, state, config)
        started += 1
        logger.info(
            f"gut_sim: 安静 {int(idle)}s，去洗手间了"
            f"（{int(state.toilet_until - now)}s 后回来，stream={stream_id[:8]}）"
        )

    return started


# ── 被消息撞见 ────────────────────────────────────────────────────────────


def hold_seconds(
    state: "gut.GutState",
    config: "GutSimConfig",
    *,
    chat_type: str,
    sender_id: str,
) -> float:
    """这条消息要不要被"拦一下"，返回要等几秒（0 = 不拦）。

    注意：本函数会改 ``state.toilet_until``（撞见即视为回来），调用方负责保存。
    """

    toilet = config.toilet
    if not is_away(state):
        return 0.0
    if not in_scope(config, chat_type=chat_type, sender_id=sender_id):
        # 不在作用域里的人：她照常安静地回来，只是不需要被拦
        state.toilet_until = 0.0
        return 0.0

    # 刚好出来的情况：不拦
    if random.random() > float(toilet.hold_chance):
        state.toilet_until = 0.0
        return 0.0

    hold = random.uniform(float(toilet.hold_min_seconds), float(toilet.hold_max_seconds))
    state.toilet_until = 0.0
    return max(0.0, hold)


def finish(
    state: "gut.GutState",
    config: "GutSimConfig",
    *,
    private: bool,
) -> float:
    """回来了：结算这一趟（清空数值、给"刚去过"提示），返回羞耻增量。"""

    toilet = config.toilet
    state.toilet_until = 0.0
    state.toilet_count += 1

    gained = 0.0
    if toilet.clear_on_return:
        gained = gut.relieve(state, config, private=private)

    if toilet.tell_after_return:
        state.effects[EFFECT_JUST_TOILET] = 1

    return gained


def describe_away(
    config: "GutSimConfig",
    *,
    chat_type: str,
    seconds: float,
) -> str:
    """她被问起"刚才去哪了"时该怎么说。"""

    toilet = config.toilet
    in_group = chat_type == "group"
    direct = (not in_group) or bool(toilet.group_direct)
    waited = max(1, int(round(seconds)))

    if direct:
        return (
            f"你刚才去了洗手间（大约 {waited} 秒），回来得有点急。"
            "如果对方问起，就如实说是去上厕所了，不用编理由——"
            "可以有点不好意思，但别把它说成什么大不了的事。"
        )
    return (
        f"你刚才离开了视线一会儿（大约 {waited} 秒），回来得有点急。"
        "如果对方问起，说「去了一下洗手间」「刚不在」这类话就够了，"
        "别展开细节，也别主动提。"
    )


__all__ = [
    "EFFECT_JUST_TOILET",
    "describe_away",
    "finish",
    "hold_seconds",
    "in_scope",
    "is_away",
    "tick",
]
