"""What the review page shows and saves for a recording: its speakers, documents, clips, names and title.

Pure data functions on the recordings folder; the HTTP handler in review.py calls them."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import wave
import webbrowser
from datetime import datetime
from pathlib import Path

from .. import settings
from ..config import Config
from ..people import DISPLAY_MODES, People, Person
from ..pipeline import (mark_edited, minutes_stale, summary_stale, do_export, enroll_names, call_partner,
                        load_segments, meeting_info, rename_recording, same_person_groups)
from ..voiceprints import Voiceprints, _unit, cosine, speech_seconds
from .. import exports, timings, trash
from ..fasttrack import choices, cloud_ready, needs_voices, plans  # the emergency fast track, separate from the local path
from ..providers.base import Segment
from ..fileio import registry_locked
from ..recording import Recording, RecordingError, iter_recordings

log = logging.getLogger(__name__)

CLIP_MAX_S = 8.0
SAMPLE_MIN_S = 2.0
SAMPLES_PER_SPEAKER = 3
EXCERPTS_PER_SPEAKER = 2
RECENT_RECORDINGS = 30


def is_label(name: str | None) -> bool:
    return bool(name) and (name.startswith("SPEAKER_") or name == "UNKNOWN")


def unnamed_speaker(name: str | None) -> bool:
    """A diarization label without a name. Replies the diarization gave nobody ("UNKNOWN") are not a speaker to
    name: they are given out one by one, and do not keep a recording unfinished."""
    return bool(name) and name.startswith("SPEAKER_")




# ---------------------------------------------------------------- data for the page

def pick_samples(segs: list[Segment], n: int = SAMPLES_PER_SPEAKER) -> list[Segment]:
    """Up to n segments spread over the recording, the longest first, each at least SAMPLE_MIN_S long."""
    good = [s for s in segs if s.end - s.start >= SAMPLE_MIN_S] or list(segs)
    if not good:
        return []
    longest = max(good, key=lambda s: s.end - s.start)
    picks = [longest]
    if len(good) > 1 and n > 1:
        span = good[-1].start - good[0].start or 1.0
        fracs = (0.15, 0.85) if n <= 3 else tuple(0.1 + 0.8 * i / (n - 2) for i in range(n - 1))  # spread evenly
        for frac in fracs[: n - 1]:
            target = good[0].start + frac * span
            cand = min((s for s in good if s not in picks), key=lambda s: abs(s.start - target), default=None)
            if cand is not None:
                picks.append(cand)
    picks.sort(key=lambda s: s.start)
    return picks


def summary_hint_names(rec: Recording) -> dict[str, str]:
    """The names the minutes' Speakers table guessed for still-unnamed labels ({label: name}), for a one-click
    "použít toto jméno" next to the note."""
    if not rec.summary_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in rec.summary_path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and is_label(cells[0]) and cells[1] not in ("?", "", "–", "-") and len(cells[1]) <= 60:
            out[cells[0]] = cells[1].strip("*_ ")
    return out


def summary_hints(rec: Recording) -> dict[str, str]:
    """Notes from the Speakers table at the end of the summary: '| SPEAKER_00 | ? | led the meeting |'."""
    if not rec.summary_path.exists():
        return {}
    hints: dict[str, str] = {}
    for line in rec.summary_path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and is_label(cells[0]):
            note = cells[2]
            if cells[1] not in ("?", "", "–", "-"):
                note = f"{cells[1]}: {note}" if note else cells[1]
            hints[cells[0]] = note
    return hints


def known_names(cfg: Config, people: People) -> list[str]:
    """Names to suggest: registered people (full name), the user, participants of any recording."""
    seen: dict[str, None] = {}
    for p in people.people:
        seen[p.full] = None
    if cfg.user_name:
        seen[cfg.user_name] = None
    for r in iter_recordings(cfg.out_dir):
        for p in r.participants:
            seen[p] = None
    return sorted(seen, key=str.casefold)


def _person_info(people: People, value: str) -> dict | None:
    p = people.get(value) or people.find(value)
    if not p:
        return None
    return {"id": p.id, "first": p.first, "last": p.last, "nick": p.nick, "display": p.display,
            "full": p.full, "shown": p.name(people.default_mode)}


VOICE_HINT_MIN = 0.40  # below this a "closest print" is noise, not a hint


def _voice_hint(vp: Voiceprints, people: People, vec, unresolved: bool, secs: float, threshold: float,
                exclude_stem: str | None = None) -> dict | None:
    """The closest voice print for a still-unknown label that did not pass the automatic threshold, so the
    user can confirm it with one click instead of guessing."""
    if not (unresolved and vec and vp.people):
        return None
    ranked = vp.scores(vec, exclude_stem)
    if not ranked or ranked[0][1] < VOICE_HINT_MIN:
        return None
    pid, score = ranked[0]
    p = people.get(pid)
    if p is None:
        return None
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    why = "krátká promluva" if secs < 30 else ("malý odstup od dalšího" if score - second < 0.10 else "pod prahem")
    return {"person": pid, "name": p.full, "score": round(score, 2), "fields": {"first": p.first, "last": p.last,
            "nick": p.nick, "display": p.display}, "why": why, "threshold": threshold}


# Two voice groups of one meeting are worth a look from this similarity on. Calibrated 2026-10-05 on 25 meetings
# (group embeddings of the diarization): the same person split in two 0.25-0.71 (median 0.45), different people
# 0.07-0.54 (median 0.25) – so this is a hint to listen, never an automatic merge.
GROUP_HINT_MIN = 0.45


def _nearest_group(label: str, embeddings: dict, secs: dict) -> dict | None:
    """The voice group of this meeting that sounds most like `label`, when it is close enough to suggest."""
    vec = embeddings.get(label)
    if not vec:
        return None
    u = _unit(vec)
    if not u:
        return None
    best = max(((other, cosine(u, w)) for other, v in embeddings.items() if other != label and v and
                secs.get(other, 0.0) > 0 and (w := _unit(v))), key=lambda kv: kv[1], default=None)
    if best is None or best[1] < GROUP_HINT_MIN:
        return None
    return {"label": best[0], "score": round(best[1], 2)}


def _fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def speaker_languages(segs: list[Segment], default: str | None) -> dict[str, dict]:
    """Per label: the language most of its speech was transcribed in, and whether that was a second pass in
    another language than the recording's (per-speaker language)."""
    spoken: dict[str, dict[str, float]] = {}
    for s in segs:
        lang = s.language or default or "?"
        by = spoken.setdefault(s.speaker or "UNKNOWN", {})
        by[lang] = by.get(lang, 0.0) + max(s.end - s.start, 0.0)
    out = {}
    for label, by in spoken.items():
        main = max(by, key=by.get)
        out[label] = {"language": main, "own": bool(default) and main not in (default, "?"),
                      "mixed": len([v for v in by.values() if v >= 5]) > 1}
    return out


