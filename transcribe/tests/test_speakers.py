"""Who speaks when: diarization labels, the video and mic tracks, the calendar, moving replies."""

import pytest

from recordingz.config import Config
from recordingz.providers.base import Segment
from recordingz.recording import Recording, RecordingError
from recordingz.speakers import apply_manual_names, apply_video_timeline, speaker_list
from recordingz.video_speakers import VideoTimeline

from helpers import _make_recording, _segs, _make_transcribed


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


def test_unassigned_replies_are_given_out_one_by_one_and_are_no_speaker_to_name(tmp_path):
    from recordingz.pipeline import assign_segments
    from recordingz.web.views import build_review, list_recordings, unresolved_labels
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


def test_direct_call_names_the_other_person_unless_the_voice_says_someone_else(tmp_path):
    """A 1:1 call: the call window names the other person. The voice that agrees or found nobody lets the name
    through (unconfirmed); a voice that says someone else takes neither name and the page asks."""
    from recordingz import pipeline as pl
    from recordingz.people import People
    from recordingz.web.views import build_review, save_names
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
