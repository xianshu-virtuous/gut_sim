"""把当前肠胃状态写成提示词。

两类载体：

**普通模式**（没检测到 sisp 私密模式）
    轻量、日常：只在二档以上写一句"身体有点感觉"，一档完全不占 token。

**私密模式**（sisp 的 ``in_sex_mode`` 为真）
    完整陈述两条、档位、羞耻与憋忍进度；羞耻够高且用户开了干扰开关时，
    追加一段"此刻说话该是什么样"的硬约束（短句 / 带音效 / 不主动展开）。

写法上刻意保持"状态陈述"而不是"命令"：陈述句让模型从自己已有的人设出发自洽推演，
命令句则容易把角色写成念台词的木偶。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api import prompt_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.prompt import SystemReminderConsumeType, SystemReminderInsertType

from . import scene
from . import state as gut
from . import toilet
from .state import TIER_LABEL, band_of, cap_of, tier_of

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

#: 与 chatter 的 actor bucket 对齐（三种 sisp 也都写在 actor 里，互不覆盖靠层名）
BUCKET = "actor"

#: 本插件自己的层名（与 sisp 的 sisp_state / deep_neo_sisp:* 互不冲突）
LAYER_PRIVATE = "gut_sim:身体"
LAYER_DAILY = "gut_sim:日常"

#: 私密模式下各档位的感受陈述
_PRIVATE_TIER_TEXT: dict[int, str] = {
    1: "身体是安静的，暂时没什么动静，注意力可以全放在别处。",
    2: "下身开始有存在感了：憋着的时候，注意力会不自觉地往那儿跑。",
    3: "已经很难当成没事了：每一次深呼吸、每一次动一下，都在提醒自己快撑不住。",
}

#: 同一档位里按剩余量再细分一点语气（按上限的比例，因为三档是绝对点）
def _pressure_line(stool: float, cap: float) -> str:
    if cap <= 0:
        return ""
    ratio = stool / cap
    if ratio >= 0.85:
        return "几乎到极限了：腿根在发紧，稍微一松就会出事。"
    if ratio >= 0.60:
        return "撑得很难受了：说话要分神去忍，坐姿一直想换。"
    if ratio >= 0.35:
        return "明显急了：注意力被身体拽着走，反应会比平时慢半拍。"
    return ""

#: 屁意单独的描述（和便意分开写，因为两者感受不同）
_GAS_TEXT: dict[int, str] = {
    1: "肚子里没什么气。",
    2: "肠道里有一团气在慢慢挪，偶尔咕一声。",
    3: "气已经顶到门口了：每一次呼吸都在往下压，稍微分神就会漏出去。",
}

#: 普通人（非私密）的一行状态（按最高的那一条取主句）
_DAILY_TEXT: dict[int, str] = {
    1: "身体没什么特别的感觉。",
    2: "下身有点沉，时不时有一点点想上厕所的感觉，不碍事。",
    3: "有点憋着了：想上厕所的念头一直戳着，坐不太住。",
}

#: 羞耻高时的说话约束（只在用户开了 disrupt 开关时追加）
_DISRUPT_TEXT = (
    "【此刻的表达方式】\n"
    "你现在满脑子都是身体那点事，很难组织长篇大论：\n"
    "回话尽量短，一句话说得完就别分两段；单句建议不超过 {limit} 个字。\n"
    "情绪漏在字缝里，而不是用整段描写去解释自己有多难受——"
    "可以带一点没忍住的鼻音、气声，或者一个短促的音；但不许把状态说成台词、"
    "也不许用括号给自己讲旁白。\n"
    "不要主动切换话题、不要长篇抒情，也别反过来安慰对方——你现在顾不上。"
)


def build_private_text(
    state: "gut.GutState",
    config: "GutSimConfig",
    *,
    chat_type: str = "",
) -> str:
    """私密模式的完整状态陈述。"""

    cap_gas, cap_stool = cap_of(state, config)
    tier_stool = tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
    tier_gas = tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)

    lines: list[str] = ["【身体状态·私密中】"]

    # 便意
    stool_line = f"便意 {state.stool:.0f}/{cap_stool:.0f}（{TIER_LABEL[tier_stool]}）"
    if state.effects.get(gut.EFFECT_DIARRHEA, 0) > 0:
        stool_line += "｜闹肚子中"
    lines.append(stool_line)

    # 屁意
    gas_line = f"屁意 {state.gas:.0f}/{cap_gas:.0f}（{TIER_LABEL[tier_gas]}）"
    if state.effects.get(gut.EFFECT_BLOAT, 0) > 0:
        gas_line += "｜胀气中"
    lines.append(gas_line)

    if tier_stool >= 2:
        lines.append(_PRIVATE_TIER_TEXT[tier_stool])
        pressure = _pressure_line(state.stool, cap_stool)
        if pressure:
            lines.append(pressure)
    elif tier_gas >= 2:
        lines.append("便意还不明显，但肚子里有气在动。")
    else:
        lines.append(_PRIVATE_TIER_TEXT[1])

    if tier_gas >= 2:
        lines.append(_GAS_TEXT[tier_gas])

    # 进度与状态
    growth = []
    if config.grow.enabled and state.level > 0:
        growth.append(f"憋忍 Lv.{state.level}（{state.exp}/{config.grow.exp_per_level}）")
    if config.shame.enabled and state.shame > 0:
        growth.append(f"羞耻 {state.shame:.0f}")
    if growth:
        lines.append("｜".join(growth))

    # 事件卡（泻药 / 受凉 / 憋气……）：说明"为什么今天特别难熬"
    if config.scene.enabled:
        card_prompt = scene.prompt_of(state)
        if card_prompt:
            lines.append(card_prompt)

    # 失禁 / 漏气刚发生（带场景：在床上 / 沙发上……）
    if state.effects.get("just_wet", 0) > 0:
        place = scene.place_of(state) or state.last_place
        where = f"在{place}" if place else ""
        lines.append(
            f"刚刚已经没忍住出了事{where}，现在身上是收拾过或没收拾干净的状态。"
        )
    if state.effects.get("just_leak", 0) > 0:
        place = scene.place_of(state) or state.last_place
        where = f"在{place}" if place else ""
        lines.append(f"刚刚漏了一点气{where}，心里又羞又闷。")

    # 刚被消息撞见从洗手间回来
    if state.effects.get("just_toilet", 0) > 0:
        lines.append(
            toilet.describe_away(
                config,
                chat_type=chat_type,
                seconds=state.last_hold_seconds or 3.0,
            )
        )

    # 干扰约束
    if (
        config.inject.disrupt_enabled
        and config.shame.enabled
        and state.shame >= float(config.inject.disrupt_from_band)
    ):
        lines.append(
            _DISRUPT_TEXT.format(limit=int(config.inject.short_sentence_limit))
        )

    lines.append(
        "这些是身体自己的状态，不是你正在说的话，也不用向对方汇报数字；"
        "只从语气、节奏、忍耐的小动作里透出来。"
    )
    return "\n".join(line for line in lines if line)


def build_daily_text(
    state: "gut.GutState",
    config: "GutSimConfig",
    *,
    chat_type: str = "",
) -> str:
    """普通模式的一行身体状态（按最高的那一条取主句）。"""

    tier_stool = tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
    tier_gas = tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
    best = max(tier_stool, tier_gas)
    text = _DAILY_TEXT[best]

    if tier_gas >= 2 and tier_stool < 3:
        text += "肚子里有气在转，偶尔想放屁。"
    if state.effects.get(gut.EFFECT_DIARRHEA, 0) > 0:
        text += "（肠胃不太舒服，跑了好几趟厕所的感觉）"
    if state.effects.get(gut.EFFECT_BLOAT, 0) > 0:
        text += "（肚子胀得有点难受）"

    parts = [f"【身体】{text}"]

    # 刚从洗手间回来：让角色自己知道，说不说由她
    if state.effects.get("just_toilet", 0) > 0:
        parts.append(
            toilet.describe_away(
                config,
                chat_type=chat_type,
                seconds=state.last_hold_seconds or 3.0,
            )
        )

    return "\n".join(parts)


def build_text(
    state: "gut.GutState",
    config: "GutSimConfig",
    *,
    private: bool,
    chat_type: str = "",
) -> tuple[str, str]:
    """返回 (层名, 文本)；不需要注入时返回 ("", "")。"""

    if not config.inject.enabled:
        return "", ""

    if private and config.inject.private_inject:
        tier_stool = tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
        tier_gas = tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
        has_effect = any(left > 0 for left in state.effects.values())
        # 一档就是没感觉：私密模式下也不往上下文里塞废话
        if max(tier_stool, tier_gas) < 2 and not has_effect:
            return "", ""
        text = build_private_text(state, config, chat_type=chat_type)
        return LAYER_PRIVATE, text

    tier_stool = tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
    tier_gas = tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
    best = max(tier_stool, tier_gas)
    has_event = any(
        state.effects.get(name, 0) > 0
        for name in ("just_toilet", "just_wet", "just_leak")
    )

    # 群里默认更克制：不到配置的档位就不注入
    if chat_type == "group":
        if best < int(config.plugin.group_inject_tier) and not has_event:
            return "", ""

    if best < int(config.inject.normal_inject_min_tier) and not has_event:
        return "", ""

    return LAYER_DAILY, build_daily_text(state, config, chat_type=chat_type)


def write_reminder(stream_id: str, layer: str, text: str) -> None:
    """把文本写进该流的 actor reminder；空文本表示清掉这一层。"""

    if not layer:
        return
    try:
        if text and text.strip():
            prompt_api.add_stream_reminder(
                stream_id,
                BUCKET,
                layer,
                text,
                insert_type=SystemReminderInsertType.FIXED,
                consume=SystemReminderConsumeType.FOREVER,
            )
        else:
            prompt_api.delete_stream_reminder(stream_id, BUCKET, layer)
    except Exception as exc:  # noqa: BLE001 - 注入失败不该影响对话
        logger.warning(f"gut_sim: 写入 reminder 失败：{exc!r}")


def clear_stream(stream_id: str) -> None:
    """清掉本插件在该流的全部层。"""

    for layer in (LAYER_PRIVATE, LAYER_DAILY):
        try:
            prompt_api.delete_stream_reminder(stream_id, BUCKET, layer)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: 清理 reminder 失败：{exc!r}")


__all__ = [
    "BUCKET",
    "LAYER_DAILY",
    "LAYER_PRIVATE",
    "build_daily_text",
    "build_private_text",
    "build_text",
    "clear_stream",
    "write_reminder",
]
