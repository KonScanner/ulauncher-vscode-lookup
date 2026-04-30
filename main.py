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
VARIANTS: Final[tuple[str, ...]] = ("Code", "VSCodium", "Code - OSS")

LABEL_MATCH_THRESHOLD: Final[int] = 60
URI_MATCH_THRESHOLD: Final[int] = 60
MAX_RESULTS: Final[int] = 20
RECENTS_KEY: Final[str] = "history.recentlyOpenedPathsList"


def icon_for(name: str) -> str:
    return str(IMAGES_DIR / f"{name}.svg")


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
        self._cache: list[Recent] = []
        self._cache_key: tuple[Path, float] | None = None
        self._locate()

    def _locate(self) -> None:
        for variant in VARIANTS:
            config_path = Path.home() / ".config" / variant
            global_storage = config_path / "User" / "globalStorage"
            if not global_storage.is_dir():
                continue
            installed = self._find_executable(variant)
            if installed is None:
                continue
            self.installed_path = installed
            self.global_state_db = global_storage / "state.vscdb"
            self.storage_json = global_storage / "storage.json"
            logger.debug(
                "located %s at %s with config %s", variant, installed, config_path
            )
            return
        logger.warning("Unable to find a VS Code installation or config directory")

    @staticmethod
    def _find_executable(variant: str) -> Path | None:
        binary = variant.lower()
        for path_dir in SEARCH_PATHS:
            candidate = path_dir / binary
            if candidate.exists():
                return candidate
        on_path = shutil.which(binary)
        return Path(on_path) if on_path else None

    def is_installed(self) -> bool:
        return self.installed_path is not None

    def get_recents(self) -> list[Recent]:
        source = self._active_source()
        if source is None:
            return []
        try:
            mtime = source.stat().st_mtime
        except OSError:
            return list(self._cache)

        cache_key = (source, mtime)
        if self._cache_key == cache_key and self._cache:
            return self._cache

        try:
            recents = self._load(source)
        except Exception:
            logger.exception("failed to load recents from %s", source)
            return list(self._cache)

        self._cache = recents
        self._cache_key = cache_key
        return recents

    def _active_source(self) -> Path | None:
        if self.global_state_db and self.global_state_db.exists():
            return self.global_state_db
        if self.storage_json and self.storage_json.exists():
            return self.storage_json
        return None

    def _load(self, source: Path) -> list[Recent]:
        if source.suffix == ".vscdb":
            return self._load_state_db(source)
        return self._load_storage_json(source)

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
        entries = data.get("openedPathsList", {}).get("entries", [])
        return Code._parse_entries(entries)

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

    def open_vscode(
        self, recent: dict[str, str], excluded_env_vars: str | None
    ) -> None:
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
                        description=(
                            "Can't find a VS Code, VSCodium, or Code - OSS installation."
                        ),
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
    def on_event(
        self, event: PreferencesUpdateEvent, extension: CodeExtension
    ) -> None:
        if event.id == "code_kw":
            extension.keyword = event.new_value
        elif event.id == "excluded_env_vars":
            extension.excluded_env_vars = event.new_value


if __name__ == "__main__":
    CodeExtension().run()
