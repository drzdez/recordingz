"""The review server's job queue: one job at a time, recordings that hold it, cancel, the tray's status file."""

import json

import pytest

from recordingz.config import Config, TranscribeSettings
from recordingz.recording import Recording

from helpers import _make_recording, _make_transcribed


def test_untranscribed_recording_is_offered_for_processing(tmp_path, monkeypatch):
    from recordingz.web import jobs, views
    cfg = Config(out_dir=tmp_path)
    rec = Recording.load(_make_recording(tmp_path))  # sidecar + mix, no transcript
    d = views.build_review(cfg, rec)
    assert d["transcribed"] is False and d["has_mix"] and d["speakers"] == []
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))  # the child process
    st = jobs.ReviewState(cfg)
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


def test_errors_logged_during_a_job_reach_the_page_history(tmp_path):
    import logging
    import time as _time
    from recordingz.web.jobs import ReviewState
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
    from recordingz.web.jobs import ReviewState, _capture_watch, capture_status
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


def test_background_jobs_queue_instead_of_refusing_and_say_what_runs(tmp_path):
    import threading as _threading
    import time as _time
    from recordingz.web.jobs import ReviewState
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
    from recordingz.web.jobs import ReviewState
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
    from recordingz.web import jobs
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv)
        code = "import time; time.sleep(30)" if len(runs) == 1 else "pass"  # the first run is long, the rerun short
        p = _sp.Popen([_sys.executable, "-c", code])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(jobs, "run_cli", cli)
    state = jobs.ReviewState(Config(out_dir=tmp_path))
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
    from recordingz.web import jobs
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        long = argv[-1] == "a" and runs.count("a") == 1  # the first run of "a" is long: it gets stopped
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if long else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(jobs, "run_cli", cli)
    state = jobs.ReviewState(Config(out_dir=tmp_path))
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
    from recordingz.web import jobs
    runs = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: runs.append(argv[-1]))
    state = jobs.ReviewState(Config(out_dir=tmp_path))
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

    cont = jobs.ReviewState(Config(out_dir=tmp_path, transcribe=TranscribeSettings(when_recording="continue")))
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
    from recordingz.web import jobs
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        long = argv[-1] == "cloud2" and runs.count("cloud2") == 1
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if long else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(jobs, "run_cli", cli)

    def idle(st):
        for _ in range(300):
            if not st.current:
                return
            _time.sleep(0.02)

    state = jobs.ReviewState(Config(out_dir=tmp_path))
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
    from recordingz.web import jobs
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if argv[-1] == "a" else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(jobs, "run_cli", cli)
    state = jobs.ReviewState(Config(out_dir=tmp_path))
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
    from recordingz.web import jobs
    import threading as _threading
    gate = _threading.Event()
    state = jobs.ReviewState(Config(out_dir=tmp_path))
    q = state.subscribe()  # a page is open
    state.run_job("summary", lambda: gate.wait(5), "zápis přegenerován", "s1")
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    assert state.jobs_state()["asking"] is True and state.held is False, "asks, does not stop yet"
    state.keep_running()
    assert state.jobs_state()["asking"] is False and state.held is False
    gate.set()
    state.unsubscribe(q)


def test_cloud_only_run_uses_cloud_services_and_needs_their_keys(tmp_path, monkeypatch):
    from recordingz import fasttrack, settings
    from recordingz.config import SummarizeSettings
    from recordingz.fasttrack import cloud_only
    from recordingz.web import jobs
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
    state = jobs.ReviewState(cfg)
    with pytest.raises(ValueError, match="klíče"):
        state.run_process(rec, cloud=True)
    keys["elevenlabs"] = "k"
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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


def test_vytvorit_zapis_box_overrides_the_config_for_one_run(tmp_path, monkeypatch):
    """The page's "vytvořit zápis": --minutes / --no-minutes for this job; without it [summarize] enabled decides."""
    from typer.testing import CliRunner
    from recordingz import cli, pipeline as pl
    from recordingz.web import jobs
    rec = _make_transcribed(tmp_path)
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False, **kw: seen.append(cfg.summarize.enabled))
    for flags in ([], ["--no-minutes"], ["--minutes"]):
        res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, *flags])
        assert res.exit_code == 0, res.output
    assert seen == [True, False, True]
    state = jobs.ReviewState(Config(out_dir=tmp_path))
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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
    from recordingz.web import jobs
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
    state = jobs.ReviewState(Config(out_dir=tmp_path))
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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
    from recordingz.web import jobs
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
    state = jobs.ReviewState(off)
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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
    from recordingz.web import jobs
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

    state = jobs.ReviewState(cfg)
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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
    from recordingz.web import jobs
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
    state = jobs.ReviewState(cfg)
    with pytest.raises(ValueError, match="Claude"):
        state.run_process(rec, minutes_with="cloud")
    calls = []
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
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


def test_missing_local_minutes_model_is_said_and_the_job_ends_as_an_error(tmp_path, monkeypatch):
    import time as _time
    from recordingz import settings
    from recordingz.web import jobs, views
    cfg = Config(out_dir=tmp_path)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: False)
    assert "nemá model gemma4:31b" in views.minutes_problem(cfg)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: None)
    assert "neběží" in views.minutes_problem(cfg)

    def cli(argv, on_proc, on_line):  # the child logs the failed minutes, the job itself ends fine
        on_line("2026-10-06 ERROR 2026-10-06_1030_x: zápis se nepodařil: Ollama nemá model gemma4:31b – stáhněte ho")
    monkeypatch.setattr(jobs, "run_cli", cli)
    state = jobs.ReviewState(cfg)
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
    from recordingz.web import jobs, views
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: False)
    assert views.build_review(cfg, rec)["minutes_pull"] == "gemma4:31b", "Ollama runs, the model is missing: offer it"
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: None)
    assert views.build_review(cfg, rec)["minutes_pull"] == "", "Ollama not running: nothing to download into"
    seen = []

    def cli(argv, on_proc, on_line):
        seen.append(argv[-3:])
        on_line("PROGRESS stahuje se gemma4:31b: 45% (9.0 / 19.9 GB)")
        texts.append(state.jobs_state()["current"]["text"])
    texts: list = []
    monkeypatch.setattr(jobs, "run_cli", cli)
    state = jobs.ReviewState(cfg)
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


def test_jobs_status_file_tells_the_tray_what_runs_and_what_ended(tmp_path, monkeypatch):
    """%TEMP%\recordingz-review-jobs.json for the capture app: busy while a job runs (yellow icon), the ended jobs
    with what came out of them (the balloon), not running once the server stops; the page learns the plan."""
    import json
    import threading
    import time
    from recordingz.web import jobs
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    rec.summary_path.unlink()
    go = threading.Event()
    monkeypatch.setattr(jobs, "run_cli", lambda argv, on_proc, on_line: go.wait(5))
    st = jobs.ReviewState(cfg)
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
