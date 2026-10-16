import json
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from recordingz.config import (Config, TranscribeSettings, VoiceprintSettings, load_config,
                                        with_overrides)

VP_ON = VoiceprintSettings(enabled=True)  # voice prints are opt-in; tests that need them say so
from recordingz.export import to_srt, to_txt
from recordingz.importer import derive_metadata
from recordingz.prompt import build_prompt
from recordingz.providers.base import Segment
from recordingz.pipeline import do_transcribe
from recordingz.recording import (Recording, RecordingError, is_sidecar, make_stem, resolve_recording,
                                          slugify)
from recordingz.speakers import apply_manual_names, apply_video_timeline, speaker_list
from recordingz.video_speakers import VideoTimeline


# ---------------------------------------------------------------- naming

def test_slugify_strips_diacritics_and_limits():
    assert slugify("Týdenní sync: ATLAS / SLA") == "tydenni-sync-atlas-sla"
    assert slugify("!!!") == "recording"
    assert len(slugify("a" * 100)) == 60


def test_make_stem():
    assert make_stem(datetime(2026, 9, 4, 14, 0, 12), "Týdenní sync") == "2026-09-04_1400_tydenni-sync"


# ---------------------------------------------------------------- import metadata

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


# ---------------------------------------------------------------- recording / sidecar

def _make_recording(out_dir: Path, stem="2026-09-04_1400_tydenni-sync", **extra) -> Path:
    d = out_dir / "2026" / "09" / stem
    d.mkdir(parents=True)
    sidecar = {"format": 1, "app": "t", "app_version": "0", "title": "Týdenní sync", "slug": "tydenni-sync",
               "source": "import", "start": "2026-09-04T14:00:00", "end": "2026-09-04T14:30:00", "duration_s": 1800,
               "stop_reason": "n/a", "tracks": {}, "mix": {"file": f"{stem}_mix.wav", "sample_rate": 16000, "channels": 1},
               "participants": [{"name": "Jana Nováková"}, {"name": "Petr Svoboda"}], **extra}
    (d / f"{stem}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (d / f"{stem}_mix.wav").write_bytes(b"RIFF")
    return d / f"{stem}.json"


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


# ---------------------------------------------------------------- prompt

def test_prompt_from_title_participants_glossary(tmp_path):
    rec = Recording.load(_make_recording(tmp_path))
    p = build_prompt(rec, ("ATLAS", "SLA", "ATLAS"))
    assert p == "Schůzka: Týdenní sync. Účastníci: Jana Nováková, Petr Svoboda. Pojmy: ATLAS, SLA."


# ---------------------------------------------------------------- speakers

def _segs():
    return [Segment(0, 5, "a", "SPEAKER_00"), Segment(5, 10, "b", "SPEAKER_01"),
            Segment(10, 15, "c", "SPEAKER_00"), Segment(15, 16, "d", "SPEAKER_02"), Segment(60, 61, "e", "SPEAKER_00")]


def test_video_timeline_names_segments_and_maps_fallback():
    tl = VideoTimeline(fps=2, speakers={"Jana": [[0, 5], [10, 15]], "Petr": [[5, 10]]}, clusters=[])
    segs = _segs()
    # SPEAKER_00 overlaps Jana for 10 s of its 11 s -> mapping; the segment at 60 s (no video) follows
    mapping = apply_video_timeline(segs, tl)
    assert mapping.get("SPEAKER_00") == "Jana"
    assert [s.speaker for s in segs] == ["Jana", "Petr", "Jana", "SPEAKER_02", "Jana"]
    # a short highlight must not own a long label: 12 s of Jana on a 30-minute SPEAKER_05
    from recordingz.speakers import video_label_mapping
    long = [Segment(i * 60, i * 60 + 55, "x", "SPEAKER_05") for i in range(30)]
    assert video_label_mapping(long, VideoTimeline(fps=2, speakers={"Jana": [[0, 12]]}, clusters=[])) == {}
    # direct-only pass leaves labels alone; the fallback pass maps them afterwards
    segs2 = _segs()
    original = [s.speaker for s in segs2]
    assert apply_video_timeline(segs2, tl, fallback=False) == {}
    assert segs2[4].speaker == "SPEAKER_00"
    from recordingz.speakers import apply_video_fallback
    segs2[3].speaker = "Jiří"  # the mic named SPEAKER_02 in between: untouched by the fallback
    apply_video_fallback(segs2, tl, original)
    assert [s.speaker for s in segs2] == ["Jana", "Petr", "Jana", "Jiří", "Jana"]


def test_manual_names_and_speaker_list():
    segs = _segs()
    apply_manual_names(segs, {"SPEAKER_02": "Ivan"})
    assert speaker_list(segs) == ["SPEAKER_00", "SPEAKER_01", "Ivan"]


# ---------------------------------------------------------------- export

def test_txt_and_srt():
    segs = [Segment(0.5, 2.25, "Ahoj.", "Jana"), Segment(3661, 3662.5, "Konec", None)]
    txt = to_txt(segs, title="T", header={"language": "cs"})
    assert txt.splitlines()[0] == "# T"
    assert "[00:00:00] Jana: Ahoj." in txt and "[01:01:01] ?: Konec" in txt  # ? = nobody assigned
    srt = to_srt(segs)
    assert "00:00:00,500 --> 00:00:02,250\nJana: Ahoj." in srt
    assert "01:01:01,000 --> 01:01:02,500\n?: Konec" in srt


# ---------------------------------------------------------------- config

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


# ---------------------------------------------------------------- summarize input

def test_summary_user_message_contains_transcript(tmp_path):
    from recordingz.summarize import SYSTEM_PROMPT, build_user_message
    rec = Recording.load(_make_recording(tmp_path))
    segs = [Segment(0, 2, "Začneme.", "Jana Nováková"), Segment(2, 5, "Úkol vezmu já.", "Petr Svoboda")]
    msg = build_user_message(rec, segs, {"start": "2026-09-04T14:00:00", "language": "cs", "participants": None})
    assert msg.startswith("Meeting: Týdenní sync\nstart: 2026-09-04T14:00:00\nlanguage: cs\n")
    assert "[00:00:02] Petr Svoboda: Úkol vezmu já." in msg
    assert "{language}" in SYSTEM_PROMPT and "{h3}" in SYSTEM_PROMPT and "{h6}" in SYSTEM_PROMPT


def test_mic_track_names_the_user(tmp_path):
    import wave
    import numpy as np
    from recordingz.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 40) / sr
    sig = np.zeros_like(t)
    hot = [(0, 5), (10, 15), (20, 22), (30, 40)]  # the user talks here
    for a, b in hot:
        sig[int(a * sr):int(b * sr)] = 0.3 * np.sin(2 * np.pi * 220 * t[int(a * sr):int(b * sr)])
    sig += np.random.default_rng(0).normal(0, 0.002, len(t))  # room noise
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    segs = [Segment(0, 5, "a", "SPEAKER_00"), Segment(5, 10, "b", "SPEAKER_01"), Segment(10, 15, "c", "SPEAKER_00"),
            Segment(15, 20, "d", "SPEAKER_01"), Segment(20, 22, "e", "SPEAKER_02"), Segment(25, 28, "f", "Jana"),
            Segment(30, 40, "g", "SPEAKER_00")]
    mapping = apply_mic_track(segs, wav, "Jan Novák")
    assert mapping == {"SPEAKER_00": "Jan Novák"}
    # SPEAKER_02's single 2 s segment sits fully inside mic activity -> the user too; Jana untouched
    assert [s.speaker for s in segs] == ["Jan Novák", "SPEAKER_01", "Jan Novák", "SPEAKER_01", "Jan Novák", "Jana", "Jan Novák"]

    # someone else's reply inside the user's label (the mic silent): it keeps the label, a group of its own
    segs = [Segment(0, 5, "a", "SPEAKER_00"), Segment(10, 15, "c", "SPEAKER_00"), Segment(30, 40, "g", "SPEAKER_00"),
            Segment(23, 29, "x", "SPEAKER_00"), Segment(22.4, 22.9, "mhm", "SPEAKER_00")]
    assert apply_mic_track(segs, wav, "Jan Novák") == {"SPEAKER_00": "Jan Novák"}
    assert [s.speaker for s in segs] == ["Jan Novák", "Jan Novák", "Jan Novák", "SPEAKER_00", "Jan Novák"],         "the 6 s reply without mic activity stays SPEAKER_00; a short one follows the label"


def test_mic_residue_of_a_gated_headset_is_not_the_user(tmp_path):
    """A headset with a noise gate sends digital silence between words and a faint residue (-85 dBFS) while
    the others talk: that residue is far above the floor but must not count as the user speaking."""
    import wave
    import numpy as np
    from recordingz.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 60) / sr
    sig = np.zeros_like(t)
    for a, b in [(0, 10), (30, 40)]:  # the user
        sig[a * sr:b * sr] = 0.3 * np.sin(2 * np.pi * 220 * t[a * sr:b * sr])
    for a, b in [(10, 30), (40, 60)]:  # the others: only a gated residue reaches the mic
        sig[a * sr:b * sr] = 10 ** (-85 / 20) * np.sin(2 * np.pi * 330 * t[a * sr:b * sr])
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(4); w.setframerate(sr)  # 32-bit: the residue survives quantisation
        w.writeframes((sig * 2147483647).astype(np.int32).tobytes())
    segs = [Segment(0, 10, "a", "SPEAKER_00"), Segment(10, 30, "b", "SPEAKER_01"),
            Segment(30, 40, "c", "SPEAKER_00"), Segment(40, 60, "d", "SPEAKER_02")]
    assert apply_mic_track(segs, wav, "Jan Novák") == {"SPEAKER_00": "Jan Novák"}
    assert [s.speaker for s in segs] == ["Jan Novák", "SPEAKER_01", "Jan Novák", "SPEAKER_02"]


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


def _make_transcribed(tmp_path, stem="2026-09-04_1400_tydenni-sync"):
    import wave
    import numpy as np
    sc = _make_recording(tmp_path, stem=stem)
    rec = Recording.load(sc)
    sr = 16000
    t = np.arange(sr * 30) / sr
    sig = (0.3 * np.sin(2 * np.pi * 330 * t) * 32767).astype(np.int16)
    with wave.open(str(rec.mix_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(sig.tobytes())
    segs = [{"start": 0, "end": 4, "text": "Dobrý den, začneme s programem.", "speaker": "SPEAKER_00"},
            {"start": 4, "end": 5, "text": "Ano.", "speaker": "SPEAKER_01"},
            {"start": 5, "end": 12, "text": "Já bych rád probral rozpočet na příští kvartál.", "speaker": "SPEAKER_01"},
            {"start": 12, "end": 20, "text": "Rozpočet je hotový, pošlu ho zítra.", "speaker": "Jana Nováková"},
            {"start": 20, "end": 28, "text": "Ještě k termínům.", "speaker": "SPEAKER_00"}]
    rec.write_json(rec.transcript_path, {"format": 1, "language": "cs", "speaker_sources": ["mic", "diarization"],
                                         "speakers": ["SPEAKER_00", "SPEAKER_01", "Jana Nováková"], "segments": segs})
    rec.summary_path.write_text("# T\n\n## Mluvčí\n| Označení | Jméno | Poznámka |\n|---|---|---|\n"
                                "| SPEAKER_00 | ? | vedl schůzku |\n| SPEAKER_01 | ? | pravděpodobně Petr: osloven |\n",
                                encoding="utf-8")
    return rec


def test_people_display_and_matching(tmp_path):
    from recordingz.people import People, Person
    ppl = People.load(tmp_path, "nick")
    petr = ppl.ensure("Petr Svoboda"); petr.nick = "Péťa"
    jana = ppl.ensure("Jana Nováková"); jana.display = "full"
    ppl.ensure("Jan Novák")
    ppl.save()
    ppl = People.load(tmp_path, "nick")
    assert [p.id for p in ppl.people] == ["petr-svoboda", "jana-novakova", "jan-novak"]
    assert ppl.display("petr-svoboda") == "Péťa" and ppl.display("Petr Svoboda") == "Péťa"
    assert ppl.display("jana-novakova") == "Jana Nováková"  # per-person override
    assert ppl.display("Jan Novák") == "Jan"                 # nick mode without nickname -> first name
    assert ppl.display("SPEAKER_00") == "SPEAKER_00" and ppl.display("Neznámý Host") == "Neznámý Host"
    assert People.load(tmp_path, "full").display("petr-svoboda") == "Petr Svoboda"
    assert ppl.find("petr svoboda").id == "petr-svoboda" and ppl.find("Svoboda Petr").id == "petr-svoboda"
    assert ppl.find("Jana") is not None and ppl.find("Jan").id == "jan-novak"
    segs = [Segment(0, 1, "a", "petr-svoboda"), Segment(1, 2, "b", "SPEAKER_01"), Segment(2, 3, "c", "Jana Nováková")]
    assert ppl.apply(segs) == {"petr-svoboda": "Péťa"}
    assert [s.speaker for s in segs] == ["Péťa", "SPEAKER_01", "Jana Nováková"]
    ppl.ensure("Petr Novotný")  # two Petrs without nicknames would both print "Petr" -> full names
    ppl.people[0].nick = ""
    segs = [Segment(0, 1, "a", "petr-svoboda"), Segment(1, 2, "b", "petr-novotny"), Segment(2, 3, "c", "jan-novak")]
    ppl.apply(segs)
    assert [s.speaker for s in segs] == ["Petr Svoboda", "Petr Novotný", "Jan"]
    ppl.replace_all([{"id": "petr-svoboda", "first": "Petr", "last": "Svoboda", "nick": "", "display": "first"},
                     {"first": "", "last": "", "nick": ""}, {"first": "Eva", "last": "", "nick": "Evka"}])
    assert [p.id for p in ppl.people] == ["petr-svoboda", "eva"] and ppl.display("petr-svoboda") == "Petr"
    assert ppl.display("eva") == "Evka"
    assert Person.from_text("Petr Svoboda", {"petr-svoboda"}).id == "petr-svoboda-2"


def test_people_dedup_and_merge(tmp_path):
    from recordingz.people import People
    ppl = People.load(tmp_path, "nick")
    z = ppl.ensure("Jiří")                      # from the mic track: first name only
    assert (z.id, z.first, z.last) == ("jiri", "Jiří", "")
    z2 = ppl.ensure("Jiří Dvořák")             # the typed full name completes the same person
    assert z2 is z and z.last == "Dvořák" and len(ppl.people) == 1
    assert ppl.find("Jiří Dvořák") is z and ppl.find("Jiří") is z
    pv = ppl.ensure("Karel Horák"); pv.nick = "Kája"
    assert ppl.ensure("Karol Horák") is pv and "Karol Horák" in pv.aliases   # spelling variant, not a new person
    assert ppl.find("Karol Horák") is pv and ppl.display("Karol Horák") == "Kája"
    other = ppl.ensure("Jiří Novák")            # a different Jiří now has to be a new person
    assert other is not z and len(ppl.people) == 3
    # merge: aliases/nick carried over, speakers.json rewritten, voice prints moved
    dup = ppl.ensure("J. Dvořák"); dup.nick = "Jirka"
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": dup.id, "SPEAKER_01": other.id})
    changed = ppl.merge(z.id, dup.id, [rec])
    assert [r.stem for r in changed] == [rec.stem] and ppl.get(dup.id) is None
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": z.id, "SPEAKER_01": other.id}
    assert z.nick == "Jirka" and "J. Dvořák" in z.aliases and ppl.find("J. Dvořák") is z
    with pytest.raises(ValueError):
        ppl.merge(z.id, "nobody")


def test_voiceprints_registry_and_recognition(tmp_path):
    from recordingz.voiceprints import Voiceprints, enroll_from_recording, remap_embeddings
    a, b = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]
    near_a = [0.95, 0.3, 0.0, 0.0]
    vp = Voiceprints.load(tmp_path)
    assert vp.enroll("petr", a, "rec1", "SPEAKER_00", "m") and not vp.enroll("petr", a, "rec1", "SPEAKER_00")
    assert vp.enroll("jana", b, "rec1", "SPEAKER_01")
    vp.save()
    vp = Voiceprints.load(tmp_path)
    assert vp.model == "m" and vp.count("petr") == 1 and vp.people["petr"][0]["v"] == [1.0, 0.0, 0.0, 0.0]
    got = vp.recognize({"SPEAKER_03": near_a, "SPEAKER_04": [0.7, 0.7, 0.0, 0.0], "SPEAKER_05": [0.0, 0.0, 1.0, 0.0],
                        "SPEAKER_06": near_a},
                       threshold=0.55, margin=0.1, durations={"SPEAKER_03": 60, "SPEAKER_04": 60, "SPEAKER_05": 60, "SPEAKER_06": 5},
                       min_seconds=30)
    assert got["SPEAKER_03"][0] == "petr" and got["SPEAKER_03"][1] > 0.9
    assert "SPEAKER_04" not in got  # tie between petr and jana: no margin
    assert "SPEAKER_05" not in got  # nobody similar
    assert "SPEAKER_06" not in got  # spoke too little for a reliable embedding
    vp.rename("jana", "jana-novakova")
    assert vp.count("jana") == 0 and vp.count("jana-novakova") == 1
    vp.forget("petr")
    assert vp.count("petr") == 0
    # cap per person (distinct prints, otherwise they are skipped as adding nothing)
    for i in range(25):
        vp.enroll("x", [1.0 if j == i else 0.0 for j in range(25)], f"r{i}", "S")
    assert vp.count("x") == 20
    # remap: two labels renamed to the same name are averaged, untouched labels keep their key
    emb = {"SPEAKER_00": a, "SPEAKER_01": [0.0, 0.0, 1.0, 0.0], "SPEAKER_02": b}
    before = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_02"]
    after = ["Jiří", "Jiří", "Jiří", "SPEAKER_02"]
    out = remap_embeddings(emb, before, after)
    assert set(out) == {"Jiří", "SPEAKER_02"} and abs(out["Jiří"][0] - out["Jiří"][2]) < 1e-6
    assert abs(sum(x * x for x in out["Jiří"]) - 1) < 1e-6
    # enrol from a transcript after naming
    vp2 = Voiceprints.load(tmp_path / "v2")
    segs = [{"start": 0, "end": 40, "speaker": "Jiří"}, {"start": 40, "end": 45, "speaker": "SPEAKER_02"}]
    n = enroll_from_recording(vp2, {"speaker_embeddings": out, "diarize_model": "m", "segments": segs},
                              {"Jiří": "jiri", "SPEAKER_02": "petr", "SPEAKER_09": "nobody"}, "rec9", min_seconds=30)
    assert n == 1 and vp2.count("jiri") == 1 and vp2.count("petr") == 0  # 5 s of speech is not enrolled


def test_review_data_clip_and_save(tmp_path):
    from recordingz.web.review import build_review, clip_wav, list_recordings, save_names, unresolved_labels
    cfg = Config(out_dir=tmp_path, user_name="Já")
    rec = _make_transcribed(tmp_path)
    assert unresolved_labels(rec) == ["SPEAKER_00", "SPEAKER_01"]
    rv = build_review(cfg, rec)
    assert [s["label"] for s in rv["speakers"]] == ["SPEAKER_00", "SPEAKER_01", "Jana Nováková"]  # by time
    s0 = rv["speakers"][0]
    assert s0["unresolved"] and s0["name"] == "" and s0["hint"] == "vedl schůzku" and s0["seconds"] == 12
    assert rv["speakers"][1]["hint"].startswith("pravděpodobně Petr")
    assert 1 <= len(s0["samples"]) <= 3 and s0["samples"][0]["end"] - s0["samples"][0]["start"] <= 8
    assert rv["speakers"][2]["name"] == "Jana Nováková" and not rv["speakers"][2]["unresolved"]
    assert "Já" in rv["known_names"] and "Petr Svoboda" in rv["known_names"]  # user + participants
    assert rv["has_mix"] and rv["speaker_sources"] == ["mic", "diarization"]
    clip = clip_wav(rec.mix_path, 1.0, 3.0)
    assert clip[:4] == b"RIFF" and 2 * 16000 * 2 - 100 < len(clip) < 2 * 16000 * 2 + 100
    rows = list_recordings(cfg)
    assert rows[0]["stem"] == rec.stem and rows[0]["unresolved"] == 2
    written = save_names(cfg, rec, {"SPEAKER_00": " Petr Svoboda ", "SPEAKER_01": "", "Jana Nováková": "Jana Nováková"})
    assert written == {"SPEAKER_00": "petr-svoboda"}  # a person was created from the typed name
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}
    assert "Petr: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")  # default display: nick -> first
    assert unresolved_labels(rec) == ["SPEAKER_01"]
    assert list_recordings(cfg)[0]["unresolved"] == 1
    rv = build_review(cfg, rec)
    assert rv["speakers"][0]["name"] == "Petr Svoboda" and rv["speakers"][0]["person"]["shown"] == "Petr"
    assert "Petr Svoboda" in rv["known_names"] and rv["people"][0]["id"] == "petr-svoboda"
    from recordingz.web.review import save_people
    save_people(cfg, [{"id": "petr-svoboda", "first": "Petr", "last": "Svoboda", "nick": "Péťa"}], rec.stem)
    assert "Péťa: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")
    assert build_review(cfg, rec)["speakers"][0]["fields"] == {"first": "Petr", "last": "Svoboda", "nick": "Péťa", "display": ""}
    # structured save: fields update a known person (display override) and create a new one from nick only
    written = save_names(cfg, rec, {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "Péťa", "display": "full"},
                                    "SPEAKER_01": {"first": "", "last": "", "nick": "Šéf", "display": ""}})
    assert written == {"SPEAKER_00": "petr-svoboda", "SPEAKER_01": "sef"}
    txt = rec.file(".txt").read_text(encoding="utf-8")
    assert "Petr Svoboda: Dobrý den" in txt and "Šéf: Já bych rád" in txt
    assert build_review(cfg, rec)["speakers"][1]["fields"]["nick"] == "Šéf"


