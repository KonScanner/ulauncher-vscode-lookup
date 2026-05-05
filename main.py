"""Ulauncher extension: open recently-used VS Code folders, files and workspaces."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from rapidfuzz import fuzz, process, utils
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.client.Extension import Extension
from ulauncher.api.shared.action.ExtensionCustomAction import ExtensionCustomAction
from ulauncher.api.shared.action.HideWindowAction import HideWindowAction
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.event import (
    ItemEnterEvent,
    KeywordQueryEvent,
    PreferencesEvent,
    PreferencesUpdateEvent,
)
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.item.ExtensionSmallResultItem import ExtensionSmallResultItem

logger = logging.getLogger(__name__)

EXTENSION_DIR: Final[Path] = Path(__file__).parent.resolve()
IMAGES_DIR: Final[Path] = EXTENSION_DIR / "images"

SEARCH_PATHS: Final[tuple[Path, ...]] = (
    Path("/usr/bin"),
    Path("/usr/local/bin"),
    Path("/bin"),
    Path("/snap/bin"),
    Path.home() / ".local" / "bin",
)
VARIANTS: Final[tuple[tuple[str, str], ...]] = (
    ("Code", "code"),
    ("Code - Insiders", "code-insiders"),
    ("VSCodium", "codium"),
    ("Code - OSS", "code-oss"),
)

LABEL_MATCH_THRESHOLD: Final[int] = 60
URI_MATCH_THRESHOLD: Final[int] = 60
MAX_RESULTS: Final[int] = 20
RECENTS_KEY: Final[str] = "history.recentlyOpenedPathsList"
RECENT_MENU_ID: Final[str] = "submenuitem.MenubarRecentMenu"


def icon_for(name: str) -> str:
    return str(IMAGES_DIR / f"{name}.svg")


def _pretty_label(uri: str) -> str:
    """Render a URI the way VS Code's File → Open Recent menu shows it."""
    decoded = urllib.parse.unquote(uri)
    if decoded.startswith("file://"):
        path = decoded[len("file://") :]
        home = str(Path.home())
        if path == home or path.startswith(home + "/"):
            return "~" + path[len(home) :]
        return path
    if decoded.startswith("vscode-remote://"):
        rest = decoded[len("vscode-remote://") :]
        host, sep, path = rest.partition("/")
        return f"[{host}] {sep}{path}" if sep else f"[{host}]"
    return decoded


@dataclass(frozen=True, slots=True)
class Recent:
    uri: str
    label: str
    icon: str
    option: str

    @property
    def display_name(self) -> str:
        return urllib.parse.unquote(self.label)

    def to_dict(self) -> dict[str, str]:
        return {
            "uri": self.uri,
            "label": self.label,
            "icon": self.icon,
            "option": self.option,
        }


