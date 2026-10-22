"""实例环境冒烟：验证 gut_sim 能在真实框架下导入、组件名齐全、注入文本能生成。

用法（实例 venv）::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/check_smoke.py

只读验证，不改实例里的任何插件，也不写实例的 data/。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


def main() -> int:
    sys.path.insert(0, str(NEO_ROOT))
    sys.path.insert(0, str(PLUGIN_REPO))

    print(f"neo 根目录: {NEO_ROOT}")
    print(f"插件仓库  : {PLUGIN_REPO}")
    print(f"工作目录  : {Path.cwd()}")
    print("-" * 72)

    # 1) 配置
    try:
        from gut_sim.config import GutSimConfig
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 导入 gut_sim.config 失败: {exc!r}")
        return 2

    config = GutSimConfig()
    check("GutSimConfig 可实例化", True)
    check(
        "档位阈值 60 / 90",
        config.numeric.tier_1 == 60.0 and config.numeric.tier_2 == 90.0,
        f"tier_1={config.numeric.tier_1} tier_2={config.numeric.tier_2}",
    )
    check(
        "上限 100 → 300、理论 320",
        config.numeric.cap_init == 100.0
        and config.numeric.cap_max == 300.0
        and config.numeric.cap_max + config.events.strengthen_bonus_cap == 320.0,
        f"init={config.numeric.cap_init} max={config.numeric.cap_max} "
        f"bonus={config.events.strengthen_bonus_cap}",
    )
    check(
        "猎奇项默认关",
        not config.shame.wetting_enabled
        and not config.shame.leak_enabled
        and not config.inject.disrupt_enabled,
        f"wetting={config.shame.wetting_enabled} "
        f"leak={config.shame.leak_enabled} disrupt={config.inject.disrupt_enabled}",
    )
    check(
        "sisp 探测顺序含 deep_neo_sisp / sisp",
        list(config.sisp.store_order) == ["deep_neo_sisp", "sisp"],
        str(config.sisp.store_order),
    )

    # 2) 插件入口
    try:
        from gut_sim.plugin import GutSimPlugin
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 导入 gut_sim.plugin 失败: {exc!r}")
        return 2
    check(
        "GutSimPlugin 已注册且名字正确",
        GutSimPlugin.plugin_name == "gut_sim",
        f"{GutSimPlugin.plugin_name}",
    )
    check(
        "插件声明了配置类",
        GutSimConfig in list(GutSimPlugin.configs),
        str([c.__name__ for c in GutSimPlugin.configs]),
    )

    # 3) 组件名与 manifest 对齐
    try:
        from gut_sim.handler import GutSimInjector, GutSimReceived, GutSimUsage
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 导入 gut_sim.handler 失败: {exc!r}")
        return 2
    handlers = [GutSimReceived, GutSimUsage, GutSimInjector]
    names = [h.handler_name for h in handlers]
    check(
        "事件处理器名字唯一",
        len(names) == len(set(names)),
        str(names),
    )
    check(
        "订阅事件正确",
        set(GutSimReceived.init_subscribe) == {"on_message_received"}
        and set(GutSimUsage.init_subscribe) == {"after_llm_request"}
        and set(GutSimInjector.init_subscribe) == {"on_chatter_step", "on_prompt_build"},
        f"{GutSimReceived.init_subscribe} / {GutSimUsage.init_subscribe} / {GutSimInjector.init_subscribe}",
    )

    import json

    manifest_path = PLUGIN_REPO / "gut_sim" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 读取 manifest 失败: {exc!r}")
        return 2
    declared = {
        item.get("component_name")
        for item in manifest.get("include", [])
        if item.get("component_type") == "event_handler"
    }
    check(
        "manifest 声明的事件处理组件与代码一致",
        declared == set(names),
        f"manifest={sorted(declared)} code={sorted(names)}",
    )

    # 4) 注入文本
    try:
        from gut_sim import inject
        from gut_sim import state as gut
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 导入 gut_sim.inject / state 失败: {exc!r}")
        return 2

    state = gut.GutState(gas_ratio=2.5, base_rate=1.0)
    state.stool = 45.0
    state.gas = 20.0
    layer, text = inject.build_text(state, config, private=False, chat_type="private")
    check("一档普通模式不注入", layer == "" and text == "", f"layer={layer!r}")

    state.stool = 75.0
    layer, text = inject.build_text(state, config, private=False, chat_type="private")
    check("二档普通模式注入日常版", layer == inject.LAYER_DAILY and "身体" in text, text[:60])

    layer_g, text_g = inject.build_text(state, config, private=False, chat_type="group")
    check("群里默认不到三档不注入", layer_g == "", f"layer={layer_g!r}")

    state.stool = 128.0
    state.shame = 80.0
    layer_p, text_p = inject.build_text(state, config, private=True, chat_type="private")
    check(
        "私密模式注入完整版",
        layer_p == inject.LAYER_PRIVATE and "私密中" in text_p and "便意" in text_p,
        text_p[:80].replace("\n", " / "),
    )
    check(
        "私密版含档位与羞耻",
        "强烈" in text_p and "羞耻" in text_p,
        "",
    )

    config.inject.disrupt_enabled = True
    _layer, text_d = inject.build_text(state, config, private=True, chat_type="private")
    check("开了干扰开关才出现表达约束", "表达方式" in text_d, "")
    config.inject.disrupt_enabled = False

    # 5) sisp 探测（读不到就应当安静地返回普通模式）
    import asyncio

    from gut_sim import sisp_bridge

    private, store = asyncio.run(sisp_bridge.detect("check-smoke-stream", config))
    check(
        "探测不存在的流 → 普通模式、不抛错",
        private is False and store == "",
        f"private={private} store={store!r}",
    )

    print("-" * 72)
    if _failures:
        print(f"{len(_failures)} 项失败: {_failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