def test_review_http_roundtrip(tmp_path):
    import json as _json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from recordingz.web.review import ReviewState, _handler
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ref = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ReviewState(cfg), ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "recordingz" in page and "@ts-check" in page
        rows = _json.loads(urllib.request.urlopen(base + "/api/recordings").read())
        assert rows[0]["stem"] == rec.stem
        rv = _json.loads(urllib.request.urlopen(base + f"/api/recordings/{rec.stem}").read())
        assert len(rv["speakers"]) == 3
        wav = urllib.request.urlopen(base + f"/api/recordings/{rec.stem}/clip?start=0&end=2").read()
        assert wav[:4] == b"RIFF"
        body = _json.dumps({"names": {"SPEAKER_01": "Petr"}, "summary": False, "title": "Nový název"}).encode()
        req = urllib.request.Request(base + f"/api/recordings/{rec.stem}/names", data=body, method="PUT",
                                     headers={"Content-Type": "application/json"})
        res = _json.loads(urllib.request.urlopen(req).read())
        assert res["ok"] and res["written"] == {"SPEAKER_01": "petr"}
        assert res["stem"] == "2026-09-04_1400_novy-nazev" and res["title"] == "Nový název"
        moved = resolve_recording(res["stem"], tmp_path)
        assert moved.title == "Nový název" and not rec.dir.exists()
        assert "# Nový název" in moved.file(".txt").read_text(encoding="utf-8")
        rec = moved  # the rest of the test works with the renamed recording
        ppl = _json.loads(urllib.request.urlopen(base + "/api/people").read())
        assert ppl["people"][0]["first"] == "Petr" and ppl["modes"] == ["first", "full", "nick"]
        assert ppl["people"][0]["prints"] == 0
        body = _json.dumps({"names": {"SPEAKER_00": "Peter Svoboda"}, "summary": False}).encode()
        urllib.request.urlopen(urllib.request.Request(base + f"/api/recordings/{rec.stem}/names", data=body, method="PUT",
                                                      headers={"Content-Type": "application/json"})).read()
        body = _json.dumps({"keep": "petr", "drop": "peter-svoboda", "stem": rec.stem}).encode()
        res = _json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/people/merge", data=body,
                                                                        headers={"Content-Type": "application/json"})).read())
        assert [p["id"] for p in res["people"]] == ["petr"] and "Peter Svoboda" in res["people"][0]["aliases"]
        det = _json.loads(urllib.request.urlopen(base + "/api/people/petr").read())
        assert det["id"] == "petr" and det["recordings"] == [rec.stem] and det["prints"] == []
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/people/nobody")
        assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr"}  # the page always sends the full mapping
        bad = urllib.request.Request(base + "/api/recordings/nope")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        srv.shutdown(); srv.server_close()


def test_review_api_refuses_other_web_pages(tmp_path):
    """Review 15. 10., T1: a web page in the browser must not drive the local API – not by a request from its
    origin (CSRF), nor through a name of its own resolved to 127.0.0.1 (DNS rebinding, a foreign Host)."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from recordingz.web.review import ReviewState, _handler
    ref = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ReviewState(Config(out_dir=tmp_path)), ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def status(path, method="GET", **headers):
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=b"{}" if method != "GET" else None)
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert status("/api/status") == 200, "a local program (no Origin)"
        assert status("/api/status", Origin=base) == 200, "the page itself"
        assert status("/api/status", Origin=f"http://localhost:{port}", Host=f"localhost:{port}") == 200
        assert status("/api/quit", "POST", Origin="https://evil.example") == 403
        assert status("/api/trash/empty", "POST", Origin="null") == 403
        assert status("/api/quit", "POST", Origin=f"http://127.0.0.1:{port + 1}") == 403, "another local port"
        assert status("/api/recordings", Host=f"evil.example:{port}") == 403, "DNS rebinding"
        assert status("/api/recordings", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}) == 403
        assert status("/", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}) == 200, "a link to the page"
        assert srv.socket.fileno() != -1, "still serving"
    finally:
        srv.shutdown(); srv.server_close()


def test_untranscribed_recording_is_offered_for_processing(tmp_path, monkeypatch):
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path)
    rec = Recording.load(_make_recording(tmp_path))  # sidecar + mix, no transcript
    d = rv.build_review(cfg, rec)
    assert d["transcribed"] is False and d["has_mix"] and d["speakers"] == []
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))  # the child process
    st = rv.ReviewState(cfg)
    assert st.run_process(rec)
    import time
    for _ in range(100):  # idle first, the job's last event right after it
        if not st.status()["busy"] and st.status()["events"][-1]["text"].startswith("zpracováno"):
            break
        time.sleep(0.02)
    assert calls and calls[0][-4:] == ["run-job", "process", rec.stem, "--minutes-when-named"], "processing runs as a child process"
    assert calls[0][:2] == ["--out-dir", str(tmp_path)], "with the server's folder"
    assert st.status()["message"] == "zpracováno" and st.status()["job"] == "process"
    ev = st.status()["events"]
    assert [e["text"] for e in ev][-1].startswith("zpracováno") and ev[-1]["reload"] is True
    assert any("zpracování spuštěno" in e["text"] for e in ev), "the page must see that a job started"

    # "přepsat znovu od nuly": queued, nothing changes yet (the names go when the job starts, in run-job)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    assert st.run_process(rec, force=True)
    for _ in range(50):
        if not st.status()["busy"]:
            break
        time.sleep(0.02)
    assert calls[-1][-5:] == ["run-job", "process", rec.stem, "--force", "--minutes-when-named"] and rec.speakers_path.exists()


def test_run_job_from_scratch_drops_the_names_when_it_starts(tmp_path, monkeypatch):
    """The manual names and the cached window analysis must not survive a new diarization – dropped by the job
    itself, so a recording waiting in the queue keeps them and can be cancelled without loss."""
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    rec.write_json(rec.speakers_video_path, {"format": 1, "source": "teams-screen", "fps": 2, "speakers": {}})
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(
        (force, r.speakers_path.exists(), r.speakers_video_path.exists())))
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--force"])
    assert res.exit_code == 0, res.output
    assert seen == [(True, False, False)]


def test_choose_language_restricts_to_expected():
    from recordingz.providers.whisperx_provider import choose_language
    votes = [{"ru": 0.23, "sk": 0.20, "cs": 0.15, "en": 0.05}, {"sk": 0.6, "cs": 0.3}, {"cs": 0.5, "sk": 0.4}]
    assert choose_language(votes, ("cs", "sk", "en")) == "sk"      # ru ignored, sk 1.20 vs cs 0.95
    assert choose_language(votes, ("en",)) == "en"
    assert choose_language([], ("cs",)) is None and choose_language([{"ru": 1.0}], ("cs",)) is None


def test_silent_recording_is_not_transcribed_and_latest_skips_it(tmp_path):
    from recordingz.recording import latest_recording
    cfg = Config(out_dir=tmp_path)
    _make_recording(tmp_path, stem="2026-09-21_0900_team-week-plan", source="live", audio_silent=True)
    good = Recording.load(_make_recording(tmp_path, stem="2026-09-20_1000_tydenni-sync", source="live"))
    assert latest_recording(tmp_path).stem == good.stem  # newest is silent -> skipped
    silent = resolve_recording("2026-09-21_0900_team-week-plan", tmp_path)
    with pytest.raises(RecordingError, match="audio_silent"):
        do_transcribe(cfg, silent)


def test_onsite_recording_skips_mic_naming(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.providers.base import ProviderResult
    cfg = Config(out_dir=tmp_path, user_name="Já")
    rec = _make_transcribed(tmp_path)  # real 30 s wav as the mix
    rec.sidecar["source"] = "onsite"
    rec.sidecar["tracks"] = {"mic": {"file": rec.mix_path.name, "sample_rate": 16000, "channels": 1}}
    rec.save_sidecar()
    calls = []
    monkeypatch.setattr(pl, "apply_mic_track", lambda *a, **k: calls.append(a) or {})
    class P:
        name = "fake"
        def transcribe(self, audio, **k):
            return ProviderResult(segments=[Segment(0, 40, "ahoj", "SPEAKER_00")], language="cs", provider="fake", provider_version="0", model="m")
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    pl.do_transcribe(cfg, rec, force=True)
    assert calls == [] and rec.read_json(rec.transcript_path)["speakers"] == ["SPEAKER_00"]


def test_video_names_whole_voice_groups_only_after_the_voices(tmp_path, monkeypatch):
    """2026-10-05: the highlighted tile no longer names single replies (a live window keeps the previous speaker
    highlighted, which made mixed groups); it names whole voice groups, and only those no voice print found."""
    from recordingz import pipeline as pl
    from recordingz.providers.base import ProviderResult
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    segs = [Segment(0, 6, "a", "SPEAKER_00"), Segment(6, 8, "b", "SPEAKER_01"),   # Jana's tile still lit at 6-8
            Segment(8, 20, "c", "SPEAKER_00"), Segment(20, 30, "d", "SPEAKER_01")]
    rec.write_json(rec.speakers_video_path, {"format": 1, "source": "teams-screen", "fps": 2,
                                             "speakers": {"Jana Nováková": [[0.0, 20.0]], "Petr Svoboda": [[20.0, 30.0]]}})

    class P:
        name = "fake"

        def transcribe(self, audio, **k):
            return ProviderResult(segments=[Segment(x.start, x.end, x.text, x.speaker) for x in segs], language="cs",
                                  provider="fake", provider_version="0", model="m",
                                  speaker_embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    # the voice knows SPEAKER_01 is Jana; the video would call SPEAKER_00 Jana too and SPEAKER_01 Petr
    monkeypatch.setattr(pl, "_voiceprints_step", lambda cfg, rec, emb, dur, mic, model: (
        rec.write_json(rec.speakers_path, {"SPEAKER_01": "jana-novakova"}) or {"SPEAKER_01": {"person": "jana-novakova", "score": 0.9}}))
    people = pl.People.load(tmp_path, "nick")
    people.ensure("Jana Nováková")
    people.save()
    pl.do_transcribe(cfg, rec, force=True)
    out = rec.read_json(rec.transcript_path)
    speakers = [x["speaker"] for x in out["segments"]]
    assert speakers[1] == speakers[3] == "SPEAKER_01", "no single reply renamed by the highlighted tile"
    assert speakers[0] == speakers[2] == "SPEAKER_00", "Jana is SPEAKER_01 by voice: the video does not make a second Jana"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_01": "jana-novakova"}

    rec.speakers_path.unlink()  # no voice print knows anybody: the video names both groups, whole
    monkeypatch.setattr(pl, "_voiceprints_step", lambda *a: {})
    pl.do_transcribe(cfg, rec, force=True)
    speakers = [x["speaker"] for x in rec.read_json(rec.transcript_path)["segments"]]
    assert speakers == ["Jana Nováková", "Petr Svoboda", "Jana Nováková", "Petr Svoboda"]


def test_summary_refuses_empty_transcript(tmp_path):
    from recordingz.pipeline import do_summarize
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)  # a handful of words
    with pytest.raises(Exception, match="nothing to summarize"):
        do_summarize(cfg, rec, force=True)


def test_recognize_voices_from_stored_embeddings(tmp_path):
    from recordingz.people import People
    from recordingz.pipeline import recognize_voices
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    with pytest.raises(Exception):
        recognize_voices(cfg, rec)  # no embeddings stored
    data = rec.read_json(rec.transcript_path)
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]}
    data["segments"][0]["end"] = 40  # SPEAKER_00 talks long enough, SPEAKER_01 (8 s) does not
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.ensure("Jana Nováková"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [0.98, 0.1, 0.0], "other", "X"); vp.enroll("jana-novakova", [0.0, 0.99, 0.1], "other", "Y"); vp.save()
    m = recognize_voices(cfg, rec)
    assert list(m) == ["SPEAKER_00"] and m["SPEAKER_00"]["person"] == "petr-svoboda"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}
    t = rec.read_json(rec.transcript_path)
    assert "SPEAKER_00" in t["voice_matches"] and t["speaker_sources"][-2:] == ["voiceprint", "diarization"]
    assert "Petr: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")
    assert recognize_voices(cfg, rec) == {}  # nothing new the second time
    # a close-but-not-close-enough print becomes a hint on the page instead of an assignment
    from recordingz.web.review import build_review
    data = rec.read_json(rec.transcript_path)
    data["speaker_embeddings"]["SPEAKER_01"] = [0.3, 0.8, 0.0]  # ~0.85 to jana but only 8 s of speech
    rec.write_json(rec.transcript_path, data)
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_01"]["voice_hint"]["name"] == "Jana Nováková" and sp["SPEAKER_01"]["voice_hint"]["why"] == "krátká promluva"
    assert sp["SPEAKER_00"]["voice_hint"] is None  # already assigned


def test_merge_same_person_folds_labels_and_keeps_distinct_prints(tmp_path):
    from recordingz.people import People
    from recordingz.pipeline import merge_same_person, same_person_groups
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Jana Nováková"); ppl.save()
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40  # both labels speak long enough to be worth a print
    data["segments"][3]["end"] = 60
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0], "Jana Nováková": [0.6, 0.8, 0.0],
                                  "SPEAKER_01": [0.0, 0.0, 1.0]}
    data["voice_matches"] = {"Jana Nováková": {"person": "jana-novakova", "score": 0.9}}
    rec.write_json(rec.transcript_path, data)
    # the video named one label, the user named the other one: one person, two cards
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "jana-novakova", "Jana Nováková": "jana-novakova",
                                       "SPEAKER_01": "petr-svoboda"})
    assert same_person_groups(rec, People.load(tmp_path)) == {"jana-novakova": ["SPEAKER_00", "Jana Nováková"]}

    res = merge_same_person(cfg, rec)
    assert res["merged"] == 1 and res["prints"] == 2  # both embeddings differ enough to be worth keeping
    t = rec.read_json(rec.transcript_path)
    speakers = {s["speaker"] for s in t["segments"]}
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}  # the label with the most speech won
    assert t["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert "Jana Nováková" not in t["speaker_embeddings"] and "Jana Nováková" not in t["voice_matches"]
    assert t["merged_speakers"][0]["kept"] == "SPEAKER_00" and t["merged_speakers"][0]["merged"] == ["Jana Nováková"]
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "jana-novakova", "SPEAKER_01": "petr-svoboda"}
    assert Voiceprints.load(tmp_path).count("jana-novakova") == 2
    assert "Jana: Rozpočet je hotový" in rec.file(".txt").read_text(encoding="utf-8")
    assert merge_same_person(cfg, rec)["merged"] == 0  # nothing left to do

    # and it can be taken back without transcribing again
    from recordingz.pipeline import unmerge_speakers
    assert unmerge_speakers(cfg, rec) == 1
    t = rec.read_json(rec.transcript_path)
    assert sorted({s["speaker"] for s in t["segments"]}) == ["Jana Nováková", "SPEAKER_00", "SPEAKER_01"]
    assert t["merged_speakers"] == [] and not any("merged_from" in s for s in t["segments"])
    assert rec.read_json(rec.speakers_path)["Jana Nováková"] == "jana-novakova"
    with pytest.raises(RecordingError, match="nothing to undo"):
        unmerge_speakers(cfg, rec)


def test_mic_named_segments_survive_the_video_pass():
    """Teams never outlines the local user's own tile, so the highlight must not overwrite the mic track."""
    from recordingz.speakers import apply_video_timeline
    from recordingz.video_speakers import VideoTimeline
    tl = VideoTimeline(fps=2.0, speakers={"Michal Bartoš": [[0, 60]]}, clusters=[])
    segs = [Segment(0, 10, "já mluvím", "Jiří Dvořák"),   # named by the microphone a moment ago
            Segment(10, 20, "a teď on", "SPEAKER_01"),
            Segment(20, 30, "mimo zvýraznění", "SPEAKER_02")]
    segs[2].start, segs[2].end = 120, 130
    apply_video_timeline(segs, tl, fallback=False, keep_named=True)
    assert [s.speaker for s in segs] == ["Jiří Dvořák", "Michal Bartoš", "SPEAKER_02"]
    # without keep_named the highlight wins, as it did for imported recordings
    segs2 = [Segment(0, 10, "já mluvím", "Jiří Dvořák")]
    apply_video_timeline(segs2, tl, fallback=False)
    assert segs2[0].speaker == "Michal Bartoš"


def test_video_names_that_match_no_participant_are_dropped():
    from recordingz.pipeline import keep_video_names
    found = {"Tomáš Beneš": [[0, 10]], "Mihoy Bardtnbnsc": [[10, 3000]], "onen Boork": [[20, 25]]}
    # the meeting has participants: only what OCR snapped onto one of them survives
    kept = keep_video_names(found, ["Tomáš Beneš", "Michal Bartoš", "Jiří Dvořák"])
    assert list(kept) == ["Tomáš Beneš"]
    # no participants (imported recording): anything name-shaped is kept, as before ("onen Boork" too:
    # without somebody to compare against there is nothing better to go on)
    kept = keep_video_names(found, [])
    assert list(kept) == ["Tomáš Beneš", "Mihoy Bardtnbnsc", "onen Boork"]


def test_a_person_can_opt_out_of_voice_recognition(tmp_path):
    """Voice prints are biometric data: whoever does not want them gets none, and the old ones go right away."""
    from recordingz.pipeline import enroll_names
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import save_people
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    assert enroll_names(cfg, rec, {"SPEAKER_00": "petr-svoboda"}) == 1

    rows = People.load(tmp_path).to_json()
    rows[0]["voiceprint"] = False                      # Petr said no
    out = save_people(cfg, rows)
    assert out[0]["voiceprint"] is False and out[0]["prints"] == 0
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 0, "his prints are deleted at once"
    assert People.load(tmp_path).get("petr-svoboda").voiceprint is False, "and the wish is remembered"
    other = _make_transcribed(tmp_path, stem="2026-09-05_1000_dalsi")
    d2 = other.read_json(other.transcript_path)
    d2["segments"][0]["end"] = 40
    d2["speaker_embeddings"] = {"SPEAKER_00": [0.9, 0.1, 0.0]}
    other.write_json(other.transcript_path, d2)
    assert enroll_names(cfg, other, {"SPEAKER_00": "petr-svoboda"}) == 0, "no new prints either"


