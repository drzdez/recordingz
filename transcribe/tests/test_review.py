"""The review page's server: its data, the REST API, events, documents, single instance, web safety."""

import json
from pathlib import Path

import pytest

from recordingz.config import Config
from recordingz.providers.base import Segment
from recordingz.recording import Recording, RecordingError, resolve_recording

from helpers import _make_recording, _make_transcribed, _serve, _call


def test_review_data_clip_and_save(tmp_path):
    from recordingz.web.views import build_review, clip_wav, list_recordings, save_names, unresolved_labels
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
    from recordingz.web.people_views import save_people
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
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from recordingz.web.jobs import ReviewState
    from recordingz.web.review import _handler
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ref = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ReviewState(cfg), ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "recordingz" in page and 'src="static/review.js"' in page and 'href="static/review.css"' in page
        with urllib.request.urlopen(base + "/static/review.js") as r:  # the page's script and stylesheet
            assert r.headers["Content-Type"].startswith("text/javascript") and b"@ts-check" in r.read()
        with urllib.request.urlopen(base + "/static/review.css") as r:
            assert r.headers["Content-Type"].startswith("text/css") and b":root" in r.read()
        for bad in ("/static/..%5Creview.py", "/static/missing.js", "/static/..%2F..%2Fcli.py"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404, bad
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
    from recordingz.web.jobs import ReviewState
    from recordingz.web.review import _handler
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
            if not pattern.startswith("/api/"):  # the page and its files, not the API
                continue
            as_doc = (pattern.replace("(?P<stem>[^/]+)", "{stem}").replace("(?P<file>[^/]+)", "{file}")
                      .replace("(?P<label>[^/]+)", "{label}").replace("(?P<pid>[^/]+)", "{id}")
                      .replace("(?P<doc>[^/]+)", "{doc}").replace("(?P<name>[^/]+)", "{name}")
                      .replace("(?P<job>[^/]+)", "{job}").replace("\\.", "."))
            assert as_doc in documented, f"{method} {as_doc} missing in openapi.py"
            assert method.lower() in spec["paths"][as_doc], f"{method} {as_doc} not documented"
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
    from recordingz.web.views import help_doc
    for name in ("user-guide", "install", "privacy"):
        d = help_doc(name)
        assert d["doc"] == name and d["text"].startswith("#"), name
    with pytest.raises(RecordingError, match="unknown help document"):
        help_doc("../README")


def test_recording_docs_and_read_doc(tmp_path):
    from recordingz.web.views import read_doc, recording_docs
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
    from recordingz.web.views import delete_summary
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
    from recordingz.web import views
    rec = _make_transcribed(tmp_path)
    rec.summary_path.write_text("# T\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(views, "_launch", lambda argv, path: calls.append((argv, path)))
    monkeypatch.setattr(views, "_clip_file", lambda path: calls.append(("clip", path)))
    viewer = tmp_path / "markdownz.exe"
    viewer.write_text("", encoding="utf-8")
    assert views.open_doc(rec, rec.summary_path.name, f'"{viewer}"') == "markdownz"
    assert calls[-1] == ([str(viewer), str(rec.summary_path)], rec.summary_path), "the configured viewer, quotes off"
    views.open_doc(rec, rec.summary_path.name, "")
    assert calls[-1] == (None, rec.summary_path), "nothing configured: the default app"
    with pytest.raises(Exception, match="not found"):
        views.open_doc(rec, rec.summary_path.name, str(tmp_path / "missing.exe"))
    assert views.copy_doc(rec, rec.summary_path.name) == rec.summary_path and calls[-1] == ("clip", rec.summary_path)
    rec.file(".txt").write_text("x", encoding="utf-8")
    views.open_doc(rec, rec.file(".txt").name, f'"{viewer}"')
    assert calls[-1] == (None, rec.file(".txt")), "only .md goes to the viewer, a transcript to the default app"
    for bad in ("../x.md", f"{rec.stem}.json", f"{rec.stem}.nothere.md"):
        with pytest.raises(Exception):
            views.copy_doc(rec, bad)
        with pytest.raises(Exception):
            views.open_doc(rec, bad, "")
    assert len(calls) == 4


def test_review_single_instance_and_idle_exit(tmp_path):
    import json as _json
    import threading
    import time
    import urllib.request
    from recordingz.web import review
    cfg = Config(out_dir=tmp_path)
    _make_transcribed(tmp_path)
    lock = tmp_path / "lock.json"
    assert review.running_instance(cfg, lock) is None  # no lock file
    lock.write_text(_json.dumps({"url": "http://127.0.0.1:1/", "pid": 0, "out_dir": str(tmp_path)}), encoding="utf-8")
    assert review.running_instance(cfg, lock) is None  # stale lock: nobody answers -> ignored
    lock.unlink()
    result = {}
    t = threading.Thread(target=lambda: result.update(url=review.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock)),
                         daemon=True)
    t.start()
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.05)
    info = _json.loads(lock.read_text(encoding="utf-8"))
    assert review.running_instance(cfg, lock) == info["url"]                     # answers -> reuse
    assert review.running_instance(Config(out_dir=tmp_path / "other"), lock) is None  # different folder -> not ours
    assert review.serve(cfg, None, open_browser=False, lock=lock) == info["url"]  # second call just returns the URL
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
    from recordingz.web import review
    monkeypatch.setattr(review, "SSE_KEEPALIVE_S", 0.3)  # a closed page is noticed at the next keepalive write
    cfg = Config(out_dir=tmp_path)
    lock = tmp_path / "lock.json"
    t = threading.Thread(target=lambda: review.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock), daemon=True)
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


