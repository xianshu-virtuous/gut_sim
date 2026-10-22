"""用便宜模型判断"此刻该不该附加某种肠胃状态"。

设计要点：
- 只有在数值到达二档以上、且到了判断间隔时才真的调模型（一档不浪费调用）；
- 一次请求判全部效果，返回一个 JSON，``max_tokens`` 给得很小；
- **判断不阻塞回复**：拿到的只是"这一轮要不要给角色加个状态"，
  下一轮注入时自然体现在身体感受里；
- 同一个效果有冷却，避免连续几轮都在闹肚子。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import llm_api, stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from . import state as gut

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

#: 可选效果
EFFECTS: tuple[str, ...] = (
    "bloat",
    "diarrhea",
    "weaken",
    "relief",
    "strengthen",
    "relax",
    "none",
)

_JUDGE_SYSTEM = """你在替一个角色判断「此刻身体的肠胃状况是否被聊天内容或处境触发」。
不是写正文，只是做个判断。

可选效果（只能选一个）：
- bloat：胀气 / 肚子发胀，气多
- diarrhea：闹肚子 / 腹泻 / 肠胃不舒服
- weaken：肠胃被折腾弱了（长期变差）
- relief：安心 / 被照顾到，身体松下来
- strengthen：肠胃被调理好了一点
- relax：舒畅 / 身体通畅、松弛
- none：什么都不发生

判断依据：最近的对话里有没有吃喝、受凉、紧张、被照顾、被折腾、被灌了什么之类的线索；
没有明显线索就输出 none，不要为了触发而编理由。

# 输出
只输出一个 JSON，形如 {"effect": "none"}，不要任何其他文字。"""


def _extract_effect(raw: str) -> str | None:
    """从模型返回里抽出效果名。"""

    text = (raw or "").strip()
    if not text:
        return None

    try:
        match = re.search(r"\{[^{}]*\}", text, re.S)
        if match:
            data = json.loads(match.group(0))
            effect = str(data.get("effect") or "").strip().lower()
            if effect in EFFECTS:
                return effect
    except Exception:  # noqa: BLE001 - JSON 失败就退回关键词匹配
        pass

    lowered = text.lower()
    for name in EFFECTS:
        if name in lowered:
            return name
    return None


def _prepare_model_set(model_set: Any, config: "GutSimConfig") -> Any:
    """给判断请求准备一份低 token、关思考的模型集。"""

    if not isinstance(model_set, list):
        return model_set

    prepared: list[Any] = []
    for entry in model_set:
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["temperature"] = float(config.events.judge_temperature)
            entry["max_tokens"] = int(config.events.judge_max_tokens)
            entry["tool_call_compat"] = False
            extra = entry.get("extra_params")
            extra = dict(extra) if isinstance(extra, dict) else {}
            extra.pop("reasoning_effort", None)
            extra["enable_thinking"] = False
            extra["thinking"] = {"type": "disabled"}
            entry["extra_params"] = extra
        prepared.append(entry)
    return prepared


def _model_set(config: "GutSimConfig") -> Any:
    task = str(config.events.judge_task or "").strip()
    if task:
        try:
            return llm_api.get_model_set_by_task(task)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: 取判断模型任务 {task} 失败，回落到 actor：{exc!r}")
    return llm_api.get_model_set_by_task("actor")


async def judge_effect(
    stream_id: str,
    state: "gut.GutState",
    config: "GutSimConfig",
) -> str | None:
    """返回这一轮建议附加的效果名；不需要判断或失败时返回 None。"""

    events = config.events
    if not events.enabled:
        return None

    tier_stool = gut.tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
    tier_gas = gut.tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
    if max(tier_stool, tier_gas) < int(events.judge_only_from_tier):
        return None

    interval = max(1, int(events.judge_interval_rounds))
    if interval > 1 and state.rounds % interval != 0:
        return None

    try:
        history = await stream_api.get_stream_messages(stream_id, limit=6)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"gut_sim: 拉取对话失败，跳过判断：{exc!r}")
        return None

    lines: list[str] = []
    for msg in history:
        role = "对方" if getattr(msg, "sender_role", "") != "bot" else "角色"
        text = str(
            getattr(msg, "processed_plain_text", None)
            or getattr(msg, "content", "")
            or ""
        ).strip()
        if text:
            lines.append(f"[{role}] {text[:200]}")
    if not lines:
        return None

    cap_gas, cap_stool = gut.cap_of(state, config)
    context_text = (
        "\n".join(lines)
        + "\n\n【角色此刻】"
        + f"便意 {state.stool:.0f}/{cap_stool:.0f}，屁意 {state.gas:.0f}/{cap_gas:.0f}，"
        + f"羞耻 {state.shame:.0f}"
    )

    try:
        model_set = _prepare_model_set(_model_set(config), config)

        request = llm_api.create_llm_request(model_set, "gut_sim_judge")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_JUDGE_SYSTEM)))
        request.add_payload(LLMPayload(ROLE.USER, Text(context_text)))
        response = await request.send(stream=False)

        raw = await response
        if not isinstance(raw, str) or not raw.strip():
            raw = str(getattr(response, "reasoning_content", "") or "")
        effect = _extract_effect(raw)
        if effect and effect != "none":
            logger.debug(f"gut_sim: 判断结果 {effect}（stream={stream_id[:8]}）")
            return effect
    except Exception as exc:  # noqa: BLE001 - 判断失败不打扰对话
        logger.debug(f"gut_sim: 状态判断失败：{exc!r}")

    return None


__all__ = ["EFFECTS", "judge_effect"]