def test_voice_prints_are_off_unless_switched_on(tmp_path):
    from recordingz.pipeline import enroll_names
    from recordingz.people import People
    cfg = Config(out_dir=tmp_path)                     # a fresh install that was not asked
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    assert enroll_names(cfg, rec, {"SPEAKER_00": "petr-svoboda"}) == 0
    assert not (tmp_path / "_speakers" / "voiceprints.json").exists()


class _FakeResponse:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def _cloud_setup(monkeypatch, tmp_path, env):
    import requests
    from recordingz.providers import cloud
    monkeypatch.setenv(*env)
    fake = tmp_path / "upload.webm"

    def compress(audio, max_bytes, **k):  # the provider deletes the upload afterwards, so make a new one each time
        fake.write_bytes(b"x")
        return fake
    monkeypatch.setattr(cloud, "compress_for_upload", compress)
    for mod in ("recordingz.providers.openai_provider", "recordingz.providers.elevenlabs_provider"):
        import importlib
        monkeypatch.setattr(importlib.import_module(mod), "compress_for_upload", compress)
    sent = {}

    def post(url, headers=None, data=None, files=None, timeout=None):
        sent.update(url=url, headers=headers, data=data, file=files["file"][0])
        return sent["reply"]
    monkeypatch.setattr(requests, "post", post)
    return sent


def test_elevenlabs_words_become_segments_with_speakers(monkeypatch, tmp_path):
    from recordingz.providers import get_provider
    sent = _cloud_setup(monkeypatch, tmp_path, ("ELEVENLABS_API_KEY", "k-el"))
    words = [{"text": "Dobrý", "start": 0.0, "end": 0.4, "type": "word", "speaker_id": "speaker_1"},
             {"text": " ", "start": 0.4, "end": 0.45, "type": "spacing", "speaker_id": "speaker_1"},
             {"text": "den.", "start": 0.45, "end": 0.9, "type": "word", "speaker_id": "speaker_1"},
             {"text": "(smích)", "start": 1.0, "end": 1.2, "type": "audio_event", "speaker_id": "speaker_2"},
             {"text": "Ahoj", "start": 1.3, "end": 1.6, "type": "word", "speaker_id": "speaker_2"}]
    sent["reply"] = _FakeResponse({"language_code": "ces", "language_probability": 0.97, "words": words})
    res = get_provider("elevenlabs").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                                settings=TranscribeSettings(), diarize=True)
    assert sent["headers"] == {"xi-api-key": "k-el"} and sent["data"]["model_id"] == "scribe_v2"
    assert sent["data"]["diarize"] == "true" and "language_code" not in sent["data"]
    assert res.language == "cs" and res.speaker_embeddings is None
    assert [(s.speaker, s.text) for s in res.segments] == [("SPEAKER_00", "Dobrý den."), ("SPEAKER_01", "Ahoj")]
    assert [w.word for w in res.segments[0].words] == ["Dobrý", "den."]


def test_openai_diarized_segments_and_errors(monkeypatch, tmp_path):
    from recordingz.providers import ProviderError, get_provider
    sent = _cloud_setup(monkeypatch, tmp_path, ("RECORDINGZ_OPENAI_API_KEY", "k-oa"))
    monkeypatch.setenv("OPENAI_API_KEY", "not-this-one")
    sent["reply"] = _FakeResponse({"segments": [
        {"id": "1", "start": 0.0, "end": 2.0, "text": "Ahoj všichni.", "speaker": "A", "type": "transcript.text.segment"},
        {"id": "2", "start": 2.1, "end": 3.0, "text": "Čau.", "speaker": "B", "type": "transcript.text.segment"},
        {"id": "3", "start": 3.1, "end": 4.0, "text": "Jdeme na to.", "speaker": "A", "type": "transcript.text.segment"}],
        "usage": {"type": "duration", "seconds": 4}})
    res = get_provider("openai").transcribe(tmp_path / "a.wav", language="cs", prompt="glosář",
                                            settings=TranscribeSettings(), diarize=True)
    assert sent["headers"] == {"Authorization": "Bearer k-oa"}, "the recordingz-specific key wins"
    assert sent["data"] == {"model": "gpt-4o-transcribe-diarize", "response_format": "diarized_json",
                            "chunking_strategy": "auto"}, "the diarize model takes neither language nor prompt"
    assert [s.speaker for s in res.segments] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert res.language == "cs" and res.segments[1].text == "Čau."
    sent["reply"] = _FakeResponse({"error": {"message": "bad key"}}, status=401)
    with pytest.raises(ProviderError, match="OpenAI 401"):
        get_provider("openai").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                          settings=TranscribeSettings(), diarize=True)


def test_cloud_provider_without_a_key_says_which_variable(monkeypatch, tmp_path):
    from recordingz.providers import ProviderError, get_provider
    for v in ("RECORDINGZ_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ProviderError, match="RECORDINGZ_ELEVENLABS_API_KEY"):
        get_provider("elevenlabs").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                              settings=TranscribeSettings(), diarize=True)


def test_invented_replies_are_recognised():
    from recordingz.providers.whisperx_provider import is_hallucination
    for junk in ("Ďakujem za pozornosť.", "Děkuji za pozornost!", "Dakujem vám za pozornost", "Konec.",
                 "www.hradeckralove.org", "Titulky vytvořil JohnyX", "Thank you for watching!"):
        assert is_hallucination(junk), junk
    for real in ("Ďakujem za pozornosť, a teraz k rozpočtu na budúci rok.", "Konec sprintu je v piatok.",
                 "Dobrý ráno.", "A to znamená, že já ten princip budu rolovat na všechny repa."):
        assert not is_hallucination(real), real
    assert is_hallucination("Pojmy ČNES, ÚJKN 2016", level_db=-36.4, median_db=-26.6), "short and far too quiet"
    assert not is_hallucination("Pojmy ČNES, ÚJKN 2016", level_db=-19.0, median_db=-26.6)
    assert not is_hallucination("A tohle je dlouhá věta, kterou někdo řekl hodně potichu do mikrofonu.",
                                level_db=-40.0, median_db=-26.6), "long replies are speech even when quiet"


def test_speaker_language_choice():
    from recordingz.providers.whisperx_provider import choose_speaker_languages
    votes = {"SPEAKER_00": [{"sk": .7, "cs": .25}], "SPEAKER_01": [{"cs": 1.0}, {"cs": .9, "sk": .1}],
             "SPEAKER_02": [{"cs": .45, "sk": .4}], "SPEAKER_03": []}
    assert choose_speaker_languages(votes, "sk", ("cs", "sk", "en")) == {"SPEAKER_01": "cs"}
    assert choose_speaker_languages(votes, "cs", ("cs", "sk", "en")) == {"SPEAKER_00": "sk"}


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


def _serve(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from recordingz.web.review import ReviewState, _handler
    cfg = Config(out_dir=tmp_path)
    state, ref = ReviewState(cfg), {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state, ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, f"http://127.0.0.1:{srv.server_address[1]}"


def _call(base, method, path, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_rest_api_routes_errors_and_openapi(tmp_path):
    from urllib.parse import quote
    from recordingz.web.review import ROUTES
    rec = _make_transcribed(tmp_path)
    srv, state, base = _serve(tmp_path)
    try:
        code, rows = _call(base, "GET", "/api/recordings")
        assert code == 200 and rows[0]["stem"] == rec.stem
        code, r = _call(base, "GET", f"/api/recordings/{rec.stem}")
        assert code == 200 and {s["label"] for s in r["speakers"]} >= {"SPEAKER_00", "Jana Nováková"}
        code, r = _call(base, "PUT", f"/api/recordings/{rec.stem}/names",
                        {"names": {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "", "display": ""}}})
        assert code == 200 and r["written"] == {"SPEAKER_00": "petr-svoboda"}
        # a label with a space and diacritics travels in the path
        code, r = _call(base, "DELETE", f"/api/recordings/{rec.stem}/speakers/{quote('Jana Nováková')}")
        assert code == 200 and r["removed"] == 1
        code, r = _call(base, "GET", f"/api/recordings/{rec.stem}/docs/{rec.stem}.txt")
        assert code == 200 and "Petr" in r["text"]
        assert _call(base, "GET", "/api/nothing-here")[0] == 404
        assert _call(base, "POST", f"/api/recordings/{rec.stem}")[0] == 405, "known path, wrong method"
        code, r = _call(base, "GET", "/api/recordings/2026-01-01_0000_nic")
        assert code == 400 and "not found" in r["error"]
        code, spec = _call(base, "GET", "/api/openapi.json")
        assert code == 200 and spec["openapi"].startswith("3.1")
        documented = {path for path in spec["paths"]}
        for method, pattern, name in ROUTES:
            if pattern == r"/":
                continue
            as_doc = (pattern.replace("(?P<stem>[^/]+)", "{stem}").replace("(?P<file>[^/]+)", "{file}")
                      .replace("(?P<label>[^/]+)", "{label}").replace("(?P<pid>[^/]+)", "{id}")
                      .replace("(?P<doc>[^/]+)", "{doc}").replace("(?P<name>[^/]+)", "{name}")
                      .replace("(?P<job>[^/]+)", "{job}").replace("\\.", "."))
            assert as_doc in documented, f"{method} {as_doc} missing in openapi.py"
            assert method.lower() in spec["paths"][as_doc], f"{method} {as_doc} not documented"
    finally:
        srv.shutdown()


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


def test_server_sent_events_stream_and_replay(tmp_path):
    import http.client
    rec = _make_transcribed(tmp_path)
    srv, state, base = _serve(tmp_path)
    port = srv.server_address[1]
    state.event("dřívější událost", "ok")

    def read_frames(resp, count):
        frames, cur = [], {}
        while len(frames) < count:
            line = resp.fp.readline().decode("utf-8").rstrip("\n")
            if not line:
                if cur:
                    frames.append(cur); cur = {}
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            cur[key] = value
        return frames

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
        hello, old = read_frames(resp, 2)
        assert hello["event"] == "hello" and json.loads(old["data"])["text"] == "dřívější událost"
        assert json.loads(old["data"])["replay"] is True, "history on connect only fills the list"
        _call(base, "PUT", f"/api/recordings/{rec.stem}/names", {"names": {}})
        (live,) = read_frames(resp, 1)
        data = json.loads(live["data"])
        assert live["event"] == "log" and live["id"] == str(data["n"]) and "uloženo" in data["text"]
        conn.close()
        # a reconnect with Last-Event-ID gets only what it missed
        state.event("zmeškaná", "ok")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events", headers={"Last-Event-ID": live["id"]})
        resp = conn.getresponse()
        hello, missed = read_frames(resp, 2)
        assert json.loads(missed["data"])["text"] == "zmeškaná" and json.loads(missed["data"])["replay"] is False
        conn.close()
    finally:
        srv.shutdown()


def test_help_documents_come_from_the_docs_folder():
    from recordingz.web.review import help_doc
    for name in ("user-guide", "install", "privacy"):
        d = help_doc(name)
        assert d["doc"] == name and d["text"].startswith("#"), name
    with pytest.raises(RecordingError, match="unknown help document"):
        help_doc("../README")


def test_voiceprint_skips_a_print_that_adds_nothing(tmp_path):
    from recordingz.voiceprints import Voiceprints
    vp = Voiceprints.load(tmp_path)
    assert vp.enroll("petr", [1.0, 0.0, 0.0], "rec1", "SPEAKER_00")
    assert not vp.enroll("petr", [0.99, 0.02, 0.0], "rec2", "SPEAKER_00")  # same voice, same conditions
    assert vp.enroll("petr", [0.8, 0.6, 0.0], "rec3", "SPEAKER_00")        # another room, worth keeping
    assert vp.count("petr") == 2


def test_remove_speaker_drops_segments_and_reexports(tmp_path):
    from recordingz.pipeline import remove_speaker
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_01": "petr"})
    assert remove_speaker(cfg, rec, "SPEAKER_01") == 2
    data = rec.read_json(rec.transcript_path)
    assert [s["speaker"] for s in data["segments"]] == ["SPEAKER_00", "Jana Nováková", "SPEAKER_00"]
    assert data["speakers"] == ["SPEAKER_00", "Jana Nováková"] and data["removed_speakers"][0]["label"] == "SPEAKER_01"
    assert rec.read_json(rec.speakers_path) == {}
    assert "SPEAKER_01" not in rec.file(".txt").read_text(encoding="utf-8")
    assert remove_speaker(cfg, rec, "SPEAKER_01") == 0
    # segments without any speaker show up as UNKNOWN on the page and can be removed under that name
    data = rec.read_json(rec.transcript_path)
    data["segments"].append({"start": 30, "end": 31, "text": "Přidáváme CJ.", "speaker": None})
    rec.write_json(rec.transcript_path, data)
    assert remove_speaker(cfg, rec, "UNKNOWN") == 1
    assert all(s.get("speaker") for s in rec.read_json(rec.transcript_path)["segments"])


def test_tile_outline_detection():
    pytest.importorskip("scipy")  # the [video] extra; CI installs the base package only
    import numpy as np
    from recordingz.video_speakers import _tile_label_box, _tile_outlines
    img = np.full((540, 960, 3), 40, dtype=np.uint8)
    # a hollow accent-coloured rectangle = the speaking tile; a filled block = a button, not a tile
    for x in range(480, 905):
        img[52:54, x] = (122, 122, 205); img[287:289, x] = (116, 116, 159)
    for y in range(52, 289):
        img[y, 480:482] = (98, 104, 143); img[y, 903:905] = (122, 122, 205)
    img[10:30, 700:760] = (122, 122, 205)
    tiles = _tile_outlines(img)
    assert len(tiles) == 1 and tiles[0][0] <= 482 and tiles[0][2] >= 903 and tiles[0][1] <= 54 and tiles[0][3] >= 287
    lx0, ly0, lx1, ly1 = _tile_label_box(tiles[0])
    assert lx0 < 500 and ly1 <= 289 and ly0 >= 255 and lx1 <= 482 + 240


def test_merge_timelines_and_shift():
    from recordingz.video_speakers import merge_timelines
    a = VideoTimeline(fps=2, speakers={"Jana": [[0, 5], [10, 12]]}, clusters=[{"box": [1]}])
    b = VideoTimeline(fps=2, speakers={"Jana": [[4, 8]], "Petr": [[1, 2]]}, clusters=[])
    m = merge_timelines([a, b.shifted(100), VideoTimeline(fps=2, speakers={"Jana": [[4, 8]]}, clusters=[])])
    assert m.speakers == {"Jana": [[0, 8], [10, 12], [104, 108]], "Petr": [[101, 102]]}  # overlap merged, shift kept
    assert m.to_json()["source"] == "teams-screen" and m.clusters == [{"box": [1]}]
    assert merge_timelines([None, None]) is None
    assert VideoTimeline.from_json(m.to_json()).speakers == m.speakers


def test_screen_analysis_writes_merged_timeline(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.people import People
    cfg = Config(out_dir=tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    rec = Recording.load(_make_recording(tmp_path, screens=[
        {"file": "2026-09-04_1400_tydenni-sync_screen1.mp4", "fps": 2, "width": 1600, "height": 900, "start_offset_s": 0.0, "titles": ["Sync | Microsoft Teams"]},
        {"file": "2026-09-04_1400_tydenni-sync_screen2.mp4", "fps": 2, "width": 1600, "height": 900, "start_offset_s": 30.0, "titles": ["Galerie"]},
        {"file": "missing_screen3.mp4", "fps": 2, "start_offset_s": 0.0}]))
    for n in ("_screen1.mp4", "_screen2.mp4"):
        rec.file(n).write_bytes(b"x")
    seen = []
    def fake_analyze(path, *, fps, names, width, height):
        seen.append((path.name, names, width))
        if path.name.endswith("_screen1.mp4"):
            return VideoTimeline(fps=fps, speakers={"Petr Svoboda": [[0, 10]]}, clusters=[])
        return VideoTimeline(fps=fps, speakers={"Jana Nováková": [[0, 5]]}, clusters=[])
    monkeypatch.setattr(pl, "analyze_screen", fake_analyze)
    tl = pl.do_video(cfg, rec)
    assert [s[0][-12:] for s in seen] == ["_screen1.mp4", "_screen2.mp4"]
    assert "Petr Svoboda" in seen[0][1] and "Jana Nováková" in seen[0][1]  # registry + participants as OCR candidates
    assert tl.speakers == {"Petr Svoboda": [[0, 10]], "Jana Nováková": [[30, 35]]}
    assert rec.read_json(rec.speakers_video_path)["source"] == "teams-screen"
    assert pl.is_meeting_screen(["Schůzka s: Petr | Microsoft Teams"]) and pl.is_meeting_screen([])
    assert not pl.is_meeting_screen(["Calendar | Microsoft Teams"]) and not pl.is_meeting_screen(["Chat | Team | Microsoft Teams"])
    # a main-window capture and OCR noise are ignored
    rec2 = Recording.load(_make_recording(tmp_path, stem="2026-09-04_1500_druha", screens=[
        {"file": "2026-09-04_1500_druha_screen1.mp4", "fps": 2, "start_offset_s": 0.0, "titles": ["Calendar | Microsoft Teams"]},
        {"file": "2026-09-04_1500_druha_screen2.mp4", "fps": 2, "start_offset_s": 0.0, "titles": ["ATLAS | Microsoft Teams"]}]))
    rec2.file("_screen1.mp4").write_bytes(b"x"); rec2.file("_screen2.mp4").write_bytes(b"x")
    monkeypatch.setattr(pl, "analyze_screen", lambda path, **k: VideoTimeline(fps=2, speakers={"Develonment": [[0, 5]], "Petr Svoboda": [[5, 9]]}, clusters=[]))
    assert pl.do_video(cfg, rec2).speakers == {"Petr Svoboda": [[5, 9]]}
    assert pl._looks_like_a_name("Petr Svoboda") and pl._looks_like_a_name("Jana Nováková-Černá")
    assert not pl._looks_like_a_name("Petr") and not pl._looks_like_a_name("x1 y2") and not pl._looks_like_a_name("Nahrávání 12:30")


def test_outlook_pick_meeting_and_fields():
    from datetime import datetime as dt
    from recordingz.outlook import calendar_fields, candidates_at, pick_meeting
    items = [{"subject": "Team week plan", "start": dt(2026, 9, 14, 8, 30), "end": dt(2026, 9, 14, 9, 15), "teams": True,
              "attendees": ["Jana Nováková", "Petr Svoboda"], "organizer": "Jana Nováková"},
             {"subject": "B", "start": dt(2026, 9, 14, 9, 15), "end": dt(2026, 9, 14, 9, 30), "teams": False, "attendees": []},
             {"subject": "CodeScan", "start": dt(2026, 9, 14, 9, 20), "end": dt(2026, 9, 14, 9, 40), "teams": True, "attendees": []}]
    assert pick_meeting(items, dt(2026, 9, 14, 8, 22)) == (items[0], "time")   # 8 min early
    assert pick_meeting(items, dt(2026, 9, 14, 9, 17))[0]["subject"] == "B"   # inside B beats A's grace
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25))[0]["subject"] == "CodeScan"   # inside both: Teams first
    assert pick_meeting(items, dt(2026, 9, 14, 12, 0)) == (None, "")
    # the Teams window / file title settles parallel meetings and ad-hoc calls
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Team week plan | Microsoft Teams") == (items[0], "title")
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Code Scan") == (items[2], "title")
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Připojení ke schůzce")[1] == "time"
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Schůzka s: Petr")[1] == "time"
    assert [c["subject"] for c in candidates_at(items, dt(2026, 9, 14, 9, 25))] == ["CodeScan", "B", "Team week plan"]
    f = calendar_fields(dict(items[0], match="title"))
    assert f["participants"] == [{"name": "Jana Nováková", "source": "calendar"}, {"name": "Petr Svoboda", "source": "calendar"}]
    assert f["calendar"]["source"] == "outlook" and f["calendar"]["start"] == "2026-09-14T08:30"
    assert f["calendar"]["match"] == "title" and f["calendar"]["status"] == "auto"


def test_meeting_link_edits(tmp_path, monkeypatch):
    from datetime import datetime as dt
    from recordingz import pipeline as pl
    from recordingz import outlook
    cfg = Config(out_dir=tmp_path, calendar_outlook=True)
    rec = _make_transcribed(tmp_path)
    rec.sidecar.update(outlook.calendar_fields({"subject": "Týdenní sync", "organizer": "Jana Nováková", "match": "time",
                                                "start": dt(2026, 9, 4, 14, 0), "end": dt(2026, 9, 4, 14, 30), "attendees": ["Jana Nováková"]}))
    rec.sidecar["participants"].append({"name": "Host Ručně", "source": "manual"})
    rec.sidecar["title_source"] = "calendar"
    rec.save_sidecar()
    info = pl.meeting_info(cfg, rec)
    assert info["calendar"]["match"] == "time" and info["calendar"]["status"] == "auto" and info["title_source"] == "calendar"
    assert [p["source"] for p in info["participants"]] == ["calendar", "manual"]
    pl.set_meeting_link(cfg, rec, "confirm")
    assert Recording.load(rec.sidecar_path).sidecar["calendar"]["status"] == "confirmed"
    rec = pl.set_meeting_link(cfg, rec, "detach")
    sc = Recording.load(rec.sidecar_path).sidecar
    assert "calendar" not in sc and sc["participants"] == [{"name": "Host Ručně", "source": "manual"}] and sc["title_source"] == "manual"
    monkeypatch.setattr(outlook, "candidates_for", lambda at, window_s=3600: [
        {"subject": "Plánování Q4", "start": "2026-09-04T14:30", "end": "2026-09-04T15:00", "teams": True,
         "attendees": ["Petr Svoboda"], "organizer": "Petr Svoboda"}])
    rec = pl.set_meeting_link(cfg, rec, "attach", {"subject": "Plánování Q4", "start": "2026-09-04T14:30"})
    assert rec.stem == "2026-09-04_1400_planovani-q4" and rec.title == "Plánování Q4"
    sc = rec.sidecar
    assert sc["calendar"]["status"] == "confirmed" and sc["calendar"]["match"] == "manual"
    assert sc["participants"] == [{"name": "Petr Svoboda", "source": "calendar"}] and sc["title_source"] == "calendar"
    with pytest.raises(Exception):
        pl.set_meeting_link(cfg, rec, "attach", {"subject": "nope", "start": "2026-09-04T14:30"})


def test_config_calendar_flag(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[calendar]\noutlook = true\n', encoding="utf-8")
    assert load_config(p).calendar_outlook is True and Config().calendar_outlook is False


def test_automatic_names_do_not_reach_the_shared_registry(tmp_path, monkeypatch):
    """A name from the video or from a voice match is a guess: no person, no voice print until it is saved."""
    from recordingz.people import People
    from recordingz.pipeline import _voiceprints_step
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, user_name="Jan Novák", voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [0.99, 0.1, 0.0], "older-recording", "X"); vp.save()
    emb = {"SPEAKER_00": [1.0, 0.0, 0.0], "Jan Novák": [0.0, 1.0, 0.0], "Jana Nováková": [0.0, 0.0, 1.0]}
    durations = {"SPEAKER_00": 120.0, "Jan Novák": 300.0, "Jana Nováková": 200.0}
    matches = _voiceprints_step(cfg, rec, emb, durations, {"SPEAKER_02": "Jan Novák"}, "m")
    assert list(matches) == ["SPEAKER_00"] and matches["SPEAKER_00"]["person"] == "petr-svoboda"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}  # shown on the page, undoable
    after = Voiceprints.load(tmp_path)
    assert after.count("petr-svoboda") == 1, "the match itself must not become a print"
    assert after.count("jan-novak") == 0, "not even the user's own microphone label"
    assert [p.id for p in People.load(tmp_path).people] == ["petr-svoboda"], "no people invented from labels"


def test_saving_a_name_confirms_it_and_stores_the_print(tmp_path):
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import build_review, save_names
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40  # long enough for a print
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    data["voice_matches"] = {"SPEAKER_00": {"person": "petr-svoboda", "score": 0.81}}
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_00"]["confirmed"] is False  # recognised by voice, nobody has said yes yet
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 0

    save_names(cfg, rec, {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "", "display": ""}})
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 1  # confirmed -> kept for the next meetings
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_00"]["confirmed"] is True
    assert rec.read_json(rec.transcript_path)["voice_matches"] == {}


