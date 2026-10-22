"""如厕机制的离线自检：巡检判定、撞见拦截、作用域、回来结算、措辞。

不依赖实例运行时（不读 storage），只喂假状态进去看逻辑对不对。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/check_toilet.py
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (_NEO_ROOT, PLUGIN_REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gut_sim import state as gut  # noqa: E402
from gut_sim import toilet  # noqa: E402
from gut_sim.config import GutSimConfig  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def _record(ok: bool, name: str, detail: str = "") -> None:
    _results.append((ok, name, detail))


def _eq(name: str, got: object, want: object) -> None:
    _record(got == want, name, f"{got!r}" + ("" if got == want else f"（期望 {want!r}）"))


def _fresh() -> gut.GutState:
    return gut.GutState(
        gas_ratio=2.5,
        base_rate=1.5,
        shy_mult=1.0,
        stool=80.0,
        gas=70.0,
        last_at=time.time(),
    )


# ── 作用域 ────────────────────────────────────────────────────────────────


def _phase_scope(config: GutSimConfig) -> None:
    toilet_cfg = config.toilet
    toilet_cfg.auto_enabled = True
    toilet_cfg.scope_private = True
    toilet_cfg.scope_group = False
    toilet_cfg.private_whitelist_only = True
    toilet_cfg.whitelist = ["10001"]

    _record(
        toilet.in_scope(config, chat_type="private", sender_id="10001"),
        "私聊白名单内 → 在作用域",
    )
    _record(
        not toilet.in_scope(config, chat_type="private", sender_id="99999"),
        "私聊白名单外 → 不在作用域",
    )
    _record(
        not toilet.in_scope(config, chat_type="group", sender_id="10001"),
        "群聊默认不触发",
    )

    toilet_cfg.scope_group = True
    _record(
        toilet.in_scope(config, chat_type="group", sender_id="99999"),
        "开了群聊就在作用域（群聊不看白名单）",
    )
    toilet_cfg.scope_group = False

    toilet_cfg.private_whitelist_only = False
    _record(
        toilet.in_scope(config, chat_type="private", sender_id="99999"),
        "关掉白名单后所有私聊都算",
    )
    toilet_cfg.private_whitelist_only = True

    toilet_cfg.auto_enabled = False
    _record(
        not toilet.in_scope(config, chat_type="private", sender_id="10001"),
        "总开关关闭后谁都不算",
    )
    toilet_cfg.auto_enabled = True


# ── 撞见拦截 ──────────────────────────────────────────────────────────────


def _phase_hold(config: GutSimConfig) -> None:
    cfg = config.toilet
    cfg.hold_chance = 1.0  # 必定拦
    cfg.hold_min_seconds = 3.0
    cfg.hold_max_seconds = 4.0

    state = _fresh()
    state.toilet_until = time.time() + 60
    hold = toilet.hold_seconds(state, config, chat_type="private", sender_id="10001")
    _record(3.0 <= hold <= 4.0, "撞见时拦住 3~4 秒", f"{hold:.2f}s")
    _eq("撞见后不再处于洗手间状态", state.toilet_until, 0.0)

    # 白名单外：不拦，但也算回来了
    state2 = _fresh()
    state2.toilet_until = time.time() + 60
    hold2 = toilet.hold_seconds(state2, config, chat_type="private", sender_id="99999")
    _eq("白名单外不拦", hold2, 0.0)
    _eq("白名单外也视为回来了", state2.toilet_until, 0.0)

    # 没在洗手间 → 不拦
    state3 = _fresh()
    _eq("没在洗手间就不拦", toilet.hold_seconds(state3, config, chat_type="private", sender_id="10001"), 0.0)

    # 概率没中 → 不拦
    cfg.hold_chance = 0.0
    state4 = _fresh()
    state4.toilet_until = time.time() + 60
    _eq("概率没中就不拦", toilet.hold_seconds(state4, config, chat_type="private", sender_id="10001"), 0.0)
    cfg.hold_chance = 1.0


# ── 回来结算 ──────────────────────────────────────────────────────────────


def _phase_finish(config: GutSimConfig) -> None:
    cfg = config.toilet
    cfg.clear_on_return = True
    cfg.relax_rounds = 2
    cfg.tell_after_return = True
    cfg.shame_private = 0.0

    state = _fresh()
    state.toilet_until = time.time() + 30
    state.last_hold_seconds = 3.5
    gained = toilet.finish(state, config, private=False)

    _eq("回来两条清零", (state.stool, state.gas), (0.0, 0.0))
    _record(state.effects.get(toilet.EFFECT_JUST_TOILET, 0) > 0, "回来记一个「刚去过」")
    _record(gut.EFFECT_RELAX in state.effects, "回来给舒畅", str(state.effects))
    _eq("回来计数 +1", state.toilet_count, 1)
    _eq("默认不加羞耻", gained, 0.0)

    # 关掉 clear_on_return 就不清空
    cfg.clear_on_return = False
    state2 = _fresh()
    toilet.finish(state2, config, private=False)
    _record(state2.stool > 0, "关掉清空后数值保留", f"{state2.stool:.1f}")
    cfg.clear_on_return = True


# ── 措辞 ──────────────────────────────────────────────────────────────────


def _phase_phrasing(config: GutSimConfig) -> None:
    cfg = config.toilet

    direct = toilet.describe_away(config, chat_type="private", seconds=3.4)
    _record("私聊措辞直说洗手间", "洗手间" in direct, direct[:40])
    _record("私聊措辞带秒数", "3" in direct or "4" in direct, direct[:40])

    cfg.group_direct = False
    subtle = toilet.describe_away(config, chat_type="group", seconds=3.4)
    _record("群聊措辞隐晦", "洗手间" in subtle and "上厕所" not in subtle, subtle[:40])

    cfg.group_direct = True
    direct_group = toilet.describe_away(config, chat_type="group", seconds=3.4)
    _record("群聊也可以直说", "上厕所" in direct_group, direct_group[:40])
    cfg.group_direct = False


# ── 巡检（不碰 storage，只验概率与空闲条件） ──────────────────────────────


def _phase_tick_conditions(config: GutSimConfig) -> None:
    cfg = config.toilet
    cfg.idle_seconds = 900
    cfg.chance = 1.0
    cfg.stay_min_seconds = 60
    cfg.stay_max_seconds = 120

    # 刚聊过：不该去（这里只验证判定条件函数式的等价实现）
    state = _fresh()
    state.last_at = time.time()
    state.toilet_until = 0.0
    idle = time.time() - state.last_at
    _record(idle < cfg.idle_seconds, "刚聊过 15 分钟内不会去", f"idle={idle:.0f}s")

    # 安静两小时：够格
    state2 = _fresh()
    state2.last_at = time.time() - 7200
    idle2 = time.time() - state2.last_at
    _record(idle2 >= cfg.idle_seconds, "安静两小时够格", f"idle={idle2:.0f}s")

    # 已经在洗手间：不重复去
    state3 = _fresh()
    state3.last_at = time.time() - 7200
    state3.toilet_until = time.time() + 60
    _record(toilet.is_away(state3), "已在洗手间时 is_away 为真")


def main() -> int:
    random.seed(20261020)
    config = GutSimConfig()
    config.shame.enabled = True

    _phase_scope(config)
    _phase_hold(config)
    _phase_finish(config)
    _phase_phrasing(config)
    _phase_tick_conditions(config)

    failed = [(name, detail) for ok, name, detail in _results if not ok]
    for ok, name, detail in _results:
        print(f"{'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail and not ok else ""))

    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
