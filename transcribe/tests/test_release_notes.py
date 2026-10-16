"""Release notes in the review page: CHANGELOG.md parsed, the notes since the version last seen (as markdownz does),
the small state file next to recordingz.toml, the API, and CHANGELOG.md shipped with the installed package."""
import tomllib
from pathlib import Path

from recordingz.web import release_notes as rn

from helpers import _call, _serve

REPO = Path(__file__).resolve().parents[2]
TEXT = """# Changes

Intro, not a release.

## 1.10.0 – 2026-10-20

- Ten, wrapped
  onto a second line.

## 1.2.0 – 2026-10-10

- Two.

## 1.9.0 – 2026-10-15

Nine.
"""
NOTES = rn.parse(TEXT)


def test_parse_headings_newest_first_by_version():
    assert [(n["version"], n["date"]) for n in NOTES] == [("1.10.0", "2026-10-20"), ("1.9.0", "2026-10-15"),
                                                          ("1.2.0", "2026-10-10")]
    assert NOTES[0]["markdown"] == "- Ten, wrapped\n  onto a second line."
    assert NOTES[1]["markdown"] == "Nine."
    assert rn.parse("## 2.0.0 - 2026-10-15\n\n- a hyphen, not an en dash\n") == []


def test_the_changelog_is_found_in_the_checkout_with_this_version():
    pyproject = tomllib.loads((REPO / "transcribe/pyproject.toml").read_text(encoding="utf-8"))
    assert rn.running_version() == pyproject["project"]["version"], "the checkout's, not a stale editable install's"
    assert rn.changelog_path() == REPO / "CHANGELOG.md"
    notes = rn.load()
    assert notes[0]["version"] == rn.running_version() and notes[0]["markdown"], "write the notes for this version"


def test_the_installed_suite_carries_the_changelog_where_the_package_looks():
    # installer/stage-suite.ps1 copies it into <package>/docs, the first place release_notes looks
    script = (REPO / "installer/stage-suite.ps1").read_text(encoding="utf-8")
    assert 'Copy-Item (Join-Path $root "CHANGELOG.md") $pkgDocs' in script
    assert rn.CHANGELOG_PLACES[0] == Path(rn.__file__).resolve().parents[1] / "docs" / "CHANGELOG.md"


def test_since_the_version_last_seen():
    versions = lambda seen, cur: [n["version"] for n in rn.since(NOTES, seen, cur)]
    assert versions(None, "1.10.0") == ["1.10.0"], "a fresh install: only the running version"
    assert versions("1.2.0", "1.10.0") == ["1.10.0", "1.9.0"], "an update over two versions"
    assert versions("1.2.0", "1.9.0") == ["1.9.0"], "not newer than what runs"
    assert versions("1.10.0", "1.10.0") == []
    assert versions("1.10.0", "1.9.0") == [], "a downgrade"
    assert versions("1.9.0", "1.9.5") == [], "a version without notes"
    assert versions(None, "0.0.0") == [] and versions(None, "dev") == []


def test_the_seen_state_file(tmp_path):
    cfg = tmp_path / "recordingz.toml"
    assert rn.read_seen(cfg) is None
    assert rn.mark_seen("1.9.0", cfg) == "1.9.0"
    assert (tmp_path / rn.SEEN_FILE).read_text(encoding="utf-8") == "1.9.0\n"
    assert not cfg.exists(), "not a setting in recordingz.toml"
    assert rn.mark_seen("1.2.0", cfg) == "1.9.0", "a downgrade keeps the newer version seen"
    assert rn.mark_seen("1.10.0", cfg) == "1.10.0"
    (tmp_path / rn.SEEN_FILE).write_text("garbage", encoding="utf-8")
    assert rn.read_seen(cfg) is None, "an unreadable state counts as nothing seen"


def test_release_notes_api(tmp_path):
    srv, state, base = _serve(tmp_path)
    state.settings_path = tmp_path / "recordingz.toml"
    try:
        code, r = _call(base, "GET", "/api/release-notes")
        assert code == 200 and r["version"] == rn.running_version()
        assert r["notes"] == rn.load()
        assert [n["version"] for n in r["unseen"]] == [rn.running_version()], "first run: the running version's notes"
        code, r = _call(base, "POST", "/api/release-notes/seen", {})
        assert code == 200 and r == {"ok": True, "seen": rn.running_version()}
        assert rn.read_seen(state.settings_path) == rn.running_version()
        assert _call(base, "GET", "/api/release-notes")[1]["unseen"] == []
    finally:
        srv.shutdown()