def call_offer(cfg: Config, rec: Recording) -> dict:
    """"na hovoru jsem jen já a …" next to the processing buttons: offered when the microphone track can tell the user
    apart (a live call, [user] name set); the other side prefilled from the call window or the only participant."""
    mic = rec.track_path("mic")
    ok = bool(cfg.user_name) and rec.source != "onsite" and bool(mic and mic.exists())
    names = sorted({p.full for p in People.load(cfg.out_dir, cfg.people_display).people if p.full}) if ok else []
    return {"call_with": call_partner(cfg, rec) if ok else "", "call_offer": ok, "user_name": cfg.user_name,
            "people_names": names}  # the other side: suggestions while typing


def build_review(cfg: Config, rec: Recording) -> dict:
    if not rec.transcript_path.exists():  # the page offers to process it
        return {"stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"), "cloud": cloud_ready(cfg),
                "duration_s": rec.sidecar.get("duration_s"), "source": rec.source, "transcribed": False,
                "has_mix": bool(rec.mix_path and rec.mix_path.exists()) or bool(rec.track_path("sys")),
                "speakers": [], "known_names": [], "people": [], "display_default": cfg.people_display,
                "language": None, "speaker_sources": [], "has_summary": False,
                "minutes_auto": cfg.summarize.enabled, "plans": plans(cfg), "choices": choices(cfg),
                **call_offer(cfg, rec)}
    data, segs = load_segments(rec)  # raw provider speakers, without speakers.json applied
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    people = People.load(cfg.out_dir, cfg.people_display)
    hints = summary_hints(rec)
    hint_names = summary_hint_names(rec)
    voice = data.get("voice_matches") or {}
    call = data.get("direct_call") or {}
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    embeddings = data.get("speaker_embeddings") or {}
    group_secs = speech_seconds(data.get("segments") or [])  # the groups that still have replies
    by: dict[str, list[Segment]] = {}
    for s in segs:
        by.setdefault(s.speaker or "UNKNOWN", []).append(s)
    langs = speaker_languages(segs, data.get("language"))
    speakers = []
    for label, ss in sorted(by.items(), key=lambda kv: -sum(s.end - s.start for s in kv[1])):
        secs = sum(s.end - s.start for s in ss)
        excerpts = sorted(ss, key=lambda s: -len(s.text))[:EXCERPTS_PER_SPEAKER]
        value = manual.get(label, "" if is_label(label) else label)
        person = _person_info(people, value) if value else None
        if person:
            fields = {k: person[k] for k in ("first", "last", "nick", "display")}
        elif value:  # a literal name (video OCR, mic config) that is not registered yet
            parts = value.split()
            fields = {"first": parts[0], "last": " ".join(parts[1:]), "nick": "", "display": ""}
        else:
            fields = {"first": "", "last": "", "nick": "", "display": ""}
        speakers.append({
            "label": label,
            "unresolved": is_label(label),
            "name": person["full"] if person else value,
            "fields": fields,  # what the inputs show
            "person": person,  # None for literal names (video / mic) that are not registered
            "voice": ({"person": voice[label]["person"], "score": voice[label]["score"],
                       "name": (people.get(voice[label]["person"]) or Person(voice[label]["person"])).full}
                      if label in voice else None),
            "voice_hint": _voice_hint(vp, people, embeddings.get(label), is_label(label) and not value, secs,
                                      cfg.voiceprints.threshold, rec.stem),
            "nearest": _nearest_group(label, embeddings, group_secs),
            "confirmed": (bool(value) and bool(manual.get(label)) and label not in voice
                          and not (call.get("label") == label and call.get("applied"))),
            # a direct call: the window named the other person (applied, or asked when the voice says someone else)
            "call": ({**call, "voice_name": (people.get(call["voice"]["person"]) or Person(call["voice"]["person"])).full}
                     if call.get("label") == label and call.get("voice") else
                     (call if call.get("label") == label else None)),
            "seconds": round(secs, 1),
            "count": len(ss),
            "unassigned": label == "UNKNOWN",
            # unassigned replies are given out one by one: all of them, not samples
            "replies": ([{"start": round(s.start, 3), "end": round(min(s.end, s.start + CLIP_MAX_S), 3),
                          "at": _fmt_hms(s.start), "text": s.text[:300]} for s in ss[:300]]
                        if label == "UNKNOWN" else []),
            **langs.get(label, {"language": data.get("language"), "own": False, "mixed": False}),
            "hint": hints.get(label, ""),
            "hint_name": hint_names.get(label, "") if is_label(label) and not value else "",
            "samples": [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                         "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(ss)],
            "excerpts": [{"at": _fmt_hms(s.start), "text": s.text[:200]} for s in
                         sorted(excerpts, key=lambda s: s.start)],
        })
    return {
        "stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"),
        "duration_s": rec.sidecar.get("duration_s"), "source": rec.source, "transcribed": True,
        "language": data.get("language"), "speaker_sources": data.get("speaker_sources", []),
        "transcribed_with": {"provider": data.get("provider"), "model": data.get("model"),
                             "languages": data.get("languages") or [],
                             # no word alignment: replies stay ~30 s long and can mix voices (2026-10-05)
                             "unaligned": is_unaligned(data)},
        "edited": minutes_stale(rec, data),
        "has_summary": has_minutes(rec), "has_mix": bool(rec.mix_path and rec.mix_path.exists()),
        "minutes_auto": cfg.summarize.enabled,  # "vytvořit zápis" ticked at first when processing starts
        "cloud": cloud_ready(cfg), "plans": plans(cfg),
        "choices": choices(cfg),  # "Přepsat znovu od nuly": each step on this PC or in the cloud
        **call_offer(cfg, rec),
        "timings": timings.latest(rec),  # how long each part of the last jobs took
        "export_folder": exports.folder_for(cfg.out_dir, rec.title),
        "minutes_problem": minutes_problem(cfg),  # the local minutes model is missing / Ollama is not running
        "minutes_pull": (cfg.summarize.model if settings.ollama_has(cfg.summarize.ollama_url, cfg.summarize.model) is False
                         and cfg.summarize.provider == "ollama" and cfg.summarize.enabled else ""),  # "Uložit jako minule" for this meeting name
        "voices_missing": needs_voices(data),  # a fast-track transcript: voices can be added locally
        "voices_mixed": sorted(((data.get("voices_from") or {}).get("mixed") or {}).keys()),
        "speakers": speakers, "known_names": known_names(cfg, people),
        "same_person": [{"person": pid, "labels": labels, "name": (people.get(pid) or Person(pid)).full}
                        for pid, labels in same_person_groups(rec, people).items()],
        "can_unmerge": any(s.get("merged_from") for s in (data.get("segments") or [])),
        "people": people.to_json(), "display_default": people.default_mode,
        "meeting": meeting_info(cfg, rec),
        "docs": recording_docs(rec),
        "dir": str(rec.dir),  # for "Kopírovat cestu" of a document
        "md_viewer": viewer_name(cfg.md_viewer),
    }


