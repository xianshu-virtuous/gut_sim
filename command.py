"""``/肠胃`` 命令：看一眼、开一下、改一处。

只给主人用（OWNER），回执走 ``send_text`` 直接发到聊天流，
**不进入 LLM 上下文**——所以 bot 不会知道主人刚偷看了它的肚子。
"""

from __future__ import annotations

import time

from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
from src.kernel.logger import get_logger

from . import inject, scene, sisp_bridge
from . import state as gut
from .config import GutSimConfig

logger = get_logger("gut_sim")

_TIER_HINT = {
    1: "还撑得住，自己都未必察觉",
    2: "有感觉了，但能压住",
    3: "很急了，注意力一直被拽走",
}


class GutCommand(BaseCommand):
    """查看 / 开关肠胃模拟。"""

    name: str = "gut"
    description: str = "查看肠胃状态（便意 / 屁意 / 羞耻 / 私密模式）；/肠胃 reset 清空数值"
    permission_level: PermissionLevel = PermissionLevel.OWNER
    command_prefix: str = "/"

    def _config(self) -> GutSimConfig:
        config = self.plugin.config if self.plugin else None
        return config if isinstance(config, GutSimConfig) else GutSimConfig()

    async def _reply(self, text: str) -> None:
        await send_text(text, stream_id=self.stream_id)

    @classmethod
    def match(cls, parts: list[str]) -> int:
        if not parts:
            return 0
        first = parts[0].lstrip("/").strip().lower()
        if first in ("肠胃", "gut", "肚子"):
            return 1
        return 0

    @cmd_route()
    async def handle_state(self) -> tuple[bool, str]:
        """输出状态快照。"""

        config = self._config()
        state = await gut.load_state(self.stream_id, config)
        cap_gas, cap_stool = gut.cap_of(state, config)
        tier_stool = gut.tier_of(state.stool, config.numeric.tier_1, config.numeric.tier_2)
        tier_gas = gut.tier_of(state.gas, config.numeric.tier_1, config.numeric.tier_2)
        private, store = await sisp_bridge.detect(self.stream_id, config)
        band = gut.band_of(state.shame, config)

        lines = ["【肠胃状态】"]
        lines.append(
            f"便意 {state.stool:.1f} / {cap_stool:.0f}"
            f"（{gut.TIER_LABEL[tier_stool]}·{_TIER_HINT[tier_stool]}）"
        )
        lines.append(
            f"屁意 {state.gas:.1f} / {cap_gas:.0f}"
            f"（{gut.TIER_LABEL[tier_gas]}·{_TIER_HINT[tier_gas]}）"
        )
        lines.append(
            f"憋忍 Lv.{state.level}（经验 {state.exp}/{config.grow.exp_per_level}）"
            f"｜羞耻 {state.shame:.0f}（{'低中高'[band - 1]}）"
        )
        lines.append(
            f"私密模式：{'是 ✅（' + (store or 'sisp') + '）' if private else '否'}"
            if config.sisp.enabled
            else "私密模式：sisp 联动已关闭"
        )

        effects = [name for name, left in state.effects.items() if left > 0]
        if effects:
            lines.append(f"进行中：{', '.join(effects)}")
        if state.wet_count or state.leak_count:
            lines.append(f"累计：失禁 {state.wet_count} 次｜漏气 {state.leak_count} 次")

        await self._reply("\n".join(lines))
        logger.info(
            f"gut 状态查询: stream={self.stream_id[:8]} "
            f"stool={state.stool:.1f} gas={state.gas:.1f} private={private}"
        )
        return True, "ok"

    @cmd_route("上厕所")
    async def handle_toilet(self) -> tuple[bool, str]:
        """主动排空：两条归零，按配置给舒畅。"""

        config = self._config()
        if not config.toilet.command_enabled:
            await self._reply("如厕命令被配置关掉了（[toilet].command_enabled）。")
            return True, "disabled"

        state = await gut.load_state(self.stream_id, config)
        was = gut.snapshot(state, config)
        private, _store = await sisp_bridge.detect(self.stream_id, config)
        gained = gut.relieve(state, config, private=private)
        await gut.save_state(self.stream_id, state, config)

        if config.toilet.refresh_layer:
            try:
                layer, text = inject.build_text(
                    state, config, private=private, chat_type="private"
                )
                inject.clear_stream(self.stream_id)
                if layer and text:
                    inject.write_reminder(self.stream_id, layer, text)
            except Exception:  # noqa: BLE001 - 刷新失败不影响回执
                pass

        lines = ["去过厕所了，两条都清空。"]
        lines.append(f"清空前：{was}")
        if config.toilet.relax_rounds > 0:
            lines.append(f"舒畅生效：接下来 {config.toilet.relax_rounds} 轮不增长。")
        if gained > 0:
            lines.append(f"羞耻 +{gained:.0f}")
        await self._reply("\n".join(lines))
        logger.info(
            f"gut 如厕: stream={self.stream_id[:8]} private={private} shame+={gained:.0f}"
        )
        return True, "ok"

    @cmd_route("卡")
    async def handle_card(self) -> tuple[bool, str]:
        """查看当前事件卡与可用卡池。"""

        config = self._config()
        state = await gut.load_state(self.stream_id, config)
        active = scene.active_card(state)
        pool = scene.cards_of(config)

        lines: list[str] = ["【事件卡】"]
        if active is None:
            lines.append("当前没有生效的卡")
        else:
            left = max(0.0, active.until - time.time()) / 3600.0
            lines.append(
                f"生效中：{active.id}（{active.title}）"
                f"｜还剩 {left:.1f} 小时"
                f"｜屁意 ×{active.gas_mult:.2f} 便意 ×{active.stool_mult:.2f}"
                f"｜来源 {active.source}"
            )

        lines.append("可用卡：" + "、".join(sorted(pool.keys())))
        lines.append("用 /肠胃 卡 off 关掉当前卡；要加卡改配置 [scene].custom_cards")
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("卡", "off")
    async def handle_card_off(self) -> tuple[bool, str]:
        """关掉当前生效的卡。"""

        config = self._config()
        state = await gut.load_state(self.stream_id, config)
        had = scene.active_card(state)
        scene.clear_card(state)
        await gut.save_state(self.stream_id, state, config)
        await self._reply(
            f"关掉 {had.id}（{had.title}）了。" if had else "本来就没有生效的卡。"
        )
        return True, "ok"

    @cmd_route("reset")
    async def handle_reset(self) -> tuple[bool, str]:
        """把当前流的两条数值清空（保留等级与羞耻抗性）。"""

        config = self._config()
        state = await gut.load_state(self.stream_id, config)
        state.stool = 0.0
        state.gas = 0.0
        state.effects.clear()
        await gut.save_state(self.stream_id, state, config)
        await self._reply("清空好了——数值归零，等级和抗性留着。")
        return True, "ok"


__all__ = ["GutCommand"]