def test_rename_recording_moves_folder_and_fixes_references(tmp_path):
    from recordingz.pipeline import rename_recording
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import build_review, save_title
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


def test_recording_docs_and_read_doc(tmp_path):
    from recordingz.web.review import read_doc, recording_docs
    rec = _make_transcribed(tmp_path)
    assert recording_docs(rec) == {"transcript": None, "summaries": [
        {"file": f"{rec.stem}.summary.md", "label": "hlavní", "provider": "", "model": "", "created": "", "main": True, "stale": False}],
        "transcripts": []}
    (rec.dir / f"{rec.stem}.elevenlabs.txt").write_text("x", encoding="utf-8")
    assert recording_docs(rec)["transcripts"] == [{"file": f"{rec.stem}.elevenlabs.txt", "label": "přepis (elevenlabs)"}]
    (rec.dir / f"{rec.stem}.elevenlabs.txt").unlink()
    rec.file(".txt").write_text("# T\n[00:00:00] Jana: Ahoj\n", encoding="utf-8")
    (rec.dir / f"{rec.stem}.summary.claude-opus-5-5.md").write_text(
        "<!-- recordingz summary | anthropic: claude-opus-5-5 | created: 2026-09-30T10:00:00 | tokens: 1 in / 2 out -->\n# T\n",
        encoding="utf-8")
    d = recording_docs(rec)
    assert d["transcript"] == f"{rec.stem}.txt" and [s["label"] for s in d["summaries"]] == ["hlavní", "claude-opus-5-5"]
    assert d["summaries"][1] == {"file": f"{rec.stem}.summary.claude-opus-5-5.md", "label": "claude-opus-5-5",
                                 "provider": "anthropic", "model": "claude-opus-5-5", "created": "2026-09-30T10:00:00",
                                 "main": False, "stale": False}
    from recordingz.web.review import delete_summary
    for bad in (f"{rec.stem}.txt", f"{rec.stem}.json", "../x.summary.md", f"{rec.stem}.summary.nothere.md"):
        with pytest.raises(Exception):
            delete_summary(rec, bad)
    delete_summary(rec, f"{rec.stem}.summary.claude-opus-5-5.md")
    assert [s["label"] for s in recording_docs(rec)["summaries"]] == ["hlavní"] and rec.file(".txt").exists()
    assert "Jana: Ahoj" in read_doc(rec, f"{rec.stem}.txt")
    for bad in ("../x.md", "other.md", f"{rec.stem}.json", f"{rec.stem}.nothere.md"):
        with pytest.raises(Exception):
            read_doc(rec, bad)


def test_open_and_copy_doc_only_the_recordings_documents(tmp_path, monkeypatch):
    from recordingz.web import review
    rec = _make_transcribed(tmp_path)
    rec.summary_path.write_text("# T\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(review, "_launch", lambda argv, path: calls.append((argv, path)))
    monkeypatch.setattr(review, "_clip_file", lambda path: calls.append(("clip", path)))
    viewer = tmp_path / "markdownz.exe"
    viewer.write_text("", encoding="utf-8")
    assert review.open_doc(rec, rec.summary_path.name, f'"{viewer}"') == "markdownz"
    assert calls[-1] == ([str(viewer), str(rec.summary_path)], rec.summary_path), "the configured viewer, quotes off"
    review.open_doc(rec, rec.summary_path.name, "")
    assert calls[-1] == (None, rec.summary_path), "nothing configured: the default app"
    with pytest.raises(Exception, match="not found"):
        review.open_doc(rec, rec.summary_path.name, str(tmp_path / "missing.exe"))
    assert review.copy_doc(rec, rec.summary_path.name) == rec.summary_path and calls[-1] == ("clip", rec.summary_path)
    rec.file(".txt").write_text("x", encoding="utf-8")
    review.open_doc(rec, rec.file(".txt").name, f'"{viewer}"')
    assert calls[-1] == (None, rec.file(".txt")), "only .md goes to the viewer, a transcript to the default app"
    for bad in ("../x.md", f"{rec.stem}.json", f"{rec.stem}.nothere.md"):
        with pytest.raises(Exception):
            review.copy_doc(rec, bad)
        with pytest.raises(Exception):
            review.open_doc(rec, bad, "")
    assert len(calls) == 4


def test_minutes_skill_is_shipped_and_the_same_as_the_apps_rules():
    from recordingz.summarize import HEADINGS, SKILL_PATH, SYSTEM_PROMPT, skill_markdown
    shipped = SKILL_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert shipped == skill_markdown(), "regenerate skills/recordingz-zapis/SKILL.md from summarize.skill_markdown()"
    assert shipped.startswith("---\nname: recordingz-zapis\ndescription: ")
    assert "Úkoly columns" in shipped and all(" · ".join(h) in shipped for h in HEADINGS.values())
    assert SYSTEM_PROMPT.split("\n")[0] in shipped and "via: agent" in shipped


def test_minutes_prompt_names_model_language_files_and_the_skill(tmp_path):
    from recordingz.pipeline import minutes_prompt
    from recordingz.summarize import SKILL_PATH
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    with pytest.raises(RecordingError, match="no transcript"):
        minutes_prompt(cfg, rec, "anthropic", "claude-opus-5-5")
    rec.file(".txt").write_text("# T\n[00:00:00] Jana: Ahoj\n", encoding="utf-8")
    r = minutes_prompt(cfg, rec, "anthropic", "claude-opus-5-5")
    target = rec.file(".summary.claude-opus-5-5.md")
    assert r["target"] == str(target) and r["skill"] == str(SKILL_PATH)
    assert r["text"].startswith(f"vygeneruj zápis modelem anthropic:claude-opus-5-5 v jazyce cs z přepisu souboru "
                                f"{rec.file('.txt')} do souboru {target}; postupuj podle skillu {SKILL_PATH}")
    main = minutes_prompt(cfg, rec, cfg.summarize.provider, cfg.summarize.model)["target"]
    assert main != str(rec.summary_path) and main.endswith(".summary.gemma4-31b.md"), "never the main minutes"
    with pytest.raises(RecordingError):
        minutes_prompt(cfg, rec, "anthropic", " ")


def test_person_detail_lists_prints_with_samples(tmp_path):
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import forget_print, person_detail
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [1.0, 0.0, 0.0], rec.stem, "SPEAKER_01", "m")
    vp.enroll("petr-svoboda", [0.0, 1.0, 0.0], "2020-01-01_0000_gone", "SPEAKER_00", "m")
    vp.save()
    d = person_detail(cfg, "petr-svoboda")
    assert d["shown"] == "Petr" and d["model"] == "m" and len(d["prints"]) == 2
    p0 = d["prints"][0]
    assert p0["title"] == "Týdenní sync" and p0["seconds"] == 8 and p0["has_mix"]
    assert [s["text"][:7] for s in p0["samples"]] == ["Já bych"]  # the 1 s "Ano." is too short for a sample
    assert d["prints"][1]["title"] is None and d["prints"][1]["samples"] == []  # recording no longer there
    assert forget_print(cfg, "petr-svoboda", rec.stem, "SPEAKER_01") == 1
    assert len(person_detail(cfg, "petr-svoboda")["prints"]) == 1
    assert forget_print(cfg, "petr-svoboda", None, None) == 1
    assert person_detail(cfg, "petr-svoboda")["prints"] == []


def test_review_single_instance_and_idle_exit(tmp_path):
    import json as _json
    import threading
    import time
    import urllib.request
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path)
    _make_transcribed(tmp_path)
    lock = tmp_path / "lock.json"
    assert rv.running_instance(cfg, lock) is None  # no lock file
    lock.write_text(_json.dumps({"url": "http://127.0.0.1:1/", "pid": 0, "out_dir": str(tmp_path)}), encoding="utf-8")
    assert rv.running_instance(cfg, lock) is None  # stale lock: nobody answers -> ignored
    lock.unlink()
    result = {}
    t = threading.Thread(target=lambda: result.update(url=rv.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock)),
                         daemon=True)
    t.start()
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.05)
    info = _json.loads(lock.read_text(encoding="utf-8"))
    assert rv.running_instance(cfg, lock) == info["url"]                     # answers -> reuse
    assert rv.running_instance(Config(out_dir=tmp_path / "other"), lock) is None  # different folder -> not ours
    assert rv.serve(cfg, None, open_browser=False, lock=lock) == info["url"]  # second call just returns the URL
    urllib.request.urlopen(info["url"] + "api/recordings").read()             # a page request starts the idle clock
    t.join(timeout=10)
    assert not t.is_alive() and result["url"] == info["url"]                  # exited ~1 s after the last request
    assert not lock.exists()


def test_review_stays_up_while_a_page_holds_its_event_stream(tmp_path, monkeypatch):
    """A hidden or minimized window throttles its 15 s pings; an open /api/events stream still counts as a page
    (2026-10-02: the desktop window lost its server and did not show new recordings)."""
    import http.client
    import json as _json
    import threading
    import time
    from urllib.parse import urlparse
    from recordingz.web import review as rv
    monkeypatch.setattr(rv, "SSE_KEEPALIVE_S", 0.3)  # a closed page is noticed at the next keepalive write
    cfg = Config(out_dir=tmp_path)
    lock = tmp_path / "lock.json"
    t = threading.Thread(target=lambda: rv.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock), daemon=True)
    t.start()
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.05)
    u = urlparse(_json.loads(lock.read_text(encoding="utf-8"))["url"])
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200
    time.sleep(3.0)                      # three idle periods without a single ping
    assert t.is_alive()
    resp.close(); conn.close()           # the page goes away: the server stops after a failed keepalive + idle
    t.join(timeout=15)
    assert not t.is_alive()


def test_clean_headings_strips_copied_instructions():
    from recordingz.summarize import HEADINGS, clean_headings
    raw = ("## Shrnutí — 5 to 10 sentences: purpose of the meeting.\nText.\n"
           "## Témata: one bullet per topic\n* a\n## Úkoly — a table\n| Kdo | Úkol |\n## Pojmy\n* ATLAS\n## Mluvčí\n| x |")
    got = clean_headings(raw, HEADINGS["cs"])
    assert got.splitlines()[0] == "## Shrnutí"
    assert "## Témata\n" in got and "## Úkoly\n" in got and "## Pojmy\n" in got and "## Mluvčí\n" in got
    assert "5 to 10" not in got and "| Kdo | Úkol |" in got
    assert len(HEADINGS["cs"]) == len(HEADINGS["en"]) == 7


def test_long_transcript_is_summarized_in_parts(tmp_path, monkeypatch):
    from recordingz import summarize as sm
    from recordingz.config import SummarizeSettings
    from recordingz.llm import LLMResult
    rec = Recording.load(_make_recording(tmp_path))
    segs = [Segment(i * 10, i * 10 + 9, "Věta číslo %d o rozpočtu a termínech projektu." % i, "Jana Nováková" if i % 2 else "Petr Svoboda")
            for i in range(400)]
    calls = []
    def fake_complete(system, user, settings):
        calls.append((system, user))
        return LLMResult(text="## Shrnutí\nx\n## Témata\n* t\n## Rozhodnutí\n1. r\n## Úkoly\n| a |\n## Otevřené otázky\n-\n## Pojmy\n-\n## Mluvčí\n| – | Jana Nováková | |",
                         model="m", input_tokens=10, output_tokens=5)
    monkeypatch.setattr(sm, "complete", fake_complete)
    out = sm.summarize(rec, segs, {"language": "cs"}, SummarizeSettings(provider="ollama", model="m", ollama_max_ctx=8192))
    assert len(calls) >= 3 and "part 1 of" in calls[0][0] and "You merge partial" in calls[-1][0]
    assert "=== Part 1/" in calls[-1][1] and out.count("## Shrnutí") == 1 and "tokens: " in out
    chunks = sm.split_segments(segs, 3000)
    assert len(chunks) > 1 and sum(len(c) for c in chunks) == 400 and all(c for c in chunks)
    # short transcripts and cloud models stay single-shot
    calls.clear()
    sm.summarize(rec, segs[:20], {"language": "cs"}, SummarizeSettings(provider="ollama", model="m", ollama_max_ctx=8192))
    assert len(calls) == 1
    calls.clear()
    sm.summarize(rec, segs, {"language": "cs"}, SummarizeSettings(provider="anthropic", model="m"))
    assert len(calls) == 1


def test_summarize_settings_defaults_and_unknown_provider():
    from recordingz.config import SummarizeSettings
    from recordingz.llm import LLMError, complete
    s = SummarizeSettings()
    assert s.provider == "ollama" and s.model.startswith("gemma4") and s.ollama_url.startswith("http")
    from recordingz.config import load_config
    cfgfile = Path(__file__).parent / "_cmp.toml"
    cfgfile.write_text('[summarize]\ncompare = ["anthropic:claude-opus-5"]\n', encoding="utf-8")
    try:
        assert load_config(cfgfile).summarize.compare == ("anthropic:claude-opus-5",)
    finally:
        cfgfile.unlink()
    with pytest.raises(LLMError):
        complete("sys", "user", SummarizeSettings(provider="nope"))


def test_anthropic_key_comes_from_recordingz_variable(monkeypatch):
    from recordingz import llm
    from recordingz.config import SummarizeSettings
    from recordingz.llm import LLMError, complete
    assert llm.API_KEY_ENV == "RECORDINGZ_ANTHROPIC_API_KEY"
    monkeypatch.delenv("RECORDINGZ_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bogus")  # ours must win over the generic one
    monkeypatch.setenv("RECORDINGZ_ANTHROPIC_API_KEY", "sk-ant-ours")
    import anthropic
    seen = {}
    def fake(api_key=None, **kw):
        seen["key"] = api_key
        raise TypeError("stop here")
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    with pytest.raises(LLMError):
        complete("sys", "user", SummarizeSettings(provider="anthropic", model="claude-opus-5"))
    assert seen["key"] == "sk-ant-ours"


def test_speaker_languages_show_a_second_pass_in_another_language():
    from recordingz.web.review import speaker_languages
    segs = [Segment(0, 20, "Dobrý den", "SPEAKER_00", language="cs"), Segment(20, 30, "Ahoj", "SPEAKER_00", language="cs"),
            Segment(30, 60, "Dobrý deň", "SPEAKER_01"), Segment(60, 62, "ok", "SPEAKER_01", language="en"),
            Segment(62, 80, "Takže", "SPEAKER_02", language="sk"), Segment(80, 90, "So", "SPEAKER_02", language="en")]
    langs = speaker_languages(segs, "sk")
    assert langs["SPEAKER_00"] == {"language": "cs", "own": True, "mixed": False}
    assert langs["SPEAKER_01"] == {"language": "sk", "own": False, "mixed": False}, "2 s of English is not mixed"
    assert langs["SPEAKER_02"] == {"language": "sk", "own": False, "mixed": True}
    assert speaker_languages([Segment(0, 5, "x", "A")], None)["A"] == {"language": "?", "own": False, "mixed": False}


def test_errors_logged_during_a_job_reach_the_page_history(tmp_path):
    import logging
    import time as _time
    from recordingz.web.review import ReviewState
    state = ReviewState(Config(out_dir=tmp_path))
    plog = logging.getLogger("recordingz.pipeline")

    def job():
        plog.error("%s: compare summary %s failed: %s", "s1", "anthropic:claude-x", "model not found")
        plog.info("not an error")

    assert state.run_job("process", job, "zpracováno", "s1")
    for _ in range(100):
        if not state.busy and any(e["job_end"] for e in state.events):
            break
        _time.sleep(0.02)
    texts = [(e["level"], e["text"]) for e in state.events]
    assert ("err", "s1: compare summary anthropic:claude-x failed: model not found") in texts
    assert not any("not an error" in t for _, t in texts)
    assert texts[-1] == ("ok", "zpracováno: s1"), "the job itself still finished"
    plog.error("after the job")  # the handler is gone
    assert not any("after the job" in t for _, t in [(e["level"], e["text"]) for e in state.events])