SUMMARY_STAMP = re.compile(r"<!--\s*recordingz summary \| (?P<provider>[^:|]+):\s*(?P<model>[^|]+?)\s*(?:\|[^>]*?created:\s*(?P<created>[^|>]+?))?\s*(?:\||-->)")


def minutes_problem(cfg: Config) -> str:
    """Why the local minutes would fail now ("" = fine or not local): Ollama not running, or without the model
    (2026-10-06: the model folder was moved in Ollama's settings and every local minutes failed in 2 s)."""
    sm = cfg.summarize
    if not sm.enabled or sm.provider != "ollama":
        return ""
    has = settings.ollama_has(sm.ollama_url, sm.model)
    if has is None:
        return f"Ollama neběží – lokální zápis ({sm.model}) teď nevznikne; spusťte Ollamu."
    if not has:
        return (f"Ollama nemá model {sm.model} – lokální zápis teď nevznikne. Stáhněte ho (ollama pull {sm.model}) "
                f"nebo zkontrolujte složku modelů v nastavení Ollamy.")
    return ""


def is_unaligned(data: dict) -> bool:
    """A local transcript without word alignment: replies ~30 s long that can mix voices (2026-10-05) – worth
    transcribing again. Cloud transcripts bring their own word times."""
    return data.get("provider") == "whisperx" and "align_s" not in (data.get("timings") or {})


