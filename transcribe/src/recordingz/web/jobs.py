"""The review server's state: the one queue of background jobs (each a `recordingz run-job` child process that
can be stopped), the event history the pages follow, and the capture app's recording that holds GPU jobs."""

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
from datetime import datetime
from pathlib import Path

from .. import settings
from ..config import Config, default_config_path
from ..fasttrack import choices, cloud_ready  # the emergency fast track, separate from the local path
from ..fileio import atomic_write_text
from ..recording import Recording, find_recording
from .views import has_minutes

log = logging.getLogger(__name__)

MAX_EVENTS = 200   # kept in memory only: the log is a convenience, the files are the truth
CAPTURE_STATUS = Path(tempfile.gettempdir()) / "recordingz-capture.json"  # written by the capture app on every change
# written here on every change of the jobs, read by the capture app: the tray icon turns yellow while a job runs, and a
# balloon says when a recording is processed – so the window can be closed while it works
JOBS_STATUS = Path(tempfile.gettempdir()) / "recordingz-review-jobs.json"
FINISHED_KEPT = 10
CAPTURE_POLL_S = 2.0


RECORDING_ASK_S = 10.0  # with when_recording = ask: no answer within this -> stop (the sound is breaking meanwhile)



def run_cli(argv: list[str], on_proc, on_line) -> None:
    """Run `recordingz <argv>` as a child process (a job that can be stopped). on_proc(proc) gets the
    process as soon as it runs; on_line(text) every line it logs. Raises RuntimeError with its last message when it
    fails. Tests replace this function."""
    import subprocess
    import sys as _sys
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([_sys.executable, "-m", "recordingz.cli", *argv], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags, env=env)
    on_proc(proc)
    last = ""
    for line in proc.stdout or []:
        line = line.rstrip()
        if line:
            last = line
            on_line(line)
    if proc.wait() != 0:
        raise RuntimeError(last or f"exit code {proc.returncode}")


def _kill_tree(proc) -> None:
    """Stop a job's process with everything it started (ffmpeg, ...). Ollama stops generating when the
    connection goes away."""
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
        else:
            proc.kill()
    except Exception as e:  # already gone
        log.debug("kill job: %s", e)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def capture_status(path: Path = CAPTURE_STATUS) -> dict:
    """What the capture app is doing: {"running", "recording", "title", "stem", "source", "started"}. A file left
    by an app that is gone (crash, power loss) counts as not running."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False, "recording": False}
    if not d.get("running") or not _pid_alive(int(d.get("pid") or 0)):
        return {"running": False, "recording": False}
    return {k: d.get(k) for k in ("running", "recording", "title", "stem", "source", "started")}


def _after(argv: list, flag: str) -> str:
    """The value after a flag in a job's command line, "" without it."""
    return str(argv[argv.index(flag) + 1]) if flag in argv and argv.index(flag) + 1 < len(argv) else ""


def _title_of(cfg: Config, stem: str) -> str:
    if not stem:
        return ""
    try:
        return find_recording(stem, cfg.out_dir).title or stem
    except Exception:
        return stem


