"""肠胃模拟插件包。"""

from __future__ import annotations

__all__ = ["GutSimPlugin"]


def __getattr__(name: str):
    if name == "GutSimPlugin":
        from .plugin import GutSimPlugin

        return GutSimPlugin
    raise AttributeError(name)
