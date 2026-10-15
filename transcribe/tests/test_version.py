"""One version for the whole suite: the capture app's <Version> is the release (the tag v<version>, the MSI); the
Python project and the review window carry the same, and CHANGELOG.md has its notes."""
import json
import re
import tomllib
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HEADING = re.compile(r"^## (\d+\.\d+\.\d+) – (\d{4}-\d{2}-\d{2})$", re.M)


def suite_version() -> str:
    csproj = (REPO / "capture/src/Recordingz.Capture/Recordingz.Capture.csproj").read_text(encoding="utf-8")
    return re.search(r"<Version>([^<]+)</Version>", csproj).group(1)


def test_every_part_carries_the_suite_version():
    v = suite_version()
    assert tomllib.loads((REPO / "transcribe/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] == v
    assert tomllib.loads((REPO / "review/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]["version"] == v
    assert json.loads((REPO / "review/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"] == v


def changelog() -> dict[str, tuple[date, str]]:
    """version -> (date, notes) from CHANGELOG.md; headings are `## 1.2.3 – 2026-10-15`, newest first."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = list(HEADING.finditer(text))
    return {m.group(1): (date.fromisoformat(m.group(2)),
                         text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip())
            for i, m in enumerate(heads)}


def test_the_changelog_has_notes_for_this_version():
    notes = changelog()
    assert suite_version() in notes and notes[suite_version()][1], "write the notes for this version in CHANGELOG.md"
    versions = [tuple(map(int, v.split("."))) for v in notes]
    assert versions == sorted(versions, reverse=True), "newest first"


def test_changelog_headings_parse():
    assert HEADING.findall("## 2.0.0 – 2026-10-15\n\n- x\n") == [("2.0.0", "2026-10-15")]
    assert HEADING.findall("## 2.0.0 - 2026-10-15\n") == [], "an en dash, not a hyphen"
