"""What several test files share: recordings made on disk, a review server on a random port."""

import json
from pathlib import Path

from recordingz.config import Config, VoiceprintSettings
from recordingz.providers.base import Segment
from recordingz.recording import Recording

VP_ON = VoiceprintSettings(enabled=True)  # voice prints are opt-in; tests that need them say so


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


def _segs():
    return [Segment(0, 5, "a", "SPEAKER_00"), Segment(5, 10, "b", "SPEAKER_01"),
            Segment(10, 15, "c", "SPEAKER_00"), Segment(15, 16, "d", "SPEAKER_02"), Segment(60, 61, "e", "SPEAKER_00")]


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


def _serve(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from recordingz.web.jobs import ReviewState
    from recordingz.web.review import _handler
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
