"""Local review page: name the speakers of a recording by ear.

A tiny HTTP server on 127.0.0.1 serves one HTML page (`index.html` with `static/review.css` and `static/review.js`,
vanilla JS) and a JSON API. Nothing leaves the machine. The page reads the same files the CLI uses and writes only
`<stem>.speakers.json`, then regenerates the exports (and, on request, the summary). The API is the boundary: a
native client could call it later without touching the data functions.

This module is the HTTP side (routes, handler, serve); what the routes call lives next to it: views.py (a
recording's data, documents, names), people_views.py (the people page, voice prints), jobs.py (the job queue, the
event history, the capture app's recording).

REST API (described in web/openapi.py, served at /api/openapi.json)
  GET    /api/recordings?limit=                        recordings with their processing state
  GET    /api/recordings/{stem}                        everything the page needs for one recording
  GET    /api/recordings/{stem}/clip?start=&end=       a WAV clip cut from the 16 kHz mix (max CLIP_MAX_S seconds)
  GET    /api/recordings/{stem}/docs/{file}            a transcript .txt or a .summary*.md of the recording
  PUT    /api/recordings/{stem}/names                  {"names": {label: name|fields}, "title"?, "summary"?}
  PUT    /api/recordings/{stem}/title                  {"title"} rename the meeting only (folder and files follow)
  POST   /api/recordings/{stem}/process                {"force"?, "transcribe"?, "minutes_with"?, "cloud"?, "minutes"?, "compare"?, "only_with"?, "minutes_anyway"?, "voices"?} transcribe + export +
                                                        minutes (locally they wait for named speakers unless asked)
  POST   /api/recordings/{stem}/voices                 fast-track post-processing: local voices for a cloud transcript
  POST   /api/recordings/{stem}/recognize              match unnamed labels against the voice prints
  POST   /api/recordings/{stem}/summaries              {"provider", "model"} a summary with that model, in the background
  DELETE /api/recordings/{stem}/summaries/{file}       delete that summary (closing its tab)
  POST   /api/recordings/{stem}/trash                  the meeting into the trash (<out_dir>/.trash)
  DELETE /api/recordings/{stem}                        the meeting deleted for good, without the trash
  GET    /api/trash                                    what is in the trash, with the days left there
  POST   /api/trash/{stem}/restore | DELETE /api/trash/{stem} | POST /api/trash/empty
  GET    /api/summary-models                           models for summaries: Ollama on this PC, Claude for the key
  POST   /api/recordings/{stem}/speakers/merge         fold the labels of one person into one speaker
  POST   /api/recordings/{stem}/speakers/unmerge       undo the merges that remember their origin
  DELETE /api/recordings/{stem}/speakers/{label}       drop that speaker's segments (noise turned into text)
  POST   /api/recordings/{stem}/segments/assign        {"segments": [{"start", "speaker", "from"?}]} move replies to a
                                                        speaker ("@new" = a new one, "UNKNOWN" = unassigned)
  GET    /api/recordings/{stem}/speakers/{label}/replies   all replies of one speaker
  POST   /api/recordings/{stem}/meeting                {"action": confirm|detach|attach, "candidate"?}
  GET    /api/recordings/{stem}/meeting/candidates     nearby Outlook items to link instead
  GET    /api/people / PUT /api/people                 the registry (PUT replaces it; opted-out people lose prints)
  POST   /api/people/merge                             {"keep", "drop", "stem"?}
  GET    /api/people/{id}                              one person with their voice prints
  DELETE /api/people/{id}/voiceprints?stem=&label=     one print, or all of them
  GET    /api/people/{id}/recompute?stem=&label=       how much GPU work recalculating the prints needs
  POST   /api/people/{id}/recompute                    {"stem"?, "label"?} recalculate prints from the replies (a job)
  GET    /api/people/{id}/replies                      every reply of the person with its own voice compared
  POST   /api/people/{id}/replies                      compute the replies' voices (a GPU job the user confirmed)
  POST   /api/people/{id}/samples                      {"action": exclude|include|add, "stem", "label", "replace"?}
                                                        the voice prints used for recognition
  POST   /api/people/{id}/candidates                   {"stem", "label", "confirm"} a guessed name confirmed (the
                                                        voice print is stored) or rejected (the name goes)
  GET    /api/settings / PUT /api/settings             fields of the shared recordingz.toml / {"values": {key: value}}
  PUT    /api/secrets/{name} / DELETE                  {"value"} an API key into / out of the Credential Manager
  GET    /api/setup                                    the setup wizard: needed?, GPU, recommendations, Ollama, keys
  POST   /api/setup/hf-check                           {"token"?} store the Hugging Face token, check model access
  POST   /api/setup/done                               the wizard ran (it is not shown again)
  POST   /api/jobs/pause                               {"all"?} stop the GPU-heavy job for a recording (it resumes
                                                       after it); all: the cloud minutes too
  POST   /api/jobs/continue                            let it run during the recording
  POST   /api/jobs/{id}/next                           a waiting job runs right after the current one
  POST   /api/jobs/{id}/now                            ... or at once: the current one stops and runs again after it
  POST   /api/jobs/{id}/cancel                         drop a waiting job, or stop the running one for good
  GET    /api/recordings/{stem}/summaries/{file}/export   the folder remembered for this meeting name, the copy's name
  POST   /api/recordings/{stem}/summaries/{file}/export   {"folder"} save a copy there (and remember the folder)
  POST   /api/ollama/pull                              {"model"?} download the minutes model into Ollama (a job)
  POST   /api/system/pick-folder                       {"initial"?} the Windows folder dialog -> {"folder"} ("" = cancel)
  POST   /api/system/sound-settings                    open the Windows sound dialog (devices, levels, the mic array)
  GET    /api/settings/models                          the model lists asked live (Ollama on this PC, Claude API)
  GET    /api/help/{doc}                               user-guide | install | privacy
  GET    /api/status                                   job state + recent events + what the capture app records
  GET    /api/events                                   Server-Sent Events (event: log), replayed after reconnects
  GET    /api/openapi.json                             this API as OpenAPI 3.1
  POST   /api/ping, /api/quit                          heartbeat (the server exits IDLE_S after the last), stop

One server per machine: the running instance is recorded in a lock file in the temp folder, a second `review`
just opens the existing page. Closing the browser tab ends the heartbeat, so the server (and its console window)
goes away by itself.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import settings
from ..config import Config
from ..pipeline import (NEW_SPEAKER, assign_segments, speaker_replies, do_export, minutes_prompt, SUMMARY_PROVIDERS,
                        summary_path_for, meeting_info, merge_same_person, recognize_voices, remove_speaker,
                        set_meeting_link, unmerge_speakers)
from .. import exports, trash
from ..fasttrack import needs_voices  # the emergency fast track, separate from the local path
from ..recording import Recording, RecordingError, find_recording
from .jobs import JOBS_STATUS, ReviewState, _capture_watch
from .people_views import (confirm_candidate, forget_print, merge_people, people_rows, person_detail,
                           reassign_candidate, reject_candidate, sample_action, save_people)
from .views import (CLIP_MAX_S, RECENT_RECORDINGS, build_review, clip_wav, copy_doc, delete_summary, help_doc,
                    list_recordings, open_doc, read_doc, recording_docs, recording_speakers, save_names, save_title,
                    trash_state)

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("index.html")
STATIC = Path(__file__).with_name("static")  # the page's stylesheet and script, next to it
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
IDLE_S = 90.0  # exit this long after the page's last heartbeat (the page pings every 15 s)
FIRST_CONTACT_S = 600.0  # exit this long after the start when no page has asked anything yet
LOCK = Path(tempfile.gettempdir()) / "recordingz-review.json"


STEM_RE_PART = r"(?P<stem>[^/]+)"
ROUTES = [  # (method, path pattern, handler method) - keep web/openapi.py in step
    ("GET", r"/", "page"),
    ("GET", r"/static/(?P<file>[^/]+)", "static"),
    ("GET", r"/api/recordings", "recordings"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}", "recording"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/clip", "clip"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/docs", "docs"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/docs/(?P<file>[^/]+)", "doc"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/docs/(?P<file>[^/]+)/open", "open_doc"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/docs/(?P<file>[^/]+)/copy", "copy_doc"),
    ("PUT", rf"/api/recordings/{STEM_RE_PART}/names", "save_names"),
    ("PUT", rf"/api/recordings/{STEM_RE_PART}/title", "save_title"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/process", "process"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/voices", "voices"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/recognize", "recognize"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/summaries", "summarize_as"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/minutes-prompt", "minutes_prompt"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)", "delete_summary"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)/export", "export_info"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)/export", "export_summary"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/trash", "trash_recording"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}", "delete_recording"),
    ("GET", r"/api/trash", "trash"),
    ("POST", r"/api/trash/empty", "trash_empty"),
    ("POST", rf"/api/trash/{STEM_RE_PART}/restore", "trash_restore"),
    ("DELETE", rf"/api/trash/{STEM_RE_PART}", "trash_delete"),
    ("POST", r"/api/system/pick-folder", "pick_folder"),
    ("POST", r"/api/ollama/pull", "ollama_pull"),
    ("GET", r"/api/summary-models", "summary_models"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/merge", "merge"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/unmerge", "unmerge"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/speakers/(?P<label>[^/]+)", "remove_speaker"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/speakers/(?P<label>[^/]+)/replies", "speaker_replies"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/segments/assign", "assign_segments"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/meeting", "meeting"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/meeting/candidates", "meeting_candidates"),
    ("GET", r"/api/people", "people"),
    ("PUT", r"/api/people", "save_people"),
    ("POST", r"/api/people/merge", "merge_people"),
    ("GET", r"/api/people/(?P<pid>[^/]+)", "person"),
    ("DELETE", r"/api/people/(?P<pid>[^/]+)/voiceprints", "forget_prints"),
    ("POST", r"/api/people/(?P<pid>[^/]+)/candidates", "candidate"),
    ("POST", r"/api/people/(?P<pid>[^/]+)/samples", "sample"),
    ("GET", r"/api/people/(?P<pid>[^/]+)/replies", "replies"),
    ("GET", r"/api/people/(?P<pid>[^/]+)/recompute", "recompute_estimate"),
    ("POST", r"/api/people/(?P<pid>[^/]+)/recompute", "recompute"),
    ("POST", r"/api/people/(?P<pid>[^/]+)/replies", "evaluate_replies"),
    ("GET", r"/api/setup", "setup"),
    ("POST", r"/api/setup/hf-check", "setup_hf_check"),
    ("POST", r"/api/setup/done", "setup_done"),
    ("GET", r"/api/settings", "settings"),
    ("PUT", r"/api/settings", "save_settings"),
    ("GET", r"/api/settings/models", "settings_models"),
    ("PUT", r"/api/secrets/(?P<name>[^/]+)", "set_secret"),
    ("DELETE", r"/api/secrets/(?P<name>[^/]+)", "delete_secret"),
    ("POST", r"/api/system/sound-settings", "sound_settings"),
    ("POST", r"/api/jobs/pause", "jobs_pause"),
    ("POST", r"/api/jobs/continue", "jobs_continue"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/next", "job_next"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/now", "job_now"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/cancel", "job_cancel"),
    ("GET", r"/api/help/(?P<doc>[^/]+)", "help"),
    ("GET", r"/api/status", "status"),
    ("GET", r"/api/events", "events"),
    ("GET", r"/api/openapi.json", "openapi"),
    ("POST", r"/api/ping", "ping"),
    ("POST", r"/api/quit", "quit"),
]
_COMPILED = [(m, re.compile(pat + "$"), name) for m, pat, name in ROUTES]
MEETING_ACTIONS = {"confirm": "spojení potvrzeno", "detach": "odpojeno", "attach": "spojeno s jinou"}
SSE_KEEPALIVE_S = 15.0


def match_route(method: str, path: str) -> tuple[str | None, dict[str, str], bool]:
    """(handler name, path parameters, path known under another method). Pure, testable."""
    known = False
    for m, rx, name in _COMPILED:
        hit = rx.match(path)
        if hit:
            if m == method:
                return name, {k: unquote(v) for k, v in hit.groupdict().items()}, True
            known = True
    return None, {}, known


def _handler(state: ReviewState, server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive; every response carries Content-Length (SSE closes itself)

        def log_message(self, fmt, *args):  # quiet; the CLI log is enough
            log.debug("http " + fmt, *args)

        # ---- helpers
        answered = False  # a response was started for the current request (an error then cannot be sent)

        def send_response(self, code, message=None):
            self.answered = True
            super().send_response(code, message)

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, data: bytes, ctype: str):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("the request body must be a JSON object")
            return data

        def _foreign(self) -> bool:
            """A request another web page made in the browser: from its origin (CSRF), or through its own name
            resolved to 127.0.0.1 (DNS rebinding). Local programs send no Origin; a link to the page is fine."""
            port = self.server.server_address[1]
            ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host, origin = self.headers.get("Host"), self.headers.get("Origin")
            if host is not None and host.lower() not in ours:
                return True
            if origin is not None and origin.lower() not in {"http://" + h for h in ours}:
                return True
            return (self.headers.get("Sec-Fetch-Site") == "cross-site"
                    and self.headers.get("Sec-Fetch-Mode") != "navigate")

        def _dispatch(self, method: str):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if self._foreign():
                log.warning("refused %s %s from another web page (Origin %s, Host %s)", method, u.path,
                            self.headers.get("Origin"), self.headers.get("Host"))
                self.close_connection = True  # its body stays unread
                self._json({"error": "cizí původ požadavku"}, HTTPStatus.FORBIDDEN)
                return
            state.seen()
            self.answered = False
            name, params, known = match_route(method, u.path)
            if name is None:
                self._json({"error": "method not allowed" if known else "not found"},
                           HTTPStatus.METHOD_NOT_ALLOWED if known else HTTPStatus.NOT_FOUND)
                return
            try:
                body = self._body() if method in ("POST", "PUT", "DELETE") else {}
                getattr(self, "r_" + name)(q=q, body=body, **params)
            except (RecordingError, ValueError, KeyError) as e:
                if method != "GET":
                    state.event(f"{method} {u.path}: {e}", "err")
                if not self.answered:
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # OSError, LLMError, MediaError ...: still an answer, never a dropped connection
                log.exception("%s %s failed", method, u.path)
                if method != "GET":
                    state.event(f"{method} {u.path}: {e}", "err")
                if self.answered:
                    self.close_connection = True  # a half-sent response: the client must not wait for the rest
                else:
                    self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _recording(self, stem: str) -> Recording:
            return find_recording(stem, state.cfg.out_dir)

        # ---- recordings
        def r_page(self, q, body):
            self._bytes(PAGE.read_bytes(), "text/html; charset=utf-8")

        def r_static(self, q, body, file):
            path = STATIC / file
            ctype = STATIC_TYPES.get(path.suffix)
            if ctype is None or path.name != file or not path.is_file():  # only the files there, no path tricks
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._bytes(path.read_bytes(), ctype)

        def r_recordings(self, q, body):
            want = (q.get("limit") or [""])[0]
            limit = min(int(want), 2000) if want.isdigit() else RECENT_RECORDINGS
            self._json(list_recordings(state.cfg, limit))

        def r_recording(self, q, body, stem):
            self._json(build_review(state.cfg, self._recording(stem)))

        def r_clip(self, q, body, stem):
            rec = self._recording(stem)
            if not rec.mix_path or not rec.mix_path.exists():
                raise RecordingError("no mix audio")
            start = float(q.get("start", ["0"])[0])
            end = min(float(q.get("end", ["0"])[0]), start + CLIP_MAX_S)
            self._bytes(clip_wav(rec.mix_path, start, end), "audio/wav")

        def r_docs(self, q, body, stem):
            self._json(recording_docs(self._recording(stem)))

        def r_doc(self, q, body, stem, file):
            self._json({"file": file, "text": read_doc(self._recording(stem), file)})

        def r_open_doc(self, q, body, stem, file):
            self._json({"ok": True, "viewer": open_doc(self._recording(stem), unquote(file), state.cfg.md_viewer)})

        def r_copy_doc(self, q, body, stem, file):
            self._json({"ok": True, "path": str(copy_doc(self._recording(stem), unquote(file)))})

        def r_save_names(self, q, body, stem):
            rec = self._recording(stem)
            if body.get("title") is not None:
                rec = save_title(state.cfg, rec, str(body.get("title")))
            if not rec.transcript_path.exists():  # right after recording: only the title can be set yet
                state.event(f"{rec.stem}: název uložen („{rec.title}“)", "ok", rec.stem, reload=True)
                self._json({"ok": True, "written": {}, "stem": rec.stem, "title": rec.title, "status": state.status()})
                return
            written = save_names(state.cfg, rec, body.get("names") or {})
            state.event(f"{rec.stem}: uloženo {len(written)} jmen, přepis a titulky přegenerovány", "ok", rec.stem)
            job = state.run_summary(rec) if body.get("summary") else None
            self._json({"ok": True, "written": written, "stem": rec.stem, "title": rec.title,
                        "job": job[0] if job else None, "position": job[1] if job else None,
                        "status": state.status()})

        def r_save_title(self, q, body, stem):
            title = str(body.get("title") or "").strip()
            if not title:
                raise ValueError("title: an empty title is not a title")
            rec = save_title(state.cfg, self._idle_recording(stem), title)
            state.event(f"{rec.stem}: název uložen („{rec.title}“)", "ok", rec.stem)
            self._json({"ok": True, "stem": rec.stem, "title": rec.title})

        def r_voices(self, q, body, stem):
            rec = self._recording(stem)
            if not rec.transcript_path.exists() or not needs_voices(rec.read_json(rec.transcript_path)):
                raise ValueError("hlasy se doplňují jen k přepisu z rychlé cesty přes cloud, který je ještě nemá")
            job, position = state.run_voices(rec)
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_process(self, q, body, stem):
            job, position = state.run_process(self._recording(stem), force=bool(body.get("force")),
                                              cloud=bool(body.get("cloud")),
                                              minutes_anyway=bool(body.get("minutes_anyway")),
                                              voices=bool(body.get("voices")),
                                              minutes=None if body.get("minutes") is None else bool(body.get("minutes")),
                                              compare=body["compare"] if isinstance(body.get("compare"), bool) else None,
                                              only_with=str(body.get("only_with") or ""),
                                              transcribe=str(body.get("transcribe") or ""),
                                              minutes_with=str(body.get("minutes_with") or ""))
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_speaker_replies(self, q, body, stem, label):
            rec = self._recording(stem)
            self._json({"label": label, "replies": speaker_replies(rec, unquote(label)),
                        "new_speaker": NEW_SPEAKER, "speakers": recording_speakers(state.cfg, rec),
                        "has_mix": bool(rec.mix_path and rec.mix_path.exists())})

        def r_assign_segments(self, q, body, stem):
            rec = self._recording(stem)
            moves = body.get("segments")
            if not isinstance(moves, list) or not moves:
                raise ValueError("segments: [{start, speaker, from?}] needed")
            n = assign_segments(state.cfg, rec, moves)
            verb = "přesunuto" if any(isinstance(m, dict) and m.get("from") for m in moves) else "přiřazeno"
            state.event(f"{rec.stem}: {verb} {n} replik, přepis a titulky přegenerovány", "ok", rec.stem, reload=True)
            self._json({"ok": True, "assigned": n})

        def r_minutes_prompt(self, q, body, stem):
            self._json(minutes_prompt(state.cfg, self._recording(stem), str(body.get("provider") or "").strip(),
                                      str(body.get("model") or "").strip()))

        def r_summarize_as(self, q, body, stem):
            rec = self._recording(stem)
            provider, model = str(body.get("provider") or "").strip(), str(body.get("model") or "").strip()
            if provider not in SUMMARY_PROVIDERS or not model:
                raise ValueError("provider (ollama | anthropic | openai) and model are needed")
            job, position = state.run_job("summary", ["run-job", "summarize-as", rec.stem, "--provider", provider,
                                                      "--model", model],
                                          f"zápis {model} hotový", rec.stem, start=f"{rec.stem}: zápis {model} se generuje",
                                          light=provider != "ollama")  # the cloud: may run during a recording
            self._json({"ok": True, "job": job, "position": position, "jobs": state.jobs_state(),
                        "file": summary_path_for(state.cfg, rec, provider, model).name})

        def r_delete_summary(self, q, body, stem, file):
            rec = self._recording(stem)
            delete_summary(rec, file)
            state.event(f"{rec.stem}: zápis {file} smazán", "info", rec.stem, reload=True)
            self._json({"ok": True})

        # ---- the trash: whole meetings
        def _idle_recording(self, stem: str) -> Recording:
            """The recording, when no job runs or waits for it and the capture app is not recording it (deleting or
            renaming it would pull its files from under them)."""
            rec = self._recording(stem)
            if queue_place := state.job_of(rec.stem):
                raise ValueError(f"{rec.stem}: {queue_place} – nejdřív ho zrušte")
            if state.capture.get("recording") and state.capture.get("stem") == rec.stem:
                raise ValueError(f"{rec.stem} se právě nahrává")
            return rec

        def r_trash_recording(self, q, body, stem):
            rec = self._idle_recording(stem)
            trash.move_to_trash(state.cfg, rec)
            state.event(f"{rec.stem}: „{rec.title}“ přesunuto do koše", "info")
            self._json({"ok": True, "trash": trash_state(state.cfg)})

        def r_delete_recording(self, q, body, stem):
            rec = self._idle_recording(stem)
            trash.delete_now(state.cfg, rec)
            state.event(f"{rec.stem}: „{rec.title}“ smazáno natrvalo", "info")
            self._json({"ok": True})

        def r_trash(self, q, body):
            self._json(trash_state(state.cfg))

        def r_trash_restore(self, q, body, stem):
            back = trash.restore(state.cfg, stem)
            state.event(f"{back}: obnoveno z koše", "ok")
            self._json({"ok": True, "stem": back, "trash": trash_state(state.cfg)})

        def r_trash_delete(self, q, body, stem):
            trash.delete_from_trash(state.cfg, stem)
            state.event(f"{stem}: smazáno z koše natrvalo", "info")
            self._json({"ok": True, "trash": trash_state(state.cfg)})

        def r_trash_empty(self, q, body):
            gone = trash.empty(state.cfg)
            state.event(f"koš vysypán: {len(gone)} schůzek smazáno natrvalo", "info")
            self._json({"ok": True, "deleted": gone, "trash": trash_state(state.cfg)})

        def r_summary_models(self, q, body):
            live = (q.get("live") or [""])[0] in ("1", "true")
            sug = settings.suggestions(settings.read_values(state.settings_path), live=live)
            sm = state.cfg.summarize
            provider, _, model = sm.offer.partition(":")
            self._json({**sug["summary"], "loaded": sug["models_loaded"],
                        "default": {"provider": provider, "model": model} if provider and model
                        else {"provider": sm.provider, "model": sm.model},
                        "main": {"provider": sm.provider, "model": sm.model}})

        def r_recognize(self, q, body, stem):
            rec = self._recording(stem)
            m = recognize_voices(state.cfg, rec)
            state.event(f"{rec.stem}: po hlase poznáno {len(m)} mluvčích" + (" (nepotvrzeno)" if m else ""),
                        "ok" if m else "info", rec.stem, reload=True)
            self._json({"ok": True, "matches": m})

        def r_merge(self, q, body, stem):
            rec = self._recording(stem)
            res = merge_same_person(state.cfg, rec)
            state.event(f"{rec.stem}: sloučeno {res['merged']} označení, otisků přibylo {res['prints']}",
                        "ok" if res["merged"] else "info", rec.stem, reload=True)
            self._json({"ok": True, **res})

        def r_unmerge(self, q, body, stem):
            rec = self._recording(stem)
            n = unmerge_speakers(state.cfg, rec)
            state.event(f"{rec.stem}: sloučení vráceno, {n} replik zpět", "ok", rec.stem, reload=True)
            self._json({"ok": True, "restored": n})

        def r_remove_speaker(self, q, body, stem, label):
            rec = self._recording(stem)
            n = remove_speaker(state.cfg, rec, label)
            state.event(f"{rec.stem}: smazáno {n} replik mluvčího {label}", "ok", rec.stem, reload=True)
            self._json({"ok": True, "removed": n})

        def r_meeting(self, q, body, stem):
            action = str(body.get("action") or "")
            rec = set_meeting_link(state.cfg, self._recording(stem), action, body.get("candidate"))
            if rec.transcript_path.exists():
                do_export(state.cfg, rec)
            state.event(f"{rec.stem}: schůzka z kalendáře – {MEETING_ACTIONS.get(action, action)}", "ok", rec.stem,
                        reload=True)
            self._json({"ok": True, "stem": rec.stem, "meeting": meeting_info(state.cfg, rec)})

        def r_meeting_candidates(self, q, body, stem):
            from ..outlook import candidates_for
            start = self._recording(stem).sidecar.get("start")
            self._json({"candidates": candidates_for(datetime.fromisoformat(start), 4 * 3600)
                        if start and state.cfg.calendar_outlook else []})

        # ---- people
        def r_people(self, q, body):
            self._json(people_rows(state.cfg))

        def r_save_people(self, q, body):
            rows = save_people(state.cfg, body.get("people") or [], body.get("stem") or None)
            state.event(f"seznam lidí uložen ({len(rows)} osob)", "ok")
            self._json({"ok": True, "people": rows})

        def r_merge_people(self, q, body):
            keep, drop = str(body.get("keep") or ""), str(body.get("drop") or "")
            rows = merge_people(state.cfg, keep, drop, body.get("stem") or None)
            state.event(f"osoba {drop} sloučena do {keep} (ve všech nahrávkách)", "ok", reload=True)
            self._json({"ok": True, "people": rows})

        def r_person(self, q, body, pid):
            self._json(person_detail(state.cfg, pid))

        def r_candidate(self, q, body, pid):
            """{"stem", "label", "confirm": true|false} or {"stem", "label", "to": person id}: an assignment confirmed,
            rejected, or given to somebody else."""
            stem, label = str(body.get("stem") or ""), str(body.get("label") or "")
            if not stem or not label:
                raise ValueError("stem and label are needed")
            if body.get("to"):
                res = reassign_candidate(state.cfg, stem, label, pid, str(body["to"]))
                state.event(f"{stem}: {label} přiřazeno jiné osobě" + (" a otisk uložen" if res["prints_added"] else ""),
                            "ok", stem, reload=True)
            elif body.get("confirm"):
                res = confirm_candidate(state.cfg, stem, label, pid)
                state.event(f"{stem}: {label} potvrzeno" + (" a hlasový otisk uložen" if res["prints_added"] else ""),
                            "ok", stem, reload=True)
            else:
                res = reject_candidate(state.cfg, stem, label, pid)
                state.event(f"{stem}: {label} – jméno odebráno (není to ta osoba)", "ok", stem, reload=True)
            self._json({"ok": True, **res})

        def r_recompute_estimate(self, q, body, pid):
            from .. import replies
            stem, label = (q.get("stem") or [""])[0], (q.get("label") or [""])[0]
            self._json(replies.recompute_estimate(state.cfg, pid, (stem, label) if stem and label else None))

        def r_recompute(self, q, body, pid):
            job, position = state.run_reprint(pid, str(body.get("stem") or ""), str(body.get("label") or ""))
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_replies(self, q, body, pid):
            from .. import replies
            self._json(replies.evaluate(state.cfg, pid))

        def r_evaluate_replies(self, q, body, pid):
            job, position = state.run_replies(pid)
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_sample(self, q, body, pid):
            """{"action": exclude | include | add, "stem", "label", "replace"?: {stem, label}}"""
            stem, label = str(body.get("stem") or ""), str(body.get("label") or "")
            if not stem or not label:
                raise ValueError("stem and label are needed")
            self._json(sample_action(state.cfg, pid, str(body.get("action") or ""), stem, label,
                                     body.get("replace") or None))

        def r_forget_prints(self, q, body, pid):
            n = forget_print(state.cfg, pid, (q.get("stem") or [None])[0], (q.get("label") or [None])[0])
            state.event(f"{pid}: smazáno {n} hlasových otisků", "ok")
            self._json({"ok": True, "removed": n})

        # ---- server
        # ---- settings (shared recordingz.toml) and API keys; key values never come back
        def r_setup(self, q, body):
            from .. import setup
            if (q.get("needed") or [""])[0] in ("1", "true"):  # the page's start: no probing of GPU or Ollama
                self._json({"needed": setup.needed(state.settings_path)})
                return
            self._json(setup.status(settings.read_values(state.settings_path), state.settings_path))

        def r_setup_hf_check(self, q, body):
            """Store the token (if given) and check that it can download the diarization model."""
            from .. import setup
            token = str(body.get("token") or "").strip()
            if token:
                settings.set_secret("huggingface", token)
            repo = str(settings.read_values(state.settings_path).get("transcribe.diarize_model") or "")
            self._json(setup.hf_access(repo, token or settings.get_secret("huggingface") or settings.hf_login_token()))

        def r_setup_done(self, q, body):
            from .. import setup
            setup.mark_done(state.settings_path)
            state.event("průvodce nastavením dokončen", "ok")
            self._json({"ok": True})

        def r_settings(self, q, body):
            self._json(settings.describe(state.settings_path))

        def r_settings_models(self, q, body):
            sug = settings.suggestions(settings.read_values(state.settings_path), live=True)
            self._json({"suggestions": sug})

        def r_save_settings(self, q, body):
            values = body.get("values")
            if not isinstance(values, dict):
                raise ValueError("values must be an object")
            res = settings.save(values, state.settings_path)
            if res["changed"]:
                state.cfg = settings.reload(state.cfg, state.settings_path)
                state.event(f"nastavení uloženo: {', '.join(res['changed'])}"
                            + (" (složka nahrávek platí po restartu)" if res["restart"] else ""), "ok")
            self._json({"ok": True, **res, **settings.describe(state.settings_path)})

        def r_set_secret(self, q, body, name):
            settings.set_secret(name, str(body.get("value") or ""))
            state.event(f"klíč {name} uložen do Správce přihlašovacích údajů", "ok")
            self._json({"ok": True, "secrets": settings.secret_states()})

        def r_delete_secret(self, q, body, name):
            removed = settings.delete_secret(name)
            state.event(f"klíč {name} " + ("smazán ze Správce přihlašovacích údajů" if removed else "tam nebyl"), "info")
            self._json({"ok": True, "removed": removed, "secrets": settings.secret_states()})

        def r_jobs_pause(self, q, body):
            state.pause_for_recording("rozhodnuto na stránce", everything=bool(body.get("all")))
            self._json({"ok": True, "jobs": state.jobs_state()})

        def r_jobs_continue(self, q, body):
            state.keep_running()
            self._json({"ok": True, "jobs": state.jobs_state()})

        def r_job_next(self, q, body, job):
            self._json({"ok": True, "text": state.move_job(_job_id(job)), "jobs": state.jobs_state()})

        def r_job_now(self, q, body, job):
            self._json({"ok": True, "text": state.move_job(_job_id(job), now=True), "jobs": state.jobs_state()})

        def r_job_cancel(self, q, body, job):
            self._json({"ok": True, "text": state.cancel_job(_job_id(job)), "jobs": state.jobs_state()})

        def r_export_info(self, q, body, stem, file):
            rec = self._recording(stem)
            path = rec.dir / unquote(file)
            self._json({"folder": exports.folder_for(state.cfg.out_dir, rec.title),
                        "name": exports.copy_name(rec, path)})

        def r_export_summary(self, q, body, stem, file):
            rec = self._recording(stem)
            target = exports.save_copy(rec, unquote(file), str(body.get("folder") or ""), state.cfg.out_dir)
            state.event(f"{rec.stem}: kopie zápisu uložena do {target}", "ok", rec.stem)
            self._json({"ok": True, "path": str(target), "folder": str(target.parent)})

        def r_ollama_pull(self, q, body):
            job, position = state.run_pull(str(body.get("model") or state.cfg.summarize.model))
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_pick_folder(self, q, body):
            self._json({"folder": exports.pick_folder(str(body.get("initial") or ""))})

        def r_sound_settings(self, q, body):
            if os.name != "nt":
                raise ValueError("the sound dialog is a Windows thing")
            import subprocess
            subprocess.Popen(["control", "mmsys.cpl"])  # the classic dialog: recording devices, levels, enable/disable
            self._json({"ok": True})

        def r_help(self, q, body, doc):
            self._json(help_doc(doc))

        def r_status(self, q, body):
            self._json(state.status())

        def r_openapi(self, q, body):
            from .openapi import spec
            from .. import __version__
            self._json(spec(__version__))

        def r_ping(self, q, body):
            self._json({"ok": True})

        def r_quit(self, q, body):
            if state.status()["busy"]:  # running or waiting jobs
                self._json({"error": "a job is still running or waiting, wait for it to finish"}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
            threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()

        def r_events(self, q, body):
            """Server-Sent Events: every state.event() as `event: log`; on connect the recent history (or what
            was missed since Last-Event-ID), then a comment every SSE_KEEPALIVE_S so dead streams are noticed."""
            last = self.headers.get("Last-Event-ID") or (q.get("since") or [""])[0]
            since = int(last) if str(last).isdigit() else None
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sub = state.subscribe()
            try:
                self._sse("hello", {"seq": state.seq, "busy": state.busy, "replay": since is None,
                                    "capture": state.capture, "jobs": state.jobs_state()})
                for e in state.events_since(since):
                    self._sse("log", {**e, "replay": since is None}, e["n"])
                while not getattr(server_ref.get("server"), "_recordingz_stopping", False):
                    try:
                        e = sub.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if e.get("type") in ("capture", "jobs"):  # states, not history (no id: not replayed)
                        self._sse(e["type"], {k: v for k, v in e.items() if k != "type"})
                    else:
                        self._sse("log", e, e["n"])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass  # the page went away
            finally:
                state.unsubscribe(sub)

        def _sse(self, event: str, data: dict, eid: int | None = None):
            frame = (f"id: {eid}\n" if eid is not None else "") + f"event: {event}\n" \
                    + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
    return Handler


def running_instance(cfg: Config, lock: Path = LOCK) -> str | None:
    """URL of a review server already running for this recordings folder, if it answers."""
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
        if Path(info.get("out_dir", "")) != cfg.out_dir:
            return None
        with urllib.request.urlopen(info["url"] + "api/status", timeout=1.0) as r:
            st = json.loads(r.read().decode("utf-8"))
            if st.get("app") == "recordingz-review" and Path(st.get("out_dir", "")) == cfg.out_dir:
                return info["url"]  # alive and really ours; a stale lock (crash, reboot) fails here
    except Exception:
        pass
    return None

def _job_id(text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"not a job id: {text}")
    return int(text)


def _idle_watchdog(state: ReviewState, server: ThreadingHTTPServer, idle_s: float,
                   first_contact_s: float | None = None) -> None:
    """Stop the server once the page has gone quiet (tab closed). Never while a job runs, and never while a page
    holds its event stream open: a hidden or minimized window throttles its timers, so its pings may stop for
    minutes (2026-10-02: the desktop window lost its server and did not show new recordings). A stream whose page
    is gone fails on the next keepalive and unsubscribes. A server no page has contacted at all stops after
    FIRST_CONTACT_S (a window that never opened, a crashed starter)."""
    first_contact_s = FIRST_CONTACT_S if first_contact_s is None else first_contact_s
    started = time.monotonic()
    while True:
        time.sleep(min(5.0, idle_s / 3, first_contact_s / 3))
        with state.lock:
            if state.subscribers:
                state.last_seen = time.monotonic()
        now = time.monotonic()
        if not state.busy and not state.last_seen and now - started > first_contact_s:
            log.info("no page came in %.0f s, stopping", first_contact_s)
            threading.Thread(target=server.shutdown, daemon=True).start()
            return
        if state.last_seen and not state.busy and now - state.last_seen > idle_s:
            log.info("review page closed, stopping")
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def serve(cfg: Config, rec: Recording | None, *, port: int = 0, open_browser: bool = True,
          idle_s: float = IDLE_S, lock: Path = LOCK) -> str:
    """Run the review server until the page's Close button, the tab is closed, or Ctrl+C. Returns the URL.
    A second call while one is running only opens the existing page."""
    fragment = f"#{rec.stem}" if rec else ""
    existing = running_instance(cfg, lock)
    if existing:
        log.info("review page already running: %s", existing)
        if open_browser:
            webbrowser.open(existing + fragment)
        return existing
    state = ReviewState(cfg)
    threading.Thread(target=trash.auto_empty, args=(cfg,), daemon=True).start()  # what has been in the trash long enough
    state.jobs_file = JOBS_STATUS
    state.write_jobs_file()
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(state, ref))
    ref["server"] = server
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    lock.write_text(json.dumps({"url": base, "pid": os.getpid(), "out_dir": str(cfg.out_dir)}), encoding="utf-8")
    log.info("review page: %s  (closes itself when the tab is closed; Ctrl+C also works)", base + fragment)
    if open_browser:
        webbrowser.open(base + fragment)
    threading.Thread(target=_idle_watchdog, args=(state, server, idle_s), daemon=True).start()
    threading.Thread(target=_capture_watch, args=(state,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.write_jobs_file(running=False)
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except Exception:
            pass
    return base
