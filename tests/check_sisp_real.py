"""用真实 sisp 状态文件验证兼容层：能读到 in_sex_mode 就算打通。

前置：把实例里 ``data/json_storage/sisp/<hash>.json`` 复制到当前工作目录的同名路径。
这里只读 ``in_sex_mode`` 一个布尔，不打印 sisp 的任何其他内容。

用法::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B <本文件绝对路径>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
PLUGIN_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_REPO))

STORE_DIR = Path.cwd() / "data" / "json_storage" / "sisp"

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


async def main() -> int:
    from gut_sim import sisp_bridge
    from gut_sim.config import GutSimConfig

    config = GutSimConfig()

    files = sorted(STORE_DIR.glob("*.json")) if STORE_DIR.exists() else []
    if not files:
        print(f"[SKIP] 没有找到真实 sisp 状态文件：{STORE_DIR}")
        print("       先复制一份过来再跑：")
        print("       copy <实例>/data/json_storage/sisp/<hash>.json 到这里")
        return 0

    print(f"找到 {len(files)} 份 sisp 状态，用第一份验证")
    target = files[0]
    stream_id = target.stem

    raw = json.loads(target.read_text(encoding="utf-8"))
    target_private = bool(raw.get("in_sex_mode")) if isinstance(raw, dict) else None
    print(f"      sisp 文件里 in_sex_mode = {target_private}")

    # 1) 探测：市场版（store = sisp）
    private, store = await sisp_bridge.detect(stream_id, config)
    check(
        "读到市场版 sisp 的 in_sex_mode",
        store == "sisp",
        f"store={store!r} private={private}",
    )
    check(
        "读到的值与文件一致",
        private == target_private,
        f"读到 {private}，文件里 {target_private}",
    )

    # 2) 缓存：短时间内不重复读盘也能拿到同样结果
    private2, store2 = await sisp_bridge.detect(stream_id, config)
    check(
        "二次探测命中缓存且结果一致",
        (private2, store2) == (private, store),
        f"{private2}/{store2}",
    )

    # 3) 缓存失效后重读
    sisp_bridge.invalidate(stream_id)
    private3, store3 = await sisp_bridge.detect(stream_id, config)
    check(
        "清缓存后重读结果一致",
        (private3, store3) == (private, store),
        f"{private3}/{store3}",
    )

    # 4) 未知流不应该被误判
    private4, store4 = await sisp_bridge.detect("0" * 64, config)
    check("未知流 → 普通模式", private4 is False and store4 == "", f"{private4}/{store4}")

    # 5) deep_neo_sisp 也应该能被探测到（构造一份只有 in_sex_mode 的最小状态）
    deep_dir = Path.cwd() / "data" / "json_storage" / "deep_neo_sisp"
    deep_dir.mkdir(parents=True, exist_ok=True)
    deep_stream = "d" * 64
    (deep_dir / f"{deep_stream}.json").write_text(
        json.dumps({"in_sex_mode": True}), encoding="utf-8"
    )
    sisp_bridge.invalidate(deep_stream)
    private_deep, store_deep = await sisp_bridge.detect(deep_stream, config)
    check(
        "deep_neo_sisp 也能读到私密模式",
        store_deep == "deep_neo_sisp" and private_deep is True,
        f"store={store_deep!r} private={private_deep}",
    )

    # 6) 关掉联动开关后永远普通模式
    config.sisp.enabled = False
    private5, store5 = await sisp_bridge.detect(stream_id, config)
    check("关闭联动后不再探测", private5 is False and store5 == "", f"{private5}/{store5}")

    print("-" * 72)
    if _failures:
        print(f"{len(_failures)} 项失败: {_failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
