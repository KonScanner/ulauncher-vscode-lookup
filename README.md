# ulauncher-vscode-lookup

> Fuzzy-search and open recently-used VS Code, VSCodium, and Code-OSS folders, files and workspaces from [Ulauncher](https://ulauncher.io/).

A modernized fork of [`plibither8/ulauncher-vscode-recent`](https://github.com/plibither8/ulauncher-vscode-recent), updated for Python 3.12+ with `rapidfuzz`, type hints, mtime-cached recents, broader install-path detection, and a stack of bug fixes.

<p align="center"><img src="images/icon.svg" alt="preview" width="120"></p>

## What's different from upstream

| | Upstream | This fork |
|---|---|---|
| Python | 3.6+ | **3.12+** |
| Fuzzy lib | `fuzzywuzzy` (slow, GPL) | **`rapidfuzz`** (fast, MIT, C++) |
| Recents source | `storage.json` only | `state.vscdb` **and** `storage.json` |
| Install paths | `/usr/bin`, `/bin`, `/snap/bin` | + `/usr/local/bin`, `~/.local/bin`, `$PATH` fallback |
| Variants | `Code`, `VSCodium` | + `Code - OSS` |
| `subprocess` | blocking `run` | non-blocking `Popen(start_new_session=True)` |
| Caching | re-reads file every keystroke | mtime-keyed in-memory cache |
| Types / linting | none | `ty` + `ruff`, full type hints, `dataclasses`, `match`/`case` |
| Bugs fixed | — | `None`-deref on empty recents, sqlite/file-handle leaks, mis-formed `logger.error`, empty `option` arg, missing `state.vscdb` detection |

## Install

### Requirements

- [Ulauncher 5](https://ulauncher.io/) (API v2)
- Python **3.12+**
- [`rapidfuzz`](https://pypi.org/project/rapidfuzz/) — `pip install --user rapidfuzz`

### Via Ulauncher UI

1. Ulauncher → **Preferences → Extensions → Add extension**
2. Paste:

   ```
   https://github.com/KonScanner/ulauncher-vscode-lookup
   ```

Ulauncher reads [`versions.json`](versions.json), checks out the matching `commit`, and pulls the extension into `~/.local/share/ulauncher/extensions/`.

> **Note** — Ulauncher < 5.15.0 expected the default branch to be named `master`. If you need to support that, push a `master` branch alongside `main` or update [`versions.json`](versions.json) to point at a tag.

## Usage

Default trigger keyword: **`code`** (configurable in Ulauncher preferences).

| Preference          | Description                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| `code_kw`           | Trigger keyword.                                                                                     |
| `excluded_env_vars` | Comma-separated environment variables stripped before launching VS Code (e.g. `PYTHONPATH,VIRTUAL_ENV`). |

Typing `code <query>` fuzzy-matches against labels and URIs of your recents. The first result is always your raw input — useful for opening arbitrary paths VS Code hasn't seen yet.

## Development

### Running locally against Ulauncher

The supported workflow is to symlink your checkout into Ulauncher's extensions directory and start Ulauncher in dev mode:

```bash
ln -sf "$PWD" ~/.local/share/ulauncher/extensions/com.github.konscanner.ulauncher-vscode-lookup
ulauncher --no-extensions --dev -v
```

Then in another terminal launch the extension itself (Ulauncher prints the exact `VERBOSE=1 ULAUNCHER_WS_API=... PYTHONPATH=... /usr/bin/python3 main.py` command on startup — copy and run it).

Restart with `Ctrl+C` and re-run after edits.

### Linting & type-checking

Install [`ruff`](https://docs.astral.sh/ruff/) and [`ty`](https://docs.astral.sh/ty/) however you prefer (`pipx`, system pip, `uv tool install`), then:

```bash
make ruff        # lint
make typecheck   # static type-check with ty
```

## Publishing your fork

This repo is already in publishable shape. To cut a release:

1. Bump `version` in [`pyproject.toml`](pyproject.toml).
2. Tag the commit: `git tag v1.0.0 && git push --tags`.
3. (Optional) Update [`versions.json`](versions.json) to point at the tag instead of `main` so Ulauncher pins to a specific revision:

   ```json
   [{ "required_api_version": "^2.0.0", "commit": "v1.0.0" }]
   ```
4. Anyone can then install via the Ulauncher UI using the repo URL.

There is no central extension registry; discoverability comes from [the community list](https://ext.ulauncher.io/) (submit via PR to that repo) and from search.

### Future: Ulauncher 6 / API v3

Ulauncher 6 renames `required_api_version` → `api_version` and adjusts a few event signatures. When you're ready to support v6, add a second entry to [`versions.json`](versions.json) pointing at a `v6` branch:

```json
[
  { "required_api_version": "^2.0.0", "commit": "main" },
  { "api_version":          "3",      "commit": "v6"   }
]
```

## License

Source code: [MIT](LICENSE).
Icons: [vscode-icons](https://github.com/vscode-icons/vscode-icons), CC BY-SA.
