"""肠胃模拟的数值引擎（纯逻辑 + 持久化）。

这里不碰任何提示词、不碰事件订阅，只回答三个问题：
1. 这一轮之后两条数值变成多少；
2. 现在处于第几档；
3. 有没有出事（失禁 / 漏气）、有没有升级。

好处是可以在没有实例的情况下单独跑自检（tests/check_engine.py）。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import storage_api

from . import scene as scene_mod

if TYPE_CHECKING:
    from .config import GutSimConfig

#: 档位：1 无感 / 2 有感 / 3 强烈
TIER_LABEL: dict[int, str] = {1: "无感", 2: "有感", 3: "强烈"}

#: 效果名
EFFECT_BLOAT = "bloat"
EFFECT_DIARRHEA = "diarrhea"
EFFECT_SHAME_BURST = "shame_burst"
EFFECT_RELIEF = "relief"
EFFECT_RELAX = "relax"

#: 负面效果（安心可以整段挡住）
NEGATIVE_EFFECTS = frozenset({EFFECT_BLOAT, EFFECT_DIARRHEA, EFFECT_SHAME_BURST})


@dataclass
class GutState:
    """单个聊天流的肠胃状态。"""

    # 两条数值
    gas: float = 0.0
    stool: float = 0.0

    # 上限奖励（不含成长）
    bonus_gas: float = 0.0
    bonus_stool: float = 0.0
    #: 累计的肠胃弱化（从便意上限里扣，压到 cap_min 为止）
    weak_total: float = 0.0

    # 憋忍经验
    level: int = 0
    exp: int = 0

    # 羞耻
    shame: float = 0.0
    shame_level: int = 0
    shame_exp: int = 0

    # 人设推断值（配置没覆盖时用）
    gas_ratio: float = 2.5
    base_rate: float = 1.0
    #: 易羞系数：易羞 1.3 / 不易羞 0.7，直接乘在羞耻增量上
    shy_mult: float = 1.0

    # 统计
    rounds: int = 0
    wet_count: int = 0
    leak_count: int = 0
    last_at: float = 0.0

    # 如厕（离线行为）
    #: 正在洗手间的结束时间戳；0 表示没在洗手间
    toilet_until: float = 0.0
    #: 这一趟什么时候去的（给"去了多久"用）
    toilet_leave_at: float = 0.0
    #: 累计去过几次
    toilet_count: int = 0
    #: 最近一次被消息撞见时拦了多少秒（给注入层描述"回来得急"用）
    last_hold_seconds: float = 0.0

    # 效果剩余轮数 + 事件冷却（事件名 -> 到期轮号）
    effects: dict[str, int] = field(default_factory=dict)
    cooldowns: dict[str, int] = field(default_factory=dict)

    #: 当前生效的事件卡（scene.ActiveCard 的字典形式；空 dict 表示没有）
    scene_card: dict[str, Any] = field(default_factory=dict)

    #: 最近一次生效的场景词（床上 / 沙发上……），失禁描述落在哪里
    last_place: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GutState":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if not isinstance(known.get("effects"), dict):
            known["effects"] = {}
        if not isinstance(known.get("cooldowns"), dict):
            known["cooldowns"] = {}
        if not isinstance(known.get("scene_card"), dict):
            known["scene_card"] = {}
        return cls(**known)


# ── 纯函数：档位 / 上限 / 容差 ────────────────────────────────────────────


def tier_of(value: float, tier_1: float, tier_2: float) -> int:
    """把一条数值换算成档位（绝对点，与上限无关）。"""

    if value <= tier_1:
        return 1
    if value <= tier_2:
        return 2
    return 3


def cap_of(state: GutState, config: "GutSimConfig") -> tuple[float, float]:
    """返回 (屁意上限, 便意上限)。成长上限 + 奖励加成。"""

    grown = min(
        float(config.numeric.cap_max),
        float(config.numeric.cap_init) + state.level * float(config.grow.cap_per_level),
    )
    # 事件卡可以临时改变上限（憋气时更耐憋、泻药时更脆）
    try:
        _gas_m, _stool_m, cap_mul = scene_mod.multipliers(state)
    except Exception:  # noqa: BLE001 - 卡坏了不影响数值
        cap_mul = 1.0
    grown *= max(0.1, float(cap_mul))

    floor = float(config.numeric.cap_min)
    gas_cap = max(floor, grown + state.bonus_gas)
    stool_cap = max(floor, grown + state.bonus_stool - state.weak_total)
    return gas_cap, stool_cap


def overflow_budget(state: GutState, config: "GutSimConfig") -> float:
    """羞耻越低，越能多撑几点才失禁。"""

    shame_cfg = config.shame
    if not shame_cfg.enabled:
        return float(shame_cfg.overflow_low)
    if state.shame <= shame_cfg.band_low:
        return float(shame_cfg.overflow_low)
    if state.shame <= shame_cfg.band_high:
        return float(shame_cfg.overflow_mid)
    return float(shame_cfg.overflow_high)


def effect_multiplier(state: GutState, name: str, multiplier: float) -> float:
    """效果生效时返回倍率，否则 1.0。"""

    return multiplier if state.effects.get(name, 0) > 0 else 1.0


def _has_negative(state: GutState) -> bool:
    return any(state.effects.get(name, 0) > 0 for name in NEGATIVE_EFFECTS)


def apply_effect(state: GutState, name: str, rounds: int) -> None:
    """施加一个效果；安心在场时挡掉负面。"""

    if rounds <= 0:
        return
    if name in NEGATIVE_EFFECTS and state.effects.get(EFFECT_RELIEF, 0) > 0:
        return
    state.effects[name] = max(int(state.effects.get(name, 0)), int(rounds))


def _tick_effects(state: GutState) -> None:
    """回合结束时效果与冷却各减一轮，到期即删。"""

    for name in list(state.effects):
        state.effects[name] = int(state.effects[name]) - 1
        if state.effects[name] <= 0:
            state.effects.pop(name, None)

    for name in list(state.cooldowns):
        state.cooldowns[name] = int(state.cooldowns[name]) - 1
        if state.cooldowns[name] <= 0:
            state.cooldowns.pop(name, None)


def on_cooldown(state: GutState, name: str) -> bool:
    return state.cooldowns.get(name, 0) > 0


def start_cooldown(state: GutState, name: str, rounds: int) -> None:
    if rounds > 0:
        state.cooldowns[name] = int(rounds)


# ── 一轮推进 ──────────────────────────────────────────────────────────────


@dataclass
class TurnReport:
    """一轮结算的结果，供日志与注入使用。"""

    rounds: int = 1
    stool_before: float = 0.0
    stool_after: float = 0.0
    gas_before: float = 0.0
    gas_after: float = 0.0
    tier_stool: int = 1
    tier_gas: int = 1
    exp_gained: int = 0
    leveled_up: bool = False
    shame_gained: float = 0.0
    wetting: bool = False
    leaking: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def tier(self) -> int:
        """取两条里更急的那一条作为整体档位。"""

        return max(self.tier_stool, self.tier_gas)


def advance(
    state: GutState,
    config: "GutSimConfig",
    *,
    rounds: int = 1,
    token_units: float = 0.0,
    private: bool = False,
) -> TurnReport:
    """推进一轮（或多轮），就地修改 state 并返回报告。

    Args:
        state: 当前状态（会被修改）
        config: 插件配置
        rounds: 推进几轮（一般 1）
        token_units: 本轮 token 总量（用户消息 + 回复），0 表示只按轮次
        private: 是否处于私密模式（影响经验与羞耻倍率）

    Returns:
        TurnReport
    """

    report = TurnReport(rounds=rounds)
    report.stool_before = state.stool
    report.gas_before = state.gas

    numeric = config.numeric
    grow = config.grow
    shame_cfg = config.shame

    # 1) 增速 ------------------------------------------------------------
    stool_rate = float(numeric.base_rate)
    gas_rate = float(numeric.base_rate) * float(state.gas_ratio)

    if state.effects.get(EFFECT_DIARRHEA, 0) > 0:
        stool_rate *= float(config.events.diarrhea_multiplier)
    if state.effects.get(EFFECT_BLOAT, 0) > 0:
        gas_rate *= float(config.events.bloat_multiplier)

    # 事件卡倍率（泻药、受凉、憋气……）
    if config.scene.enabled:
        try:
            card_gas, card_stool, _cap = scene_mod.multipliers(state)
        except Exception:  # noqa: BLE001
            card_gas = card_stool = 1.0
        gas_rate *= float(card_gas)
        stool_rate *= float(card_stool)
        place = scene_mod.place_of(state)
        if place:
            state.last_place = place

    if state.effects.get(EFFECT_RELAX, 0) > 0:
        stool_rate = 0.0
        gas_rate = 0.0
        report.notes.append("舒畅生效：本回合不增长")

    # token 加成按便意算，屁意跟着按比例走
    token_bonus = 0.0
    if numeric.token_div > 0 and token_units > 0:
        token_bonus = min(
            float(numeric.token_cap_per_round),
            float(token_units) / float(numeric.token_div),
        )

    if stool_rate <= 0.0 and gas_rate <= 0.0:
        stool_gain = 0.0
        gas_gain = 0.0
    else:
        stool_gain = stool_rate * rounds + token_bonus
        gas_gain = gas_rate * rounds + token_bonus * float(state.gas_ratio)

    state.stool += stool_gain
    state.gas += gas_gain

    # 2) 自然衰减 --------------------------------------------------------
    if numeric.decay_per_hour > 0 and state.last_at > 0:
        elapsed_hours = max(0.0, (time.time() - state.last_at) / 3600.0)
        drop = elapsed_hours * float(numeric.decay_per_hour)
        state.stool = max(0.0, state.stool - drop)
        state.gas = max(0.0, state.gas - drop)

    cap_gas, cap_stool = cap_of(state, config)
    budget = overflow_budget(state, config)

    # 3) 失禁 / 漏气判定 -------------------------------------------------
    if shame_cfg.enabled and shame_cfg.wetting_enabled and state.stool > cap_stool + budget:
        state.stool = 0.0
        state.wet_count += 1
        report.wetting = True
        report.notes.append("便意失禁")
        add_shame(state, config, float(shame_cfg.shame_on_wetting), private=private, report=report)
        start_cooldown(state, "wetting", int(shame_cfg.wet_cooldown_rounds))

    if shame_cfg.enabled and shame_cfg.leak_enabled and state.gas > cap_gas + budget:
        state.gas = 0.0
        state.leak_count += 1
        report.leaking = True
        report.notes.append("漏气")
        add_shame(state, config, float(shame_cfg.shame_on_leak), private=private, report=report)
        start_cooldown(state, "leaking", int(shame_cfg.wet_cooldown_rounds))

    # 数值没有失禁机制时，封顶停住（不会无限溢出）
    state.stool = min(state.stool, cap_stool + (budget if shame_cfg.wetting_enabled else 0.0))

    # 自然排气：到顶后自己漏掉一点，别让屁意永远卡在满值
    if numeric.passive_vent > 0 and state.gas >= cap_gas:
        state.gas = max(0.0, cap_gas - float(numeric.passive_vent))
        report.notes.append("肚里的气自己漏掉了一点")

    state.gas = min(state.gas, cap_gas + (budget if shame_cfg.leak_enabled else 0.0))

    # 4) 档位 ------------------------------------------------------------
    report.tier_stool = tier_of(state.stool, numeric.tier_1, numeric.tier_2)
    report.tier_gas = tier_of(state.gas, numeric.tier_1, numeric.tier_2)
    self_tier = report.tier

    # 5) 憋忍经验 --------------------------------------------------------
    if grow.enabled and self_tier >= 2:
        gained = int(grow.exp_tier3 if self_tier >= 3 else grow.exp_tier2) * rounds
        if shame_cfg.enabled and state.shame > shame_cfg.band_high:
            gained = int(round(gained * float(grow.high_shame_multiplier)))
        state.exp += max(1, gained)
        report.exp_gained = max(1, gained)

        while state.exp >= int(grow.exp_per_level):
            state.exp -= int(grow.exp_per_level)
            state.level += 1
            report.leveled_up = True
            report.notes.append(f"憋忍升级 Lv.{state.level}")

    # 6) 羞耻增长 --------------------------------------------------------
    if shame_cfg.enabled and not report.wetting and not report.leaking:
        per_round = float(
            shame_cfg.per_round_tier3 if self_tier >= 3 else
            (shame_cfg.per_round_tier2 if self_tier >= 2 else 0.0)
        )
        if per_round > 0:
            add_shame(
                state,
                config,
                per_round * rounds,
                private=private,
                report=report,
            )

    # 抗羞耻经验：羞耻够高时每轮攒一点
    if shame_cfg.enabled and state.shame > shame_cfg.resist_exp_band:
        state.shame_exp += rounds
        if state.shame_exp >= int(shame_cfg.resist_exp_per_level):
            state.shame_exp = 0
            if state.shame_level < int(shame_cfg.resist_max):
                state.shame_level += 1
                report.notes.append(f"羞耻抗性 +1（R{state.shame_level}）")

    # 7) 收尾 ------------------------------------------------------------
    state.rounds += rounds
    state.last_at = time.time()
    report.stool_after = state.stool
    report.gas_after = state.gas
    _tick_effects(state)
    return report


def add_shame(
    state: GutState,
    config: "GutSimConfig",
    delta: float,
    *,
    private: bool = False,
    report: TurnReport | None = None,
) -> float:
    """给羞耻值加一点（会先过抗性与爆棚倍率），返回实际增加量。"""

    shame_cfg = config.shame
    if not shame_cfg.enabled or delta <= 0:
        return 0.0

    # 羞耻心爆棚：提升羞耻增长
    if state.effects.get(EFFECT_SHAME_BURST, 0) > 0:
        delta *= float(config.events.shame_burst_multiplier)
    if private:
        delta *= float(shame_cfg.private_multiplier)
    if state.shy_mult > 0:
        delta *= float(state.shy_mult)

    # 抗性抵掉一部分，再四舍五入取整（主人要求打整；这里是半进位，不是银行家舍入）
    effective = max(0.0, float(delta) - float(state.shame_level))
    applied = float(int(effective + 0.5))

    before = state.shame
    state.shame = min(float(shame_cfg.max), state.shame + applied)
    gained = state.shame - before
    if report is not None and gained:
        report.shame_gained += gained
    return gained


def band_of(shame: float, config: "GutSimConfig") -> int:
    """羞耻档位：1 低 / 2 中 / 3 高。"""

    cfg = config.shame
    if shame <= cfg.band_low:
        return 1
    if shame <= cfg.band_high:
        return 2
    return 3


def relieve(
    state: GutState,
    config: "GutSimConfig",
    *,
    private: bool = False,
) -> float:
    """主动排空（如厕）：两条归零，按配置给一点舒畅，私密模式下可选加羞耻。

    Returns:
        这次增加了多少羞耻（没加就是 0）
    """

    state.stool = 0.0
    state.gas = 0.0
    for name in (EFFECT_BLOAT, EFFECT_DIARRHEA, EFFECT_SHAME_BURST):
        state.effects.pop(name, None)

    if int(config.toilet.relax_rounds) > 0:
        apply_effect(state, EFFECT_RELAX, int(config.toilet.relax_rounds))

    return add_shame(
        state,
        config,
        float(config.toilet.shame_private),
        private=private,
    )


# ── 持久化 ────────────────────────────────────────────────────────────────


#: Windows 文件名里不能出现的字符（stream_id 正常是 SHA256，这里只是兜底）
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    """把 stream_id 变成合法文件名；不合法时退化成空串以外的占位。"""

    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "")).strip(" .")
    return cleaned or "stream"


def _key(stream_id: str, config: "GutSimConfig") -> str:
    suffix = str(config.sisp.state_key_suffix or "").strip()
    base = _safe_filename(stream_id)
    return f"{base}{_UNSAFE_FILENAME.sub('_', suffix)}" if suffix else base


async def load_state(stream_id: str, config: "GutSimConfig") -> GutState:
    """读取某个流的状态；没有就返回一份新的（不落盘）。"""

    store = str(config.plugin.store_name or "gut_sim")
    data = await storage_api.load_json(store, _key(stream_id, config))
    if not isinstance(data, dict):
        return GutState(gas_ratio=float(config.numeric.gas_ratio))
    state = GutState.from_dict(data)
    # 人设推断值缺失时回落到配置
    if state.gas_ratio <= 0:
        state.gas_ratio = float(config.numeric.gas_ratio)
    if state.base_rate <= 0:
        state.base_rate = float(config.numeric.base_rate)
    return state


async def save_state(stream_id: str, state: GutState, config: "GutSimConfig") -> None:
    """把状态写回 JSON 存储。"""

    store = str(config.plugin.store_name or "gut_sim")
    await storage_api.save_json(store, _key(stream_id, config), state.to_dict())


def snapshot(state: GutState, config: "GutSimConfig") -> str:
    """一行摘要，给日志与面板用。"""

    cap_gas, cap_stool = cap_of(state, config)
    tier_stool = tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
    tier_gas = tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
    return (
        f"便意 {state.stool:.1f}/{cap_stool:.0f}（{TIER_LABEL[tier_stool]}）"
        f"｜屁意 {state.gas:.1f}/{cap_gas:.0f}（{TIER_LABEL[tier_gas]}）"
        f"｜Lv.{state.level} 经验 {state.exp}"
        f"｜羞耻 {state.shame:.0f}"
    )


__all__ = [
    "EFFECT_BLOAT",
    "EFFECT_DIARRHEA",
    "EFFECT_RELAX",
    "EFFECT_RELIEF",
    "EFFECT_SHAME_BURST",
    "NEGATIVE_EFFECTS",
    "TIER_LABEL",
    "GutState",
    "TurnReport",
    "add_shame",
    "advance",
    "apply_effect",
    "band_of",
    "cap_of",
    "effect_multiplier",
    "load_state",
    "on_cooldown",
    "overflow_budget",
    "relieve",
    "save_state",
    "snapshot",
    "start_cooldown",
    "tier_of",
]