def recording_speakers(cfg: Config, rec: Recording) -> list[dict]:
    """The recording's speakers with the names they show ({label, name}), for moving replies from outside the
    meeting's page (Lidé → a sample → Projít repliky)."""
    if not rec.transcript_path.exists():
        return []
    data = rec.read_json(rec.transcript_path)
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    people = People.load(cfg.out_dir, cfg.people_display)
    out = []
    for label in data.get("speakers") or []:
        if label == "UNKNOWN":
            continue
        value = manual.get(label, "" if is_label(label) else label)
        person = people.get(value) or (people.find(value) if value else None)
        out.append({"label": label, "name": person.full if person else (value or label)})
    return out


def has_minutes(rec: Recording) -> bool:
    """Any minutes at all: the main one or one by another model (the list must not say "bez zápisu" when the only
    minutes are a Claude one)."""
    return rec.summary_path.exists() or any(rec.dir.glob(f"{rec.stem}.summary.*.md"))


def summary_info(path: Path, main: bool, edited: str | None = None) -> dict:
    """A summary file for the page: which provider/model wrote it (from its first-line stamp) and when."""
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        first = ""
    m = SUMMARY_STAMP.search(first)
    provider, model = (m.group("provider").strip(), m.group("model").strip()) if m else ("", "")
    fallback = "hlavní" if main else path.name.split(".summary.", 1)[-1][:-3]
    return {"file": path.name, "label": model or fallback, "provider": provider, "model": model,
            "created": (m.group("created") or "").strip() if m else "", "main": main,
            "stale": summary_stale(edited, path)}