def test_the_capture_status_file_is_read_and_changes_reach_the_pages(tmp_path):
    import os as _os
    import threading as _threading
    from recordingz.web.review import ReviewState, _capture_watch, capture_status
    f = tmp_path / "recordingz-capture.json"
    assert capture_status(f) == {"running": False, "recording": False}, "no file: no capture app"
    rec = {"app": "recordingz", "pid": _os.getpid(), "running": True, "recording": True, "title": "Plánování",
           "stem": "2026-09-30_1827_planovani", "source": "onsite", "started": "2026-09-30T18:27:05"}
    f.write_text(json.dumps(rec), encoding="utf-8")
    assert capture_status(f)["recording"] is True and capture_status(f)["title"] == "Plánování"
    f.write_text(json.dumps({**rec, "pid": 999999}), encoding="utf-8")
    assert capture_status(f) == {"running": False, "recording": False}, "a crashed app shows nothing"

    state = ReviewState(Config(out_dir=tmp_path))
    q = state.subscribe()
    stop = _threading.Event()
    f.write_text(json.dumps(rec), encoding="utf-8")
    t = _threading.Thread(target=_capture_watch, args=(state, f, 0.05, stop), daemon=True)
    t.start()
    first = q.get(timeout=2)
    assert first["type"] == "capture" and first["recording"] is True
    assert state.status()["capture"]["title"] == "Plánování"
    f.write_text(json.dumps({**rec, "recording": False, "title": None, "stem": None}), encoding="utf-8")
    items = [q.get(timeout=2) for _ in range(3)]
    stop.set()
    assert any(i.get("type") == "capture" and i["recording"] is False for i in items)
    ended = [i for i in state.events if "nahrávání skončilo" in i["text"]]
    assert ended and ended[0]["reload"] is True and ended[0]["stem"] == "2026-09-30_1827_planovani"


def test_unassigned_replies_are_given_out_one_by_one_and_are_no_speaker_to_name(tmp_path):
    from recordingz.pipeline import assign_segments, is_finished
    from recordingz.web.review import build_review, list_recordings, unresolved_labels
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["speaker"] = None  # "Dobrý den…" at 0 s: no diarization turn
    data["segments"][3]["speaker"] = None  # "Rozpočet je hotový…" at 12 s
    data["speakers"] = ["UNKNOWN", "SPEAKER_00", "SPEAKER_01", "Jana Nováková"]
    rec.write_json(rec.transcript_path, data)
    r = build_review(Config(out_dir=tmp_path), rec)
    un = next(s for s in r["speakers"] if s["unassigned"])
    assert [x["at"] for x in un["replies"]] == ["00:00:00", "00:00:12"], "every unassigned reply, in order"
    assert "UNKNOWN" not in unresolved_labels(rec), "not a speaker to name"
    assert list_recordings(Config(out_dir=tmp_path))[0]["unresolved"] == 2  # SPEAKER_00 and SPEAKER_01 only
    with pytest.raises(RecordingError):
        assign_segments(Config(out_dir=tmp_path), rec, [{"start": 0, "speaker": "SPEAKER_99"}])
    assert assign_segments(Config(out_dir=tmp_path), rec, [{"start": 0.0, "speaker": "SPEAKER_00"}]) == 1
    data = rec.read_json(rec.transcript_path)
    assert data["segments"][0]["speaker"] == "SPEAKER_00" and data["segments"][0]["assigned"] == "manual"
    assert "UNKNOWN" in data["speakers"], "one is still unassigned"
    assert "[00:00:12] ?: Rozpočet je hotový" in rec.file(".txt").read_text(encoding="utf-8")
    assign_segments(Config(out_dir=tmp_path), rec, [{"start": 12, "speaker": "Jana Nováková"}])
    assert "UNKNOWN" not in rec.read_json(rec.transcript_path)["speakers"]
    assert not any(s["unassigned"] for s in build_review(Config(out_dir=tmp_path), rec)["speakers"])


def test_the_mic_takes_an_unassigned_reply_when_the_user_spoke(tmp_path):
    import wave
    import numpy as np
    from recordingz.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 30) / sr
    sig = np.zeros_like(t)
    sig[0:5 * sr] = 0.3 * np.sin(2 * np.pi * 220 * t[0:5 * sr])  # the user, 0-5 s
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    segs = [Segment(0, 4, "Ahoj", None), Segment(10, 14, "Dobrý den", None), Segment(20, 30, "x", "SPEAKER_01")]
    apply_mic_track(segs, wav, "Jan Novák")
    assert [s.speaker for s in segs] == ["Jan Novák", None, "SPEAKER_01"], "only where the mic was on"


def test_background_jobs_queue_instead_of_refusing_and_say_what_runs(tmp_path):
    import threading as _threading
    import time as _time
    from recordingz.web.review import ReviewState
    state = ReviewState(Config(out_dir=tmp_path))
    gate, order = _threading.Event(), []

    def first():
        gate.wait(5); order.append("first")

    a = state.run_job("process", first, "zpracováno", "s1", start="s1: zpracování spuštěno")
    b = state.run_job("summary", lambda: order.append("second"), "zápis přegenerován", "s2", start="s2: zápis se generuje")
    assert a == (1, 1) and b == (2, 2), "the second waits, it is not refused"
    for _ in range(100):
        if state.jobs_state()["current"]:
            break
        _time.sleep(0.01)
    js = state.jobs_state()
    assert js["current"]["id"] == 1 and js["current"]["text"] == "s1: zpracování spuštěno"
    assert [q["id"] for q in js["queue"]] == [2]
    assert state.status()["busy"] is True and state.status()["jobs"]["queue"][0]["stem"] == "s2"
    gate.set()
    for _ in range(200):
        if not state.busy:
            break
        _time.sleep(0.01)
    assert order == ["first", "second"], "in order, one at a time"
    ends = [(e["job"], e["text"]) for e in state.events if e["job_end"]]
    assert ends == [(1, "zpracováno: s1"), (2, "zápis přegenerován: s2")]
    js = state.jobs_state()
    assert (js["current"], js["queue"], js["held"], js["asking"]) == (None, [], False, False)


def test_jobs_asked_for_while_the_last_one_ends_run_one_at_a_time(tmp_path):
    """Review 15. 10., T2: the worker said idle before its end event, so a job asked for in that moment started a
    second worker next to it and two jobs ran at once (two on the GPU)."""
    import threading as _threading
    import time as _time
    from recordingz.web.review import ReviewState
    state = ReviewState(Config(out_dir=tmp_path))
    guard, running, peak = _threading.Lock(), [0], [0]

    def work():
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        _time.sleep(0.2)
        with guard:
            running[0] -= 1

    finished = state._finished

    def at_the_end(job, ok, text):  # the end of the first job: the moment the page asks for more
        finished(job, ok, text)
        if job["name"] == "a":
            state.run_job("b", work, "b hotovo")
            state.run_job("c", work, "c hotovo")

    state._finished = at_the_end
    state.run_job("a", lambda: None, "a hotovo")
    for _ in range(300):
        if sum(1 for e in state.events if e["job_end"]) == 3 and not state.busy:
            break
        _time.sleep(0.01)
    assert sum(1 for e in state.events if e["job_end"]) == 3
    assert peak[0] == 1, "one job at a time"


def test_a_recording_stops_the_running_job_which_resumes_after_it(tmp_path, monkeypatch, _no_real_ollama_unload):
    """The GPU must not break the recorded sound: a job's child process is stopped when the capture app starts
    recording (when_recording = ask, and no page to ask: stop), goes back to the front of the queue, and runs
    again once the recording has ended."""
    import subprocess as _sp
    import sys as _sys
    import time as _time
    from recordingz.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv)
        code = "import time; time.sleep(30)" if len(runs) == 1 else "pass"  # the first run is long, the rerun short
        p = _sp.Popen([_sys.executable, "-c", code])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    job, _ = state.run_job("process", ["run-job", "process", "s1"], "zpracováno", "s1", start="s1: zpracování spuštěno")
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    state.set_capture({"running": True, "recording": True, "title": "Porada", "stem": "s2"})
    for _ in range(300):
        js = state.jobs_state()
        if not js["current"] and js["queue"]:
            break
        _time.sleep(0.02)
    js = state.jobs_state()
    assert js["held"] is True and [q["id"] for q in js["queue"]] == [job], "stopped and back in the queue"
    assert any("přerušeno kvůli nahrávání" in e["text"] for e in state.events)
    assert not any(e["job_end"] for e in state.events), "a stopped job did not fail"
    _time.sleep(0.3)
    assert _no_real_ollama_unload == ["gemma4:31b"], "the summary model leaves the GPU for the meeting"
    assert len(runs) == 1, "nothing starts while recording"
    state.set_capture({"running": True, "recording": False})
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert len(runs) == 2, "it ran again after the recording"
    ends = [e for e in state.events if e["job_end"]]
    assert len(ends) == 1 and ends[0]["job"] == job and ends[0]["level"] == "ok"


def test_a_waiting_job_can_go_next_or_stop_the_running_one(tmp_path, monkeypatch):
    """"jako další" moves a waiting job to the front; "hned" also stops the running job, which goes right behind it
    and starts over."""
    import subprocess as _sp
    import sys as _sys
    import time as _time
    import pytest as _pytest
    from recordingz.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        long = argv[-1] == "a" and runs.count("a") == 1  # the first run of "a" is long: it gets stopped
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if long else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    ids = {s: state.run_job("process", ["run-job", "process", s], "zpracováno", s)[0] for s in ("a", "b", "c")}
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["b", "c"]
    assert "jako další" in state.move_job(ids["c"])
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["c", "b"]
    with _pytest.raises(ValueError):
        state.move_job(ids["a"])  # running, not waiting
    assert "pokračuje po něm" in state.move_job(ids["c"], now=True)
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["a", "c", "a", "b"], "c at once, the stopped a right behind it, then b"
    assert any("přerušeno kvůli přednostnímu zpracování" in e["text"] for e in state.events)
    ends = [e for e in state.events if e["job_end"]]
    assert len(ends) == 3 and all(e["level"] == "ok" for e in ends), "the stopped run is not a failure"


def test_a_job_asked_for_during_a_recording_waits_for_its_end(tmp_path, monkeypatch):
    """Nothing ran when the recording started, so nothing was stopped – but a job asked for during it must not
    start either (the GPU would break the recorded sound); with when_recording = continue it runs."""
    import time as _time
    from recordingz.web import review as rv
    runs = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: runs.append(argv[-1]))
    state = rv.ReviewState(Config(out_dir=tmp_path))
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    assert state.jobs_state()["held"] is True
    state.run_job("process", ["run-job", "process", "s1"], "zpracováno", "s1", start="s1: zpracování spuštěno")
    _time.sleep(0.3)
    assert runs == [] and [q["stem"] for q in state.jobs_state()["queue"]] == ["s1"]
    assert any("čeká na konec nahrávání" in e["text"] for e in state.events)
    state.set_capture({"running": True, "recording": False})
    for _ in range(200):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["s1"], "it ran after the recording"

    cont = rv.ReviewState(Config(out_dir=tmp_path, transcribe=TranscribeSettings(when_recording="continue")))
    cont.set_capture({"running": True, "recording": True, "title": "Porada"})
    cont.run_job("process", ["run-job", "process", "s2"], "zpracováno", "s2")
    for _ in range(200):
        if not cont.busy:
            break
        _time.sleep(0.02)
    assert runs == ["s1", "s2"], "continue: it runs during the recording"


def test_cloud_minutes_run_during_a_recording_unless_all_is_stopped(tmp_path, monkeypatch, _no_real_ollama_unload):
    """Only GPU work breaks the recorded sound: during a recording a light job (minutes in the cloud) runs while a
    heavy one waits, and stopping the heavy work leaves a running light job alone. "Přerušit vše" stops it too and
    makes the light jobs wait as well."""
    import subprocess as _sp
    import sys as _sys
    import time as _time
    from recordingz.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        long = argv[-1] == "cloud2" and runs.count("cloud2") == 1
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if long else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)

    def idle(st):
        for _ in range(300):
            if not st.current:
                return
            _time.sleep(0.02)

    state = rv.ReviewState(Config(out_dir=tmp_path))
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    state.run_job("process", ["run-job", "process", "gpu"], "zpracováno", "gpu")
    state.run_job("summary", ["run-job", "summarize-as", "cloud"], "zápis hotový", "cloud", light=True)
    for _ in range(300):
        if runs:
            break
        _time.sleep(0.02)
    idle(state)
    assert runs == ["cloud"], "the cloud minutes ran, the GPU job waits"
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["gpu"]

    state.run_job("summary", ["run-job", "summarize-as", "cloud2"], "zápis hotový", "cloud2", light=True)
    for _ in range(300):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    state.pause_for_recording("test")  # "Přerušit náročné": the running cloud job goes on
    assert state.running and state.running["stem"] == "cloud2" and not state.running.get("stopped")
    state.pause_for_recording("test", everything=True)  # "Přerušit vše"
    for _ in range(300):
        js = state.jobs_state()
        if not js["current"] and len(js["queue"]) == 2:
            break
        _time.sleep(0.02)
    js = state.jobs_state()
    assert js["hold_all"] is True and [q["stem"] for q in js["queue"]] == ["cloud2", "gpu"], "stopped, back in front"
    assert any("veškeré zpracování přerušeno" in e["text"] for e in state.events)
    state.set_capture({"running": True, "recording": False})
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["cloud", "cloud2", "cloud2", "gpu"], "after the recording: all of it, in order"


def test_cancel_drops_a_waiting_job_and_stops_the_running_one_for_good(tmp_path, monkeypatch):
    import subprocess as _sp
    import sys as _sys
    import time as _time
    from recordingz.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if argv[-1] == "a" else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    ids = {s: state.run_job("process", ["run-job", "process", s], "zpracováno", s)[0] for s in ("a", "b", "c")}
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    assert "vyřazeno z fronty" in state.cancel_job(ids["b"])
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["c"]
    assert "ruší" in state.cancel_job(ids["a"])
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["a", "c"], "b never ran, a was not run again"
    ends = {e["job"]: e for e in state.events if e["job_end"]}
    assert "zrušeno" in ends[ids["a"]]["text"] and ends[ids["a"]]["level"] == "info", "cancelled is not a failure"
    assert "zrušeno" in ends[ids["b"]]["text"] and ends[ids["c"]]["level"] == "ok"
    import pytest as _pytest
    with _pytest.raises(ValueError):
        state.cancel_job(ids["c"])  # finished: nothing to cancel


def test_with_a_page_open_the_recording_asks_first(tmp_path, monkeypatch):
    from recordingz.web import review as rv
    import threading as _threading
    gate = _threading.Event()
    state = rv.ReviewState(Config(out_dir=tmp_path))
    q = state.subscribe()  # a page is open
    state.run_job("summary", lambda: gate.wait(5), "zápis přegenerován", "s1")
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    assert state.jobs_state()["asking"] is True and state.held is False, "asks, does not stop yet"
    state.keep_running()
    assert state.jobs_state()["asking"] is False and state.held is False
    gate.set()
    state.unsubscribe(q)


def test_nearest_voice_group_is_a_hint_from_0_45():
    from recordingz.web.review import GROUP_HINT_MIN, _nearest_group
    emb = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.8, 0.6], "SPEAKER_02": [0.0, 1.0], "SPEAKER_03": [0.7, 0.71]}
    secs = {"SPEAKER_00": 60.0, "SPEAKER_01": 40.0, "SPEAKER_02": 30.0}  # SPEAKER_03 has no replies left (merged)
    assert _nearest_group("SPEAKER_00", emb, secs) == {"label": "SPEAKER_01", "score": 0.8}
    assert _nearest_group("SPEAKER_02", emb, secs) == {"label": "SPEAKER_01", "score": 0.6}
    assert _nearest_group("SPEAKER_02", {"SPEAKER_02": [0.0, 1.0], "SPEAKER_00": [1.0, 0.05]}, secs) is None
    assert GROUP_HINT_MIN == 0.45


def test_a_recording_is_not_recognised_by_its_own_prints(tmp_path):
    from recordingz.voiceprints import Voiceprints
    vp = Voiceprints.load(tmp_path)
    vp.enroll("jana", [1.0, 0.0], "2026-10-01_0900_a", "SPEAKER_00")
    vp.enroll("petr", [0.6, 0.8], "2026-09-01_0900_b", "SPEAKER_01")
    assert vp.scores([1.0, 0.0])[0][0] == "jana"
    ranked = vp.scores([1.0, 0.0], exclude_stem="2026-10-01_0900_a")  # Jana's only print is from this recording
    assert [pid for pid, _ in ranked] == ["petr"]
    assert vp.recognize({"SPEAKER_00": [1.0, 0.0]}, 0.5, 0.1, exclude_stem="2026-10-01_0900_a") == {"SPEAKER_00": ("petr", 0.6)}


def test_replies_of_a_mixed_group_move_to_another_speaker_a_new_one_or_unassigned(tmp_path):
    from recordingz.pipeline import NEW_SPEAKER, assign_segments, speaker_replies
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    assert [r["start"] for r in speaker_replies(rec, "SPEAKER_00")] == [0, 20]
    n = assign_segments(cfg, rec, [{"start": 0, "from": "SPEAKER_00", "speaker": "SPEAKER_01"},
                                   {"start": 20, "from": "SPEAKER_00", "speaker": NEW_SPEAKER},
                                   {"start": 4, "from": "SPEAKER_01", "speaker": NEW_SPEAKER},  # the same new one
                                   {"start": 12, "from": "Jana Nováková", "speaker": "UNKNOWN"}])
    assert n == 4
    data = rec.read_json(rec.transcript_path)
    who = {s["start"]: s["speaker"] for s in data["segments"]}
    assert who == {0: "SPEAKER_01", 4: "SPEAKER_02", 5: "SPEAKER_01", 12: None, 20: "SPEAKER_02"}
    assert data["speakers"] == ["SPEAKER_01", "SPEAKER_02", "UNKNOWN"], "emptied groups go, the new one is there"
    assert [s.get("assigned") for s in data["segments"]] == ["manual", "manual", None, None, "manual"]
    assert "SPEAKER_02" in rec.file(".txt").read_text(encoding="utf-8"), "exports regenerated"
    with pytest.raises(RecordingError):
        assign_segments(cfg, rec, [{"start": 5, "from": "SPEAKER_01", "speaker": "SPEAKER_77"}])


def test_cloud_only_run_uses_cloud_services_and_needs_their_keys(tmp_path, monkeypatch):
    from recordingz import fasttrack, settings
    from recordingz.config import SummarizeSettings
    from recordingz.fasttrack import cloud_only
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path, summarize=SummarizeSettings(compare=("ollama:qwen3:8b", "anthropic:claude-opus-5-5")))
    fast = cloud_only(cfg)
    assert (fast.transcribe.provider, fast.summarize.provider, fast.summarize.model) == ("elevenlabs", "anthropic", "claude-opus-5-5")
    assert fast.summarize.compare == (), "no comparison on the fast track: the big model is trusted"
    assert fast.video.enabled is False and cfg.transcribe.provider == "whisperx", "the config itself stays"
    keys = {"anthropic": "k"}
    monkeypatch.setattr(settings, "get_secret", lambda name: keys.get(name, ""))
    ready = fasttrack.cloud_ready(cfg)
    assert not ready["ok"] and "elevenlabs" in ready["missing"]
    rec = _make_transcribed(tmp_path)
    state = rv.ReviewState(cfg)
    with pytest.raises(ValueError, match="klíče"):
        state.run_process(rec, cloud=True)
    keys["elevenlabs"] = "k"
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    state.run_process(rec, force=True, cloud=True)
    import time as _time
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    assert calls[0][-4:] == [rec.stem, "--force", "--cloud", "--minutes-when-named"], "the fast track waits for names too"
    calls.clear()
    state.run_process(rec, cloud=True, minutes_anyway=True, voices=True)
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    assert calls[0][-3:] == [rec.stem, "--cloud", "--voices"]


