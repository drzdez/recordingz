"""Release notes in the review page: CHANGELOG.md of the repository (one source for the whole suite), shown once
after an install or update as "what is new since the version last seen", and always in Nápověda → O aplikaci.

The pattern of the user's apps (rule-them-all-z decision 2026-10-15, reference markdownz src/releaseNotes.ts):
the notes newer than the last version seen, up to the running one; after a fresh install (nothing seen yet) only
the running version's. The last seen version is a small file next to recordingz.toml, not a setting in it."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .. import __version__
from ..config import default_config_path
from ..fileio import atomic_write_text

HEADING = re.compile(r"^## (\d+\.\d+\.\d+) – (\d{4}-\d{2}-\d{2})$", re.M)  # tests/test_version.py checks the same
SEEN_FILE = "release-notes-seen"
_HERE = Path(__file__).resolve()
CHANGELOG_PLACES = (_HERE.parents[1] / "docs" / "CHANGELOG.md",  # the installed package (installer/stage-suite.ps1)
                    _HERE.parents[4] / "CHANGELOG.md")  # the repository


def running_version() -> str:
    """The suite's version: in a source checkout pyproject.toml's (an editable install keeps the version it was
    installed with until the next `uv sync`), else the installed package's."""
    pyproject = _HERE.parents[3] / "pyproject.toml"
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return __version__


def changelog_path() -> Path | None:
    return next((p for p in CHANGELOG_PLACES if p.is_file()), None)


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse(text: str) -> list[dict]:
    """[{version, date, markdown}] from the changelog, newest first (by version, whatever the file's order)."""
    heads = list(HEADING.finditer(text))
    notes = [{"version": m.group(1), "date": m.group(2),
              "markdown": text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()}
             for i, m in enumerate(heads)]
    return sorted(notes, key=lambda n: parse_version(n["version"]), reverse=True)


def load() -> list[dict]:
    path = changelog_path()
    return parse(path.read_text(encoding="utf-8")) if path else []


def since(notes: list[dict], seen: str | None, current: str) -> list[dict]:
    """The notes newer than the last one seen, up to the running version; only the running one when nothing was seen
    (a fresh install, or an unreadable state). Nothing after a downgrade (seen is newer than what runs)."""
    cur, last = parse_version(current), parse_version(seen)
    if cur is None:
        return []
    return [n for n in notes if parse_version(n["version"]) <= cur
            and (parse_version(n["version"]) > last if last else parse_version(n["version"]) == cur)]


def seen_path(config_path: Path | None = None) -> Path:
    return (config_path or default_config_path()).parent / SEEN_FILE


def read_seen(config_path: Path | None = None) -> str | None:
    try:
        text = seen_path(config_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if parse_version(text) else None


def mark_seen(current: str, config_path: Path | None = None) -> str:
    """Remember the running version as seen; after a downgrade the newer one stays (its notes were read)."""
    seen = read_seen(config_path)
    if seen and parse_version(current) and parse_version(seen) > parse_version(current):
        current = seen
    p = seen_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, current + "\n")
    return current


def state(current: str, config_path: Path | None = None) -> dict:
    """What the page needs: the running version, every note, and the ones not seen yet."""
    notes = load()
    return {"version": current, "notes": notes, "unseen": since(notes, read_seen(config_path), current)}
