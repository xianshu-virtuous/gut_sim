"""真实加载验证：让框架的 PluginManager 真的把这个插件目录加载一遍。

比 import 冒烟更狠：会走 manifest 校验 → 模块导入 → 配置生成 → 实例化 → 组件注册
整条链路。用来提前抓"上架后才炸"的问题。

用法（实例 venv，建议在临时目录里跑，避免污染仓库）::

    $env:NEO_ROOT = "F:\\Neo-MoFox-Aemeath"
    & "$env:NEO_ROOT\\.venv\\Scripts\\python.exe" -B <本文件绝对路径>
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEO_ROOT = Path(os.environ.get("NEO_ROOT", r"F:\Neo-MoFox-Aemeath")).resolve()
#: 支持传 .mfp / 目录路径：python check_load.py <路径>
PLUGIN_PATH = (
    Path(sys.argv[1]).resolve()
    if len(sys.argv) > 1
    else Path(__file__).resolve().parent.parent
)

_failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {extra}" if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


async def main() -> int:
    sys.path.insert(0, str(NEO_ROOT))

    print(f"neo 根目录 : {NEO_ROOT}")
    print(f"插件目录   : {PLUGIN_PATH}")
    print(f"工作目录   : {Path.cwd()}（配置与数据会落在这里）")
    print("-" * 72)

    from src.core.components.loader import load_manifest
    from src.core.managers.plugin_manager import get_plugin_manager

    manifest = await load_manifest(str(PLUGIN_PATH))
    check("manifest 可解析", manifest is not None, "" if manifest else "解析失败")
    if manifest is None:
        return 1
    check("插件名 = gut_sim", manifest.name == "gut_sim", manifest.name)
    check(
        "include 里的组件都声明了",
        len(manifest.include) >= 4,
        f"{[(i.component_type, i.component_name) for i in manifest.include]}",
    )

    manager = get_plugin_manager()
    ok = await manager.load_plugin_from_manifest(str(PLUGIN_PATH), manifest)
    check("框架接受并加载插件", ok, "" if ok else "load_plugin_from_manifest 返回 False")

    plugin = manager.get_plugin("gut_sim")
    check("插件实例可取得", plugin is not None, type(plugin).__name__ if plugin else "")

    if plugin is not None:
        check(
            "插件拿到配置实例",
            plugin.config is not None,
            type(plugin.config).__name__ if plugin.config else "",
        )

        components = list(getattr(plugin, "_components", {}) or {})
        print(f"      已注册组件: {components}")

    config_file = Path.cwd() / "config" / "plugins" / "gut_sim" / "config.toml"
    check(
        "配置文件已生成",
        config_file.exists(),
        str(config_file) if config_file.exists() else "未生成",
    )
    if config_file.exists():
        text = config_file.read_text(encoding="utf-8")
        check("配置里有档位段", "tier_1 = 60" in text or "tier_1 = 60.0" in text, "")
        check("配置里有 sisp 联动段", "store_order" in text, "")

    print("-" * 72)
    if _failures:
        print(f"{len(_failures)} 项失败: {_failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
