"""肠胃模拟的事件处理器。

三个订阅者，各管一段：

1. ``gut_sim_received``（on_message_received）
   用户消息到达。推进一轮数值、判档位、决定要不要附加状态；
   数值落盘。**排在 sisp 的状态处理器之后**（weight 更大），
   这样读到的是它刚更新过的私密模式。

2. ``gut_sim_usage``（after_llm_request）
   从 LLM 的 usage 里取本轮的 token 量，攒起来给下一轮当"吃了多少"的加成。

3. ``gut_sim_injector``（on_chatter_step / on_prompt_build）
   把当前身体状态写进该流的 actor reminder。私密模式写完整版，
   普通模式写一句日常版；不该注入时把这两层都清掉。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.core.components.base import BaseEventHandler
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from . import initialize, integrations, inject, judge, scene, sisp_bridge, toilet
from . import state as gut

logger = get_logger("gut_sim")

#: 本轮 token 累计：stream_id -> token 数（重启丢了也无所谓）
_PENDING_TOKENS: dict[str, float] = {}

#: 后台任务集合（防止被 GC）
_TASKS: set[asyncio.Task[Any]] = set()

#: 超过这么久没动静，就当新的一段（避免一口气补算几十轮）
_RESUME_GAP_SECONDS = 6 * 3600


def _spawn(coro: Any) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # 没有事件循环（离线自检）时不同步跑，直接丢弃
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _text_of(message: Any) -> str:
    if message is None:
        return ""
    for attr in ("processed_plain_text", "plain_text"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    return ""


def _chat_type_of(params: dict[str, Any], message: Any = None) -> str:
    envelope = params.get("envelope")
    if isinstance(envelope, dict):
        info = envelope.get("message_info")
        if isinstance(info, dict):
            return "group" if info.get("group_info") else "private"
    if message is not None:
        value = getattr(message, "chat_type", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_command(text: str) -> bool:
    return bool(text) and text.lstrip().startswith("/")


def _config_of(handler: Any) -> Any:
    return getattr(handler.plugin, "config", None)


# ── 1. 消息到达：推进一轮 ─────────────────────────────────────────────────


class GutSimReceived(BaseEventHandler):
    """用户消息到达 → 推进一轮肠胃数值。"""

    handler_name: str = "gut_sim_received"
    handler_description: str = "推进屁意/便意数值，判定档位与状态事件"
    #: 排在 sisp 的状态处理器（weight 20）之后，读取它刚写入的私密模式
    weight: int = 25
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_message_received"]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        try:
            await self._handle(params)
        except Exception as exc:  # noqa: BLE001 - 数值推进失败不影响对话
            logger.warning(f"gut_sim: 推进失败：{exc!r}")
        return EventDecision.SUCCESS, params

    async def _handle(self, params: dict[str, Any]) -> None:
        from .config import GutSimConfig

        config = _config_of(self)
        if not isinstance(config, GutSimConfig) or not config.plugin.enabled:
            return

        message = params.get("message")
        text = _text_of(message)
        if not text or _is_command(text):
            return

        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        if not stream_id:
            return

        chat_type = _chat_type_of(params, message)
        if chat_type == "group" and not config.plugin.allow_group:
            return

        state = await gut.load_state(stream_id, config)

        # 全新的一段：把人设档案里的推断值套上去
        if state.rounds == 0 and state.last_at <= 0:
            profile = initialize.current_profile()
            try:
                state.gas_ratio = float(profile.get("gas_ratio") or config.numeric.gas_ratio)
                state.base_rate = float(profile.get("base_rate") or config.numeric.base_rate)
            except (TypeError, ValueError):
                pass
            state.shy_mult = 1.3 if str(profile.get("shy")) == "easy" else 0.7

        # 久别重逢：只补一轮，不按真实小时数爆算
        gap = time.time() - state.last_at if state.last_at > 0 else 0.0
        rounds = 1
        if gap > _RESUME_GAP_SECONDS:
            rounds = max(1, min(3, int(gap // (12 * 3600)) + 1))

        private = await sisp_bridge.private_mode(stream_id, config)

        # 她正"在洗手间"：这条消息先被拦一下，回来再处理
        sender_id = str(getattr(message, "sender_id", "") or "").strip()
        hold = toilet.hold_seconds(
            state, config, chat_type=chat_type, sender_id=sender_id
        )
        if hold > 0:
            logger.info(
                f"gut_sim: 撞见她去洗手间，消息压 {hold:.1f}s"
                f"（stream={stream_id[:8]}）"
            )
            await asyncio.sleep(hold)
            gained = toilet.finish(state, config, private=private)
            state.last_hold_seconds = round(hold, 1)
            # 向日程登记"去了洗手间"（软依赖，失败静默）
            _spawn(integrations.note_toilet(config, hold))
            if config.plugin.debug_log:
                logger.info(
                    f"gut_sim: 从洗手间回来（羞耻 +{gained:.0f}）"
                    f"｜{gut.snapshot(state, config)}"
                )

        pending = _PENDING_TOKENS.pop(stream_id, 0.0)
        # 用户这条消息本身的长度也算一部分"吃进去的东西"
        token_units = pending + len(text) * 1.5

        # 事件卡：清过期 → 跟随 sisp 的卡 → 自己的触发词
        if config.scene.enabled:
            try:
                if scene.tick(state):
                    logger.debug(f"gut_sim: 事件卡到期（stream={stream_id[:8]}）")

                if config.scene.follow_sisp:
                    sisp_scene = await sisp_bridge.current_scene(stream_id, config)
                    pool = scene.cards_of(config)
                    if (
                        sisp_scene
                        and sisp_scene in pool
                        and scene.active_card(state) is None
                    ):
                        scene.set_card(state, scene.make_card(sisp_scene, pool[sisp_scene]))
                        logger.info(
                            f"gut_sim: 跟随 sisp 场景卡 {sisp_scene}"
                            f"（stream={stream_id[:8]}）"
                        )

                if config.scene.trigger_enabled:
                    pool = scene.cards_of(config)
                    hit = scene.match_card(text, pool)
                    if hit:
                        scene.set_card(state, scene.make_card(hit, pool[hit]))
                        logger.info(
                            f"gut_sim: 触发事件卡 {hit}"
                            f"（{pool[hit].get('title') or hit}，stream={stream_id[:8]}）"
                        )
            except Exception as exc:  # noqa: BLE001 - 卡系统坏了不影响数值
                logger.debug(f"gut_sim: 事件卡处理失败：{exc!r}")

        report = gut.advance(
            state,
            config,
            rounds=rounds,
            token_units=token_units,
            private=private,
        )

        # 刚出事：给注入层留一个"刚才发生了什么"的短提示
        if report.wetting:
            state.effects["just_wet"] = 1
            _spawn(integrations.note_wetting(config, scene.place_of(state)))
        if report.leaking:
            state.effects["just_leak"] = 1

        await gut.save_state(stream_id, state, config)

        if config.plugin.debug_log:
            logger.info(
                f"gut_sim: 推进 rounds={rounds} token={token_units:.0f} "
                f"private={private} → {gut.snapshot(state, config)}"
            )

        # 状态事件判断放后台：调模型慢，别拖住消息主流程
        if config.events.enabled:
            _spawn(self._maybe_effect(stream_id, state, config, report.tier))

    async def _maybe_effect(
        self, stream_id: str, state: "gut.GutState", config: Any, tier: int
    ) -> None:
        """判断并施加一个状态效果（不改变数值本身，只挂 buff）。"""

        if tier < int(config.events.judge_only_from_tier):
            return

        effect = await judge.judge_effect(stream_id, state, config)
        if not effect:
            return

        events = config.events
        applied = False

        if effect == "bloat" and events.bloat_enabled and not gut.on_cooldown(state, "bloat"):
            gut.apply_effect(state, gut.EFFECT_BLOAT, int(events.bloat_rounds))
            gut.start_cooldown(state, "bloat", int(events.judge_cooldown_rounds))
            applied = True
        elif effect == "diarrhea" and events.diarrhea_enabled and not gut.on_cooldown(state, "diarrhea"):
            gut.apply_effect(state, gut.EFFECT_DIARRHEA, int(events.diarrhea_rounds))
            gut.start_cooldown(state, "diarrhea", int(events.judge_cooldown_rounds))
            applied = True
        elif effect == "weaken" and events.weaken_enabled and not gut.on_cooldown(state, "weaken"):
            # 便意上限永久 -amount；下限由 cap_of 统一收敛到 cap_min
            state.weak_total += float(events.weaken_amount)
            gut.start_cooldown(state, "weaken", int(events.judge_cooldown_rounds) * 3)
            applied = True
        elif effect == "relief" and events.relief_enabled and not gut.on_cooldown(state, "relief"):
            gut.apply_effect(state, gut.EFFECT_RELIEF, int(events.relief_rounds))
            gut.start_cooldown(state, "relief", int(events.judge_cooldown_rounds))
            applied = True
        elif effect == "strengthen" and events.strengthen_enabled and not gut.on_cooldown(state, "strengthen"):
            cap_bonus = float(events.strengthen_bonus_cap)
            if state.bonus_stool < cap_bonus:
                state.bonus_stool = min(
                    cap_bonus, state.bonus_stool + float(events.strengthen_amount)
                )
                gut.start_cooldown(state, "strengthen", int(events.judge_cooldown_rounds) * 2)
                applied = True
        elif effect == "relax" and events.relax_enabled and not gut.on_cooldown(state, "relax"):
            gut.apply_effect(state, gut.EFFECT_RELAX, int(events.relax_rounds))
            gut.start_cooldown(state, "relax", int(events.judge_cooldown_rounds))
            applied = True

        if not applied:
            return

        await gut.save_state(stream_id, state, config)
        logger.info(f"gut_sim: 附加状态 {effect}（stream={stream_id[:8]}）")


# ── 2. LLM 用完后：攒 token ───────────────────────────────────────────────


class GutSimUsage(BaseEventHandler):
    """从 after_llm_request 的 usage 里取本轮 token 量。"""

    handler_name: str = "gut_sim_usage"
    handler_description: str = "记录本轮 LLM 的 token 用量，供下一轮数值推进使用"
    weight: int = 20
    intercept_message: bool = False
    init_subscribe: list[str] = ["after_llm_request"]

    #: 这些请求不计入"吃进去的量"（我们自己的判断请求、别人的评分请求）
    _EXCLUDE_PREFIXES = ("gut_sim_", "sisp", "deep_neo_sisp")

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        try:
            self._handle(params)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: token 记录失败：{exc!r}")
        return EventDecision.SUCCESS, params

    def _handle(self, params: dict[str, Any]) -> None:
        request_name = str(params.get("request_name") or "")
        if request_name.startswith(self._EXCLUDE_PREFIXES):
            return

        meta = params.get("meta_data")
        stream_id = ""
        if isinstance(meta, dict):
            stream_id = str(meta.get("stream_id") or "").strip()
        if not stream_id:
            return

        usage = params.get("usage")
        completion = 0
        if isinstance(usage, dict):
            completion = int(usage.get("completion_tokens") or 0)
        if completion <= 0:
            return

        _PENDING_TOKENS[stream_id] = _PENDING_TOKENS.get(stream_id, 0.0) + float(completion)


# ── 3. 注入 ───────────────────────────────────────────────────────────────


class GutSimInjector(BaseEventHandler):
    """把身体状态写进该流的 actor reminder。"""

    handler_name: str = "gut_sim_injector"
    handler_description: str = "按档位把肠胃感受注入提示词（私密模式写详细版）"
    weight: int = 12
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_chatter_step", "on_prompt_build"]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        try:
            await self._handle(event_name, params)
        except Exception as exc:  # noqa: BLE001 - 注入失败不影响对话
            logger.warning(f"gut_sim: 注入失败：{exc!r}")
        return EventDecision.SUCCESS, params

    async def _handle(self, event_name: str, params: dict[str, Any]) -> None:
        from .config import GutSimConfig

        config = _config_of(self)
        if not isinstance(config, GutSimConfig) or not config.plugin.enabled:
            return
        if not config.inject.enabled:
            return

        stream_id = str(params.get("stream_id", "") or "").strip()
        if not stream_id:
            return

        # on_prompt_build 兜底：只在用户开了开关时跑
        if event_name == "on_prompt_build" and not config.inject.refresh_on_prompt:
            return

        state = await gut.load_state(stream_id, config)
        private = await sisp_bridge.private_mode(stream_id, config)

        chat_type = ""
        message = params.get("message")
        if event_name == "on_chatter_step":
            chat_type = _chat_type_of(params, message)

        layer, text = inject.build_text(
            state, config, private=private, chat_type=chat_type
        )

        # 只保留当前该在的那一层，另一层清掉，避免两版同时在场
        if layer == inject.LAYER_PRIVATE:
            inject.clear_stream(stream_id)
            inject.write_reminder(stream_id, inject.LAYER_PRIVATE, text)
        elif layer == inject.LAYER_DAILY:
            inject.clear_stream(stream_id)
            inject.write_reminder(stream_id, inject.LAYER_DAILY, text)
        else:
            inject.clear_stream(stream_id)

        if config.plugin.debug_log and text:
            logger.debug(
                f"gut_sim: 注入 {layer}（private={private}）"
                f"｜{gut.snapshot(state, config)}"
            )


__all__ = ["GutSimInjector", "GutSimReceived", "GutSimUsage"]
