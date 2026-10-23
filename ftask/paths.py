"""Where ftask keeps its data and configuration, per platform.

Linux / BSD  data    $XDG_DATA_HOME/ftask   (default ~/.local/share/ftask)
             config  $XDG_CONFIG_HOME/ftask (default ~/.config/ftask)
macOS        data and config  ~/Library/Application Support/ftask
Windows      (not supported yet; paths below are a best effort)  %LOCALAPPDATA%\\ftask, %APPDATA%\\ftask

Databases created by pre-release builds lived in a directory called `founder-tasks`. If that database exists and the
new one does not, it is used in place, so an existing board keeps working with no migration step.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "ftask"
LEGACY_APP = "founder-tasks"


def _base(kind: str) -> Path:
    """Platform base directory for `data` or `config` (without the app name)."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if os.name == "nt":
        var, fallback = ("LOCALAPPDATA", "AppData/Local") if kind == "data" else ("APPDATA", "AppData/Roaming")
        return Path(os.environ.get(var) or home / fallback)
    if kind == "data":
        return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")


def data_dir() -> Path:
    new, old = _base("data") / APP, _base("data") / LEGACY_APP
    return old if (old / "tasks.db").exists() and not (new / "tasks.db").exists() else new


def config_dir() -> Path:
    new, old = _base("config") / APP, _base("config") / LEGACY_APP
    return old if old.is_dir() and not new.is_dir() else new


def default_db_path() -> Path:
    env = os.environ.get("FTASK_DB")
    if env:
        return Path(env).expanduser()
    return data_dir() / "tasks.db"


def real_db_path() -> Path:
    """The per-user database, ignoring FTASK_DB (hooks only run for this one; see hooks.py)."""
    return data_dir() / "tasks.db"


def hooks_dir() -> Path:
    return config_dir() / "hooks"
