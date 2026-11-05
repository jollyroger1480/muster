"""Muster paths — where the fleet ledger lives.

Resolved once per process: the active Hermes home's ``plugin-data/muster``
directory. Uses the documented ``hermes_cli.config.get_hermes_home()`` when it
is importable (the same helper core's own plugin code uses) and falls back to
the ``HERMES_HOME`` environment variable / ``~/.hermes``.
"""

from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home  # type: ignore

        home = get_hermes_home()
        if home:
            return Path(str(home))
    except Exception:  # noqa: BLE001 - dashboard/plugin_api may run without core importable
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def muster_data_dir() -> Path:
    path = hermes_home() / "plugin-data" / "muster"
    path.mkdir(parents=True, exist_ok=True)
    return path
