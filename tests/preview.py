"""推演预览：不碰存储、不调模型，纯内存跑一遍数值并打印注入文本。

用来肉眼检查文案手感（尤其私密模式那段话读起来像不像"身体的感觉"）。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/preview.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (_NEO_ROOT, PLUGIN_REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gut_sim import inject  # noqa: E402
from gut_sim import state as gut  # noqa: E402
from gut_sim.config import GutSimConfig  # noqa: E402


def main() -> int:
    config = GutSimConfig()
    # 为了看清完整链路，把三档开关都打开
    config.shame.enabled = True
    config.shame.wetting_enabled = True
    config.shame.leak_enabled = True
    config.inject.disrupt_enabled = True

    state = gut.GutState(gas_ratio=2.6, base_rate=1.0, shy_mult=1.3)

    print("=" * 78)
    print("普通模式：每 6 轮看一次")
    print("=" * 78)
    for i in range(1, 25):
        report = gut.advance(state, config, rounds=1, token_units=180.0, private=False)
        if i % 6 != 0:
            continue
        layer, text = inject.build_text(state, config, private=False, chat_type="private")
        print(f"\n— 第 {i} 轮 —")
        print(f"  {gut.snapshot(state, config)}")
        print(f"  注入层：{layer or '（不注入）'}")
        if text:
            print("  " + text.replace("\n", "\n  "))

    print()
    print("=" * 78)
    print("私密模式：从第 1 轮开始逐轮看")
    print("=" * 78)
    state2 = gut.GutState(gas_ratio=2.6, base_rate=1.0, shy_mult=1.3)
    for i in range(1, 14):
        report = gut.advance(state2, config, rounds=1, token_units=320.0, private=True)
        if report.notes:
            print(f"  [第 {i} 轮事件] {'；'.join(report.notes)}")
        if i not in (2, 5, 9, 13):
            continue
        layer, text = inject.build_text(state2, config, private=True, chat_type="private")
        print(f"\n— 第 {i} 轮 —")
        print(f"  {gut.snapshot(state2, config)}")
        print(f"  注入层：{layer}")
        print("  " + text.replace("\n", "\n  "))

    print()
    print("=" * 78)
    print("群里（默认阈值三档才注入）")
    print("=" * 78)
    layer, text = inject.build_text(state, config, private=False, chat_type="group")
    print(f"  便意 {state.stool:.0f}｜注入层：{layer or '（不注入）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
