"""肠胃模拟插件入口。

装配顺序：读配置 → 生成/读取人设档案 → 注册组件 → 注册如厕巡检。
卸载时取消巡检、清掉本插件写在所有流上的 reminder，不留残影。
"""

from __future__ import annotations

import asyncio

from src.core.components import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from . import inject, sisp_bridge, toilet
from .command import GutCommand
from .config import GutSimConfig
from .handler import GutSimInjector, GutSimReceived, GutSimUsage
from .initialize import init_profile, set_current_profile

logger = get_logger("gut_sim")

#: 如厕巡检的调度器任务名
_TICK_TASK_NAME = "gut_sim_toilet_tick"

#: 等调度器就绪的最多轮数（每轮 0.5s）
_SCHEDULER_WAIT_ROUNDS = 40


@register_plugin
class GutSimPlugin(BasePlugin):
    """肠胃模拟插件。"""

    plugin_name: str = "gut_sim"
    configs: list[type] = [GutSimConfig]

    _schedule_id: str | None = None
    _register_task: asyncio.Task[None] | None = None

    def get_components(self) -> list[type]:
        config = self.config
        if isinstance(config, GutSimConfig) and not config.plugin.enabled:
            return []
        return [
            GutSimReceived,
            GutSimUsage,
            GutSimInjector,
            GutCommand,
        ]

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def on_plugin_loaded(self) -> None:
        config = self.config
        if not isinstance(config, GutSimConfig):
            logger.error("gut_sim: 配置加载失败，插件不生效")
            return
        if not config.plugin.enabled:
            logger.info("gut_sim: 配置中已禁用，跳过初始化")
            return

        profile = await init_profile(config)
        set_current_profile(profile)

        logger.info(
            "gut_sim: 已加载｜"
            f"上限 {config.numeric.cap_init:.0f}→{config.numeric.cap_max:.0f}"
            f"（便意理论 {config.numeric.cap_max + config.events.strengthen_bonus_cap:.0f}）"
            f"｜档位 {config.numeric.tier_1:.0f}/{config.numeric.tier_2:.0f}"
            f"｜失禁 {'开' if config.shame.wetting_enabled else '关'}"
            f"｜干扰 {'开' if config.inject.disrupt_enabled else '关'}"
            f"｜sisp 联动 {'开' if config.sisp.enabled else '关'}"
            f"｜自动如厕 {'开' if config.toilet.auto_enabled else '关'}"
        )

        # 如厕巡检：等调度器就绪后注册（Bot 启动阶段调度器可能还没起）
        if config.toilet.auto_enabled:
            self._register_task = asyncio.create_task(self._register_tick_when_ready())

    async def on_plugin_unloaded(self) -> None:
        await self._cancel_tick()
        sisp_bridge.invalidate()
        try:
            from src.app.plugin_system.api import stream_api

            for stream_id in stream_api.get_all_stream_ids():
                inject.clear_stream(stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"gut_sim: 卸载清理 reminder 失败：{exc!r}")

    # ── 如厕巡检 ──────────────────────────────────────────────────────────

    async def _register_tick_when_ready(self) -> None:
        """轮询等待调度器就绪，然后注册周期巡检。"""

        config = self.config
        if not isinstance(config, GutSimConfig):
            return
        interval = max(10, int(config.toilet.auto_check_seconds))

        try:
            from src.kernel.scheduler import TriggerType, get_unified_scheduler
        except Exception as exc:  # noqa: BLE001 - 没有调度器就退化成不巡检
            logger.warning(f"gut_sim: 调度器不可用，自动如厕未启用：{exc!r}")
            return

        for _ in range(_SCHEDULER_WAIT_ROUNDS):
            try:
                scheduler = get_unified_scheduler()
                schedule_id = await scheduler.create_schedule(
                    callback=self._tick_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=_TICK_TASK_NAME,
                    force_overwrite=True,
                )
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"gut_sim: 注册如厕巡检失败：{exc!r}")
                await asyncio.sleep(2.0)
                continue

            self._schedule_id = schedule_id
            logger.info(f"gut_sim: 如厕巡检已注册（每 {interval}s）")
            return

        logger.warning("gut_sim: 等待调度器就绪超时，自动如厕未注册")

    async def _tick_job(self) -> None:
        """调度器回调：跑一次如厕巡检。"""

        config = self.config
        if not isinstance(config, GutSimConfig) or not config.plugin.enabled:
            return
        try:
            started = await toilet.tick(config)
            if started:
                logger.debug(f"gut_sim: 巡检送进洗手间 {started} 个流")
        except Exception as exc:  # noqa: BLE001 - 巡检失败不打断调度
            logger.error(f"gut_sim: 如厕巡检异常：{exc!r}")

    async def _cancel_tick(self) -> None:
        """取消巡检与注册任务。"""

        if self._register_task is not None and not self._register_task.done():
            self._register_task.cancel()
            try:
                await self._register_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._register_task = None

        if self._schedule_id:
            try:
                from src.kernel.scheduler import get_unified_scheduler

                await get_unified_scheduler().remove_schedule(self._schedule_id)
            except Exception as exc:  # noqa: BLE001 - 任务可能已结束
                logger.debug(f"gut_sim: 移除如厕巡检失败：{exc!r}")
            self._schedule_id = None