def recording_docs(rec: Recording) -> dict:
    """Readable documents of a recording for the Přepis / Zápis tabs; minutes older than the last edit are stale."""
    try:
        edited = rec.read_json(rec.transcript_path).get("edited_at") if rec.transcript_path.exists() else None
    except Exception:
        edited = None
    summaries = []
    if rec.summary_path.exists():
        summaries.append(summary_info(rec.summary_path, True, edited))
    for p in sorted(rec.dir.glob(f"{rec.stem}.summary.*.md")):
        summaries.append(summary_info(p, False, edited))
    # two tabs of the same model (the main minutes and a comparison by it): the main one says so, so a close does
    # not delete the wrong one
    for s in summaries:
        if s["main"] and any(o is not s and o["label"] == s["label"] for o in summaries):
            s["label"] = f"{s['label']} · hlavní"
    others = [{"file": p.name, "label": f"přepis ({p.name[len(rec.stem) + 1:-4]})"}
              for p in sorted(rec.dir.glob(f"{rec.stem}.*.txt"))]
    return {"transcript": rec.file(".txt").name if rec.file(".txt").exists() else None, "summaries": summaries,
            "transcripts": others}


def delete_summary(rec: Recording, name: str) -> None:
    """Delete one summary of the recording (the page's tab close). Only <stem>.summary*.md, nothing else."""
    if ("/" in name or "\\" in name or not name.startswith(rec.stem + ".summary") or not name.endswith(".md")):
        raise RecordingError("not a summary of this recording")
    path = rec.dir / name
    if not path.exists():
        raise RecordingError(f"{name} does not exist")
    path.unlink()


def doc_path(rec: Recording, name: str) -> Path:
    """Only the recording's own text documents (no path tricks)."""
    if "/" in name or "\\" in name or not name.startswith(rec.stem) or not (name.endswith(".md") or name.endswith(".txt")):
        raise RecordingError("not a document of this recording")
    path = rec.dir / name
    if not path.exists():
        raise RecordingError(f"{name} does not exist")
    return path


def read_doc(rec: Recording, name: str) -> str:
    return doc_path(rec, name).read_text(encoding="utf-8")


def viewer_name(viewer: str) -> str:
    """What the page calls the .md viewer: the configured program, else the Windows default for .md ("" = unknown)."""
    if viewer:
        return Path(viewer.strip().strip('"')).stem
    if os.name != "nt":
        return ""
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                                r"\FileExts\.md\UserChoice") as k:
                prog = winreg.QueryValueEx(k, "ProgId")[0]
        except OSError:
            prog = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, ".md")
        command = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, rf"{prog}\shell\open\command")
    except OSError:
        return ""
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', command)
    return Path((m.group(1) or m.group(2)) if m else "").stem


