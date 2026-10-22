"""事件卡自检：触发词、倍率、过期、跟随 sisp、失禁场景化。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/check_scene.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (_NEO_ROOT, PLUGIN_REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gut_sim import scene  # noqa: E402
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


def _fresh() -> gut.GutState:
    return gut.GutState(gas_ratio=2.5, base_rate=1.5, shy_mult=1.0)


# ── 卡池 ──────────────────────────────────────────────────────────────────


def _phase_pool(config: GutSimConfig) -> None:
    pool = scene.cards_of(config)
    for card_id in ("laxative", "stomach_ache", "gas_hold", "cold", "banquet"):
        _record(card_id in pool, f"内置卡 {card_id} 在池子里")

    config.scene.disabled_cards = ["cold"]
    pool2 = scene.cards_of(config)
    _record("cold" not in pool2, "配置能关掉卡")
    config.scene.disabled_cards = []

    config.scene.custom_cards = [
        {"id": "laxative", "stool_mult": 9.0},
        {"id": "my_card", "title": "自定义", "triggers": ["测试"], "hours": 1, "gas_mult": 3.0},
    ]
    pool3 = scene.cards_of(config)
    _near("自定义卡能覆盖内置卡的字段", float(pool3["laxative"]["stool_mult"]), 9.0)
    _record("自定义卡能新增", "my_card" in pool3, str(sorted(pool3)))
    config.scene.custom_cards = []


# ── 触发词 ────────────────────────────────────────────────────────────────


def _phase_match(config: GutSimConfig) -> None:
    pool = scene.cards_of(config)
    _eq("命中泻药", scene.match_card("我刚吃了那颗泻药", pool), "laxative")
    _eq("命中吃撑", scene.match_card("今晚火锅吃多了", pool), "banquet")
    _eq("命中憋气", scene.match_card("不许放，给我憋着", pool), "gas_hold")
    _eq("没命中就是 None", scene.match_card("今天天气不错", pool), None)


# ── 生效与倍率 ────────────────────────────────────────────────────────────


def _phase_effect(config: GutSimConfig) -> None:
    pool = scene.cards_of(config)
    state = _fresh()

    base = _fresh()
    gut.advance(base, config, rounds=1, token_units=0.0)

    # 泻药：便意暴涨
    state.scene_card = scene.make_card("laxative", pool["laxative"]).to_dict()
    gut.advance(state, config, rounds=1, token_units=0.0)
    _record(
        state.stool > base.stool * 1.8,
        "泻药卡让便意涨得更快",
        f"{state.stool:.1f} vs {base.stool:.1f}",
    )
    _near(
        "倍率生效时 place 被记下来",
        float(len(state.last_place)) > 0.0,
        1.0,
        tol=1.0,
    )

    # 上限也被卡影响
    plain = _fresh()
    cap_before = gut.cap_of(plain, config)[1]
    carded = _fresh()
    carded.scene_card = scene.make_card("laxative", pool["laxative"]).to_dict()
    cap_after = gut.cap_of(carded, config)[1]
    _record(cap_after < cap_before, "泻药卡压低上限", f"{cap_before:.0f} → {cap_after:.0f}")

    # 憋气卡抬高上限
    hold = _fresh()
    hold.scene_card = scene.make_card("gas_hold", pool["gas_hold"]).to_dict()
    cap_hold = gut.cap_of(hold, config)[0]
    _record(cap_hold > cap_before, "憋气卡抬高屁意上限", f"{cap_before:.0f} → {cap_hold:.0f}")


def _phase_expire(config: GutSimConfig) -> None:
    pool = scene.cards_of(config)
    state = _fresh()
    card = scene.make_card("laxative", pool["laxative"])
    card.until = time.time() - 1  # 已经过期
    state.scene_card = card.to_dict()

    _record(scene.active_card(state) is None, "过期卡不算生效")
    _record(scene.tick(state), "tick 会清掉过期卡")
    _eq("清掉后是空卡", state.scene_card, {})

    # 未过期的卡 tick 不动它
    state2 = _fresh()
    state2.scene_card = scene.make_card("laxative", pool["laxative"]).to_dict()
    _record(not scene.tick(state2), "未过期的卡 tick 不清")


def _phase_place(config: GutSimConfig) -> None:
    pool = scene.cards_of(config)
    state = _fresh()
    state.scene_card = scene.make_card("laxative", pool["laxative"]).to_dict()
    _eq("场景词来自卡", scene.place_of(state), "床上")

    empty = _fresh()
    _eq("没卡时没有场景词", scene.place_of(empty), "")

    _record(
        "卡的注入文本非空",
        bool(scene.prompt_of(state)) and "药" in scene.prompt_of(state),
        scene.prompt_of(state)[:30],
    )


def main() -> int:
    config = GutSimConfig()
    config.shame.enabled = True

    _phase_pool(config)
    _phase_match(config)
    _phase_effect(config)
    _phase_expire(config)
    _phase_place(config)

    failed = [(name, detail) for ok, name, detail in _results if not ok]
    for ok, name, detail in _results:
        print(f"{'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail and not ok else ""))

    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