def test_the_processing_offer_says_what_each_way_transcribes_and_writes_the_minutes_with(tmp_path):
    from recordingz.config import SummarizeSettings, TranscribeSettings
    from recordingz.fasttrack import plans
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path, summarize=SummarizeSettings(compare=("ollama:qwen3:8b", "anthropic:claude-opus-5-5")))
    p = plans(cfg)
    assert p["local"] == {"transcribe": "WhisperX large-v3 + mluvčí pyannote na grafické kartě",
                          "minutes": "Ollama gemma4:31b na grafické kartě",
                          "compare": "Ollama qwen3:8b, Claude claude-opus-5-5"}
    assert p["cloud"] == {"transcribe": "ElevenLabs scribe_v2 (i mluvčí, v cloudu)",
                          "minutes": "Claude claude-opus-5-5 (v cloudu)", "compare": ""}, "the fast track: no comparison"
    cloudy = Config(out_dir=tmp_path, transcribe=TranscribeSettings(provider="openai", cloud_provider="openai"),
                    summarize=SummarizeSettings(provider="anthropic", model="claude-sonnet-5-5", enabled=False))
    assert plans(cloudy)["local"] == {"transcribe": "OpenAI gpt-4o-transcribe-diarize (i mluvčí, v cloudu)",
                                      "minutes": "Claude claude-sonnet-5-5 (v cloudu)", "compare": ""}, "as configured"
    rec = _make_transcribed(tmp_path)
    rec.transcript_path.unlink()
    offer = rv.build_review(cloudy, rec)
    assert offer["plans"] == plans(cloudy) and offer["minutes_auto"] is False, "vytvořit zápis unticked by the config"


def test_run_job_cloud_flag_switches_the_services(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    rec = _make_transcribed(tmp_path)
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(
        (cfg.transcribe.provider, cfg.summarize.provider, cfg.video.enabled)))
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--cloud"])
    assert res.exit_code == 0, res.output
    assert seen == [("elevenlabs", "anthropic", False)]


def test_vytvorit_zapis_box_overrides_the_config_for_one_run(tmp_path, monkeypatch):
    """The page's "vytvořit zápis": --minutes / --no-minutes for this job; without it [summarize] enabled decides."""
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    from recordingz.web import review as rv
    rec = _make_transcribed(tmp_path)
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(cfg.summarize.enabled))
    for flags in ([], ["--no-minutes"], ["--minutes"]):
        res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, *flags])
        assert res.exit_code == 0, res.output
    assert seen == [True, False, True]
    state = rv.ReviewState(Config(out_dir=tmp_path))
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    import time as _time
    for minutes, tail in ((False, "--no-minutes"), (True, "--minutes"), (None, "--minutes-when-named")):
        calls.clear()
        state.run_process(rec, minutes=minutes)
        for _ in range(100):
            if calls:
                break
            _time.sleep(0.02)
        assert calls[0][-1] == tail, calls


def test_srovnavaci_zapis_box_drops_the_comparison_for_one_local_run(tmp_path, monkeypatch):
    """The page's "srovnávací zápis" unticked: --no-compare, only the main minutes; the fast track never compares."""
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl, settings
    from recordingz.web import review as rv
    rec = _make_transcribed(tmp_path)
    toml = tmp_path / "recordingz.toml"
    toml.write_text('[summarize]\ncompare = ["ollama:qwen3:8b"]\n', encoding="utf-8")
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(cfg.summarize.compare))
    for flags in ([], ["--no-compare"], ["--cloud"]):
        res = CliRunner().invoke(cli.app, ["--config", str(toml), "--out-dir", str(tmp_path),
                                           "run-job", "process", rec.stem, *flags])
        assert res.exit_code == 0, res.output
    assert seen == [("ollama:qwen3:8b",), (), ()]
    monkeypatch.setattr(settings, "get_secret", lambda name: "k")
    state = rv.ReviewState(Config(out_dir=tmp_path))
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    import time as _time
    for kw, has in (({"compare": False}, True), ({}, False), ({"compare": False, "cloud": True}, False)):
        calls.clear()
        state.run_process(rec, **kw)
        for _ in range(100):
            if calls:
                break
            _time.sleep(0.02)
        assert ("--no-compare" in calls[0]) is has, (kw, calls)


def test_vytvaret_alternativni_zapis_off_writes_the_comparison_only_when_ticked(tmp_path, monkeypatch):
    """Nastavení → Zápis → Vytvářet alternativní zápis pro porovnání off: processing and regenerating write only the
    main minutes, unless the page's "srovnávací zápis" is ticked for the run (--compare); the page gets the setting."""
    from typer.testing import CliRunner
    from dataclasses import replace
    from recordingz import cli, fasttrack, pipeline as pl, settings
    from recordingz.config import SummarizeSettings
    from recordingz.web import review as rv
    rec = _make_transcribed(tmp_path)
    toml = tmp_path / "recordingz.toml"
    toml.write_text('[summarize]\ncompare = ["ollama:qwen3:8b"]\ncompare_auto = false\n', encoding="utf-8")
    seen, process = [], pl.do_process
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(cfg.summarize.compare_auto))
    for flags in ([], ["--compare"]):
        res = CliRunner().invoke(cli.app, ["--config", str(toml), "--out-dir", str(tmp_path),
                                           "run-job", "process", rec.stem, *flags])
        assert res.exit_code == 0, res.output
    assert seen == [False, True]
    compared = []
    monkeypatch.setattr(pl, "do_summarize", lambda cfg, r, force=False: None)
    monkeypatch.setattr(pl, "do_summarize_compare", lambda cfg, r, force=False: compared.append(r.stem) or [])
    off = Config(out_dir=tmp_path, summarize=SummarizeSettings(compare=("ollama:qwen3:8b",), compare_auto=False))
    process(off, rec)
    cli._run_job(off, rec, "summary", False, "", "")
    assert compared == [], "off: no comparison by itself"
    process(replace(off, summarize=replace(off.summarize, compare_auto=True)), rec)
    assert compared == [rec.stem]
    monkeypatch.setattr(settings, "get_secret", lambda name: "k")
    assert fasttrack.choices(off)["compare_auto"] is False
    state = rv.ReviewState(off)
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    import time as _time
    state.run_process(rec, compare=True)
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    assert "--compare" in calls[0], calls


def test_only_me_and_one_other_names_every_longer_unnamed_voice_and_the_minutes_do_not_wait(tmp_path, monkeypatch):
    """"na hovoru jsem jen já a Marek Hudec" confirmed before processing: the longer voice groups the microphone did
    not take for the user are Marek – also one split off by the diarization and one the voice took for someone else;
    a short one stays; the person is created; the minutes are written right away. Without the user found on the
    microphone nothing is named (the user's own voice would get the name)."""
    from recordingz import pipeline as pl
    from recordingz.people import People
    from recordingz.web import review as rv
    rec = Recording.load(_make_recording(tmp_path))
    segs = [{"start": 0, "end": 30, "text": "a", "speaker": "Jan Novák"},
            {"start": 30, "end": 60, "text": "b", "speaker": "SPEAKER_00"},
            {"start": 60, "end": 85, "text": "c", "speaker": "SPEAKER_01"},
            {"start": 85, "end": 88, "text": "d", "speaker": "SPEAKER_02"}]
    data = {"format": 1, "language": "cs", "speaker_sources": ["mic", "voiceprint", "diarization"],
            "speakers": ["Jan Novák", "SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], "segments": segs,
            "voice_matches": {"SPEAKER_01": {"person": "petr-svoboda", "score": 0.7}}}
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path, "nick"); ppl.ensure("Petr Svoboda"); ppl.ensure("Jan Novák"); ppl.save()
    rec.write_json(rec.speakers_path, {"SPEAKER_01": "petr-svoboda"})
    cfg = Config(out_dir=tmp_path, user_name="Jan Novák")
    assert pl.only_with_step(cfg, rec, "Marek Hudec") == {"name": "Marek Hudec", "person": "marek-hudec",
                                                          "labels": ["SPEAKER_00", "SPEAKER_01"]}
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "marek-hudec", "SPEAKER_01": "marek-hudec"}
    assert People.load(tmp_path, "nick").find("Marek Hudec") is not None, "a new person"
    assert rec.read_json(rec.transcript_path)["voice_matches"] == {}, "the user decided, not the voice"
    assert pl.unnamed_speakers(rec) == [] and "] Marek: c" in rec.file(".txt").read_text(encoding="utf-8")
    rec.write_json(rec.transcript_path, {**data, "speaker_sources": ["diarization"]})
    rec.write_json(rec.speakers_path, {})
    assert pl.only_with_step(cfg, rec, "Marek Hudec") == {} and rec.read_json(rec.speakers_path) == {}, "no mic: no"

    rec.write_json(rec.transcript_path, data)
    written = []
    monkeypatch.setattr(pl, "do_transcribe", lambda cfg, r, force=False: None)
    monkeypatch.setattr(pl, "do_summarize", lambda cfg, r, force=False: written.append("minutes"))
    monkeypatch.setattr(pl, "do_summarize_compare", lambda cfg, r, force=False: None)
    pl.do_process(cfg, rec, minutes_anyway=False)
    assert written == [], "unnamed: the minutes wait"
    pl.do_process(cfg, rec, minutes_anyway=False, only_with="Marek Hudec")
    assert written == ["minutes"], "confirmed: no waiting"

    state = rv.ReviewState(cfg)
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    state.run_process(rec, only_with=" Marek Hudec ")
    import time as _time
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    assert calls[0][-2:] == ["--only-with", "Marek Hudec"]


def test_a_choice_per_step_local_or_cloud_with_the_settings_as_defaults(tmp_path, monkeypatch):
    """The page's processing: the transcript and the minutes each on this PC or in the cloud. The defaults follow the
    settings; a cloud step without its key is offered off with the reason; the job gets the choice per step."""
    from recordingz import settings
    from recordingz.config import SummarizeSettings, TranscribeSettings
    from recordingz.fasttrack import choices, choose
    from recordingz.web import review as rv
    keys = {"elevenlabs": "k"}
    monkeypatch.setattr(settings, "get_secret", lambda name: keys.get(name, ""))
    cfg = Config(out_dir=tmp_path)
    c = choices(cfg)
    assert (c["transcribe"]["default"], c["minutes"]["default"]) == ("local", "local")
    assert c["transcribe"]["cloud"]["missing"] == "" and "Claude" in c["minutes"]["cloud"]["missing"]
    both = choose(cfg, "cloud", "cloud")
    assert (both.transcribe.provider, both.video.enabled, both.summarize.provider, both.summarize.model) == \
        ("elevenlabs", False, "anthropic", "claude-opus-5-5"), "the cloud: its own speakers, no window OCR"
    mixed = choose(cfg, "cloud", "local")
    assert (mixed.transcribe.provider, mixed.summarize.provider, mixed.summarize.model) == ("elevenlabs", "ollama", "gemma4:31b")
    claude = Config(out_dir=tmp_path, transcribe=TranscribeSettings(provider="openai"),
                    summarize=SummarizeSettings(provider="anthropic", model="claude-sonnet-5-5", enabled=False))
    c = choices(claude)
    assert (c["transcribe"]["default"], c["minutes"]["default"]) == ("cloud", "off"), "as set; minutes off"
    assert c["minutes"]["local"]["missing"], "no local model set: the local minutes say so"
    assert choose(claude, "local", "cloud").transcribe.provider == "whisperx"
    assert choose(claude, "", "cloud").summarize.model == "claude-sonnet-5-5", "the cloud choice: the configured model"
    with pytest.raises(ValueError, match="lokální model"):
        choose(claude, "", "local")
    rec = _make_transcribed(tmp_path)
    state = rv.ReviewState(cfg)
    with pytest.raises(ValueError, match="Claude"):
        state.run_process(rec, minutes_with="cloud")
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    state.run_process(rec, transcribe="cloud", minutes_with="local", voices=True)
    import time as _time
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    argv = calls[0]
    assert argv[argv.index("--transcribe-with") + 1] == "cloud" and argv[argv.index("--minutes-with") + 1] == "local"
    assert "--voices" in argv and "--cloud" not in argv
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(
        (cfg.transcribe.provider, cfg.summarize.provider, cfg.video.enabled)))
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem,
                                       "--transcribe-with", "cloud", "--minutes-with", "local"])
    assert res.exit_code == 0, res.output
    assert seen == [("elevenlabs", "ollama", False)]


def test_the_call_partner_suggested_for_only_me_and(tmp_path):
    """Prefilled for the user to confirm: the direct call, the only other participant, or a window title shaped like a
    name even when the compact view made the automatic direct-call rule careful."""
    from recordingz import pipeline as pl
    cfg = Config(out_dir=tmp_path, user_name="Jan Novák")
    seen = ["Kompaktní zobrazení schůzky | Marek Hudec | Microsoft Teams"]
    rec = Recording.load(_make_recording(tmp_path, title="Marek Hudec", title_source="window", participants=[],
                                         teams_windows_seen=seen))
    assert pl.direct_call_name(cfg, rec) == "" and pl.call_partner(cfg, rec) == "Marek Hudec"
    rec2 = Recording.load(_make_recording(tmp_path, stem="2026-09-05_1000_x", title="Team standup",
                                          title_source="window", participants=[]))
    assert pl.call_partner(cfg, rec2) == "", "a meeting subject is not a person"
    rec3 = Recording.load(_make_recording(tmp_path, stem="2026-09-06_1000_y", title="Planning",
                                          participants=[{"name": "Jan Novák"}, {"name": "Petr Svoboda"}]))
    assert pl.call_partner(cfg, rec3) == "Petr Svoboda", "the only participant who is not the user"


def test_fast_track_local_voices_run_before_the_minutes_decision(tmp_path, monkeypatch):
    """--cloud --voices: the cloud transcript, then the local voices (they may name everyone), then the minutes
    only when nobody is left unnamed; a failing voices step just leaves the minutes waiting."""
    from typer.testing import CliRunner
    from recordingz import cli, fasttrack, pipeline as pl
    rec = _make_transcribed(tmp_path)
    order = []
    monkeypatch.setattr(pl, "do_transcribe", lambda cfg, r, force=False: order.append("transcript"))
    monkeypatch.setattr(pl, "do_export", lambda cfg, r: None)
    monkeypatch.setattr(pl, "do_summarize", lambda cfg, r, force=False: order.append(("minutes", cfg.summarize.provider)))
    monkeypatch.setattr(pl, "do_summarize_compare", lambda cfg, r, force=False: None)
    unnamed = [["SPEAKER_00"]]
    monkeypatch.setattr(pl, "unnamed_speakers", lambda r, min_seconds=20.0: unnamed[0])

    def voices(cfg, r):
        order.append("voices")
        unnamed[0] = []  # recognised everyone
    monkeypatch.setattr(fasttrack, "add_voices", voices)
    args = ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--cloud", "--minutes-when-named"]
    res = CliRunner().invoke(cli.app, args + ["--voices"])
    assert res.exit_code == 0, res.output
    assert order == ["transcript", "voices", ("minutes", "anthropic")]
    order.clear(); unnamed[0] = ["SPEAKER_00"]
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert order == ["transcript"], "no local voices: the cloud names nobody, so the minutes wait"

    def broken(cfg, r):
        raise RuntimeError("no GPU")
    monkeypatch.setattr(fasttrack, "add_voices", broken)
    order.clear()
    res = CliRunner().invoke(cli.app, args + ["--voices"])
    assert res.exit_code == 0, res.output
    assert order == ["transcript"]


def test_fasttrack_voices_come_from_the_local_voice_that_overlaps_each_cloud_group(tmp_path, monkeypatch):
    """Fast-track post-processing: the cloud keeps the groups, the local diarization lends their voices."""
    from recordingz import fasttrack, pipeline as pl
    from recordingz.providers import whisperx_provider as wp
    segs = [{"start": 0, "end": 10, "text": "a", "speaker": "speaker_0"},
            {"start": 10, "end": 20, "text": "b", "speaker": "speaker_1"},
            {"start": 20, "end": 30, "text": "c", "speaker": "speaker_1"}]
    turns = [(0, 10, "SPEAKER_00"), (10, 22, "SPEAKER_01"), (22, 30, "SPEAKER_02")]  # speaker_1 is two local voices
    emb = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0], "SPEAKER_02": [0.6, 0.8]}
    voices, mixed = fasttrack.group_voices(segs, turns, emb)
    assert voices == {"speaker_0": [1.0, 0.0], "speaker_1": [0.0, 1.0]}
    assert mixed == {"speaker_1": {"voices": ["SPEAKER_01", "SPEAKER_02"], "share": 0.4}}

    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    assert not fasttrack.needs_voices(data), "a local transcript has its own voices"
    data.update(provider="elevenlabs", segments=segs, speakers=["speaker_0", "speaker_1"])
    rec.write_json(rec.transcript_path, data)
    assert fasttrack.needs_voices(data)
    pytest.importorskip("whisperx")  # add_voices loads the audio with whisperx (the [whisperx] extra)
    import pandas as pd
    monkeypatch.setattr(wp, "diarize_audio", lambda wav, model, device: (
        pd.DataFrame([{"start": a, "end": b, "speaker": v} for a, b, v in turns]), emb))
    seen = []
    monkeypatch.setattr(pl, "recognize_voices", lambda cfg, r: seen.append(r.stem) or {})
    from recordingz.config import VoiceprintSettings
    out = fasttrack.add_voices(Config(out_dir=tmp_path, voiceprints=VoiceprintSettings(enabled=True)), rec)
    assert out == {"groups": 2, "matches": 0, "mixed": ["speaker_1"]}
    stored = rec.read_json(rec.transcript_path)
    assert stored["speaker_embeddings"]["speaker_0"] == [1.0, 0.0] and not fasttrack.needs_voices(stored)
    assert seen == [rec.stem], "the standard voice recognition ran"


def test_a_job_records_how_long_each_part_took(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl, timings
    from recordingz.web.review import build_review
    rec = _make_transcribed(tmp_path)

    def fake_process(cfg, r, force=False, **kw):
        with timings.step("přepis (fake)", {"transcribe_s": 1.5, "align_s": 0.5, "note": "x"}):
            pass
        with timings.step("zápis (ollama m)"):
            pass
    monkeypatch.setattr(pl, "do_process", fake_process)
    for _ in range(2):
        res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--force"])
        assert res.exit_code == 0, res.output
    runs = timings.latest(rec, 5)
    assert len(runs) == 2 and runs[0]["job"] == "process --force" and runs[0]["ok"] is True
    assert [s["step"] for s in runs[0]["steps"]] == ["přepis (fake)", "zápis (ollama m)"]
    assert runs[0]["steps"][0]["parts"] == {"transcribe_s": 1.5, "align_s": 0.5}, "numbers only"
    assert build_review(Config(out_dir=tmp_path), rec)["timings"][0]["job"] == "process --force"

    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem])
    assert timings.latest(rec, 5)[0]["ok"] is False, "a failed job is recorded too"
    with timings.step("outside a job"):  # a CLI command: measured and logged, nothing written
        pass
    assert len(timings.latest(rec, 10)) == 3


def test_a_cloud_request_that_hangs_is_tried_once_more_then_fails(tmp_path, monkeypatch):
    import requests
    from recordingz.providers import cloud
    from recordingz.providers.base import ProviderError
    up = tmp_path / "a.webm"
    up.write_bytes(b"x")
    calls = []

    def hang(url, **kw):
        calls.append(kw["timeout"])
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(requests, "post", hang)
    with pytest.raises(ProviderError, match="did not answer"):
        cloud.post_with_retry("https://x", headers={}, data={}, upload=up, timeout=(30.0, 400.0), vendor="ElevenLabs")
    assert calls == [(30.0, 400.0), (30.0, 400.0)]
    assert cloud.request_timeout(tmp_path / "missing.wav") == (30.0, 1200.0)


def test_fasttrack_voices_are_unit_vectors_and_the_group_hint_never_exceeds_one():
    from recordingz.fasttrack import group_voices
    from recordingz.web.review import _nearest_group
    voices, _ = group_voices([{"start": 0, "end": 5, "speaker": "a"}, {"start": 5, "end": 9, "speaker": "b"}],
                             [(0, 5, "X"), (5, 9, "Y")], {"X": [3.0, 4.0], "Y": [4.0, 3.0]})
    assert voices == {"a": [0.6, 0.8], "b": [0.8, 0.6]}
    hint = _nearest_group("a", {"a": [3.0, 4.0], "b": [4.0, 3.0]}, {"a": 5.0, "b": 4.0})  # not unit: still a cosine
    assert hint == {"label": "b", "score": 0.96}