class Code:
    """Locate a VS Code installation and read its recent-projects list."""

    def __init__(self) -> None:
        self.installed_path: Path | None = None
        self.global_state_db: Path | None = None
        self.storage_json: Path | None = None
        self.workspace_storage_dir: Path | None = None
        self._cache: list[Recent] = []
        self._cache_key: tuple[tuple[Path, float], ...] | None = None
        self._locate()

    def _locate(self) -> None:
        for variant, binary in VARIANTS:
            config_path = Path.home() / ".config" / variant
            global_storage = config_path / "User" / "globalStorage"
            if not global_storage.is_dir():
                continue
            installed = self._find_executable(binary)
            if installed is None:
                continue
            self.installed_path = installed
            self.global_state_db = global_storage / "state.vscdb"
            self.storage_json = global_storage / "storage.json"
            self.workspace_storage_dir = config_path / "User" / "workspaceStorage"
            logger.debug("located %s at %s with config %s", variant, installed, config_path)
            return
        logger.warning("Unable to find a VS Code installation or config directory")

    @staticmethod
    def _find_executable(binary: str) -> Path | None:
        for path_dir in SEARCH_PATHS:
            candidate = path_dir / binary
            if candidate.exists():
                return candidate
        on_path = shutil.which(binary)
        return Path(on_path) if on_path else None

    def is_installed(self) -> bool:
        return self.installed_path is not None

    def get_recents(self) -> list[Recent]:
        cache_key = self._cache_signature()
        if cache_key is None:
            return []
        if self._cache_key == cache_key and self._cache:
            return self._cache

        try:
            recents = self.load_recents(
                self.global_state_db, self.storage_json, self.workspace_storage_dir
            )
        except Exception:
            logger.exception("failed to load recents")
            return list(self._cache)

        self._cache = recents
        self._cache_key = cache_key
        return recents

    def _cache_signature(self) -> tuple[tuple[Path, float], ...] | None:
        parts: list[tuple[Path, float]] = []
        for path in (self.global_state_db, self.storage_json, self.workspace_storage_dir):
            if path is None or not path.exists():
                continue
            try:
                parts.append((path, path.stat().st_mtime))
            except OSError:
                continue
        return tuple(parts) if parts else None

    @staticmethod
    def load_recents(
        state_db: Path | None,
        storage_json: Path | None,
        workspace_storage_dir: Path | None,
    ) -> list[Recent]:
        """Load recents from whichever source VS Code is actually using.

        Order:
        1. Legacy `state.vscdb` key — authoritative on older VS Code builds.
        2. Per-workspace `workspaceStorage/<hash>/workspace.json` — the most
           accurate modern source: complete folder list, recency-sorted by
           directory mtime. Augmented with file recents from the menubar
           cache, since `workspaceStorage` only tracks folders/workspaces.
        3. `storage.json` fallback — legacy `openedPathsList` or the
           truncated `lastKnownMenubarData` menu cache (last 10 of each).
        """
        if state_db is not None and state_db.exists():
            recents = Code._load_state_db(state_db)
            if recents:
                return recents

        folders: list[Recent] = []
        if workspace_storage_dir is not None and workspace_storage_dir.is_dir():
            folders = Code._load_workspace_storage(workspace_storage_dir)

        if storage_json is not None and storage_json.exists():
            from_storage = Code._load_storage_json(storage_json)
            if not folders:
                return from_storage
            files = [r for r in from_storage if r.icon == "file"]
            return folders + files

        return folders

    @staticmethod
    def _load_state_db(path: Path) -> list[Recent]:
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as con:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (RECENTS_KEY,)
            ).fetchone()
        if not row:
            return []
        data = json.loads(row[0])
        return Code._parse_entries(data.get("entries", []))

    @staticmethod
    def _load_storage_json(path: Path) -> list[Recent]:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        legacy = data.get("openedPathsList", {}).get("entries", [])
        if legacy:
            return Code._parse_entries(legacy)
        return Code._parse_menubar(data)

    @staticmethod
    def _load_workspace_storage(directory: Path) -> list[Recent]:
        """List recent folders/workspaces from per-workspace storage dirs.

        VS Code creates `workspaceStorage/<hash>/workspace.json` for every
        workspace it has ever opened. The hash dir's mtime tracks when that
        workspace was last active — that's the recency signal we need.
        Far better than `lastKnownMenubarData`, which is truncated to ~10
        entries and only updated on menubar rebuilds.
        """
        items: list[tuple[float, Recent]] = []
        for entry in directory.iterdir():
            if not entry.is_dir():
                continue
            ws_json = entry / "workspace.json"
            if not ws_json.is_file():
                continue
            try:
                data = json.loads(ws_json.read_text(encoding="utf-8"))
                mtime = entry.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if folder := data.get("folder"):
                uri, icon, option = folder, "folder", "--folder-uri"
            elif config := data.get("configuration"):
                uri, icon, option = config, "workspace", "--file-uri"
            else:
                continue
            items.append(
                (mtime, Recent(uri=uri, label=_pretty_label(uri), icon=icon, option=option))
            )
        items.sort(key=lambda pair: pair[0], reverse=True)
        return [recent for _, recent in items]

    @staticmethod
    def _parse_entries(entries: Iterable[dict[str, Any]]) -> list[Recent]:
        recents: list[Recent] = []
        for entry in entries:
            match entry:
                case {"folderUri": str(uri)}:
                    icon, option = "folder", "--folder-uri"
                case {"fileUri": str(uri)}:
                    icon, option = "file", "--file-uri"
                case {"workspace": {"configPath": str(uri)}}:
                    icon, option = "workspace", "--file-uri"
                case _:
                    logger.warning("unrecognized entry: %s", entry)
                    continue
            label = entry.get("label") or uri.rsplit("/", 1)[-1]
            recents.append(Recent(uri=uri, label=label, icon=icon, option=option))
        return recents

    @staticmethod
    def _parse_menubar(storage: dict[str, Any]) -> list[Recent]:
        """Read recents from VS Code's cached File → Open Recent menu.

        Modern VS Code (≈2025+) no longer writes `history.recentlyOpenedPathsList`
        but keeps a recents-ordered cache here so the menu can render before
        the workbench finishes loading.
        """
        file_menu = (
            storage.get("lastKnownMenubarData", {})
            .get("menus", {})
            .get("File", {})
            .get("items", [])
        )
        submenu: list[dict[str, Any]] = []
        for item in file_menu:
            if item.get("id") == RECENT_MENU_ID:
                submenu = item.get("submenu", {}).get("items", [])
                break

        recents: list[Recent] = []
        for item in submenu:
            match item.get("id"):
                case "openRecentFolder":
                    icon, option = "folder", "--folder-uri"
                case "openRecentFile":
                    icon, option = "file", "--file-uri"
                case "openRecentWorkspace":
                    icon, option = "workspace", "--file-uri"
                case _:
                    continue
            uri_obj = item.get("uri") or {}
            uri = uri_obj.get("external") or uri_obj.get("path")
            if not uri:
                continue
            label = item.get("label") or uri.rsplit("/", 1)[-1]
            recents.append(Recent(uri=uri, label=label, icon=icon, option=option))
        return recents

    def open_vscode(self, recent: dict[str, str], excluded_env_vars: str | None) -> None:
        if self.installed_path is None:
            logger.error("cannot open VS Code: no installation located")
            return
        env = os.environ.copy()
        if excluded_env_vars:
            for var in (v.strip() for v in excluded_env_vars.split(",")):
                if var:
                    env.pop(var, None)

        cmd: list[str] = [str(self.installed_path)]
        if option := recent.get("option"):
            cmd.append(option)
        uri = recent.get("uri", "").strip()
        if not uri:
            logger.error("cannot open VS Code: empty URI")
            return
        cmd.append(uri)

        try:
            subprocess.Popen(  # noqa: S603 - command is a fixed binary path + sanitized args
                cmd,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("failed to launch VS Code: %s", cmd)


class CodeExtension(Extension):
    def __init__(self) -> None:
        super().__init__()
        self.keyword: str | None = None
        self.excluded_env_vars: str | None = None
        self.code = Code()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(ItemEnterEvent, ItemEnterEventListener())
        self.subscribe(PreferencesEvent, PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent, PreferencesUpdateEventListener())

    def get_ext_result_items(self, query: str) -> list[ExtensionResultItem]:
        query_raw = (query or "").strip()
        recents = self.code.get_recents()
        items: list[ExtensionResultItem] = []

        if query_raw:
            items.append(
                ExtensionSmallResultItem(
                    icon=icon_for("icon"),
                    name=query_raw,
                    on_enter=ExtensionCustomAction(
                        {"option": "", "uri": query_raw, "label": query_raw, "icon": "file"}
                    ),
                )
            )

        if not recents:
            return items

        if not query_raw:
            for recent in recents[:MAX_RESULTS]:
                items.append(self._make_result(recent))
            return items

        items.extend(self._fuzzy_match(query_raw, recents))
        return items

    def _fuzzy_match(self, query: str, recents: list[Recent]) -> list[ExtensionResultItem]:
        label_choices = {i: r.label for i, r in enumerate(recents)}
        uri_choices = {i: r.uri for i, r in enumerate(recents)}
        label_matches = process.extract(
            query,
            label_choices,
            scorer=fuzz.partial_ratio,
            processor=utils.default_process,
            limit=MAX_RESULTS,
        )
        uri_matches = process.extract(
            query,
            uri_choices,
            scorer=fuzz.partial_ratio,
            processor=utils.default_process,
            limit=MAX_RESULTS,
        )

        seen: set[int] = set()
        results: list[ExtensionResultItem] = []
        for matches, threshold in (
            (label_matches, LABEL_MATCH_THRESHOLD),
            (uri_matches, URI_MATCH_THRESHOLD),
        ):
            for _, score, idx in matches:
                if score < threshold or idx in seen:
                    continue
                seen.add(idx)
                results.append(self._make_result(recents[idx]))
                if len(results) >= MAX_RESULTS:
                    return results
        return results

    @staticmethod
    def _make_result(recent: Recent) -> ExtensionSmallResultItem:
        return ExtensionSmallResultItem(
            icon=icon_for(recent.icon),
            name=recent.display_name,
            on_enter=ExtensionCustomAction(recent.to_dict()),
        )


class KeywordQueryEventListener(EventListener):
    def on_event(
        self, event: KeywordQueryEvent, extension: CodeExtension
    ) -> RenderResultListAction:
        if not extension.code.is_installed():
            return RenderResultListAction(
                [
                    ExtensionResultItem(
                        icon=icon_for("icon"),
                        name="No VS Code?",
                        description=("Can't find a VS Code, VSCodium, or Code - OSS installation."),
                        highlightable=False,
                        on_enter=HideWindowAction(),
                    )
                ]
            )
        argument = event.get_argument() or ""
        return RenderResultListAction(extension.get_ext_result_items(argument))


class ItemEnterEventListener(EventListener):
    def on_event(self, event: ItemEnterEvent, extension: CodeExtension) -> None:
        recent = event.get_data()
        if not isinstance(recent, dict):
            logger.error("unexpected event payload: %r", recent)
            return
        extension.code.open_vscode(recent, extension.excluded_env_vars)


class PreferencesEventListener(EventListener):
    def on_event(self, event: PreferencesEvent, extension: CodeExtension) -> None:
        prefs = event.preferences or {}
        extension.keyword = prefs.get("code_kw")
        extension.excluded_env_vars = prefs.get("excluded_env_vars")


class PreferencesUpdateEventListener(EventListener):
    def on_event(self, event: PreferencesUpdateEvent, extension: CodeExtension) -> None:
        if event.id == "code_kw":
            extension.keyword = event.new_value
        elif event.id == "excluded_env_vars":
            extension.excluded_env_vars = event.new_value


if __name__ == "__main__":
    CodeExtension().run()