def test_speaker_languages_show_a_second_pass_in_another_language():
    from recordingz.web.views import speaker_languages
    segs = [Segment(0, 20, "Dobrý den", "SPEAKER_00", language="cs"), Segment(20, 30, "Ahoj", "SPEAKER_00", language="cs"),
            Segment(30, 60, "Dobrý deň", "SPEAKER_01"), Segment(60, 62, "ok", "SPEAKER_01", language="en"),
            Segment(62, 80, "Takže", "SPEAKER_02", language="sk"), Segment(80, 90, "So", "SPEAKER_02", language="en")]
    langs = speaker_languages(segs, "sk")
    assert langs["SPEAKER_00"] == {"language": "cs", "own": True, "mixed": False}
    assert langs["SPEAKER_01"] == {"language": "sk", "own": False, "mixed": False}, "2 s of English is not mixed"
    assert langs["SPEAKER_02"] == {"language": "sk", "own": False, "mixed": True}
    assert speaker_languages([Segment(0, 5, "x", "A")], None)["A"] == {"language": "?", "own": False, "mixed": False}


def test_the_processing_offer_says_what_each_way_transcribes_and_writes_the_minutes_with(tmp_path):
    from recordingz.config import SummarizeSettings, TranscribeSettings
    from recordingz.fasttrack import plans
    from recordingz.web import views
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
    offer = views.build_review(cloudy, rec)
    assert offer["plans"] == plans(cloudy) and offer["minutes_auto"] is False, "vytvořit zápis unticked by the config"


