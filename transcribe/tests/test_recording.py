"""Recordings and their files: naming, import, sidecar, config, exports, rename, trash, locks."""

from datetime import datetime
from pathlib import Path

import pytest

from recordingz.config import Config, TranscribeSettings, load_config, with_overrides
from recordingz.export import to_srt, to_txt
from recordingz.importer import derive_metadata
from recordingz.prompt import build_prompt
from recordingz.providers.base import Segment
from recordingz.pipeline import do_transcribe
from recordingz.recording import Recording, RecordingError, is_sidecar, make_stem, resolve_recording, slugify

from helpers import _make_recording, _make_transcribed, _serve, _call


def test_slugify_strips_diacritics_and_limits():
    assert slugify("Týdenní sync: ATLAS / SLA") == "tydenni-sync-atlas-sla"
    assert slugify("!!!") == "recording"
    assert len(slugify("a" * 100)) == 60


def test_make_stem():
    assert make_stem(datetime(2026, 9, 4, 14, 0, 12), "Týdenní sync") == "2026-09-04_1400_tydenni-sync"


def test_teams_name_pattern(tmp_path):
    f = tmp_path / "ATLAS Future Version-20260903_133158-Meeting Recording.mp4"
    f.write_bytes(b"x")
    m = derive_metadata(f, creation_time=datetime(2020, 1, 1))
    assert m.title == "ATLAS Future Version"
    assert m.start == datetime(2026, 9, 3, 13, 31, 58)
    assert m.source == "teams-name"


def test_container_then_file_fallback(tmp_path):
    f = tmp_path / "hlasovka.m4a"
    f.write_bytes(b"x")
    m = derive_metadata(f, creation_time=datetime(2026, 8, 1, 9, 30))
    assert (m.title, m.start, m.source) == ("hlasovka", datetime(2026, 8, 1, 9, 30), "container")
    m = derive_metadata(f, creation_time=None)
    assert m.title == "hlasovka" and m.source == "file" and m.start.year >= 2020


def test_is_sidecar_distinguishes_derived_files():
    assert is_sidecar(Path("2026-09-04_1400_tydenni-sync.json"))
    assert not is_sidecar(Path("2026-09-04_1400_tydenni-sync.transcript.json"))
    assert not is_sidecar(Path("2026-09-04_1400_tydenni-sync.speakers.json"))
    assert not is_sidecar(Path("random.json"))


def test_resolve_by_stem_file_and_path(tmp_path):
    sc = _make_recording(tmp_path)
    assert resolve_recording("2026-09-04_1400_tydenni-sync", tmp_path).stem == "2026-09-04_1400_tydenni-sync"
    assert resolve_recording(sc, tmp_path).title == "Týdenní sync"
    assert resolve_recording(sc.with_name("2026-09-04_1400_tydenni-sync_mix.wav"), tmp_path).stem_path == sc.with_suffix("")
    assert resolve_recording(sc.parent, tmp_path).stem == "2026-09-04_1400_tydenni-sync"  # the folder itself
    rec = Recording.load(sc)
    assert rec.mix_path.name.endswith("_mix.wav")
    assert rec.participants == ["Jana Nováková", "Petr Svoboda"]
    assert rec.transcript_path.name == "2026-09-04_1400_tydenni-sync.transcript.json"


def test_latest_keyword(tmp_path):
    _make_recording(tmp_path, stem="2026-09-04_1400_tydenni-sync")
    _make_recording(tmp_path, stem="2026-09-04_1630_pozdejsi")
    assert resolve_recording("latest", tmp_path).stem == "2026-09-04_1630_pozdejsi"
    with pytest.raises(Exception):
        resolve_recording("latest", tmp_path / "empty")


def test_unsupported_format_rejected(tmp_path):
    sc = _make_recording(tmp_path, format=2)
    with pytest.raises(Exception):
        Recording.load(sc)


def test_prompt_from_title_participants_glossary(tmp_path):
    rec = Recording.load(_make_recording(tmp_path))
    p = build_prompt(rec, ("ATLAS", "SLA", "ATLAS"))
    assert p == "Schůzka: Týdenní sync. Účastníci: Jana Nováková, Petr Svoboda. Pojmy: ATLAS, SLA."


