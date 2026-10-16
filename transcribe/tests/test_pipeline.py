"""Transcription and minutes: providers (local and cloud), run-job, summaries, the fast track, timings."""

from pathlib import Path

import pytest

from recordingz.config import Config, TranscribeSettings
from recordingz.providers.base import Segment
from recordingz.recording import Recording, RecordingError

from helpers import _make_recording, _make_transcribed, _FakeResponse, _cloud_setup


def test_summary_user_message_contains_transcript(tmp_path):
    from recordingz.summarize import SYSTEM_PROMPT, build_user_message
    rec = Recording.load(_make_recording(tmp_path))
    segs = [Segment(0, 2, "Začneme.", "Jana Nováková"), Segment(2, 5, "Úkol vezmu já.", "Petr Svoboda")]
    msg = build_user_message(rec, segs, {"start": "2026-09-04T14:00:00", "language": "cs", "participants": None})
    assert msg.startswith("Meeting: Týdenní sync\nstart: 2026-09-04T14:00:00\nlanguage: cs\n")
    assert "[00:00:02] Petr Svoboda: Úkol vezmu já." in msg
    assert "{language}" in SYSTEM_PROMPT and "{h3}" in SYSTEM_PROMPT and "{h6}" in SYSTEM_PROMPT


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


def test_summary_refuses_empty_transcript(tmp_path):
    from recordingz.pipeline import do_summarize
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)  # a handful of words
    with pytest.raises(Exception, match="nothing to summarize"):
        do_summarize(cfg, rec, force=True)


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


# 2026-10-12, an on-site recording that heard only room noise (names and title replaced): every reply is the prompt
ECHOES = [(131.15, 143.78, "Pojmy Nováková."), (143.80, 144.26, "Pojmy Nováková."), (3185.56, 3188.26, "Pojmy Nováková."),
          (3935.75, 3964.62, "Jana Nováková."), (3964.66, 3965.10, "Pojmy Nováková."),
          (3976.58, 3983.20, "Příprava na sprint."), (3983.24, 3987.37, "Účastníci."), (4040.62, 4045.28, "Jana Nováková.")]


def _silent_recording(tmp_path):
    rec = _make_transcribed(tmp_path)
    rec.sidecar.update(title="Příprava na sprint", participants=[{"name": "Jana Nováková"}], source="onsite")
    rec.save_sidecar()
    return rec


def test_replies_echoing_the_prompt_are_recognised():
    from recordingz.prompt import is_prompt_echo
    args = ("Příprava na sprint", ["Jana Nováková", "Petr Svoboda"], ("ATLAS", "SLA", "Entra ID"))
    for _, _, echo in ECHOES:
        assert is_prompt_echo(echo, *args), echo
    for echo in ("pojmy novakova", "Účastníci: Jana Nováková, Petr Svoboda.", "Pojmy: ATLAS, SLA, Entra ID.",
                 "Schůzka: Příprava na sprint.", "Svoboda.", "ATLAS", "Pojmy Entra ID"):
        assert is_prompt_echo(echo, *args), echo
    for real in ("Jano, pošli to.", "Petr Svoboda to má hotové.", "ATLAS je dole.", "Na.", "Sprint.",
                 "Ano.", "Příprava na sprint začne v pondělí.", ""):
        assert not is_prompt_echo(real, *args), real


def test_transcript_of_prompt_echoes_only_says_there_is_no_speech(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.export import NO_SPEECH
    from recordingz.providers.base import ProviderResult
    from recordingz.web.views import build_review
    cfg = Config(out_dir=tmp_path, transcribe=TranscribeSettings(glossary=("ATLAS", "SLA")))
    rec = _silent_recording(tmp_path)

    class P:
        name = "fake"

        def transcribe(self, audio, **k):
            assert "Pojmy: ATLAS, SLA." in k["prompt"]
            return ProviderResult(segments=[Segment(a, b, t, "SPEAKER_00") for a, b, t in ECHOES], language="cs",
                                  provider="fake", provider_version="0", model="m",
                                  speaker_embeddings={"SPEAKER_00": [1.0, 0.0]})
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    pl.do_transcribe(cfg, rec, force=True)
    data = rec.read_json(rec.transcript_path)
    assert data["no_speech"] is True and data["segments"] == [] and data["speakers"] == []
    assert data["speaker_embeddings"] == {}, "no voice left to name"
    pl.do_export(cfg, rec)
    txt = rec.file(".txt").read_text(encoding="utf-8")
    assert NO_SPEECH in txt and "SPEAKER_00" not in txt and "speakers: \n" in txt
    assert NO_SPEECH in rec.file(".srt").read_text(encoding="utf-8")
    review = build_review(cfg, rec)
    assert review["transcribed"] and review["speakers"] == []


def test_real_speech_with_names_survives_the_echo_filter(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.providers.base import ProviderResult
    cfg = Config(out_dir=tmp_path)
    rec = _silent_recording(tmp_path)
    segs = [Segment(0, 3, "Jano, pošli to prosím.", "SPEAKER_00"), Segment(3, 4, "Účastníci.", "SPEAKER_01")]

    class P:
        name = "fake"

        def transcribe(self, audio, **k):
            return ProviderResult(segments=list(segs), language="cs", provider="fake", provider_version="0", model="m")
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    pl.do_transcribe(cfg, rec, force=True)
    data = rec.read_json(rec.transcript_path)
    assert [s["text"] for s in data["segments"]] == ["Jano, pošli to prosím."] and data["speakers"] == ["SPEAKER_00"]
    assert "no_speech" not in data


def test_summary_refuses_a_transcript_without_speech(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz import summarize as sm
    cfg = Config(out_dir=tmp_path)
    rec = _silent_recording(tmp_path)
    rec.write_json(rec.transcript_path, {"format": 1, "language": "cs", "speakers": [], "segments": [],
                                         "no_speech": True})
    monkeypatch.setattr(sm, "summarize", lambda *a, **k: pytest.fail("no LLM call on an empty transcript"))
    with pytest.raises(RecordingError, match="no recognisable speech"):
        pl.do_summarize(cfg, rec, force=True)
    with pytest.raises(RecordingError, match="no recognisable speech"):
        pl.summarize_as(cfg, rec, "ollama", "m")


def test_speaker_language_choice():
    from recordingz.providers.whisperx_provider import choose_speaker_languages
    votes = {"SPEAKER_00": [{"sk": .7, "cs": .25}], "SPEAKER_01": [{"cs": 1.0}, {"cs": .9, "sk": .1}],
             "SPEAKER_02": [{"cs": .45, "sk": .4}], "SPEAKER_03": []}
    assert choose_speaker_languages(votes, "sk", ("cs", "sk", "en")) == {"SPEAKER_01": "cs"}
    assert choose_speaker_languages(votes, "cs", ("cs", "sk", "en")) == {"SPEAKER_00": "sk"}


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
    from recordingz.web.views import build_review
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
    from recordingz.web.views import _nearest_group
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
