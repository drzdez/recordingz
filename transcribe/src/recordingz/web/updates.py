"""Nápověda → O aplikaci → Zkontrolovat aktualizace: the capture app (the tray) checks GitHub and installs the MSI;
this server only passes the page's request on in a file and reads the answer back (capture UpdateRequest.cs)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..fileio import atomic_write_text

UPDATE_REQUEST = Path(tempfile.gettempdir()) / "recordingz-update.request"  # "check" or "install", read by the app
UPDATE_STATE = Path(tempfile.gettempdir()) / "recordingz-update.json"      # its answer
ACTIONS = ("check", "install")
NOT_RUNNING = "aplikace recordingz pro nahrávání neběží – spusťte ji (nabídka Start), aktualizace hledá a instaluje ona"


def state(capture: dict) -> dict:
    """What the page shows: whether the capture app runs, and its last answer (none yet: state "")."""
    try:
        d = json.loads(UPDATE_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    keep = ("state", "current", "available", "message", "at")
    return {"running": bool(capture.get("running")), **{k: d.get(k) for k in keep}}


def request(action: str, capture: dict) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"neznámá akce {action!r}")
    if not capture.get("running"):
        raise ValueError(NOT_RUNNING)
    atomic_write_text(UPDATE_REQUEST, action)
    return {"ok": True, "asked": action}
