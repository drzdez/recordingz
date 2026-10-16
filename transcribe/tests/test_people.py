"""People and voice prints: the registry, recognition, merges, guesses to review, samples, replies."""

import pytest

from recordingz.config import Config
from recordingz.providers.base import Segment
from recordingz.recording import RecordingError

from helpers import VP_ON, _make_transcribed


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
    from recordingz.web.views import build_review
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


def test_a_person_can_opt_out_of_voice_recognition(tmp_path):
    """Voice prints are biometric data: whoever does not want them gets none, and the old ones go right away."""
    from recordingz.pipeline import enroll_names
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.people_views import save_people
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


def test_voiceprint_skips_a_print_that_adds_nothing(tmp_path):
    from recordingz.voiceprints import Voiceprints
    vp = Voiceprints.load(tmp_path)
    assert vp.enroll("petr", [1.0, 0.0, 0.0], "rec1", "SPEAKER_00")
    assert not vp.enroll("petr", [0.99, 0.02, 0.0], "rec2", "SPEAKER_00")  # same voice, same conditions
    assert vp.enroll("petr", [0.8, 0.6, 0.0], "rec3", "SPEAKER_00")        # another room, worth keeping
    assert vp.count("petr") == 2


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
    from recordingz.web.views import build_review, save_names
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


def test_person_detail_lists_prints_with_samples(tmp_path):
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.people_views import forget_print, person_detail
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


def test_nearest_voice_group_is_a_hint_from_0_45():
    from recordingz.web.views import GROUP_HINT_MIN, _nearest_group
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


def test_guessed_names_wait_for_review_and_are_confirmed_or_rejected_one_by_one(tmp_path):
    """Lidé → ke kontrole: a voice match and a video name tag are guesses (no print yet); confirming one group
    stores its print and leaves the rest of the recording as it was; rejecting removes the name and the same guess
    does not come back."""
    from recordingz import pipeline as pl
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.people_views import confirm_candidate, people_rows, reject_candidate, voice_candidates
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
    from recordingz.web.people_views import reject_candidate
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
    from recordingz.web.people_views import reassign_candidate, reject_candidate
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


def test_recognition_samples_excluded_added_and_replaced(tmp_path):
    """Lidé → Vzorky pro rozpoznávání: Nepoužívat keeps the name but the print never comes back (not even when the
    meeting is saved again); ＋ přidat makes a group a print (a guess is confirmed), replacing a chosen one; a short
    group is refused with the reason."""
    from recordingz.people import People
    from recordingz.voiceprints import Voiceprints
    from recordingz.web.people_views import sample_action
    from recordingz.web.views import save_names
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
