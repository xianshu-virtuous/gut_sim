"""事件卡：和 sisp 同构的一套「场景卡」。

结构刻意对齐 sisp 的 ``scene_cards.json``（``id / title / triggers / hours / multiplier / prompt``），
这样两边可以是同一件事，而不是各算各的：

- **我们自己触发**：聊天里命中触发词（"泻药""吃多了"）→ 卡生效 N 小时。
- **跟着 sisp 走**：sisp 当前生效的场景标签（``state.scene``）如果和我们的卡 id 同名
  （比如 ``laxative``），就直接把我们这张卡也点亮——它管它的进度，我们管我们的两条数值，
  但描述的是同一件事。
- **失禁场景化**：卡里带场景词，失禁时描述落在这个场景里（床上 / 沙发上 / 路上）。

卡片的 ``gas_mult`` / ``stool_mult`` 直接乘在增速上；``cap_mult`` 影响上限（憋气时更耐憋）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import GutSimConfig

#: 默认内置卡：id -> 定义
DEFAULT_CARDS: dict[str, dict[str, Any]] = {
    "laxative": {
        "title": "泻药",
        "triggers": ["泻药", "吃了药", "那颗药", "药效", "开塞露"],
        "hours": 4.0,
        "gas_mult": 1.3,
        "stool_mult": 2.5,
        "cap_mult": 0.85,
        "place": "床上",
        "prompt": "药劲还没过去：肠子里一阵一阵地绞，肚子里翻江倒海，随时可能忍不住。",
    },
    "stomach_ache": {
        "title": "闹肚子",
        "triggers": ["肚子疼", "闹肚子", "腹泻", "拉肚子", "肚子不舒服"],
        "hours": 3.0,
        "gas_mult": 1.5,
        "stool_mult": 1.6,
        "cap_mult": 0.9,
        "place": "沙发上",
        "prompt": "肠胃今天不太安分：肚子里一直咕噜咕噜响，稍微一动就有感觉往下面走。",
    },
    "gas_hold": {
        "title": "憋着气",
        "triggers": ["憋着", "憋住", "不许放", "忍着别放"],
        "hours": 2.0,
        "gas_mult": 2.0,
        "stool_mult": 1.0,
        "cap_mult": 1.2,
        "place": "被窝里",
        "prompt": "被勒令憋着：气在肠子里鼓成一团，越想忍越明显，连呼吸都得收着。",
    },
    "cold": {
        "title": "受凉",
        "triggers": ["着凉", "吹风", "喝了冰", "冰可乐", "冰水"],
        "hours": 3.0,
        "gas_mult": 1.4,
        "stool_mult": 1.8,
        "cap_mult": 0.95,
        "place": "出租车后座",
        "prompt": "被凉着的那一下还没过去：肚子隐隐发紧，一阵一阵地往下面坠。",
    },
    "banquet": {
        "title": "吃撑了",
        "triggers": ["吃多了", "吃撑", "火锅", "自助", "夜宵"],
        "hours": 3.0,
        "gas_mult": 1.8,
        "stool_mult": 1.4,
        "cap_mult": 1.0,
        "place": "餐桌旁",
        "prompt": "吃得太满了：胃和肠子都胀着，坐直都觉得顶，气在里头打转。",
    },
}


@dataclass
class ActiveCard:
    """一张正在生效的卡。"""

    id: str
    title: str = ""
    until: float = 0.0
    gas_mult: float = 1.0
    stool_mult: float = 1.0
    cap_mult: float = 1.0
    place: str = ""
    prompt: str = ""
    #: 来源：self（我们自己的触发词）/ sisp（跟着 sisp 的场景）
    source: str = "self"
    #: 列表形式存卡（便于 JSON 落盘）
    triggers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveCard":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if not isinstance(known.get("triggers"), list):
            known["triggers"] = []
        return cls(**known)

    def active(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return self.until > moment


# ── 卡池 ──────────────────────────────────────────────────────────────────


def cards_of(config: "GutSimConfig") -> dict[str, dict[str, Any]]:
    """取当前可用的卡池：内置卡 + 配置里覆盖的卡。"""

    pool: dict[str, dict[str, Any]] = {
        key: dict(value) for key, value in DEFAULT_CARDS.items()
    }

    # 配置里关掉的卡
    for key in list(config.scene.disabled_cards or []):
        pool.pop(str(key).strip(), None)

    # 配置里覆盖/新增的卡
    for item in config.scene.custom_cards or []:
        if not isinstance(item, dict):
            continue
        card_id = str(item.get("id") or "").strip()
        if not card_id:
            continue
        merged = dict(pool.get(card_id) or {})
        merged.update({k: v for k, v in item.items() if k != "id"})
        pool[card_id] = merged

    return pool


def make_card(card_id: str, data: dict[str, Any], *, hours: float | None = None) -> ActiveCard:
    """从卡定义造一张生效中的卡。"""

    duration = float(hours if hours is not None else data.get("hours") or 0.0)
    return ActiveCard(
        id=card_id,
        title=str(data.get("title") or card_id),
        until=time.time() + max(0.0, duration) * 3600.0,
        gas_mult=float(data.get("gas_mult") or 1.0),
        stool_mult=float(data.get("stool_mult") or 1.0),
        cap_mult=float(data.get("cap_mult") or 1.0),
        place=str(data.get("place") or ""),
        prompt=str(data.get("prompt") or ""),
        triggers=[str(t) for t in (data.get("triggers") or [])],
        source="self",
    )


def match_card(text: str, pool: dict[str, dict[str, Any]]) -> str | None:
    """在文本里找触发词，返回命中的卡 id。"""

    if not text or not pool:
        return None
    for card_id, data in pool.items():
        for trigger in data.get("triggers") or []:
            word = str(trigger or "").strip()
            if word and word in text:
                return card_id
    return None


# ── 状态里那张"当前卡" ─────────────────────────────────────────────────────


def current_card(state: Any) -> dict[str, Any] | None:
    """读状态里存的当前卡（state.scene_card）。"""

    raw = getattr(state, "scene_card", None)
    if isinstance(raw, dict) and raw:
        return raw
    return None


def active_card(state: Any, now: float | None = None) -> ActiveCard | None:
    """解析成 ActiveCard；过期返回 None。"""

    raw = current_card(state)
    if not raw:
        return None
    card = ActiveCard.from_dict(raw)
    return card if card.active(now) else None


def set_card(state: Any, card: ActiveCard) -> None:
    state.scene_card = card.to_dict()


def clear_card(state: Any) -> None:
    state.scene_card = {}


def multipliers(state: Any, now: float | None = None) -> tuple[float, float, float]:
    """当前卡带来的 (屁意倍率, 便意倍率, 上限倍率)。"""

    card = active_card(state, now)
    if card is None:
        return 1.0, 1.0, 1.0
    return card.gas_mult, card.stool_mult, card.cap_mult


def place_of(state: Any) -> str:
    """失禁发生在哪（空串表示没有场景，按普通处理）。"""

    card = active_card(state)
    return card.place if card else ""


def prompt_of(state: Any) -> str:
    """卡的注入文本（空串表示没有）。"""

    card = active_card(state)
    return card.prompt if card else ""


def tick(state: Any, now: float | None = None) -> bool:
    """清掉过期卡，返回是否清掉了。"""

    raw = current_card(state)
    if not raw:
        return False
    card = ActiveCard.from_dict(raw)
    if card.active(now):
        return False
    clear_card(state)
    return True


__all__ = [
    "DEFAULT_CARDS",
    "ActiveCard",
    "active_card",
    "cards_of",
    "clear_card",
    "current_card",
    "make_card",
    "match_card",
    "multipliers",
    "place_of",
    "prompt_of",
    "set_card",
    "tick",
]