def _launch(argv: list[str] | None, path: Path) -> None:
    """Start the viewer on `path` (argv = the program and the file), or the default app for it (argv None)."""
    if argv:
        import subprocess
        subprocess.Popen(argv, close_fds=True)
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def _clip_file(path: Path) -> None:
    """Put the file on the clipboard as a file (like Ctrl+C in Explorer): paste it into a mail or a folder."""
    if os.name != "nt":
        raise RecordingError("copying a file to the clipboard works on Windows only")
    import subprocess
    script = ("Add-Type -AssemblyName System.Windows.Forms; $c = New-Object System.Collections.Specialized.StringCollection; "
              "[void]$c.Add($env:RECORDINGZ_CLIP_FILE); [System.Windows.Forms.Clipboard]::SetFileDropList($c)")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True,
                       env={**os.environ, "RECORDINGZ_CLIP_FILE": str(path)}, timeout=30, creationflags=0x08000000)
    if r.returncode != 0:
        log.warning("clipboard failed: %s", r.stderr.strip()[-300:])
        raise RecordingError("the file could not be put on the clipboard")


def open_doc(rec: Recording, name: str, viewer: str) -> str:
    """Open a minutes .md in the configured viewer (or the default app), a .txt in the default app; returns what
    opened it ("" = the default app, name unknown)."""
    path = doc_path(rec, name)
    if not name.endswith(".md"):
        _launch(None, path)
        return ""
    program = viewer.strip().strip('"')
    if program and not (Path(program).exists() or shutil.which(program)):
        raise RecordingError(f"the .md viewer {program} was not found (Nastavení → Obecné → Prohlížeč zápisů)")
    _launch([program, str(path)] if program else None, path)
    return viewer_name(viewer)


def copy_doc(rec: Recording, name: str) -> Path:
    """The document onto the clipboard as a file."""
    path = doc_path(rec, name)
    _clip_file(path)
    return path


HELP_DOCS = {"user-guide": "user-guide.md", "install": "install.md", "privacy": "privacy.md"}
DOCS_DIR = Path(__file__).resolve().parents[4] / "docs"  # the repository
if not DOCS_DIR.is_dir():
    DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"  # the installed package (installer/stage-suite.ps1)
DOCS_URL = "https://github.com/drzdez/recordingz/blob/main/docs/"


def help_doc(name: str) -> dict:
    """One of the guides for the page's help: the local file of this version, else its page on GitHub."""
    fname = HELP_DOCS.get(name)
    if not fname:
        raise RecordingError(f"unknown help document {name!r} (available: {', '.join(HELP_DOCS)})")
    path = DOCS_DIR / fname
    if path.exists():
        return {"doc": name, "text": path.read_text(encoding="utf-8")}
    return {"doc": name, "url": DOCS_URL + fname}


def list_recordings(cfg: Config, limit: int = RECENT_RECORDINGS) -> list[dict]:
    out = []
    recs = sorted(iter_recordings(cfg.out_dir), key=lambda r: r.stem, reverse=True)[:limit]
    for r in recs:
        unresolved = None
        unaligned = False
        edited = None
        if r.transcript_path.exists():
            try:
                names = r.read_json(r.speakers_path) if r.speakers_path.exists() else {}
                data = r.read_json(r.transcript_path)
                labels = data.get("speakers", [])
                unresolved = sum(1 for l in labels if unnamed_speaker(l) and not names.get(l))
                unaligned = is_unaligned(data)
                edited = minutes_stale(r, data)
            except Exception:
                unresolved = None
        out.append({"stem": r.stem, "title": r.title, "start": r.sidecar.get("start"),
                    "transcribed": r.transcript_path.exists(), "unresolved": unresolved,
                    "summary": has_minutes(r), "unaligned": unaligned,
                    "edited": edited})  # names or replies changed after the minutes: regenerate them
    return out


