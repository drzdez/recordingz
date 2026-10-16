"""The installer sources: a quit command written through a script once got a TAB where a backslash and "t" were meant
(`Programs<TAB>eamsrec-capture`), so the update never asked teamsrec 1.x to quit."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_installer_sources_have_no_stray_control_characters():
    for path in sorted((REPO / "installer").glob("*.wx[sl]")):
        text = path.read_text(encoding="utf-8")
        bad = sorted({hex(ord(c)) for c in text if ord(c) < 32 and c not in "\r\n"})
        assert not bad, f"{path.name}: {bad}"


def test_the_quit_command_also_finds_teamsrec_1x():
    text = (REPO / "installer/Package.wxs").read_text(encoding="utf-8")
    assert r"[LocalAppDataFolder]Programs\teamsrec-capture" in text