def test_minutes_by_another_model_count_as_minutes_and_the_main_tab_says_so(tmp_path, monkeypatch):
    from recordingz import pipeline as pl
    from recordingz.config import SummarizeSettings
    from recordingz.web.views import has_minutes, list_recordings, summary_info
    rec = _make_transcribed(tmp_path)
    rec.summary_path.unlink()
    assert not has_minutes(rec) and list_recordings(Config(out_dir=tmp_path))[0]["summary"] is False
    other = rec.file(".summary.claude-opus-5-5.md")
    other.write_text("<!-- recordingz summary | anthropic: claude-opus-5-5 | created: x -->\n# T", encoding="utf-8")
    assert has_minutes(rec) and list_recordings(Config(out_dir=tmp_path))[0]["summary"] is True
    rec.summary_path.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
    from recordingz.web.views import recording_docs
    assert [x["label"] for x in recording_docs(rec)["summaries"]] == ["claude-opus-5-5 · hlavní", "claude-opus-5-5"]
    assert summary_info(rec.summary_path, True)["label"] == "claude-opus-5-5", "alone it is just the model"
    calls = []
    monkeypatch.setattr(pl, "summarize_as", lambda cfg, r, p, m: calls.append((p, m)))
    cfg = Config(out_dir=tmp_path, summarize=SummarizeSettings(provider="anthropic", model="claude-opus-5-5",
                                                              compare=("anthropic:claude-opus-5-5", "ollama:qwen3:8b")))
    pl.do_summarize_compare(cfg, rec, force=True)
    assert calls == [("ollama", "qwen3:8b")], "no second copy of the main minutes"


def test_the_list_flags_transcripts_without_word_alignment(tmp_path):
    from recordingz.web.views import is_unaligned, list_recordings
    rec = _make_transcribed(tmp_path)
    assert list_recordings(Config(out_dir=tmp_path))[0]["unaligned"] is False
    data = rec.read_json(rec.transcript_path)
    data.update(provider="whisperx", timings={"transcribe_s": 60.0, "diarize_s": 20.0})  # alignment skipped
    rec.write_json(rec.transcript_path, data)
    assert list_recordings(Config(out_dir=tmp_path))[0]["unaligned"] is True
    assert not is_unaligned({"provider": "whisperx", "timings": {"align_s": 12.0}})
    assert not is_unaligned({"provider": "elevenlabs", "timings": {}}), "the cloud brings its own word times"


def test_a_name_the_minutes_guessed_can_be_used_with_one_click(tmp_path):
    from recordingz.web.views import build_review, summary_hint_names
    rec = _make_transcribed(tmp_path)
    rec.summary_path.write_text("# T\n\n## Mluvčí\n| Označení | Jméno | Poznámka |\n|---|---|---|\n"
                                "| SPEAKER_00 | ? | vedl schůzku |\n| SPEAKER_01 | **Roman Kučera** | navrhl proces |\n",
                                encoding="utf-8")
    assert summary_hint_names(rec) == {"SPEAKER_01": "Roman Kučera"}
    sp = {s["label"]: s for s in build_review(Config(out_dir=tmp_path), rec)["speakers"]}
    assert sp["SPEAKER_01"]["hint_name"] == "Roman Kučera" and sp["SPEAKER_00"]["hint_name"] == ""
    assert sp["SPEAKER_01"]["hint"].startswith("**Roman Kučera**") or "Roman Kučera" in sp["SPEAKER_01"]["hint"]


def test_a_recording_edited_after_its_minutes_is_marked_until_they_are_written_again(tmp_path):
    """"✎ upraveno": names or replies changed after the minutes (the transcript is the source of truth, the minutes
    may say the old names); writing the minutes again clears it; without minutes there is nothing stale."""
    import os
    import time
    from recordingz import pipeline as pl
    from recordingz.web.views import build_review, list_recordings, save_names
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
    from recordingz.web.views import recording_docs
    stale = {d["file"]: d["stale"] for d in recording_docs(rec)["summaries"]}
    assert stale == {rec.summary_path.name: True, other.name: False}, "the older one is marked outdated"
    other.unlink()
    rec.summary_path.unlink()
    assert pl.minutes_stale(rec) is None, "no minutes: the transcript texts are already current"


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
    from recordingz.web import review
    monkeypatch.setattr(review, "FIRST_CONTACT_S", 1.0)
    lock = tmp_path / "lock.json"
    t = threading.Thread(target=lambda: review.serve(Config(out_dir=tmp_path), None, open_browser=False, lock=lock),
                         daemon=True)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive() and not lock.exists()
