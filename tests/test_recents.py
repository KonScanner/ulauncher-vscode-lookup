"""Recents loader tests.

Each storage format VS Code has used over the years is covered with a
synthetic fixture so a future VS Code migration that breaks one path
fails loudly instead of silently producing zero results.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest


def _write_state_db(path: Path, recents: dict[str, Any] | None) -> None:
    """Build a state.vscdb shaped like VS Code's globalStorage SQLite file."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        if recents is not None:
            con.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                ("history.recentlyOpenedPathsList", json.dumps(recents)),
            )
        con.commit()
    finally:
        con.close()


def _menubar_storage(folders: list[str], files: list[str]) -> dict[str, Any]:
    """Mimic the modern lastKnownMenubarData cache in storage.json."""
    items: list[dict[str, Any]] = [
        {"id": "workbench.action.reopenClosedEditor"},
        {"id": "vscode.menubar.separator"},
    ]
    for folder in folders:
        items.append(
            {
                "id": "openRecentFolder",
                "label": folder.replace("file://", ""),
                "uri": {"$mid": 1, "external": folder, "scheme": "file"},
            }
        )
    items.append({"id": "vscode.menubar.separator"})
    for file in files:
        items.append(
            {
                "id": "openRecentFile",
                "label": file.replace("file://", ""),
                "uri": {"$mid": 1, "external": file, "scheme": "file"},
            }
        )
    return {
        "lastKnownMenubarData": {
            "menus": {
                "File": {
                    "items": [
                        {"id": "workbench.action.files.newUntitledFile"},
                        {
                            "id": "submenuitem.MenubarRecentMenu",
                            "label": "Open &&Recent",
                            "submenu": {"items": items},
                        },
                    ]
                }
            }
        }
    }


# --- _parse_entries: legacy openedPathsList / state.vscdb shape ---


def test_parse_entries_handles_folder_file_and_workspace(code_module):
    entries = [
        {"folderUri": "file:///home/u/proj", "label": "proj"},
        {"fileUri": "file:///home/u/notes.md"},
        {"workspace": {"configPath": "file:///home/u/multi.code-workspace"}},
    ]
    recents = code_module.Code._parse_entries(entries)
    assert [(r.icon, r.option, r.uri, r.label) for r in recents] == [
        ("folder", "--folder-uri", "file:///home/u/proj", "proj"),
        ("file", "--file-uri", "file:///home/u/notes.md", "notes.md"),
        (
            "workspace",
            "--file-uri",
            "file:///home/u/multi.code-workspace",
            "multi.code-workspace",
        ),
    ]


def test_parse_entries_skips_unrecognized(code_module):
    entries = [{"weirdField": "something"}, {"folderUri": "file:///x"}]
    recents = code_module.Code._parse_entries(entries)
    assert len(recents) == 1
    assert recents[0].uri == "file:///x"


# --- _load_state_db: legacy SQLite path ---


def test_load_state_db_returns_entries(code_module, tmp_path):
    db = tmp_path / "state.vscdb"
    _write_state_db(
        db,
        {"entries": [{"folderUri": "file:///home/u/legacy"}]},
    )
    recents = code_module.Code._load_state_db(db)
    assert len(recents) == 1
    assert recents[0].uri == "file:///home/u/legacy"


def test_load_state_db_missing_key_returns_empty(code_module, tmp_path):
    """Modern VS Code: SQLite exists but no longer holds the recents key."""
    db = tmp_path / "state.vscdb"
    _write_state_db(db, recents=None)
    assert code_module.Code._load_state_db(db) == []


# --- _load_storage_json: legacy openedPathsList vs modern menubar cache ---