# ---------------------------------------------------------------- audio clips

def clip_wav(mix: Path, start: float, end: float) -> bytes:
    """A slice of the mix as a complete WAV file (in memory)."""
    with wave.open(str(mix), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        total = w.getnframes()
        a = max(0, min(int(start * sr), total))
        b = max(a, min(int(end * sr), total))
        w.setpos(a)
        frames = w.readframes(b - a)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(ch); o.setsampwidth(sw); o.setframerate(sr)
        o.writeframes(frames)
    return buf.getvalue()


# ---------------------------------------------------------------- saving

def _person_from_fields(people: People, fields: dict) -> "Person | None":
    """Find or create the person for {first, last, nick, display}; updates nick/display of a known person."""
    first, last, nick = (str(fields.get(k) or "").strip() for k in ("first", "last", "nick"))
    display = str(fields.get("display") or "")
    if not (first or last or nick):
        return None
    full = f"{first} {last}".strip()
    p = (people.find(full) if full else None) or (people.find(nick) if nick and not full else None)
    if p is None:
        p = Person.from_text(full or nick, {q.id for q in people.people})
        if not full:
            p.first = ""
        people.people.append(p)
    if nick:
        p.nick = nick
    if display in DISPLAY_MODES or display == "":
        p.display = display
    return p


def save_title(cfg: Config, rec: Recording, title: str) -> Recording:
    """New meeting title: folder, files, sidecar, voice prints and summary headings are renamed along
    (see pipeline.rename_recording). Returns the (possibly moved) recording."""
    title = " ".join(title.split())
    if not title or title == rec.title:
        return rec
    return rename_recording(cfg, rec, title)


@registry_locked
def save_names(cfg: Config, rec: Recording, names: dict) -> dict[str, str]:
    """Write speakers.json with person ids (people created/updated from the fields), regenerate txt/srt.
    Returns {label: person id} of what was written."""
    people = People.load(cfg.out_dir, cfg.people_display)
    snapshot = json.dumps(people.to_json(), sort_keys=True)
    clean: dict[str, str] = {}
    for label, name in names.items():
        if isinstance(name, dict):
            p = _person_from_fields(people, name)
            if p is not None:
                clean[label] = p.id
            continue
        name = (name or "").strip()
        if not name or name == label or is_label(name):
            continue
        clean[label] = people.ensure(name).id
    if json.dumps(people.to_json(), sort_keys=True) != snapshot:
        people.save()
    rec.write_json(rec.speakers_path, clean)
    do_export(cfg, rec)
    if clean and rec.transcript_path.exists():  # a name the user saw and saved is confirmed, not a guess any more
        data = rec.read_json(rec.transcript_path)
        voice = data.get("voice_matches") or {}
        call = data.get("direct_call") or {}
        if any(lab in voice for lab in clean) or call.get("label") in clean:
            data["voice_matches"] = {lab: v for lab, v in voice.items() if lab not in clean}
            if call.get("label") in clean:
                data.pop("direct_call")  # decided by the user
            rec.write_json(rec.transcript_path, data)
    enroll_names(cfg, rec, clean)  # only now does the print go into the shared registry
    mark_edited(rec)
    return clean




def trash_state(cfg: Config) -> dict:
    """What is in the trash, with how many days each one has left there (None: emptied only by hand)."""
    days = cfg.retention.trash_days
    now = datetime.now()
    out = []
    for it in trash.items(cfg):
        left = None
        if days > 0:
            try:
                left = max(0, days - (now - datetime.fromisoformat(it["trashed"])).days)
            except ValueError:
                left = 0
        out.append({**it, "days_left": left})
    return {"items": out, "trash_days": days}


def unresolved_labels(rec: Recording) -> list[str]:
    if not rec.transcript_path.exists():
        return []
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    return [l for l in rec.read_json(rec.transcript_path).get("speakers", []) if unnamed_speaker(l) and not names.get(l)]