class ReviewState:
    settings_path: Path  # the recordingz.toml the settings page edits (the one the config came from)

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings_path = cfg.source_path or default_config_path()
        self.capture: dict = {"running": False, "recording": False}  # the capture app, from _capture_watch
        self.lock = threading.RLock()
        self.busy = False
        self.working = False  # the worker thread is alive (busy goes idle before its last end event)
        self.message = ""
        self.error = ""
        self.last_seen = 0.0  # time of the last request from the page (0 = no page yet)
        self.events: list[dict] = []
        self.seq = 0
        self.subscribers: list[queue.Queue] = []  # one per open /api/events stream
        self.jobs: list[dict] = []        # waiting background jobs, in order
        self.current: dict | None = None  # the job running now (id, name, stem, text, started)
        self.running: dict | None = None  # ... the whole job (its process, to stop it)
        self.next_job = 0
        self.held = False    # a recording runs: jobs wait (the GPU would break the recorded sound) ...
        self.hold_all = False  # ... only the GPU-heavy ones, unless the user chose "Přerušit vše": then all
        self.asking = False  # the pages are asked whether to stop the running job for the recording
        self.jobs_file: Path | None = None  # JOBS_STATUS when serving (never in tests)
        self.jobs_file_lock = threading.Lock()
        self.finished: list[dict] = []  # the last jobs that ended, for the tray's balloon
        self.wake = threading.Condition(self.lock)

    def seen(self) -> None:
        self.last_seen = time.monotonic()

    def event(self, text: str, level: str = "ok", stem: str = "", reload: bool = False, job_end: bool = False,
              job: int | None = None) -> int:
        """One line of history: what the server did, for the status bar and the list behind it. A long job is
        started and forgotten, so the page has to be able to ask later what happened. job_end marks the line that
        closes a background job; `job` is its id (the page waiting for that job wakes on it)."""
        with self.lock:
            self.seq += 1
            e = {"n": self.seq, "at": datetime.now().strftime("%H:%M:%S"), "text": text,
                 "level": level, "stem": stem, "reload": reload, "busy": self.busy, "job_end": job_end, "job": job}
            self.events.append(e)
            del self.events[:-MAX_EVENTS]
            for q in self.subscribers:
                q.put(e)
        log.info("%s", text)
        return self.seq

    def set_capture(self, status: dict) -> None:
        """The capture app's state changed: tell the pages (SSE `capture`), and log the start and end."""
        with self.lock:
            before, self.capture = getattr(self, "capture", {"recording": False}), status
            subscribers = list(self.subscribers)
        for q in subscribers:
            q.put({"type": "capture", **status})
        if status.get("recording") and not before.get("recording"):
            self.event(f"nahrávání probíhá: {status.get('title') or status.get('stem')}", "info")
            self._recording_started()
        elif before.get("recording") and not status.get("recording"):
            # the new recording appears in the list once its sidecar is written: reload
            self.event(f"nahrávání skončilo: {before.get('title') or before.get('stem')}", "info",
                       before.get("stem") or "", reload=True)
            self._recording_ended()

    # ---- a recording must not be broken by processing
    def _recording_started(self) -> None:
        with self.lock:
            # only GPU work can break the recorded sound; minutes in the cloud go on (2026-10-14)
            working = bool(self.running and not self.running.get("light"))
            mode = self.cfg.transcribe.when_recording
            pages = bool(self.subscribers)
        if mode == "continue":
            if working:
                self.event("zpracování běží dál i během nahrávání (nastavení: nechat běžet)", "info")
            return
        if not working:  # nothing to stop, but what is asked for during the recording waits for its end
            with self.lock:
                self.held = True
            self._push_jobs()
            return
        if mode == "ask" and pages:
            with self.lock:
                self.asking = True
            self._push_jobs()

            def timeout():
                time.sleep(RECORDING_ASK_S)
                if self.asking and self.capture.get("recording"):
                    self.pause_for_recording("bez odpovědi do %d s" % RECORDING_ASK_S)
            threading.Thread(target=timeout, daemon=True).start()
            return
        self.pause_for_recording("nastavení: hned přerušit" if mode == "stop" else "žádná stránka není otevřená")

    def pause_for_recording(self, why: str = "", everything: bool = False) -> None:
        """Stop the running GPU-heavy job (it goes back to the front of the queue) and let no heavy job start until the
        recording ends; light ones (minutes in the cloud) go on. everything: the light ones stop and wait too.
        A job that runs in this process (tests) cannot be stopped: it finishes, the queue waits."""
        with self.lock:
            self.held, self.hold_all, self.asking = True, everything, False
            job = self.running
            if job and (everything or not job.get("light")) and job.get("proc"):
                job["stopped"] = True
            else:
                job = None
        if job and job.get("proc"):
            _kill_tree(job["proc"])
        # Ollama keeps the summary model in VRAM for minutes after the last request (2026-10-02: 21 GB held
        # through a call); let the GPU go to the meeting as well
        threading.Thread(target=_unload_ollama, args=(self.cfg,), daemon=True).start()
        self.event(("veškeré zpracování přerušeno" if everything else "náročné zpracování přerušeno")
                   + " kvůli nahrávání" + (f" ({why})" if why else "") + ", doběhne samo po jeho konci"
                   + ("" if everything else "; zápisy přes cloud běží dál"), "info")
        self._push_jobs()

    def move_job(self, job_id: int, now: bool = False) -> str:
        """Move a waiting job to the front of the queue. now: also stop the running job; it goes back right behind
        the moved one and starts again from the beginning (what it had done so far is lost). Returns what happened,
        for the page."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if job is None:
                raise ValueError(f"job {job_id} is not waiting (it runs, finished, or never was)")
            self.jobs.remove(job)
            self.jobs.insert(0, job)
            run = self.running
            stoppable = bool(now and run and run.get("proc") and not run.get("stopped"))
            if stoppable:
                run["stopped"] = "preempted"
            current = run["stem"] if run else ""
        if stoppable:
            _kill_tree(run["proc"])
            text = f"{job['stem'] or job['name']}: zpracuje se hned, {current} se přerušilo a pokračuje po něm od začátku"
        elif now and run:
            text = f"{job['stem'] or job['name']}: zpracuje se hned po {current} (to se zastavit nedá)"
        else:
            text = f"{job['stem'] or job['name']}: zpracuje se jako další"
        self.event(text, "info", job["stem"], job=job["id"])
        self._push_jobs()
        return text

    def cancel_job(self, job_id: int) -> str:
        """Drop a waiting job, or stop the running one for good (what it did so far stays as it is, half done; the
        recording can be processed again). Returns what happened, for the page."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if job is not None:
                self.jobs.remove(job)
            run = self.running if self.running and self.running["id"] == job_id else None
            if job is None and run is None:
                raise ValueError(f"job {job_id} is neither waiting nor running")
            if run is not None:
                if not run.get("proc"):
                    raise ValueError("this job cannot be stopped, it finishes by itself")
                run["stopped"] = "cancelled"
        if run is not None:
            _kill_tree(run["proc"])  # the worker notes the end and goes on with the queue
            self._push_jobs()
            return f"{run['stem'] or run['name']}: zpracování se ruší"
        text = f"{job['stem'] or job['name']}: zpracování zrušeno, vyřazeno z fronty"
        self.event(text, "info", job["stem"], reload=True, job_end=True, job=job["id"])
        self._push_jobs()
        return text

    def keep_running(self) -> None:
        with self.lock:
            self.asking = False
        self.event("zpracování běží dál i během nahrávání (rozhodnuto na stránce)", "info")
        self._push_jobs()

    def _recording_ended(self) -> None:
        with self.lock:
            was_held, self.held, self.hold_all, self.asking = self.held, False, False, False
            waiting = len(self.jobs)
            self.wake.notify_all()
        if was_held and waiting:
            self.event(f"nahrávání skončilo, zpracování pokračuje ({waiting} ve frontě)", "info")
        self._push_jobs()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def events_since(self, n: int | None, last: int = 40) -> list[dict]:
        """What a (re)connecting page missed: everything after event n, or the last few on a first connect."""
        with self.lock:
            return [e for e in self.events if e["n"] > n] if n is not None else self.events[-last:]

    def _job_errors(self, stem: str) -> logging.Handler:
        """A log handler for the time of a job: what the pipeline logs as an error without failing the job (a
        comparison summary with an unknown model, a missing key) goes to the page's history, not only to the
        console window nobody looks at."""
        state = self

        class ToEvents(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    state.event(record.getMessage(), "err", stem)
                except Exception:  # never let the log break the job
                    pass

        h = ToEvents(logging.ERROR)
        logging.getLogger("recordingz").addHandler(h)
        return h

    def jobs_state(self) -> dict:
        """What runs and what waits, for every page (SSE `jobs`, hello, /api/status)."""
        with self.lock:
            return {"current": dict(self.current) if self.current else None,
                    "queue": [{**{k: j[k] for k in ("id", "name", "stem", "text")}, "force": j.get("force", False),
                               "plan": j.get("plan", {}), "light": bool(j.get("light"))}
                              for j in self.jobs],
                    "held": self.held, "hold_all": self.hold_all, "asking": self.asking, "ask_s": RECORDING_ASK_S}

    def jobs_file_json(self, running: bool = True) -> dict:
        """For the capture app (JOBS_STATUS): busy, what runs, how many wait, and the jobs that ended."""
        with self.lock:
            cur = self.current
            return {"app": "recordingz-review", "pid": os.getpid(), "running": running,
                    "busy": running and bool(self.busy), "held": self.held,
                    "current": ({"stem": cur["stem"], "title": _title_of(self.cfg, cur["stem"]), "text": cur["text"],
                                 "since": cur.get("started_at")} if cur and running else None),
                    "queued": len(self.jobs) if running else 0, "finished": list(self.finished),
                    "updated": datetime.now().isoformat(timespec="seconds")}

    def write_jobs_file(self, running: bool = True) -> None:
        if not self.jobs_file:
            return
        with self.jobs_file_lock:  # one writer at a time; a reader (the tray) may hold it for a moment: retried
            try:
                atomic_write_text(self.jobs_file, json.dumps(self.jobs_file_json(running), ensure_ascii=False))
            except OSError as e:
                log.warning("jobs status not written: %s", e)

    def _finished(self, job: dict, ok: bool, text: str) -> None:
        """A job ended: remembered for the tray's balloon, with what came out of it."""
        stem = job.get("stem") or ""
        if ok and job.get("name") == "process" and stem:
            try:
                rec = find_recording(stem, self.cfg.out_dir)
                text = ("přepis i zápis hotové" if has_minutes(rec)
                        else "přepis hotový; zápis počká, až pojmenujete mluvčí")
            except Exception:
                pass
        with self.lock:
            self.finished = (self.finished + [{"id": job["id"], "name": job.get("name", ""), "stem": stem,
                                               "title": _title_of(self.cfg, stem), "ok": ok, "text": text,
                                               "at": datetime.now().isoformat(timespec="seconds")}])[-FINISHED_KEPT:]

    def _push_jobs(self) -> None:
        self.write_jobs_file()
        state = self.jobs_state()
        with self.lock:
            subscribers = list(self.subscribers)
        for q in subscribers:
            q.put({"type": "jobs", **state})

    def run_job(self, name: str, fn, done: str, stem: str = "", start: str = "",
                light: bool = False) -> tuple[int, int]:
        """Queue fn() as a background job. One runs at a time (the GPU does one thing), in order; a request while
        another runs waits instead of being refused. Returns (job id, position: 1 = runs now, 2 = next ...).
        light: no GPU work (minutes in the cloud) – it may run during a recording.
        Its start and end are events carrying the id (the end with job_end), and every change of what runs or
        waits goes to the pages as SSE `jobs`."""
        with self.lock:
            self.next_job += 1
            job = {"id": self.next_job, "name": name, "stem": stem, "text": start or name, "fn": fn, "done": done,
                   "force": isinstance(fn, list) and "--force" in fn, "light": light,
                   # what the job will make, for the page to say it up front
                   "plan": ({"cloud": "--cloud" in fn or _after(fn, "--transcribe-with") == "cloud",
                             "voices": "--voices" in fn,
                             "minutes_wait": "--minutes-when-named" in fn,
                             "minutes": "--no-minutes" not in fn,
                             "only_with": _after(fn, "--only-with") or ""} if isinstance(fn, list) else {})}
            if isinstance(fn, list):  # a command: run in a child process that can be stopped
                job["argv"], job["fn"] = fn, None
            self.jobs.append(job)
            position = len(self.jobs) + (1 if self.current else 0)
            starts_worker = not self.working
            self.busy = self.working = True
            held = self.held and (self.hold_all or not light)
            self.wake.notify_all()  # a worker waiting for a recording's end may run a light job now
        if held:
            self.event(f"{job['text']} – čeká na konec nahrávání" + (f" (před ní {position - 1})" if position > 1 else ""),
                       "info", stem, job=job["id"])
        elif not starts_worker:
            self.event(f"{job['text']} – ve frontě (před ní {position - 1})", "info", stem, job=job["id"])
        self._push_jobs()
        if starts_worker:
            threading.Thread(target=self._work, daemon=True).start()
        return job["id"], position

    def _runnable(self) -> dict | None:
        """The job to run next (under the lock): the first one; during a recording the first light one, or none."""
        if not self.held:
            return self.jobs[0] if self.jobs else None
        return None if self.hold_all else next((j for j in self.jobs if j.get("light")), None)

    def _work(self) -> None:
        """The one worker: runs the queued jobs one after another until the queue is empty."""
        while True:
            with self.lock:
                while self.jobs and self._runnable() is None:  # a recording runs: wait for its end
                    self.wake.wait(timeout=5)
                if not self.jobs:
                    self.busy, self.current, self.working = False, None, False
                    break
                job = self._runnable()
                self.jobs.remove(job)
                self.running = job
                self.current = {"id": job["id"], "name": job["name"], "stem": job["stem"], "text": job["text"],
                                "force": job.get("force", False), "plan": job.get("plan", {}), "light": bool(job.get("light")),
                                "started": datetime.now().strftime("%H:%M"),
                                "started_at": datetime.now().isoformat(timespec="seconds")}
                self.message, self.error, self.job = job["name"], "", job["name"]
            # logged before the work, so the order in the log is the real one
            self.event(job["text"], "busy", job["stem"], job=job["id"])
            self._push_jobs()
            stem, done, name = job["stem"], job["done"], job["name"]
            outcome = (f"{done}: {stem}" if stem else done, "ok")
            errors = self._job_errors(stem)
            try:
                if job.get("argv"):
                    self._run_command(job)
                else:
                    job["fn"]()
                with self.lock:
                    self.message = done
                if job.get("errors"):  # the job ended, but a part of it failed (typically the local minutes)
                    first = job["errors"][0].split(": ", 1)[-1]
                    outcome = (f"{done}: {stem}, ale část se nepodařila – {first}", "err")
            except Exception as e:  # shown on the page, not fatal
                with self.lock:
                    self.error = str(e)
                outcome = (f"{name} selhalo: {e}", "err")
            finally:
                logging.getLogger("recordingz").removeHandler(errors)
                if name == "pull":
                    settings._ollama_seen.clear()  # the model may be there now: ask Ollama again
                with self.lock:
                    self.current = self.running = None
                    stopped = job.pop("stopped", False)
                    job.pop("proc", None)
                    if stopped == "cancelled":  # the user dropped it: not back into the queue
                        pass
                    elif stopped == "preempted":  # another job goes first, this one right behind it
                        self.jobs.insert(1 if self.jobs else 0, job)
                    elif stopped:  # stopped for a recording: back to the front, it runs again after it
                        self.jobs.insert(0, job)
                    if not self.jobs:
                        self.busy = False  # before the end event: the page sees it idle
            if stopped == "cancelled":
                self.event(f"{stem or name}: zpracování zrušeno (zastaveno během běhu)", "info", stem, reload=True,
                           job_end=True, job=job["id"])
            elif stopped == "preempted":
                self.event(f"{job['text']} – přerušeno kvůli přednostnímu zpracování, poběží znovu po něm", "info",
                           stem, job=job["id"])
            elif stopped:
                self.event(f"{job['text']} – přerušeno, pokračuje po konci nahrávání", "info", stem, job=job["id"])
            else:
                self._finished(job, outcome[1] == "ok", outcome[0])
                self.event(outcome[0], outcome[1], stem, reload=True, job_end=True, job=job["id"])
            self._push_jobs()

    def _run_command(self, job: dict) -> None:
        """A job's command in a child process; what it logs as an error goes to the history."""
        args = list(job["argv"])
        if self.cfg.source_path or self.settings_path.exists():
            args = ["--config", str(self.cfg.source_path or self.settings_path)] + args
        args = ["--out-dir", str(self.cfg.out_dir)] + args

        def on_proc(proc):
            with self.lock:
                job["proc"] = proc
                stop_now = self.held and (self.hold_all or not job.get("light"))  # a recording started while it was being launched
            if stop_now:
                job["stopped"] = True
                _kill_tree(proc)

        def on_line(line: str):
            if line.startswith("PROGRESS "):  # a long job says how far it is (a model download)
                with self.lock:
                    job["text"] = line[len("PROGRESS "):].strip()
                    if self.current and self.current.get("id") == job["id"]:
                        self.current["text"] = job["text"]
                self._push_jobs()
                return
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1] == "ERROR":
                self.event(parts[2], "err", job["stem"])
                job.setdefault("errors", []).append(parts[2])
        try:
            run_cli(args, on_proc, on_line)
        except RuntimeError:
            if job.get("stopped"):
                return  # stopped for a recording, not a failure
            raise

    def run_summary(self, rec: Recording) -> tuple[int, int]:
        # in a child process (it can be stopped for a recording); the compare summaries follow the new names too
        s = self.cfg.summarize
        compare = s.compare if s.compare_auto else ()
        light = s.provider != "ollama" and not any(c.split(":", 1)[0] == "ollama" for c in compare)
        return self.run_job("summary", ["run-job", "summary", rec.stem], "zápis přegenerován", rec.stem,
                            start=f"{rec.stem}: zápis se generuje"
                     + (f" (i srovnávací: {', '.join(compare)})" if compare else ""),
                            light=light)

    def run_reprint(self, pid: str, stem: str = "", label: str = "") -> tuple[int, int]:
        """Recalculate the person's prints (one: stem + label) from the groups' current replies (GPU)."""
        if not re.fullmatch(r"[a-z0-9-]{1,80}", pid):
            raise ValueError(f"not a person id: {pid!r}")
        argv = ["run-job", "reprint", pid] + (["--model", f"{stem}::{label}"] if stem and label else [])
        return self.run_job("reprint", argv, f"vzorky přepočítány ({pid})", "",
                            start=f"přepočítávají se vzorky ({pid})")

    def run_replies(self, pid: str) -> tuple[int, int]:
        """Every reply's own voice for the person's recordings (replies.py): GPU work the user asked for."""
        if not re.fullmatch(r"[a-z0-9-]{1,80}", pid):
            raise ValueError(f"not a person id: {pid!r}")
        return self.run_job("replies", ["run-job", "replies", pid], f"repliky vyhodnoceny ({pid})", "",
                            start=f"vyhodnocují se repliky ({pid})")

    def run_pull(self, model: str) -> tuple[int, int]:
        """Download the local minutes model into Ollama (a queued job: it can be cancelled, its progress shows)."""
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model):
            raise ValueError(f"not a model name: {model!r}")
        return self.run_job("pull", ["run-job", "pull", model], f"model {model} stažen", "",
                            start=f"stahuje se {model}")

    def run_voices(self, rec: Recording) -> tuple[int, int]:
        """Fast-track post-processing: local voices for a cloud transcript (fasttrack.add_voices), on the GPU."""
        return self.run_job("voices", ["run-job", "voices", rec.stem], "hlasy doplněny", rec.stem,
                            start=f"{rec.stem}: doplňují se hlasy (lokální diarizace)")

    def run_process(self, rec: Recording, force: bool = False, cloud: bool = False,
                    minutes_anyway: bool = False, voices: bool = False,
                    minutes: bool | None = None, compare: bool | None = None, only_with: str = "",
                    transcribe: str = "", minutes_with: str = "") -> tuple[int, int]:
        """force = transcribe again from scratch (new diarization, new labels), so the manual names go first:
        they are keyed by labels that will not exist any more, and a wrong one must not come back."""
        # from scratch drops the manual names and the cached window analysis when the job starts (run-job), so a
        # recording waiting in the queue keeps everything and can be cancelled without loss
        if cloud and not cloud_ready(self.cfg)["ok"]:
            raise ValueError("rychle přes cloud potřebuje klíče: " + cloud_ready(self.cfg)["missing"])
        # the page's choice per step: transcribe / minutes_with = local | cloud ("" = as set)
        transcribe = transcribe if transcribe in ("local", "cloud") else ""
        minutes_with = minutes_with if minutes_with in ("local", "cloud") else ""
        offer = choices(self.cfg)
        for step, where in (("transcribe", transcribe), ("minutes", minutes_with if minutes is not False else "")):
            if where and offer[step][where]["missing"]:
                raise ValueError(offer[step][where]["missing"])
        voices = voices and (cloud or transcribe == "cloud")
        what = (("nový přepis od nuly" if force else "zpracování")
                + ((" (cloud + hlasy lokálně)" if voices else " (cloud)") if cloud else ""))
        # the minutes wait for named speakers unless asked otherwise (local and fast track alike); the fast track
        # names nobody but the user, unless its local voices step (voices) recognises them
        return self.run_job("process", ["run-job", "process", rec.stem] + (["--force"] if force else [])
                            + (["--cloud"] if cloud else []) + (["--voices"] if voices else [])
                            + ([] if minutes_anyway else ["--minutes-when-named"])
                            + ([] if minutes is None else ["--minutes" if minutes else "--no-minutes"])
                            # "srovnávací zápis" as ticked (none: [summarize] compare_auto); the fast track never compares
                            + ([] if compare is None or cloud else ["--compare" if compare else "--no-compare"])
                            + (["--only-with", only_with.strip()] if only_with.strip() else [])
                            + (["--transcribe-with", transcribe] if transcribe else [])
                            + (["--minutes-with", minutes_with] if minutes_with else []),
                            "zpracováno", rec.stem, start=f"{rec.stem}: {what} spuštěno")

    def job_of(self, stem: str) -> str:
        """"" when no job of this recording runs or waits, else what it does (a meeting with a job is not deleted)."""
        with self.lock:
            if self.running and self.running.get("stem") == stem:
                return "právě se zpracovává"
            return "čeká ve frontě zpracování" if any(j.get("stem") == stem for j in self.jobs) else ""

    def status(self, events: int = 40) -> dict:
        with self.lock:
            return {"app": "recordingz-review", "out_dir": str(self.cfg.out_dir), "job": getattr(self, "job", ""),
                    "busy": self.busy, "message": self.message, "error": self.error,
                    "seq": self.seq, "events": self.events[-events:], "capture": self.capture,
                    "jobs": self.jobs_state()}


def _unload_ollama(cfg: Config) -> None:
    """Ask Ollama to drop the configured summary model from memory now (keep_alive 0). Quiet when Ollama is not
    used or not running."""
    sm = cfg.summarize
    if sm.provider != "ollama" and not any(c.startswith("ollama:") for c in sm.compare):
        return
    body = json.dumps({"model": sm.model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(sm.ollama_url.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log.info("ollama: %s unloaded for the recording", sm.model)
    except Exception as e:  # not running, another model: nothing to free
        log.debug("ollama unload: %s", e)


def _capture_watch(state: "ReviewState", path: Path = CAPTURE_STATUS, poll_s: float = CAPTURE_POLL_S,
                   stop: threading.Event | None = None) -> None:
    """Poll the capture app's status file and push changes (a file, not a socket: the capture app needs no server)."""
    last = None
    while not (stop and stop.is_set()):
        now = capture_status(path)
        if now != last:
            state.set_capture(now)
            last = now
        time.sleep(poll_s)
