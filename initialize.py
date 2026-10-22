"""首次加载时按人设推断一次参数。

推断内容（一次请求，返回 JSON）：

- ``shy``：easy / hard —— 易羞 / 不易羞，决定羞耻增长倍率
- ``gut``：good / normal / bad —— 肠胃底子，决定事件倾向
- ``gas_ratio``：2.0 ~ 3.0 —— 屁意相对便意的增速倍率
- ``base_rate``：0.8 ~ 1.2 —— 便意基础增速的微调

人设从 ``config/core.toml`` 的 ``[personality]`` 段读
（personality_core / personality_side）；读不到就随机落一个档，
并把原因写进日志——绝不因为推断失败就让插件起不来。
"""

from __future__ import annotations

import json
import random
import re
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import config_api, llm_api, storage_api
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

if TYPE_CHECKING:
    from .config import GutSimConfig

logger = get_logger("gut_sim")

PROFILE_KEY = "profile"

_INIT_SYSTEM = """你在给一个角色做「肠胃设定档案」的初始化判断。
根据给出的人格描述，判断四件事，然后输出一个 JSON。

1. shy：这个角色容易害羞吗？"easy" = 容易（脸皮薄、被看穿会慌）；"hard" = 不容易（脸皮厚、大大咧咧）
2. gut：肠胃底子怎么样？"good" / "normal" / "bad"
3. gas_ratio：屁意相对便意的增速倍率，2.0 ~ 3.0 之间的小数。
   提示：爱喝酒、喝碳酸饮料、吃饭快、不拘小节、体型偏胖的角色偏 2.8~3.0；
   敏感、拘谨、肠胃弱的偏 2.0~2.2；普通角色 2.4~2.6。
4. base_rate：便意基础增速微调，0.8 ~ 1.2 之间的小数。

# 输出
只输出 JSON，形如：
{"shy": "easy", "gut": "normal", "gas_ratio": 2.5, "base_rate": 1.0}
不要任何解释文字。"""

_DEFAULT_PROFILE: dict[str, Any] = {
    "shy": "easy",
    "gut": "normal",
    "gas_ratio": 2.5,
    "base_rate": 1.0,
    "source": "default",
}

#: 当前生效的档案（plugin 加载时写进来，handler 读）
_CURRENT: dict[str, Any] = dict(_DEFAULT_PROFILE)


def current_profile() -> dict[str, Any]:
    """取当前生效的人设档案（副本）。"""

    return dict(_CURRENT)


def set_current_profile(profile: dict[str, Any]) -> None:
    """写入当前生效的人设档案。"""

    _CURRENT.clear()
    _CURRENT.update(profile or _DEFAULT_PROFILE)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _random_profile() -> dict[str, Any]:
    return {
        "shy": random.choice(["easy", "hard"]),
        "gut": random.choice(["good", "normal", "bad"]),
        "gas_ratio": round(random.uniform(2.0, 3.0), 2),
        "base_rate": round(random.uniform(0.8, 1.2), 2),
        "source": "random",
    }


def _parse_profile(raw: str) -> dict[str, Any] | None:
    """把模型输出解析成档案；任何一项缺失就整份作废（走随机）。"""

    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{[^{}]*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return None

    shy = str(data.get("shy") or "").strip().lower()
    gut_health = str(data.get("gut") or "").strip().lower()
    if shy not in ("easy", "hard") or gut_health not in ("good", "normal", "bad"):
        return None

    try:
        gas_ratio = _clamp(float(data.get("gas_ratio")), 2.0, 3.0)
        base_rate = _clamp(float(data.get("base_rate")), 0.8, 1.2)
    except (TypeError, ValueError):
        return None

    return {
        "shy": shy,
        "gut": gut_health,
        "gas_ratio": round(gas_ratio, 2),
        "base_rate": round(base_rate, 2),
        "source": "model",
    }


def _personality_text() -> str:
    """从核心配置里取人格描述。"""

    try:
        core = config_api.get_core_config()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"gut_sim: 读取核心配置失败：{exc!r}")
        return ""

    personality = getattr(core, "personality", None)
    if personality is None:
        return ""

    parts: list[str] = []
    for field_name in ("personality_core", "personality_side"):
        value = getattr(personality, field_name, None)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _prepare_model_set(model_set: Any, config: "GutSimConfig") -> Any:
    """给推断请求准备一份低 token、关思考的模型集。"""

    if not isinstance(model_set, list):
        return model_set

    prepared: list[Any] = []
    for entry in model_set:
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["temperature"] = float(config.init.temperature)
            entry["max_tokens"] = int(config.init.max_tokens)
            entry["tool_call_compat"] = False
            extra = entry.get("extra_params")
            extra = dict(extra) if isinstance(extra, dict) else {}
            extra.pop("reasoning_effort", None)
            extra["enable_thinking"] = False
            extra["thinking"] = {"type": "disabled"}
            entry["extra_params"] = extra
        prepared.append(entry)
    return prepared


async def load_profile(config: "GutSimConfig") -> dict[str, Any] | None:
    """读已存在的档案。"""

    try:
        data = await storage_api.load_json(
            str(config.plugin.store_name or "gut_sim"), PROFILE_KEY
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"gut_sim: 读取档案失败：{exc!r}")
        return None
    return data if isinstance(data, dict) else None


async def save_profile(config: "GutSimConfig", profile: dict[str, Any]) -> None:
    """写档案。"""

    try:
        await storage_api.save_json(
            str(config.plugin.store_name or "gut_sim"), PROFILE_KEY, profile
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"gut_sim: 写入档案失败：{exc!r}")


async def init_profile(config: "GutSimConfig", *, force: bool = False) -> dict[str, Any]:
    """确保档案存在；返回最终使用的档案。

    Args:
        config: 插件配置
        force: 为真时忽略已存在的档案，重新推断
    """

    if not force:
        existing = await load_profile(config)
        if existing:
            return existing

    personality = _personality_text()
    if not config.init.enabled or not personality:
        profile = _random_profile()
        logger.info(
            "gut_sim: 人设不可用或推断已关闭，肠胃档案随机落档 "
            f"（shy={profile['shy']} gut={profile['gut']} "
            f"gas_ratio={profile['gas_ratio']}）"
        )
        await save_profile(config, profile)
        return profile

    try:
        task = str(config.init.task or "actor").strip() or "actor"
        model_set = _prepare_model_set(llm_api.get_model_set_by_task(task), config)

        request = llm_api.create_llm_request(model_set, "gut_sim_init")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_INIT_SYSTEM)))
        request.add_payload(
            LLMPayload(ROLE.USER, Text(f"【人格描述】\n{personality[:3000]}"))
        )
        response = await request.send(stream=False)
        raw = await response
        if not isinstance(raw, str) or not raw.strip():
            raw = str(getattr(response, "reasoning_content", "") or "")

        parsed = _parse_profile(raw)
        if parsed is None:
            logger.warning("gut_sim: 人设推断解析失败，随机落档")
            parsed = _random_profile()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"gut_sim: 人设推断失败（{exc!r}），随机落档")
        parsed = _random_profile()

    await save_profile(config, parsed)
    logger.info(
        "gut_sim: 肠胃档案已生成 "
        f"（shy={parsed['shy']} gut={parsed['gut']} "
        f"gas_ratio={parsed['gas_ratio']} base_rate={parsed['base_rate']} "
        f"来源={parsed['source']}）"
    )
    return parsed


__all__ = [
    "PROFILE_KEY",
    "current_profile",
    "init_profile",
    "load_profile",
    "save_profile",
    "set_current_profile",
]