def test_load_storage_json_legacy_openedPathsList(code_module, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text(
        json.dumps(
            {
                "openedPathsList": {
                    "entries": [
                        {"folderUri": "file:///home/u/old", "label": "old"},
                    ]
                }
            }
        )
    )
    recents = code_module.Code._load_storage_json(storage)
    assert len(recents) == 1
    assert recents[0].label == "old"


def test_load_storage_json_modern_menubar(code_module, tmp_path):
    """Regression for the bug we just fixed: only `lastKnownMenubarData` exists."""
    storage = tmp_path / "storage.json"
    storage.write_text(
        json.dumps(
            _menubar_storage(
                folders=["file:///home/u/proj-a", "file:///home/u/proj-b"],
                files=["file:///home/u/script.py"],
            )
        )
    )
    recents = code_module.Code._load_storage_json(storage)
    assert [(r.icon, r.uri) for r in recents] == [
        ("folder", "file:///home/u/proj-a"),
        ("folder", "file:///home/u/proj-b"),
        ("file", "file:///home/u/script.py"),
    ]


def test_load_storage_json_legacy_takes_precedence_over_menubar(code_module, tmp_path):
    """If both shapes exist, the authoritative legacy list wins over the menu cache."""
    storage = tmp_path / "storage.json"
    payload = _menubar_storage(folders=["file:///cached"], files=[])
    payload["openedPathsList"] = {"entries": [{"folderUri": "file:///authoritative"}]}
    storage.write_text(json.dumps(payload))
    recents = code_module.Code._load_storage_json(storage)
    assert len(recents) == 1
    assert recents[0].uri == "file:///authoritative"


def test_load_storage_json_empty_returns_empty(code_module, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text("{}")
    assert code_module.Code._load_storage_json(storage) == []


# --- _parse_menubar: edge cases on the menu shape ---


def test_parse_menubar_skips_separators_and_actions(code_module):
    storage = _menubar_storage(folders=["file:///a"], files=["file:///b"])
    # Slip in extra non-recent items the way real VS Code does.
    submenu = storage["lastKnownMenubarData"]["menus"]["File"]["items"][1]["submenu"]
    submenu["items"].extend(
        [
            {"id": "workbench.action.openRecent", "label": "&&More..."},
            {"id": "workbench.action.clearRecentFiles"},
        ]
    )
    recents = code_module.Code._parse_menubar(storage)
    assert [r.icon for r in recents] == ["folder", "file"]


def test_parse_menubar_falls_back_to_path_when_external_missing(code_module):
    storage = {
        "lastKnownMenubarData": {
            "menus": {
                "File": {
                    "items": [
                        {
                            "id": "submenuitem.MenubarRecentMenu",
                            "submenu": {
                                "items": [
                                    {
                                        "id": "openRecentFolder",
                                        "label": "x",
                                        "uri": {"$mid": 1, "path": "/home/u/x"},
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }
    recents = code_module.Code._parse_menubar(storage)
    assert recents[0].uri == "/home/u/x"


def test_parse_menubar_drops_items_with_no_uri(code_module):
    storage = {
        "lastKnownMenubarData": {
            "menus": {
                "File": {
                    "items": [
                        {
                            "id": "submenuitem.MenubarRecentMenu",
                            "submenu": {"items": [{"id": "openRecentFolder", "label": "broken"}]},
                        }
                    ]
                }
            }
        }
    }
    assert code_module.Code._parse_menubar(storage) == []


def test_parse_menubar_no_recent_submenu(code_module):
    """File menu exists but has no Recent submenu — nothing to return."""
    storage = {
        "lastKnownMenubarData": {
            "menus": {"File": {"items": [{"id": "workbench.action.files.newFile"}]}}
        }
    }
    assert code_module.Code._parse_menubar(storage) == []


# --- load_recents orchestrator: fallback chain ---


def test_load_recents_prefers_state_db_when_populated(code_module, tmp_path):
    db = tmp_path / "state.vscdb"
    _write_state_db(db, {"entries": [{"folderUri": "file:///from-db"}]})
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps(_menubar_storage(folders=["file:///from-storage"], files=[])))
    recents = code_module.Code.load_recents(db, storage)
    assert [r.uri for r in recents] == ["file:///from-db"]


def test_load_recents_falls_back_to_storage_when_db_empty(code_module, tmp_path):
    """The exact failure mode hit on modern VS Code: DB exists but empty key."""
    db = tmp_path / "state.vscdb"
    _write_state_db(db, recents=None)
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps(_menubar_storage(folders=["file:///from-menubar"], files=[])))
    recents = code_module.Code.load_recents(db, storage)
    assert [r.uri for r in recents] == ["file:///from-menubar"]


def test_load_recents_no_sources_returns_empty(code_module, tmp_path):
    assert code_module.Code.load_recents(None, None) == []
    assert code_module.Code.load_recents(tmp_path / "missing.vscdb", None) == []


def test_load_recents_state_db_missing_storage_only(code_module, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps(_menubar_storage(folders=["file:///only"], files=[])))
    recents = code_module.Code.load_recents(None, storage)
    assert [r.uri for r in recents] == ["file:///only"]


# --- get_recents: caching behavior ---


@pytest.fixture
def code_instance(code_module):
    """Build a Code without invoking _locate(), so we control its paths."""
    code = code_module.Code.__new__(code_module.Code)
    code.installed_path = Path("/usr/bin/code")
    code.global_state_db = None
    code.storage_json = None
    code._cache = []
    code._cache_key = None
    return code


def test_get_recents_caches_until_mtime_changes(code_instance, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps(_menubar_storage(folders=["file:///v1"], files=[])))
    code_instance.storage_json = storage

    first = code_instance.get_recents()
    assert [r.uri for r in first] == ["file:///v1"]

    storage.write_text(json.dumps(_menubar_storage(folders=["file:///v2"], files=[])))
    new_time = storage.stat().st_mtime + 5
    os.utime(storage, (new_time, new_time))

    second = code_instance.get_recents()
    assert [r.uri for r in second] == ["file:///v2"]


def test_get_recents_returns_empty_when_no_sources(code_instance):
    assert code_instance.get_recents() == []
