"""事件链路实测：真的走一遍「收消息 → 注入 → LLM 用量」三个订阅者。

和 check_load.py 的区别：那个只验证"能加载"，这个验证"加载后接到事件真的会干活"。
会往当前工作目录的 data/json_storage 写测试状态（建议在 .tmp 里跑）。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B <本文件绝对路径>
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_PATH = Path(__file__).resolve().parent.parent
#: 每次跑用新的流，避免上一轮留下的状态把数值顶高
STREAM_ID = hashlib.sha256(os.urandom(16)).hexdigest()

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


def _fake_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        stream_id=STREAM_ID,
        processed_plain_text=text,
        plain_text=text,
        content=text,
        chat_type="private",
        sender_role="user",
    )


async def main() -> int:
    sys.path.insert(0, str(NEO_ROOT))

    from src.core.components.loader import load_manifest
    from src.core.managers.plugin_manager import get_plugin_manager
    from src.kernel.event import get_event_bus

    manifest = await load_manifest(str(PLUGIN_PATH))
    if manifest is None:
        print("[FAIL] manifest 解析失败")
        return 1

    manager = get_plugin_manager()
    ok = await manager.load_plugin_from_manifest(str(PLUGIN_PATH), manifest)
    if not ok:
        print("[FAIL] 插件加载失败")
        return 1

    plugin = manager.get_plugin("gut_sim")
    config = getattr(plugin, "config", None)
    from gut_sim.config import GutSimConfig

    if not isinstance(config, GutSimConfig):
        print("[FAIL] 拿不到 GutSimConfig")
        return 1

    # 关掉模型判断（离线环境不调真模型），把档位阈值调低方便快速跨档
    config.events.enabled = False
    config.inject.refresh_on_prompt = False
    config.shame.enabled = True

    bus = get_event_bus()

    # 1) 连发消息，看数值有没有涨
    for i in range(1, 31):
        await bus.publish(
            "on_message_received",
            {
                "message": _fake_message(f"第 {i} 轮：随便聊两句，喝了一大杯冰可乐。"),
                "envelope": {"message_info": {"group_info": None}},
            },
        )

    from gut_sim import state as gut

    state = await gut.load_state(STREAM_ID, config)
    check("收到消息后有状态落盘", state.rounds >= 30, f"rounds={state.rounds}")
    check(
        "数值在涨",
        state.stool > 0 and state.gas > 0,
        f"stool={state.stool:.1f} gas={state.gas:.1f}",
    )
    check(
        "屁意涨得比便意快",
        state.gas > state.stool,
        f"gas={state.gas:.1f} stool={state.stool:.1f}",
    )
    print(f"      {gut.snapshot(state, config)}")

    # 2) 注入了什么
    await bus.publish(
        "on_chatter_step",
        {"stream_id": STREAM_ID, "message": _fake_message("在吗")},
    )

    from src.app.plugin_system.api import prompt_api

    reminder = prompt_api.get_stream_reminder(STREAM_ID, "actor")
    check("actor reminder 被写入", bool(reminder.strip()), reminder[:80].replace("\n", " / "))
    if state.stool >= config.numeric.tier_1:
        check("跨档后有身体描述", "身体" in reminder, reminder[:60])

    # 3) after_llm_request 攒 token → 下一轮数值更高
    before = state.stool
    await bus.publish(
        "after_llm_request",
        {
            "request_name": "default_chatter",
            "stream": False,
            "success": True,
            "usage": {"completion_tokens": 1200, "prompt_tokens": 3000},
            "meta_data": {"stream_id": STREAM_ID},
        },
    )
    await bus.publish(
        "on_message_received",
        {
            "message": _fake_message("再聊一句"),
            "envelope": {"message_info": {"group_info": None}},
        },
    )
    state2 = await gut.load_state(STREAM_ID, config)
    check(
        "token 计入下一轮（增量明显大于基础值）",
        (state2.stool - before) > 2.0,
        f"{before:.1f} → {state2.stool:.1f}",
    )

    # 4) 自己的判断请求不该被计入
    before2 = state2.stool
    await bus.publish(
        "after_llm_request",
        {
            "request_name": "gut_sim_judge",
            "success": True,
            "usage": {"completion_tokens": 5000},
            "meta_data": {"stream_id": STREAM_ID},
        },
    )
    await bus.publish(
        "on_message_received",
        {
            "message": _fake_message("第三句"),
            "envelope": {"message_info": {"group_info": None}},
        },
    )
    state3 = await gut.load_state(STREAM_ID, config)
    check(
        "gut_sim_judge 的 token 被排除",
        (state3.stool - before2) < 3.5,
        f"{before2:.1f} → {state3.stool:.1f}",
    )

    # 5) 命令消息不推进
    before3 = state3.rounds
    await bus.publish(
        "on_message_received",
        {
            "message": _fake_message("/肠胃"),
            "envelope": {"message_info": {"group_info": None}},
        },
    )
    state4 = await gut.load_state(STREAM_ID, config)
    check("命令消息不推进数值", state4.rounds == before3, f"{before3} → {state4.rounds}")

    print("-" * 72)
    if _failures:
        print(f"{len(_failures)} 项失败: {_failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
