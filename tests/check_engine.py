"""肠胃数值引擎的离线自检（不调 LLM、不碰实例运行时）。

只验证纯逻辑：档位、上限成长、token 加成、失禁、羞耻抗性、效果倍率。

用法（实例 venv）::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/check_engine.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (_NEO_ROOT, PLUGIN_REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gut_sim import state as gut  # noqa: E402
from gut_sim.config import GutSimConfig  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def _record(ok: bool, name: str, detail: str = "") -> None:
    _results.append((ok, name, detail))


def _eq(name: str, got: object, want: object) -> None:
    _record(got == want, name, f"{got!r}" + ("" if got == want else f"（期望 {want!r}）"))


def _near(name: str, got: float, want: float, tol: float = 1e-6) -> None:
    ok = abs(float(got) - float(want)) <= tol
    _record(ok, name, f"{got!r}" + ("" if ok else f"（期望 {want!r}）"))


def _fresh(config: GutSimConfig) -> gut.GutState:
    return gut.GutState(
        gas_ratio=float(config.numeric.gas_ratio),
        base_rate=float(config.numeric.base_rate),
        shy_mult=1.0,
    )


# ── A. 档位 ───────────────────────────────────────────────────────────────


def _phase_tier(config: GutSimConfig) -> None:
    t1, t2 = config.numeric.tier_1, config.numeric.tier_2
    _eq("0 → 一档", gut.tier_of(0.0, t1, t2), 1)
    _eq("60 → 一档（边界含）", gut.tier_of(60.0, t1, t2), 1)
    _eq("61 → 二档", gut.tier_of(61.0, t1, t2), 2)
    _eq("90 → 二档（边界含）", gut.tier_of(90.0, t1, t2), 2)
    _eq("91 → 三档", gut.tier_of(91.0, t1, t2), 3)
    _eq("300 → 三档（与上限无关）", gut.tier_of(300.0, t1, t2), 3)


# ── B. 增速与 token ───────────────────────────────────────────────────────


def _phase_rate(config: GutSimConfig) -> None:
    state = _fresh(config)
    gut.advance(state, config, rounds=1, token_units=0.0)
    _near("一轮后便意 = base_rate", state.stool, config.numeric.base_rate)
    _near(
        "一轮后屁意 = base_rate × gas_ratio",
        state.gas,
        config.numeric.base_rate * config.numeric.gas_ratio,
    )
    _record(
        state.gas > state.stool,
        "屁意跑得比便意快",
        f"gas={state.gas:.2f} stool={state.stool:.2f}",
    )

    state2 = _fresh(config)
    gut.advance(state2, config, rounds=1, token_units=400.0)
    _near(
        "400 token 折合 +1 便意",
        state2.stool,
        config.numeric.base_rate + 400.0 / config.numeric.token_div,
    )

    state3 = _fresh(config)
    gut.advance(state3, config, rounds=1, token_units=100000.0)
    _record(
        state3.stool <= config.numeric.token_cap_per_round + config.numeric.base_rate + 1e-6,
        "单轮 token 加成有上限（防长文推爆）",
        f"stool={state3.stool:.2f}",
    )


# ── C. 上限成长 ───────────────────────────────────────────────────────────


def _phase_grow(config: GutSimConfig) -> None:
    state = _fresh(config)
    cap_gas, cap_stool = gut.cap_of(state, config)
    _near("初始屁意上限 100", cap_gas, config.numeric.cap_init)
    _near("初始便意上限 100", cap_stool, config.numeric.cap_init)

    state.exp = int(config.grow.exp_per_level)
    state.stool = 85.0  # 三档，触发经验
    gut.advance(state, config, rounds=1, token_units=0.0)
    _eq("经验够 20 就升级", state.level, 1)
    cap_gas2, cap_stool2 = gut.cap_of(state, config)
    _near("升级后屁意上限 +10（与便意同步）", cap_gas2, config.numeric.cap_init + 10)
    _near("升级后便意上限 +10", cap_stool2, config.numeric.cap_init + 10)

    state2 = _fresh(config)
    state2.level = 30
    cap_gas3, cap_stool3 = gut.cap_of(state2, config)
    _near("成长上限封顶 300", cap_gas3, config.numeric.cap_max)
    _near("成长上限封顶 300（便意）", cap_stool3, config.numeric.cap_max)

    # 奖励加成：300 + 20 = 320
    state3 = _fresh(config)
    state3.level = 30
    state3.bonus_stool = 20.0
    _cap_gas3, cap_stool4 = gut.cap_of(state3, config)
    _near("300 + 强化奖励 20 = 便意理论极限 320", cap_stool4, 320.0)


# ── D. 溢出与失禁 ─────────────────────────────────────────────────────────


def _phase_overflow(config: GutSimConfig) -> None:
    config.shame.enabled = True
    config.shame.wetting_enabled = True

    state = _fresh(config)
    _near("羞耻 0 时溢出容差 10", gut.overflow_budget(state, config), 10.0)

    state.shame = 45.0
    _near("羞耻中档容差 6", gut.overflow_budget(state, config), 6.0)

    state.shame = 80.0
    _near("羞耻高档容差 2", gut.overflow_budget(state, config), 2.0)

    # 越线失禁：便意归零、羞耻增加
    state = _fresh(config)
    state.stool = 111.0  # 100 上限 + 10 容差 = 110，再加一点点才越线
    before_shame = state.shame
    report = gut.advance(state, config, rounds=1, token_units=0.0)
    _record(report.wetting, "便意越过失禁线 → 失禁", f"report={report.notes}")
    _near("失禁后便意归零", state.stool, 0.0)
    _record(state.shame > before_shame, "失禁会加羞耻", f"{before_shame} → {state.shame}")
    _eq("失禁计数 +1", state.wet_count, 1)

    # 关掉失禁后不再出事，只封顶
    config.shame.wetting_enabled = False
    state2 = _fresh(config)
    state2.stool = 500.0
    report2 = gut.advance(state2, config, rounds=1, token_units=0.0)
    _record(not report2.wetting, "失禁关闭时不触发", "")
    _record(state2.stool <= 100.0 + 1e-6, "失禁关闭时封顶停住", f"stool={state2.stool}")
    config.shame.wetting_enabled = True


# ── E. 羞耻抗性 ───────────────────────────────────────────────────────────


def _phase_shame(config: GutSimConfig) -> None:
    config.shame.enabled = True
    config.shame.per_round_tier3 = 2.0

    state = _fresh(config)
    state.stool = 150.0  # 三档
    gained_raw = gut.add_shame(state, config, 10.0)
    _near("无抗性时羞耻按原值加", gained_raw, 10.0)

    state2 = _fresh(config)
    state2.stool = 150.0
    state2.shame_level = 3
    gained_resist = gut.add_shame(state2, config, 10.0)
    _near("抗性 3 级时扣掉 3 点", gained_resist, 7.0)

    # 四舍五入取整
    state3 = _fresh(config)
    state3.shame_level = 2
    gained_round = gut.add_shame(state3, config, 6.5)
    _near("小数增量四舍五入（6.5-2=4.5→5）", gained_round, 5.0)


# ── F. 效果倍率 ───────────────────────────────────────────────────────────


def _phase_effects(config: GutSimConfig) -> None:
    config.events.bloat_multiplier = 2.0

    base = _fresh(config)
    gut.advance(base, config, rounds=1, token_units=0.0)

    bloat = _fresh(config)
    bloat.effects[gut.EFFECT_BLOAT] = 3
    gut.advance(bloat, config, rounds=1, token_units=0.0)
    _record(
        bloat.gas > base.gas * 1.5,
        "胀气时屁意涨得更快",
        f"{bloat.gas:.2f} vs {base.gas:.2f}",
    )

    relax = _fresh(config)
    relax.effects[gut.EFFECT_RELAX] = 3
    gut.advance(relax, config, rounds=1, token_units=0.0)
    _near("舒畅时不增长", relax.stool, 0.0)

    relief = _fresh(config)
    relief.effects[gut.EFFECT_RELIEF] = 3
    gut.apply_effect(relief, gut.EFFECT_BLOAT, 3)
    _record(
        gut.EFFECT_BLOAT not in relief.effects,
        "安心在场时负面被挡掉",
        f"effects={relief.effects}",
    )


# ── G. 自然排气与如厕 ─────────────────────────────────────────────────────


def _phase_vent_and_toilet(config: GutSimConfig) -> None:
    # 默认关闭：到顶就停住
    config.numeric.passive_vent = 0.0
    state = _fresh(config)
    state.gas = 100.0
    gut.advance(state, config, rounds=1, token_units=0.0)
    _near("自然排气关闭时屁意封顶不动", state.gas, 100.0)

    # 打开：到顶后自己漏一点
    config.numeric.passive_vent = 4.0
    state2 = _fresh(config)
    state2.gas = 100.0
    report = gut.advance(state2, config, rounds=1, token_units=0.0)
    _record(state2.gas < 100.0, "自然排气开启后屁意回落", f"gas={state2.gas:.2f}")
    _record(
        any("漏掉" in note for note in report.notes),
        "排气会记进报告",
        str(report.notes),
    )
    config.numeric.passive_vent = 0.0

    # 如厕：两条归零 + 舒畅
    config.toilet.relax_rounds = 2
    state3 = _fresh(config)
    state3.stool = 80.0
    state3.gas = 90.0
    state3.effects[gut.EFFECT_DIARRHEA] = 3
    gut.relieve(state3, config)
    _near("如厕后便意归零", state3.stool, 0.0)
    _near("如厕后屁意归零", state3.gas, 0.0)
    _record(
        gut.EFFECT_RELAX in state3.effects,
        "如厕后获得舒畅",
        str(state3.effects),
    )
    _record(
        gut.EFFECT_DIARRHEA not in state3.effects,
        "如厕顺带清掉闹肚子",
        str(state3.effects),
    )

    # 私密模式下如厕加羞耻（配置为 0 时不加）
    config.toilet.shame_private = 0.0
    state4 = _fresh(config)
    gained = gut.relieve(state4, config, private=True)
    _near("默认如厕不加羞耻", gained, 0.0)

    config.toilet.shame_private = 8.0
    state5 = _fresh(config)
    gained2 = gut.relieve(state5, config, private=True)
    _record(gained2 > 0, "配置了就会加", f"+{gained2:.1f}")
    config.toilet.shame_private = 0.0


def main() -> int:
    config = GutSimConfig()
    config.shame.enabled = True

    _phase_tier(config)
    _phase_rate(config)
    _phase_grow(config)
    _phase_overflow(config)
    _phase_shame(config)
    _phase_effects(config)
    _phase_vent_and_toilet(config)

    failed = [(name, detail) for ok, name, detail in _results if not ok]
    for ok, name, detail in _results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f"  [{detail}]" if detail and not ok else ""))

    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
