"""Shared fixtures.

Stubs out the `ulauncher.*` runtime so `main.py` is importable on a host
without Ulauncher installed (CI, dev shells). Only the symbols `main.py`
imports at module load time need to exist.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


class _Stub:
    """Stand-in for any ulauncher base class or action our module imports."""

    def __init__(self, *_args: object, **_kwargs: object) -> None: ...

    def subscribe(self, *_args: object, **_kwargs: object) -> None: ...


_ULAUNCHER_MODULES: dict[str, tuple[str, ...]] = {
    "ulauncher.api.client.EventListener": ("EventListener",),
    "ulauncher.api.client.Extension": ("Extension",),
    "ulauncher.api.shared.action.ExtensionCustomAction": ("ExtensionCustomAction",),
    "ulauncher.api.shared.action.HideWindowAction": ("HideWindowAction",),
    "ulauncher.api.shared.action.RenderResultListAction": ("RenderResultListAction",),
    "ulauncher.api.shared.event": (
        "ItemEnterEvent",
        "KeywordQueryEvent",
        "PreferencesEvent",
        "PreferencesUpdateEvent",
    ),
    "ulauncher.api.shared.item.ExtensionResultItem": ("ExtensionResultItem",),
    "ulauncher.api.shared.item.ExtensionSmallResultItem": ("ExtensionSmallResultItem",),
}


def _install_ulauncher_stubs() -> None:
    if "ulauncher" in sys.modules:
        return
    for full_name, classes in _ULAUNCHER_MODULES.items():
        parts = full_name.split(".")
        for i in range(1, len(parts) + 1):
            sub = ".".join(parts[:i])
            sys.modules.setdefault(sub, types.ModuleType(sub))
        module = sys.modules[full_name]
        for cls_name in classes:
            setattr(module, cls_name, type(cls_name, (_Stub,), {}))


_install_ulauncher_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as _main  # noqa: E402


@pytest.fixture
def code_module():
    return importlib.reload(_main)
