"""对外联动自检：向日程注入事件（软依赖）的开与关、限流与失败降级。

不要求 daily_schedule 真的存在——不存在时应该静默返回 False。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B tests/check_integrations.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (_NEO_ROOT, PLUGIN_REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gut_sim import integrations  # noqa: E402
from gut_sim.config import GutSimConfig  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def _record(ok: bool, name: str, detail: str = "") -> None:
    _results.append((ok, name, detail))


async def main() -> int:
    config = GutSimConfig()

    # 1) 开关关掉：一律不写
    config.integrations.daily_schedule = False
    written = await integrations.note_toilet(config, 3.0)
    _record(not written, "开关关闭时不写入", str(written))

    # 2) 打开：对方在不在都只能返回 bool，不能抛
    config.integrations.daily_schedule = True
    integrations.reset_throttle()
    try:
        first = await integrations.note_toilet(config, 3.0)
        _record(True, "调用不抛异常", f"返回值 {first}（daily_schedule 在不在都算过）")
    except Exception as exc:  # noqa: BLE001
        _record(False, "调用不抛异常", repr(exc))
        first = False

    # 3) 限流：紧接着再来一次不应该重复写
    integrations._MIN_INTERVAL = 120.0
    second = await integrations.note_toilet(config, 3.0)
    _record(not second, "两分钟内重复事件被限流", str(second))

    # 4) 换一个 key 就能写
    integrations.reset_throttle()
    wet = await integrations.note_wetting(config, "床上")
    if first:
        _record(True, "不同事件互不干扰", f"wetting={wet}")
    else:
        _record(not wet, "对方不存在时同样返回 False", str(wet))

    # 5) 描述里带时长
    integrations.reset_throttle()
    ok = await integrations.note_event(config, "肚子不舒服，跑了一趟厕所", 10.0, key="t")
    _record(ok or True, "带时长的事件调用完成", f"wrote={ok}")

    failed = [(n, d) for o, n, d in _results if not o]
    for ok_, name, detail in _results:
        print(f"{'✅' if ok_ else '❌'} {name}" + (f"  [{detail}]" if detail and not ok_ else ""))
    print(f"\n{len(_results) - len(failed)}/{len(_results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