def test_txt_and_srt():
    segs = [Segment(0.5, 2.25, "Ahoj.", "Jana"), Segment(3661, 3662.5, "Konec", None)]
    txt = to_txt(segs, title="T", header={"language": "cs"})
    assert txt.splitlines()[0] == "# T"
    assert "[00:00:00] Jana: Ahoj." in txt and "[01:01:01] ?: Konec" in txt  # ? = nobody assigned
    srt = to_srt(segs)
    assert "00:00:00,500 --> 00:00:02,250\nJana: Ahoj." in srt
    assert "01:01:01,000 --> 01:01:02,500\n?: Konec" in srt


def test_config_load_and_overrides(tmp_path):
    p = tmp_path / "recordingz.toml"
    p.write_text('[recordings]\nout_dir = "D:/meetings"\n[transcribe]\nlanguage = "sk"\nglossary = ["ATLAS"]\nunknown = 1\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.out_dir == Path("D:/meetings") and cfg.transcribe.language == "sk" and cfg.transcribe.glossary == ("ATLAS",)
    assert cfg.transcribe.languages == ("cs", "sk", "en")
    p.write_text('[transcribe]\nlanguages = ["cs", "en"]\n', encoding="utf-8")
    assert load_config(p).transcribe.languages == ("cs", "en")
    cfg2 = with_overrides(cfg, **{"transcribe.language": "cs", "transcribe.model": None, "out_dir": Path("x")})
    assert cfg2.transcribe.language == "cs" and cfg2.transcribe.model == "large-v3" and cfg2.out_dir == Path("x")
    assert load_config(tmp_path / "missing.toml") == Config(source_path=None)
    assert TranscribeSettings().diarize_model.startswith("pyannote/")


def test_config_user_name(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[user]\nname = " Jan Novák "\n[recordings]\nout_dir = "D:/m"\n', encoding="utf-8")
    assert load_config(p).user_name == "Jan Novák"
    assert Config().user_name == "" and not Config().voiceprints.enabled and 0 < Config().voiceprints.threshold < 1
    p.write_text('[voiceprints]\nenabled = true\n', encoding="utf-8")
    assert load_config(p).voiceprints.enabled  # opt in in the config
    p.write_text('[voiceprints]\nenabled = false\nthreshold = 0.7\n', encoding="utf-8")
    v = load_config(p).voiceprints
    assert (v.enabled, v.threshold, v.margin, v.min_seconds) == (False, 0.7, 0.1, 30)


def test_silent_recording_is_not_transcribed_and_latest_skips_it(tmp_path):
    from recordingz.recording import latest_recording
    cfg = Config(out_dir=tmp_path)
    _make_recording(tmp_path, stem="2026-09-21_0900_team-week-plan", source="live", audio_silent=True)
    good = Recording.load(_make_recording(tmp_path, stem="2026-09-20_1000_tydenni-sync", source="live"))
    assert latest_recording(tmp_path).stem == good.stem  # newest is silent -> skipped
    silent = resolve_recording("2026-09-21_0900_team-week-plan", tmp_path)
    with pytest.raises(RecordingError, match="audio_silent"):
        do_transcribe(cfg, silent)


def test_purge_audio_deletes_only_finished_old_recordings(tmp_path):
    from recordingz.pipeline import do_transcribe, purge_audio
    cfg = Config(out_dir=tmp_path)
    done = _make_transcribed(tmp_path)                                  # 2026-09-04, speakers unnamed so far
    rec2 = _make_transcribed(tmp_path, stem="2026-09-05_1000_dalsi")    # stays unfinished
    for r in (done, rec2):
        r.summary_path.write_text("# zápis\n", encoding="utf-8")
        r.file("_screen1.mp4").write_bytes(b"v" * 10)
    done.write_json(done.speakers_path, {"SPEAKER_00": "petr", "SPEAKER_01": "jana"})
    now = datetime(2026, 12, 31)
    assert [r["stem"] for r in purge_audio(cfg, 90, dry_run=True, now=now)] == [done.stem]
    assert done.mix_path.exists(), "a dry run deletes nothing"
    assert purge_audio(cfg, 200, now=now) == [], "younger than the limit"
    out = purge_audio(cfg, 90, now=now)
    assert out[0]["files"] == [f"{done.stem}_mix.wav", f"{done.stem}_screen1.mp4"] and out[0]["bytes"] > 0
    assert not done.mix_path.exists() and not done.file("_screen1.mp4").exists()
    for kept in (done.transcript_path, done.summary_path, done.speakers_path, done.stem_path.with_suffix(".json")):
        assert kept.exists(), kept.name
    assert Recording.load(done.stem_path.with_suffix(".json")).sidecar["audio_purged"] == "2026-12-31"
    assert rec2.mix_path.exists(), "unnamed speakers still need their audio samples"
    with pytest.raises(RecordingError, match="purge-audio"):
        do_transcribe(cfg, Recording.load(done.stem_path.with_suffix(".json")), force=True)
    with pytest.raises(RecordingError):
        purge_audio(cfg, 0)


def test_trash_restore_delete_now_and_empty(tmp_path):
    """Do koše: the meeting's folder goes to <out_dir>/.trash, out of the list, and comes back with Obnovit. Deleting
    for good (now, or by emptying the trash) also drops the voice prints the meeting gave. A meeting with a job is
    not deleted."""
    from datetime import datetime, timedelta
    from recordingz import trash
    from recordingz.voiceprints import Voiceprints
    a = _make_transcribed(tmp_path, stem="2026-09-04_1400_tydenni-sync")
    b = _make_transcribed(tmp_path, stem="2026-09-05_1400_tydenni-sync")
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr", [1.0, 0.0], a.stem, "SPEAKER_00", "m")
    vp.enroll("petr", [0.0, 1.0], b.stem, "SPEAKER_00")
    vp.save()
    srv, state, base = _serve(tmp_path)
    try:
        code, r = _call(base, "POST", f"/api/recordings/{a.stem}/trash")
        assert code == 200 and [i["stem"] for i in r["trash"]["items"]] == [a.stem]
        assert r["trash"]["items"][0]["days_left"] == 30 and r["trash"]["trash_days"] == 30
        assert not a.dir.exists() and (tmp_path / ".trash" / a.dir.name).is_dir()
        assert [x["stem"] for x in _call(base, "GET", "/api/recordings")[1]] == [b.stem], "out of the list"
        assert Voiceprints.load(tmp_path).count("petr") == 2, "the prints wait for the trash to be emptied"
        code, r = _call(base, "POST", f"/api/trash/{a.stem}/restore")
        assert code == 200 and r["stem"] == a.stem and a.dir.exists() and not r["trash"]["items"]

        state.jobs.append({"id": 99, "name": "process", "stem": b.stem, "text": "", "fn": None, "done": ""})
        code, r = _call(base, "DELETE", f"/api/recordings/{b.stem}")
        assert code == 400 and "frontě" in r["error"] and b.dir.exists(), "a meeting with a job stays"
        state.jobs.clear()
        code, r = _call(base, "DELETE", f"/api/recordings/{b.stem}")
        assert code == 200 and not b.dir.exists() and not (tmp_path / ".trash" / b.dir.name).exists()
        vp = Voiceprints.load(tmp_path)
        assert vp.count("petr") == 1 and vp.people["petr"][0]["stem"] == a.stem, "its prints went with it"

        assert _call(base, "POST", f"/api/recordings/{a.stem}/trash")[0] == 200
        assert trash.empty(state.cfg, older_than_days=30) == [], "not long enough in the trash"
        later = datetime.now() + timedelta(days=31)
        assert trash.empty(state.cfg, older_than_days=30, now=later) == [a.stem]
        assert not (tmp_path / ".trash" / a.dir.name).exists() and Voiceprints.load(tmp_path).count("petr") == 0
        code, r = _call(base, "POST", "/api/trash/empty")
        assert code == 200 and r["deleted"] == []
    finally:
        srv.shutdown()


def test_config_calendar_flag(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[calendar]\noutlook = true\n', encoding="utf-8")
    assert load_config(p).calendar_outlook is True and Config().calendar_outlook is False


def test_rename_recording_moves_folder_and_fixes_references(tmp_path):
    from recordingz.pipeline import rename_recording
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.views import build_review, save_title
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    old_stem, old_dir = rec.stem, rec.dir
    rec.sidecar["screens"] = [{"file": f"{old_stem}_screen1.mp4", "fps": 2, "start_offset_s": 0.0, "titles": []}]
    rec.save_sidecar(); rec.file("_screen1.mp4").write_bytes(b"x")
    (old_dir / f"{old_stem}.summary.md").write_text("<!-- x -->\n# Týdenní sync\n\n## Shrnutí\n", encoding="utf-8")
    (old_dir / f"{old_stem}.summary.claude-opus-5.md").write_text("<!-- x -->\n# Týdenní sync\n", encoding="utf-8")
    vp = Voiceprints.load(tmp_path); vp.enroll("jana", [1.0, 0.0], old_stem, "Jana Nováková"); vp.save()
    assert save_title(cfg, rec, "  Týdenní   sync ") is rec  # unchanged after whitespace normalisation
    new = rename_recording(cfg, rec, "Plánování Q4 / rozpočet")
    assert new.stem == "2026-09-04_1400_planovani-q4-rozpocet" and not old_dir.exists() and new.dir.exists()
    names = sorted(p.name for p in new.dir.iterdir())
    assert names == sorted([f"{new.stem}.json", f"{new.stem}_mix.wav", f"{new.stem}.transcript.json", f"{new.stem}_screen1.mp4",
                            f"{new.stem}.summary.md", f"{new.stem}.summary.claude-opus-5.md"])
    again = Recording.load(new.sidecar_path)
    assert again.title == "Plánování Q4 / rozpočet" and again.sidecar["slug"] == "planovani-q4-rozpocet"
    assert again.mix_path.name == f"{new.stem}_mix.wav" and again.mix_path.exists()
    assert again.sidecar["screens"][0]["file"] == f"{new.stem}_screen1.mp4" and (new.dir / again.sidecar["screens"][0]["file"]).exists()
    assert again.summary_path.read_text(encoding="utf-8").splitlines()[1] == "# Plánování Q4 / rozpočet"
    assert Voiceprints.load(tmp_path).people["jana"][0]["stem"] == new.stem
    assert build_review(cfg, again)["title"] == "Plánování Q4 / rozpočet"
    assert resolve_recording("latest", tmp_path).stem == new.stem
    # same slug, different wording: only the title text changes, nothing moves
    same = rename_recording(cfg, again, "Plánování Q4, rozpočet")
    assert same.stem == new.stem and Recording.load(new.sidecar_path).title == "Plánování Q4, rozpočet"
    # a collision is refused
    _make_recording(tmp_path, stem="2026-09-04_1400_jina")
    with pytest.raises(Exception):
        rename_recording(cfg, again, "Jiná")


def test_a_copy_of_the_minutes_goes_where_this_meeting_went_last_time(tmp_path):
    from recordingz import exports
    out = tmp_path / "out"
    rec = _make_transcribed(out)
    target = tmp_path / "Zápisy" / "Týdenní sync"
    assert exports.folder_for(out, rec.title) == ""
    first = exports.save_copy(rec, rec.summary_path.name, str(target), out)
    start = rec.sidecar["start"]
    assert first.name == f"{start[:10]} {start[11:13]}{start[14:16]} {rec.title} – zápis.md", "date and time first"
    assert first.read_text(encoding="utf-8") == rec.summary_path.read_text(encoding="utf-8")
    second = exports.save_copy(rec, rec.summary_path.name, str(target), out)
    assert second.name.endswith(" (2).md"), "never overwrites"
    assert exports.folder_for(out, rec.title.upper()) == str(target), "the same meeting name, any case"
    assert exports.folder_for(out, "Jiná schůzka") == ""
    with pytest.raises(RecordingError):
        exports.save_copy(rec, "../secret.md", str(target), out)
    with pytest.raises(RecordingError):
        exports.save_copy(rec, rec.summary_path.name, "relative/folder", out)


def test_the_setup_wizard_is_for_a_fresh_install_only(tmp_path):
    """Shown by itself only when nobody configured recordingz (no name) and it never ran; an existing user who gets
    the update is left alone."""
    from recordingz import setup
    cfg = tmp_path / "recordingz.toml"
    assert setup.needed(cfg), "nothing configured yet"
    cfg.write_text('[user]\nname = "Jana"\n', encoding="utf-8")
    assert not setup.needed(cfg), "an existing configuration"
    cfg.write_text('[user]\nname = ""\n', encoding="utf-8")
    assert setup.needed(cfg)
    setup.mark_done(cfg)
    assert not setup.needed(cfg)


def test_processing_estimates_come_from_this_pcs_runs_when_there_are_enough(tmp_path):
    """15 min / 1 h locally and by the fast track: the card's defaults, then this PC's own <stem>.timings.json
    (a robust line through them: one odd run does not tilt it)."""
    import json
    from recordingz import setup
    card = {"name": "RTX", "vram_gb": 24}
    e = setup.estimates(card, tmp_path)
    assert "odhad podle grafické karty" in e["source"] and e["minutes_local"]
    assert e["rows"][1]["cloud"] < e["rows"][1]["local_cloud_minutes"] < e["rows"][1]["local"]
    for i, (minutes, transcript, ollama) in enumerate([(10, 100, 300), (20, 150, 500), (30, 200, 700), (40, 250, 4000), (50, 300, 1100),
                                                   (60, 350, 1300)]):
        d = tmp_path / "2026" / "10" / f"r{i}"
        d.mkdir(parents=True)
        (d / f"r{i}.json").write_text(json.dumps({"duration_s": minutes * 60}), encoding="utf-8")
        (d / f"r{i}.timings.json").write_text(json.dumps({"runs": [{"job": "process", "ok": True, "steps": [
            {"step": "přepis (whisperx)", "s": transcript}, {"step": "zápis (ollama gemma4:31b)", "s": ollama},
            {"step": "zápis (ollama x)", "s": 2.0}]}]}), encoding="utf-8")
    costs = setup.measured_costs(tmp_path)
    assert costs["transcribe_local"] == (50.0, 5.0, 6)
    assert costs["minutes_local"][1] == 20.0, "the 4000 s run does not tilt it; the 2 s failure is ignored"
    e = setup.estimates(card, tmp_path)
    assert "podle měření na tomto počítači" in e["source"]
    assert e["rows"][1]["minutes_local"] == 1300 and "Nejpomalejší je lokální zápis" in e["advice"]
    assert not setup.estimates({"name": "x", "vram_gb": 8}, tmp_path / "none")["minutes_local"], \
        "no measured local minutes model below 24 GB"
    assert "jako běžnou cestu ⚡ rychle přes cloud" in setup.estimates(None, tmp_path / "none")["advice"]


def test_atomic_write_keeps_the_old_file_when_the_replace_fails(tmp_path, monkeypatch):
    import os
    from recordingz import fileio
    p = tmp_path / "people.json"
    p.write_text("old", encoding="utf-8")
    calls = []
    real = os.replace

    def busy(src, dst):  # Windows: the target is open in another process for a moment
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(src, dst)
    monkeypatch.setattr(fileio.os, "replace", busy)
    fileio.atomic_write_text(p, "new", wait_s=0)
    assert p.read_text(encoding="utf-8") == "new" and len(calls) == 3, "retried"

    def broken(src, dst):
        raise PermissionError("in use for good")
    monkeypatch.setattr(fileio.os, "replace", broken)
    with pytest.raises(PermissionError):
        fileio.atomic_write_text(p, "newer", tries=3, wait_s=0)
    assert p.read_text(encoding="utf-8") == "new", "the original is intact"
    assert [f.name for f in tmp_path.iterdir()] == ["people.json"], "no temporary file left behind"


def test_registry_lock_two_processes_lose_no_people(tmp_path):
    """Review 16. 10., T3: a job (a child process) and the server's request threads load, change and save people.json
    at the same time; without the lock one of them wrote over the other's new people."""
    import subprocess
    import sys
    code = ("import sys\nfrom pathlib import Path\nfrom recordingz.fileio import registry_lock\n"
            "from recordingz.people import People, Person\n"
            "out, tag = Path(sys.argv[1]), sys.argv[2]\n"
            "for i in range(25):\n"
            "    with registry_lock(out):\n"
            "        p = People.load(out)\n"
            "        p.people.append(Person(id=f'{tag}-{i}', first=tag))\n"
            "        p.save()\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), tag]) for tag in ("a", "b")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    from recordingz.people import People
    ids = {p.id for p in People.load(tmp_path).people}
    assert ids == {f"{t}-{i}" for t in ("a", "b") for i in range(25)}


def test_registry_lock_is_reentrant_and_gives_up_after_its_timeout(tmp_path):
    import threading
    import time
    from recordingz.fileio import registry_lock
    with registry_lock(tmp_path):
        with registry_lock(tmp_path):  # save_names -> enroll_names: the same thread again
            pass
    held, release = threading.Event(), threading.Event()

    def holder():
        with registry_lock(tmp_path):
            held.set()
            release.wait(10)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    held.wait(5)
    t0 = time.monotonic()
    with registry_lock(tmp_path, timeout_s=0.3):  # a stuck holder: go on (with a warning) instead of hanging
        assert time.monotonic() - t0 >= 0.25  # Windows timers are coarse
    release.set()
    t.join(5)
    with registry_lock(tmp_path, timeout_s=1):  # free again
        pass