def test_processing_places_say_what_runs_where():
    from recordingz.settings import processing_places
    base = {"transcribe.provider": "whisperx", "transcribe.align": True, "transcribe.diarize": True,
            "transcribe.per_speaker_language": True, "video.enabled": True, "user.name": "Jan", "voiceprints.enabled": True,
            "summarize.enabled": True, "summarize.provider": "ollama", "summarize.model": "gemma4:31b",
            "summarize.compare": ["anthropic:claude-opus-5-5"], "transcribe.cloud_provider": "elevenlabs",
            "summarize.cloud_model": "claude-opus-5-5"}
    where = {r["step"]: r["where"] for r in processing_places(base) if not r.get("fasttrack")}
    assert where["Přepis řeči"] == where["Zarovnání slov (časy slov, hranice replik)"] == "local"
    assert where["Srovnávací zápis"] == "cloud", "a Claude comparison makes the normal run a combination"
    cloud = {r["step"]: (r["where"], r["choice"]) for r in processing_places({**base, "transcribe.provider": "elevenlabs"})}
    assert cloud["Přepis řeči"] == ("cloud", "transcribe.provider")
    assert cloud["Rozlišení mluvčích (diarizace)"] == ("cloud", ""), "part of the cloud transcription, no own setting"
    assert cloud["Poznání po hlase"][0] == "off"
    assert processing_places(base)[-1]["fasttrack"] is True


def test_minutes_by_another_model_count_as_minutes_and_the_main_tab_says_so(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.config import SummarizeSettings
    from recordingz.web.review import has_minutes, list_recordings, summary_info
    rec = _make_transcribed(tmp_path)
    rec.summary_path.unlink()
    assert not has_minutes(rec) and list_recordings(Config(out_dir=tmp_path))[0]["summary"] is False
    other = rec.file(".summary.claude-opus-5-5.md")
    other.write_text("<!-- recordingz summary | anthropic: claude-opus-5-5 | created: x -->\n# T", encoding="utf-8")
    assert has_minutes(rec) and list_recordings(Config(out_dir=tmp_path))[0]["summary"] is True
    rec.summary_path.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
    from recordingz.web.review import recording_docs
    assert [x["label"] for x in recording_docs(rec)["summaries"]] == ["claude-opus-5-5 · hlavní", "claude-opus-5-5"]
    assert summary_info(rec.summary_path, True)["label"] == "claude-opus-5-5", "alone it is just the model"
    calls = []
    monkeypatch.setattr(pl, "summarize_as", lambda cfg, r, p, m: calls.append((p, m)))
    cfg = Config(out_dir=tmp_path, summarize=SummarizeSettings(provider="anthropic", model="claude-opus-5-5",
                                                              compare=("anthropic:claude-opus-5-5", "ollama:qwen3:8b")))
    pl.do_summarize_compare(cfg, rec, force=True)
    assert calls == [("ollama", "qwen3:8b")], "no second copy of the main minutes"


def test_the_list_flags_transcripts_without_word_alignment(tmp_path):
    from recordingz.web.review import is_unaligned, list_recordings
    rec = _make_transcribed(tmp_path)
    assert list_recordings(Config(out_dir=tmp_path))[0]["unaligned"] is False
    data = rec.read_json(rec.transcript_path)
    data.update(provider="whisperx", timings={"transcribe_s": 60.0, "diarize_s": 20.0})  # alignment skipped
    rec.write_json(rec.transcript_path, data)
    assert list_recordings(Config(out_dir=tmp_path))[0]["unaligned"] is True
    assert not is_unaligned({"provider": "whisperx", "timings": {"align_s": 12.0}})
    assert not is_unaligned({"provider": "elevenlabs", "timings": {}}), "the cloud brings its own word times"


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


def test_missing_local_minutes_model_is_said_and_the_job_ends_as_an_error(tmp_path, monkeypatch):
    import time as _time
    from recordingz import settings
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: False)
    assert "nemá model gemma4:31b" in rv.minutes_problem(cfg)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: None)
    assert "neběží" in rv.minutes_problem(cfg)

    def cli(argv, on_proc, on_line):  # the child logs the failed minutes, the job itself ends fine
        on_line("2026-10-06 ERROR 2026-10-06_1030_x: zápis se nepodařil: Ollama nemá model gemma4:31b – stáhněte ho")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(cfg)
    state.run_job("process", ["run-job", "process", "s1"], "zpracováno", "s1")
    for _ in range(200):  # idle first, the job's end event right after it
        if not state.busy and any(e["job_end"] for e in state.events):
            break
        _time.sleep(0.02)
    end = [e for e in state.events if e["job_end"]][-1]
    assert end["level"] == "err" and "ale část se nepodařila" in end["text"] and "Ollama nemá model" in end["text"]


def test_pulling_the_minutes_model_is_a_job_whose_progress_shows(tmp_path, monkeypatch):
    import time as _time
    from recordingz import settings
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: False)
    assert rv.build_review(cfg, rec)["minutes_pull"] == "gemma4:31b", "Ollama runs, the model is missing: offer it"
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: None)
    assert rv.build_review(cfg, rec)["minutes_pull"] == "", "Ollama not running: nothing to download into"
    seen = []

    def cli(argv, on_proc, on_line):
        seen.append(argv[-3:])
        on_line("PROGRESS stahuje se gemma4:31b: 45% (9.0 / 19.9 GB)")
        texts.append(state.jobs_state()["current"]["text"])
    texts: list = []
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(cfg)
    settings._ollama_seen[("u", "m")] = (0.0, False)
    state.run_pull("gemma4:31b")
    for _ in range(200):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert seen == [["run-job", "pull", "gemma4:31b"]]
    assert texts == ["stahuje se gemma4:31b: 45% (9.0 / 19.9 GB)"], "the progress is the job's text"
    assert settings._ollama_seen == {}, "after the download Ollama is asked again"
    with pytest.raises(ValueError):
        state.run_pull("gemma; rm -rf")


def test_run_job_pull_prints_progress(monkeypatch, capsys):
    from recordingz import cli, llm
    from typer.testing import CliRunner

    def fake_pull(url, model, progress):
        progress(0, 0, "pulling manifest")
        progress(10_000_000_000, 20_000_000_000, "pulling")
        progress(20_000_000_000, 20_000_000_000, "success")
    monkeypatch.setattr(llm, "ollama_pull", fake_pull)
    res = CliRunner().invoke(cli.app, ["run-job", "pull", "gemma4:31b"])
    assert res.exit_code == 0, res.output
    assert "PROGRESS stahuje se gemma4:31b: pulling manifest" in res.output
    assert "PROGRESS stahuje se gemma4:31b: 100% (20.0 / 20.0 GB)" in res.output and "PROGRESS gemma4:31b stažen" in res.output


def test_a_reply_is_split_where_its_words_change_speaker():
    from recordingz.providers.whisperx_provider import split_on_speaker_change
    w = lambda t, word, who: {"start": t, "end": t + 0.4, "word": word, "speaker": who}
    martin = [w(i * 0.5, f"m{i}", "SPEAKER_01") for i in range(10)]
    ivan = [w(5 + i * 0.5, f"i{i}", "SPEAKER_00") for i in range(5)]
    blip = [w(2.0, "x", "SPEAKER_00")]  # one word flipped in the middle: noise
    seg = {"start": 0, "end": 7.4, "speaker": "SPEAKER_01", "text": "…", "words": martin[:4] + blip + martin[4:] + ivan}
    parts = split_on_speaker_change([seg])
    assert [(p["speaker"], len(p["words"])) for p in parts] == [("SPEAKER_01", 11), ("SPEAKER_00", 5)]
    assert parts[1]["start"] == 5 and parts[1]["text"] == "i0 i1 i2 i3 i4"
    one = {"start": 0, "end": 4.9, "speaker": "SPEAKER_01", "text": "m", "words": martin}
    assert split_on_speaker_change([one]) == [one], "one speaker: the reply stays as it was"
    unknown = {"start": 0, "end": 7.4, "speaker": "SPEAKER_01", "text": "…",
               "words": martin + [{"start": 5.0, "end": 5.3, "word": "a"}] + ivan}
    assert [p["speaker"] for p in split_on_speaker_change([unknown])] == ["SPEAKER_01", "SPEAKER_00"]
    short_start = {"start": 0, "end": 7.4, "speaker": "SPEAKER_00", "text": "…",
                   "words": [w(0, "a", "SPEAKER_01")] + [w(1 + i * 0.5, f"i{i}", "SPEAKER_00") for i in range(8)]}
    assert [p["speaker"] for p in split_on_speaker_change([short_start])] == ["SPEAKER_00"], "a one-word start joins"


def test_a_name_the_minutes_guessed_can_be_used_with_one_click(tmp_path):
    from recordingz.web.review import build_review, summary_hint_names
    rec = _make_transcribed(tmp_path)
    rec.summary_path.write_text("# T\n\n## Mluvčí\n| Označení | Jméno | Poznámka |\n|---|---|---|\n"
                                "| SPEAKER_00 | ? | vedl schůzku |\n| SPEAKER_01 | **Roman Kučera** | navrhl proces |\n",
                                encoding="utf-8")
    assert summary_hint_names(rec) == {"SPEAKER_01": "Roman Kučera"}
    sp = {s["label"]: s for s in build_review(Config(out_dir=tmp_path), rec)["speakers"]}
    assert sp["SPEAKER_01"]["hint_name"] == "Roman Kučera" and sp["SPEAKER_00"]["hint_name"] == ""
    assert sp["SPEAKER_01"]["hint"].startswith("**Roman Kučera**") or "Roman Kučera" in sp["SPEAKER_01"]["hint"]


def test_local_minutes_wait_for_named_speakers_unless_asked(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)  # SPEAKER_00 (12 s) and SPEAKER_01 (8 s) unnamed, Jana named
    monkeypatch.setattr(pl, "do_transcribe", lambda cfg, rec, force=False: rec.transcript_path)
    made = []
    monkeypatch.setattr(pl, "do_summarize", lambda cfg, r, force=False: made.append("main"))
    monkeypatch.setattr(pl, "do_summarize_compare", lambda cfg, r, force=False: made.append("compare"))
    assert pl.unnamed_speakers(rec, min_seconds=5) == ["SPEAKER_00", "SPEAKER_01"]
    assert pl.unnamed_speakers(rec) == [], "under 20 s they do not hold the minutes back"
    pl.do_process(cfg, rec, minutes_anyway=False)
    assert made == ["main", "compare"]
    monkeypatch.setattr(pl, "UNNAMED_MIN_SECONDS", 5.0)
    monkeypatch.setattr(pl, "unnamed_speakers", lambda r, min_seconds=5.0: ["SPEAKER_00"])
    made.clear()
    pl.do_process(cfg, rec, minutes_anyway=False)
    assert made == [], "an unnamed speaker: the minutes wait"
    pl.do_process(cfg, rec, minutes_anyway=True)
    assert made == ["main", "compare"], "asked for them anyway"


def test_direct_call_names_the_other_person_unless_the_voice_says_someone_else(tmp_path):
    """A 1:1 call: the call window names the other person. The voice that agrees or found nobody lets the name
    through (unconfirmed); a voice that says someone else takes neither name and the page asks."""
    from recordingz import pipeline as pl
    from recordingz.people import People
    from recordingz.web.review import build_review, save_names
    cfg = Config(out_dir=tmp_path, user_name="Ondřej")
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    ppl.ensure("Lukáš Krejčí"); ppl.ensure("Tomáš Malý"); ppl.save()
    lukas, tomas = ppl.find("Lukáš Krejčí").id, ppl.find("Tomáš Malý").id
    durations = {"Ondřej": 300.0, "SPEAKER_00": 140.0, "SPEAKER_01": 4.0}

    assert pl._direct_call_step(cfg, rec, durations, {}) is None, "a meeting title, not a call window"
    rec.sidecar.update(title="Lukáš Krejčí", title_source="window"); rec.save_sidecar()
    assert pl.direct_call_name(cfg, rec) == "Lukáš Krejčí"
    rec.sidecar.update(title="Team standup"); rec.save_sidecar()
    assert pl.direct_call_name(cfg, rec) == "", "a meeting subject, not a name (2026-10-07)"
    rec.sidecar.update(title="Team Standup", teams_windows_seen=[
        "Kompaktní zobrazení schůzky | Team Standup | Microsoft Teams"]); rec.save_sidecar()
    assert pl.direct_call_name(cfg, rec) == "", "the meeting's compact view names a meeting"
    rec.sidecar.update(title="Lukáš Krejčí", teams_windows_seen=[]); rec.save_sidecar()
    assert pl._direct_call_step(cfg, rec, {**durations, "SPEAKER_01": 60.0}, {}) is None, "two long voices: not 1:1"

    call = pl._direct_call_step(cfg, rec, durations, {})
    assert call == {"label": "SPEAKER_00", "name": "Lukáš Krejčí", "person": lukas, "applied": True}
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": lukas}

    rec.speakers_path.write_text('{"SPEAKER_00": "%s"}' % tomas, encoding="utf-8")  # the voice named Tomáš
    matches = {"SPEAKER_00": {"person": tomas, "score": 0.83}}
    call = pl._direct_call_step(cfg, rec, durations, matches)
    assert call["voice"] == {"person": tomas, "score": 0.83} and not call.get("applied")
    assert matches == {} and rec.read_json(rec.speakers_path) == {}, "neither name: the page asks"

    data = rec.read_json(rec.transcript_path); data["direct_call"] = call; rec.write_json(rec.transcript_path, data)
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_00"]["call"]["voice_name"] == "Tomáš Malý" and sp["SPEAKER_01"]["call"] is None
    save_names(cfg, rec, {"SPEAKER_00": "Lukáš Krejčí"})
    assert "direct_call" not in rec.read_json(rec.transcript_path), "decided by the user"

    matches = {"SPEAKER_00": {"person": lukas, "score": 0.8}}
    assert "voice" not in pl._direct_call_step(cfg, rec, durations, matches) and matches, "the voice agrees"


def test_from_scratch_drops_only_this_recordings_voice_prints(tmp_path, monkeypatch):
    """run-job process --force: the prints this recording gave go, the people's other prints stay."""
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    from recordingz.voiceprints import Voiceprints
    rec = _make_transcribed(tmp_path)
    vp = Voiceprints.load(tmp_path)
    vp.enroll("jana", [1.0, 0.0, 0.0], rec.stem, "SPEAKER_00")
    vp.enroll("jana", [0.0, 1.0, 0.0], "2026-09-01_0900_jine", "SPEAKER_01")
    vp.enroll("petr", [0.0, 0.0, 1.0], rec.stem, "SPEAKER_01")
    vp.save()
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: None)
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem])
    assert res.exit_code == 0, res.output
    assert Voiceprints.load(tmp_path).count("jana") == 2, "not from scratch: the prints stay"
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--force"])
    assert res.exit_code == 0, res.output
    vp = Voiceprints.load(tmp_path)
    assert [p["stem"] for p in vp.people["jana"]] == ["2026-09-01_0900_jine"] and "petr" not in vp.people


def test_voiceprints_over_the_limit_drop_the_weakest_not_the_oldest():
    """20 prints per person; over that a poor one goes first (mixed, outlier, little speech), else the most
    redundant one (the older of the closest pair)."""
    import math
    from recordingz.voiceprints import MAX_PER_PERSON, prune

    def vec(angle, z=0.0):  # unit vectors around one voice; z pulls one away (another voice mixed in)
        v = [math.cos(angle), math.sin(angle), z]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def prints(n):
        return [{"v": vec(i * 0.05), "stem": f"s{i:02d}", "label": "A", "added": f"2026-09-{i + 1:02d}"} for i in range(n)]

    assert MAX_PER_PERSON == 20
    ps = prints(21)
    ps[3]["v"] = vec(0.15, 2.5)  # an outlier
    gone = prune(ps)
    assert [g["stem"] for g in gone] == ["s03"] and len(ps) == 20

    ps = prints(21)
    ps[5]["mixed"] = True
    assert [g["stem"] for g in prune(ps)] == ["s05"]

    ps = prints(21)
    ps[7]["seconds"] = 35.0
    ps[8]["seconds"] = 120.0
    assert [g["stem"] for g in prune(ps)] == ["s07"]

    ps = prints(21)
    ps[12]["v"] = vec(0.601)  # s12 and s13 nearly the same: the closest pair
    ps[13]["v"] = vec(0.602)
    assert [g["stem"] for g in prune(ps)] == ["s12"], "the older of the two closest prints"


def test_voiceprints_limit_comes_from_the_settings(tmp_path):
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path)
    assert cfg.voiceprints.max_prints == 20
    vp = Voiceprints.load(tmp_path, 3)
    for i in range(5):
        vp.enroll("x", [1.0 if j == i else 0.0 for j in range(5)], f"r{i}", "S")
    assert vp.count("x") == 3


def test_jobs_status_file_tells_the_tray_what_runs_and_what_ended(tmp_path, monkeypatch):
    """%TEMP%\recordingz-review-jobs.json for the capture app: busy while a job runs (yellow icon), the ended jobs
    with what came out of them (the balloon), not running once the server stops; the page learns the plan."""
    import json
    import threading
    import time
    from recordingz.web import review as rv
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    rec.summary_path.unlink()
    go = threading.Event()
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: go.wait(5))
    st = rv.ReviewState(cfg)
    st.jobs_file = tmp_path / "jobs.json"
    st.run_process(rec)
    d = {}
    for _ in range(100):
        d = json.loads(st.jobs_file.read_text(encoding="utf-8")) if st.jobs_file.exists() else {}
        if d.get("current"):
            break
        time.sleep(0.02)
    assert d["busy"] and d["current"]["stem"] == rec.stem and d["current"]["title"] == rec.title
    assert st.jobs_state()["current"]["plan"] == {"cloud": False, "voices": False, "minutes_wait": True,
                                                  "minutes": True, "only_with": ""}
    # what the page says while it waits: a 1:1 call names the other side (its minutes go at once), no minutes
    st.run_process(rec, minutes=False, only_with="Marek Hudec")
    assert st.jobs_state()["queue"][0]["plan"] == {
        "cloud": False, "voices": False, "minutes_wait": True, "minutes": False, "only_with": "Marek Hudec"}
    go.set()
    for _ in range(100):
        if not st.status()["busy"]:
            break
        time.sleep(0.02)
    time.sleep(0.1)
    d = json.loads(st.jobs_file.read_text(encoding="utf-8"))
    assert not d["busy"] and d["current"] is None
    assert d["finished"][-1]["stem"] == rec.stem and d["finished"][-1]["ok"]
    assert d["finished"][-1]["text"] == "přepis hotový; zápis počká, až pojmenujete mluvčí"
    st.write_jobs_file(running=False)
    assert json.loads(st.jobs_file.read_text(encoding="utf-8"))["running"] is False


def test_openai_minutes_speak_the_chat_completions_api(monkeypatch):
    """provider = "openai": the Chat Completions API with the OpenAI key; errors say what to fix."""
    import requests
    from dataclasses import replace
    from recordingz import llm, settings
    from recordingz.config import SummarizeSettings
    monkeypatch.setattr(settings, "get_secret", lambda name: "sk-test" if name == "openai" else "")
    sent = {}

    class Resp:
        def __init__(self, code, data):
            self.status_code, self._data, self.ok, self.text = code, data, code < 400, str(data)

        def json(self):
            return self._data

    def post(url, json, timeout, headers):
        sent.update(url=url, body=json, headers=headers)
        return Resp(200, {"model": "gpt-5", "choices": [{"message": {"content": " # Zápis "}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    monkeypatch.setattr(requests, "post", post)
    s = replace(SummarizeSettings(), provider="openai", model="gpt-5")
    r = llm.complete("system", "přepis", s)
    assert (r.text, r.model, r.input_tokens, r.truncated) == ("# Zápis", "gpt-5", 10, False)
    assert sent["url"] == "https://api.openai.com/v1/chat/completions" and sent["headers"]["Authorization"] == "Bearer sk-test"
    assert [m["role"] for m in sent["body"]["messages"]] == ["system", "user"]
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(401, {"error": "expired"}))
    with pytest.raises(llm.LLMError, match="refused"):
        llm.complete("system", "přepis", s)


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


def test_guessed_names_wait_for_review_and_are_confirmed_or_rejected_one_by_one(tmp_path):
    """Lidé → ke kontrole: a voice match and a video name tag are guesses (no print yet); confirming one group
    stores its print and leaves the rest of the recording as it was; rejecting removes the name and the same guess
    does not come back."""
    from recordingz import pipeline as pl
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import (confirm_candidate, people_rows, reject_candidate, voice_candidates)
    cfg = Config(out_dir=tmp_path, voiceprints=type(Config().voiceprints)(enabled=True))
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr, jana = ppl.ensure("Petr Svoboda").id, ppl.ensure("Jana Nováková").id
    ppl.save()
    data = rec.read_json(rec.transcript_path)
    data["segments"] += [{"start": 30 + i * 10, "end": 40 + i * 10, "text": "dlouhá řeč", "speaker": lab}
                         for i, lab in enumerate(["SPEAKER_00"] * 3 + ["SPEAKER_01"] * 3 + ["Jana Nováková"] * 3)]
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0], "Jana Nováková": [0.7, 0.7]}
    data["voice_matches"] = {"SPEAKER_00": {"person": petr, "score": 0.9}, "SPEAKER_01": {"person": petr, "score": 0.7}}
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": petr, "SPEAKER_01": petr})

    cands = voice_candidates(cfg)
    assert sorted(c["label"] for c in cands[petr]) == ["SPEAKER_00", "SPEAKER_01"]
    assert [(c["label"], c["source"], c["can_reject"]) for c in cands[jana]] == [("Jana Nováková", "video", False)]
    rows = {r["id"]: r for r in people_rows(cfg)["people"]}
    assert (rows[petr]["prints"], rows[petr]["candidates"]) == (0, 2)

    assert confirm_candidate(cfg, rec.stem, "SPEAKER_00", petr)["prints_added"] == 1
    assert Voiceprints.load(tmp_path).count(petr) == 1
    data = rec.read_json(rec.transcript_path)
    assert "SPEAKER_00" not in data["voice_matches"] and "SPEAKER_01" in data["voice_matches"], "only that group"
    assert [c["label"] for c in voice_candidates(cfg)[petr]] == ["SPEAKER_01"]

    reject_candidate(cfg, rec.stem, "SPEAKER_01", petr)
    assert "SPEAKER_01" not in rec.read_json(rec.speakers_path)
    assert petr not in voice_candidates(cfg) or not voice_candidates(cfg)[petr]
    vp = Voiceprints.load(tmp_path)
    vp.enroll(petr, [0.0, 1.0], "other", "S")  # a print that would match SPEAKER_01 again
    vp.save()
    assert "SPEAKER_01" not in pl.recognize_voices(cfg, rec), "a rejected guess does not come back"

    confirm_candidate(cfg, rec.stem, "Jana Nováková", jana)
    assert rec.read_json(rec.speakers_path)["Jana Nováková"] == jana and jana not in voice_candidates(cfg)


def test_assignments_compare_each_group_with_the_persons_other_prints(tmp_path):
    """Lidé → osoba: every assigned group, confirmed or guessed, gets its similarity to the person's other prints
    (its own left out); a group closer to somebody else is flagged; ✗ on a confirmed group removes its print."""
    import math
    from recordingz import assignments
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import reject_candidate
    cfg = Config(out_dir=tmp_path, voiceprints=type(Config().voiceprints)(enabled=True))
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr, jana = ppl.ensure("Petr Svoboda").id, ppl.ensure("Jana Nováková").id
    ppl.save()

    def v(angle):  # unit vectors on a circle: the angle decides the similarity
        return [math.cos(angle), math.sin(angle)]
    vp = Voiceprints.load(tmp_path)
    for i, a in enumerate((0.0, 0.1, 0.2)):
        vp.enroll(petr, v(a), f"old{i}", "S", near_duplicate=2)
    vp.enroll(jana, v(1.5), "oldj", "S")
    vp.enroll(petr, v(0.05), rec.stem, "SPEAKER_00", near_duplicate=2)  # this recording's confirmed group
    vp.save()
    data = rec.read_json(rec.transcript_path)
    data["speaker_embeddings"] = {"SPEAKER_00": v(0.05), "SPEAKER_01": v(1.45)}
    data["voice_matches"] = {"SPEAKER_01": {"person": petr, "score": 0.6}}  # a guess that sounds like Jana
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": petr, "SPEAKER_01": petr})

    rows = {r.label: r for r in assignments.for_people(cfg)[petr] if r.stem == rec.stem}
    ok, odd = rows["SPEAKER_00"], rows["SPEAKER_01"]
    assert (ok.status, ok.has_print, ok.match["level"]) == ("confirmed", True, 0) and ok.match["own"] > 0.99
    assert odd.status == "review" and odd.match["level"] == 3
    assert odd.match["other"]["person"] == jana and "podobnější jiné osobě" in odd.match["why"]
    assert assignments.summary(assignments.for_people(cfg)[petr])["attention"] == 1

    assert reject_candidate(cfg, rec.stem, "SPEAKER_00", petr)["prints_removed"] == 1, "a confirmed group was wrong"
    assert Voiceprints.load(tmp_path).count(petr) == 3 and "SPEAKER_00" not in rec.read_json(rec.speakers_path)


def test_a_group_goes_to_somebody_else_or_loses_a_wrong_name_tag(tmp_path):
    """"→ jinému…": the group gets the other person for good and its print moves to them; "✗ Není to" on a name-tag
    group (the Teams video named it "Jana Nováková") makes it an unnamed SPEAKER_NN, voice included."""
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import reassign_candidate, reject_candidate
    cfg = Config(out_dir=tmp_path, voiceprints=type(Config().voiceprints)(enabled=True))
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr, jana = ppl.ensure("Petr Svoboda").id, ppl.ensure("Jana Nováková").id
    ppl.save()
    data = rec.read_json(rec.transcript_path)
    data["segments"] += [{"start": 30 + i * 10, "end": 40 + i * 10, "text": "řeč", "speaker": lab}
                         for i, lab in enumerate(["SPEAKER_00"] * 3 + ["Jana Nováková"] * 3)]
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0], "Jana Nováková": [0.0, 1.0]}
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": petr})
    vp = Voiceprints.load(tmp_path)
    vp.enroll(petr, [1.0, 0.0], rec.stem, "SPEAKER_00")
    vp.save()

    r = reassign_candidate(cfg, rec.stem, "SPEAKER_00", petr, jana)
    assert (r["prints_removed"], r["prints_added"]) == (1, 1)
    vp = Voiceprints.load(tmp_path)
    assert vp.count(petr) == 0 and vp.count(jana) == 1
    assert rec.read_json(rec.speakers_path)["SPEAKER_00"] == jana
    assert petr in rec.read_json(rec.transcript_path)["voice_rejected"]["SPEAKER_00"]

    r = reject_candidate(cfg, rec.stem, "Jana Nováková", jana)
    new = r["label"]
    assert new.startswith("SPEAKER_") and new != "SPEAKER_00"
    data = rec.read_json(rec.transcript_path)
    assert "Jana Nováková" not in data["speakers"] and new in data["speakers"]
    assert not any(s.get("speaker") == "Jana Nováková" for s in data["segments"])
    assert data["speaker_embeddings"][new] == [0.0, 1.0] and data["voice_rejected"][new] == [jana]


def test_a_recording_edited_after_its_minutes_is_marked_until_they_are_written_again(tmp_path):
    """"✎ upraveno": names or replies changed after the minutes (the transcript is the source of truth, the minutes
    may say the old names); writing the minutes again clears it; without minutes there is nothing stale."""
    import os
    import time
    from recordingz import pipeline as pl
    from recordingz.web.review import build_review, list_recordings, save_names
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)  # with minutes
    old = time.time() - 600
    os.utime(rec.summary_path, (old, old))
    assert pl.minutes_stale(rec) is None, "never edited"
    save_names(cfg, rec, {"SPEAKER_00": "Petr Svoboda"})
    assert pl.minutes_stale(rec), "edited after the minutes"
    row = [r for r in list_recordings(cfg) if r["stem"] == rec.stem][0]
    assert row["edited"] and build_review(cfg, rec)["edited"]
    rec.summary_path.write_text("# nový zápis\n", encoding="utf-8")  # the minutes written again
    assert pl.minutes_stale(rec) is None
    pl.assign_segments(cfg, rec, [{"start": 20.0, "speaker": "SPEAKER_01", "from": "SPEAKER_00"}])
    os.utime(rec.summary_path, (old, old))
    assert pl.minutes_stale(rec), "a moved reply counts too"
    other = rec.file(".summary.claude-opus-5-5.md")
    other.write_text("# zápis jiným modelem\n", encoding="utf-8")  # one fresh minutes by another model
    assert pl.minutes_stale(rec) is None, "one up-to-date minutes is enough"
    from recordingz.web.review import recording_docs
    stale = {d["file"]: d["stale"] for d in recording_docs(rec)["summaries"]}
    assert stale == {rec.summary_path.name: True, other.name: False}, "the older one is marked outdated"
    other.unlink()
    rec.summary_path.unlink()
    assert pl.minutes_stale(rec) is None, "no minutes: the transcript texts are already current"


def test_recognition_samples_excluded_added_and_replaced(tmp_path):
    """Lidé → Vzorky pro rozpoznávání: Nepoužívat keeps the name but the print never comes back (not even when the
    meeting is saved again); ＋ přidat makes a group a print (a guess is confirmed), replacing a chosen one; a short
    group is refused with the reason."""
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.review import sample_action, save_names
    cfg = Config(out_dir=tmp_path, voiceprints=type(Config().voiceprints)(enabled=True))
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr = ppl.ensure("Petr Svoboda").id
    ppl.save()
    data = rec.read_json(rec.transcript_path)
    data["segments"] += [{"start": 30 + i * 10, "end": 40 + i * 10, "text": "řeč", "speaker": lab}
                         for i, lab in enumerate(["SPEAKER_00"] * 3 + ["SPEAKER_01"] * 3)]
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}
    data["voice_matches"] = {"SPEAKER_01": {"person": petr, "score": 0.7}}
    rec.write_json(rec.transcript_path, data)
    save_names(cfg, rec, {"SPEAKER_00": "Petr Svoboda"})
    assert Voiceprints.load(tmp_path).count(petr) == 1

    assert sample_action(cfg, petr, "exclude", rec.stem, "SPEAKER_00")["removed"]
    assert rec.read_json(rec.speakers_path)["SPEAKER_00"] == petr, "the name stays"
    save_names(cfg, rec, {"SPEAKER_00": "Petr Svoboda"})
    assert Voiceprints.load(tmp_path).count(petr) == 0, "an excluded print does not come back"

    r = sample_action(cfg, petr, "add", rec.stem, "SPEAKER_01", replace={"stem": "x", "label": "y"})
    assert r["ok"] and rec.read_json(rec.speakers_path)["SPEAKER_01"] == petr, "the guess is confirmed"
    assert sample_action(cfg, petr, "add", rec.stem, "SPEAKER_00",
                         replace={"stem": rec.stem, "label": "SPEAKER_01"})["ok"], "added by hand: no longer excluded"
    vp = Voiceprints.load(tmp_path)
    assert [(p["label"]) for p in vp.people[petr]] == ["SPEAKER_00"] and not vp.is_excluded(petr, rec.stem, "SPEAKER_00")
    short = sample_action(cfg, petr, "add", rec.stem, "Jana Nováková")
    assert not short["ok"] and "aspoň 30 s" in short["reason"]



def test_every_reply_is_compared_with_the_persons_prints_once_its_voice_is_computed(tmp_path):
    """Lidé → Všechny repliky: replies without a computed voice are counted (and estimated) for the confirmed job;
    computed ones get their similarity to the person's prints, and a reply that sounds like somebody else is flagged."""
    import json
    from recordingz import replies
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr, jana = ppl.ensure("Petr Svoboda").id, ppl.ensure("Jana Nováková").id
    ppl.save()
    rec.write_json(rec.speakers_path, {"SPEAKER_00": petr})
    vp = Voiceprints.load(tmp_path)
    vp.enroll(petr, [1.0, 0.0], "old1", "S", near_duplicate=2)
    vp.enroll(jana, [0.0, 1.0], "old2", "S")
    vp.save()

    e = replies.evaluate(cfg, petr)
    assert e["replies"] == [] and e["todo"] == [rec.stem] and e["todo_replies"] == 2 and e["estimate_s"] > 0
    model = Voiceprints.load(tmp_path).model or cfg.transcribe.diarize_model
    replies.cache_path(rec).write_text(json.dumps({"model": model, "replies": {"0.00": [1.0, 0.0], "20.00": [0.05, 1.0]}}),
                                       encoding="utf-8")
    e = replies.evaluate(cfg, petr)
    got = {r["start"]: r for r in e["replies"]}
    assert e["todo"] == [] and set(got) == {0.0, 20.0}
    assert got[0.0]["level"] == 0 and got[0.0]["own"] > 0.99
    assert got[20.0]["level"] == 3 and got[20.0]["other"]["person"] == jana, "somebody else's voice in Petr's group"


def test_moving_replies_marks_the_groups_print_and_recalculating_it_uses_the_current_replies(tmp_path):
    """A reply moved out of a group a print came from: the print is "stale" (↻); recalculating makes it the
    length-weighted mean of the group's current replies' voices and clears the mark."""
    import json
    from recordingz import pipeline as pl, replies
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, voiceprints=type(Config().voiceprints)(enabled=True))
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path, "nick")
    petr = ppl.ensure("Petr Svoboda").id
    ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll(petr, [0.6, 0.8], rec.stem, "SPEAKER_00")
    vp.save()
    pl.assign_segments(cfg, rec, [{"start": 20.0, "speaker": "SPEAKER_01", "from": "SPEAKER_00"}])
    assert Voiceprints.load(tmp_path).people[petr][0]["stale"] is True

    model = cfg.transcribe.diarize_model  # SPEAKER_00 keeps only its reply at 0 s
    replies.cache_path(rec).write_text(json.dumps({"model": model, "replies": {
        "0.00": [1.0, 0.0], "4.00": [0.0, 1.0], "5.00": [0.0, 1.0], "12.00": [0.0, 1.0], "20.00": [0.0, 1.0]}}),
        encoding="utf-8")
    assert replies.recompute_estimate(cfg, petr)["missing_replies"] == 0
    assert replies.recompute_prints(cfg, petr) == {"recomputed": 1, "skipped": 0}
    p = Voiceprints.load(tmp_path).people[petr][0]
    assert p["v"] == [1.0, 0.0] and "stale" not in p and p["recomputed"] == 1

# ---------------------------------------------------------------- shared files, web safety (review 16. 10.)

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


def test_web_stem_is_a_name_never_a_path(tmp_path):
    """Review 16. 10., T4: %2F, %5C and %3A in {stem} decoded into a path, and DELETE removed that folder."""
    from urllib.parse import quote
    from recordingz import trash
    out = tmp_path / "out"
    out.mkdir()
    victim = Recording.load(_make_recording(tmp_path / "elsewhere"))
    srv, state, base = _serve(out)
    try:
        for stem in ("..%2F..%2Felsewhere%2F2026%2F09%2F2026-09-04_1400_tydenni-sync",
                     quote(str(victim.dir), safe=""), quote(str(victim.sidecar_path), safe="")):
            code, r = _call(base, "DELETE", f"/api/recordings/{stem}")
            assert code == 400 and "error" in r, stem
            assert _call(base, "POST", f"/api/recordings/{stem}/trash")[0] == 400
            assert _call(base, "GET", f"/api/recordings/{stem}")[0] == 400
        assert victim.dir.is_dir() and victim.sidecar_path.exists(), "the outside folder is still there"
    finally:
        srv.shutdown()
    cfg = Config(out_dir=out)
    (out / ".trash" / victim.dir.name).mkdir(parents=True)  # a same-named meeting in the trash
    with pytest.raises(RecordingError):
        trash.delete_now(cfg, victim)
    with pytest.raises(RecordingError):
        trash.move_to_trash(cfg, victim)
    assert victim.dir.is_dir() and (out / ".trash" / victim.dir.name).is_dir(), "refused before anything was removed"


def test_rename_is_refused_during_a_job_undone_on_failure_and_moves_exclusions(tmp_path, monkeypatch):
    """Review 16. 10., T5."""
    from recordingz.pipeline import rename_recording
    from recordingz.voiceprints import Voiceprints
    rec = _make_transcribed(tmp_path)
    srv, state, base = _serve(tmp_path)
    try:
        state.jobs.append({"id": 99, "name": "process", "stem": rec.stem, "text": "", "fn": None, "done": ""})
        code, r = _call(base, "PUT", f"/api/recordings/{rec.stem}/title", {"title": "Jiný název"})
        assert code == 400 and "frontě" in r["error"] and rec.dir.exists(), "a meeting with a job keeps its name"
        state.jobs.clear()
        state.capture = {"running": True, "recording": True, "stem": rec.stem}
        code, r = _call(base, "PUT", f"/api/recordings/{rec.stem}/title", {"title": "Jiný název"})
        assert code == 400 and "nahrává" in r["error"]
    finally:
        srv.shutdown()

    cfg = Config(out_dir=tmp_path)
    before = sorted(p.name for p in rec.dir.iterdir())
    sidecar = rec.sidecar_path.read_text(encoding="utf-8")
    real, calls = Path.rename, []

    def flaky(self, target):
        calls.append(self)
        if len(calls) == 3:
            raise PermissionError("in use")
        return real(self, target)
    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(PermissionError):
        rename_recording(cfg, rec, "Jiný název")
    monkeypatch.setattr(Path, "rename", real)
    assert rec.dir.exists() and sorted(p.name for p in rec.dir.iterdir()) == before, "every rename undone"
    assert rec.sidecar_path.read_text(encoding="utf-8") == sidecar and rec.title == "Týdenní sync"

    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr", [1.0, 0.0], rec.stem, "SPEAKER_00", "m")
    vp.exclude("jana", rec.stem, "Jana Nováková")
    vp.save()
    new = rename_recording(cfg, rec, "Jiný název")
    assert new.stem == "2026-09-04_1400_jiny-nazev" and not rec.dir.exists()
    vp = Voiceprints.load(tmp_path)
    assert vp.people["petr"][0]["stem"] == new.stem
    assert vp.is_excluded("jana", new.stem, "Jana Nováková"), "the exclusion follows the rename"


def test_an_unexpected_error_still_answers_with_json(tmp_path, monkeypatch):
    """Review 16. 10., T6: an OSError (or LLMError, MediaError ...) escaped _dispatch and the connection just dropped."""
    from recordingz import trash
    rec = _make_transcribed(tmp_path)

    def locked(cfg, r):
        raise PermissionError("the file is open in another program")
    monkeypatch.setattr(trash, "delete_now", locked)
    srv, state, base = _serve(tmp_path)
    try:
        code, r = _call(base, "DELETE", f"/api/recordings/{rec.stem}")
        assert code == 500 and "open in another program" in r["error"]
        code, rows = _call(base, "GET", "/api/recordings")
        assert code == 200 and rows[0]["stem"] == rec.stem, "the server keeps serving"
    finally:
        srv.shutdown()


def test_review_server_nobody_contacts_stops_by_itself(tmp_path, monkeypatch):
    """Review 16. 10., T7: the idle clock started only with the first request, so a server no page ever reached ran
    for ever."""
    import threading
    from recordingz.web import review as rv
    monkeypatch.setattr(rv, "FIRST_CONTACT_S", 1.0)
    lock = tmp_path / "lock.json"
    t = threading.Thread(target=lambda: rv.serve(Config(out_dir=tmp_path), None, open_browser=False, lock=lock),
                         daemon=True)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive() and not lock.exists()
