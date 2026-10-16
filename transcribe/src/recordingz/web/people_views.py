"""The people page's data and actions: the registry, voice prints, merges and the guesses to review."""

from __future__ import annotations

import logging

from ..config import Config
from ..people import DISPLAY_MODES, People
from ..pipeline import mark_edited, do_export, enroll_names, load_segments
from ..voiceprints import Voiceprints, speech_seconds
from ..fileio import registry_locked
from ..recording import Recording, RecordingError, find_recording, iter_recordings
from .views import CLIP_MAX_S, _fmt_hms, is_label, pick_samples

log = logging.getLogger(__name__)


@registry_locked
def merge_people(cfg: Config, keep: str, drop: str, stem: str | None = None) -> list[dict]:
    people = People.load(cfg.out_dir, cfg.people_display)
    changed = people.merge(keep, drop, iter_recordings(cfg.out_dir))
    people.save()
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    vp.rename(drop, keep)
    vp.save()
    for r in changed:
        if r.transcript_path.exists():
            mark_edited(r)
            do_export(cfg, r)
    if stem and stem not in {r.stem for r in changed}:
        r = find_recording(stem, cfg.out_dir)
        if r.transcript_path.exists():
            do_export(cfg, r)
    return people.to_json()


def person_detail(cfg: Config, pid: str) -> dict:
    people = People.load(cfg.out_dir, cfg.people_display)
    p = people.get(pid)
    if p is None:
        raise RecordingError(f"unknown person {pid!r}")
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    recs = {r.stem: r for r in iter_recordings(cfg.out_dir)}
    prints = []
    for pr in vp.people.get(pid, []):
        row = {"stem": pr.get("stem"), "label": pr.get("label"), "added": pr.get("added"), "dims": len(pr.get("v", [])),
               "title": None, "start": None, "seconds": None, "samples": [], "has_mix": False}
        rec = recs.get(pr.get("stem") or "")
        if rec is not None:
            row["title"], row["start"] = rec.title, rec.sidecar.get("start")
            row["has_mix"] = bool(rec.mix_path and rec.mix_path.exists())
            if rec.transcript_path.exists():
                try:
                    _, segs = load_segments(rec)
                    mine = [s for s in segs if s.speaker == pr.get("label")]
                    row["seconds"] = round(sum(s.end - s.start for s in mine), 1)
                    row["samples"] = [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                                       "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(mine)]
                except Exception:
                    pass
        prints.append(row)
    from .. import assignments
    rows = assignments.for_people(cfg).get(pid, [])
    assigned = [r.public() for r in rows]
    samples_view = assignments.sample_view(cfg, pid, rows, vp)
    excerpts: dict[tuple[str, str], list[dict]] = {}

    def five(stem: str, label: str) -> list[dict]:
        """Up to 5 replies of the group to listen to (the samples view)."""
        if (stem, label) not in excerpts:
            excerpts[(stem, label)] = []
            rec = recs.get(stem)
            if rec is not None and rec.transcript_path.exists():
                try:
                    _, segs = load_segments(rec)
                    mine = [s for s in segs if s.speaker == label]
                    excerpts[(stem, label)] = [
                        {"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                         "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(mine, 5)]
                except Exception:
                    pass
        return excerpts[(stem, label)]
    for row in samples_view["used"] + samples_view["addable"]:
        rec = recs.get(row["stem"])
        row["has_mix"] = bool(rec and rec.mix_path and rec.mix_path.exists())
        row["samples"] = five(row["stem"], row["label"])
    for c in assigned:
        rec = recs.get(c["stem"])
        c["has_mix"] = bool(rec and rec.mix_path and rec.mix_path.exists())
        c["samples"] = []
        if rec is None or not rec.transcript_path.exists() or c["status"] == "print_only":
            continue
        try:
            _, segs = load_segments(rec)
            mine = [s for s in segs if s.speaker == c["label"]]
            c["samples"] = [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                             "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(mine)]
        except Exception:
            pass
    candidates = voice_candidates(cfg, people).get(pid, [])
    for c in candidates:
        rec = recs.get(c["stem"])
        c["has_mix"] = bool(rec and rec.mix_path and rec.mix_path.exists())
        c["samples"] = []
        try:
            _, segs = load_segments(rec)
            mine = [s for s in segs if s.speaker == c["label"]]
            c["samples"] = [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                             "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(mine)]
        except Exception:
            pass
    d = {"id": p.id, "first": p.first, "last": p.last, "nick": p.nick, "display": p.display, "aliases": p.aliases,
         "shown": p.name(people.default_mode), "model": vp.model, "limit": vp.limit, "prints": prints,
         "candidates": candidates, "assignments": assigned, "summary": assignments.summary(rows),
         "samples_view": samples_view,
         "recordings": sorted({r.stem for r in recs.values() if r.speakers_path.exists()
                               and pid in r.read_json(r.speakers_path).values()}, reverse=True)}
    return d


# ---------------------------------------------------------------- guesses to review (Lidé → ke kontrole)

def voice_candidates(cfg: Config, people: People | None = None) -> dict[str, list[dict]]:
    """Speaker groups the application put a person's name to without the user's word: a voice match, a direct
    call, a name tag of the Teams video, the microphone track. Until confirmed they store no voice print (a wrong
    guess must not teach the next recording the same mistake); on the person's page they can be confirmed or
    rejected one by one. {person id: [{stem, title, start, label, source, score?, seconds, can_reject}]}"""
    people = people or People.load(cfg.out_dir, cfg.people_display)
    out: dict[str, list[dict]] = {}
    for rec in iter_recordings(cfg.out_dir):
        if not rec.transcript_path.exists():
            continue
        try:
            data = rec.read_json(rec.transcript_path)
            manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
        except (OSError, ValueError):
            continue
        voice = data.get("voice_matches") or {}
        call = data.get("direct_call") or {}
        rejected = data.get("voice_rejected") or {}
        secs = speech_seconds(data.get("segments") or [])
        for label, seconds in secs.items():
            if seconds <= 0 or label == "UNKNOWN":
                continue
            score = None
            if label in voice:
                pid, source, score = voice[label].get("person"), "hlas", voice[label].get("score")
            elif call.get("label") == label and call.get("applied"):
                pid, source = call.get("person"), "přímý hovor"
            elif not is_label(label) and label not in manual:  # a literal name: the Teams video or the microphone
                person = people.find(label)
                pid = person.id if person else None
                source = "mikrofon" if cfg.user_name and label == cfg.user_name else "video"
            else:
                continue
            if not pid or pid in rejected.get(label, []) or people.get(pid) is None:
                continue
            out.setdefault(pid, []).append({
                "stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"), "label": label,
                "source": source, "score": score, "seconds": round(seconds, 1),
                "short": seconds < cfg.voiceprints.min_seconds,
                "can_reject": source in ("hlas", "přímý hovor")})
    for rows in out.values():
        rows.sort(key=lambda r: r["stem"], reverse=True)
    return out


@registry_locked
def confirm_candidate(cfg: Config, stem: str, label: str, pid: str) -> dict:
    """"Yes, it is them": this one group gets the person for good (as if saved on the page) and its voice
    print is stored. The other groups of the recording stay as they were."""
    rec = find_recording(stem, cfg.out_dir)
    if People.load(cfg.out_dir, cfg.people_display).get(pid) is None:
        raise RecordingError(f"unknown person {pid!r}")
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    manual[label] = pid
    rec.write_json(rec.speakers_path, manual)
    data = rec.read_json(rec.transcript_path)
    (data.get("voice_matches") or {}).pop(label, None)
    if (data.get("direct_call") or {}).get("label") == label:
        data.pop("direct_call")
    rec.write_json(rec.transcript_path, data)
    added = enroll_names(cfg, rec, {label: pid})
    mark_edited(rec)
    do_export(cfg, rec)
    return {"prints_added": added}


def _unname_group(rec: Recording, data: dict, label: str) -> str:
    """A group named by its label itself – a name tag of the Teams video or the microphone ("Matúš Kováč") – becomes
    an unnamed SPEAKER_NN: its replies, the speaker list, its voice embedding and a manual name move to the new
    label. Returns it. (Changes `data` and speakers.json; the caller writes the transcript.)"""
    taken = {s.get("speaker") for s in data.get("segments") or []} | set(data.get("speakers") or [])
    new = next(f"SPEAKER_{i:02d}" for i in range(100) if f"SPEAKER_{i:02d}" not in taken)
    for s in data.get("segments") or []:
        if s.get("speaker") == label:
            s["speaker"] = new
    data["speakers"] = [new if lab == label else lab for lab in data.get("speakers") or []]
    emb = data.get("speaker_embeddings") or {}
    if label in emb:
        emb[new] = emb.pop(label)
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if label in manual:
        manual[new] = manual.pop(label)
        rec.write_json(rec.speakers_path, manual)
    log.info("%s: group %r renamed to %s (its name was wrong)", rec.stem, label, new)
    return new


@registry_locked
def reassign_candidate(cfg: Config, stem: str, label: str, pid: str, to_pid: str) -> dict:
    """"It is somebody else": the group gets `to_pid` for good (as if saved on the page), the voice print it gave
    `pid` goes, the new person gets its print, and `pid` is not guessed for it again."""
    people = People.load(cfg.out_dir, cfg.people_display)
    if people.get(to_pid) is None:
        raise RecordingError(f"unknown person {to_pid!r}")
    rec = find_recording(stem, cfg.out_dir)
    data = rec.read_json(rec.transcript_path)
    removed = forget_print(cfg, pid, stem, label)
    if not is_label(label):  # a name-tag group: an unnamed label first, so the old name does not stay as the label
        label = _unname_group(rec, data, label)
    (data.get("voice_matches") or {}).pop(label, None)
    if (data.get("direct_call") or {}).get("label") == label:
        data.pop("direct_call")
    rejected = data.setdefault("voice_rejected", {})
    if pid not in rejected.setdefault(label, []):
        rejected[label].append(pid)
    rec.write_json(rec.transcript_path, data)
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    manual[label] = to_pid
    rec.write_json(rec.speakers_path, manual)
    added = enroll_names(cfg, rec, {label: to_pid})
    mark_edited(rec)
    do_export(cfg, rec)
    return {"ok": True, "label": label, "prints_removed": removed, "prints_added": added}


@registry_locked
def reject_candidate(cfg: Config, stem: str, label: str, pid: str) -> dict:
    """"Not them": the name goes from this group (it is unnamed again), the same guess is not made for it again,
    and a voice print the group gave this person is deleted (a confirmed group was wrong after all). A name-tag
    group (the video, the microphone) becomes an unnamed SPEAKER_NN."""
    rec = find_recording(stem, cfg.out_dir)
    removed = forget_print(cfg, pid, stem, label)
    data = rec.read_json(rec.transcript_path)
    if not is_label(label):
        label = _unname_group(rec, data, label)
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if manual.get(label) == pid:
        manual.pop(label)
        rec.write_json(rec.speakers_path, manual)
    (data.get("voice_matches") or {}).pop(label, None)
    if (data.get("direct_call") or {}).get("label") == label:
        data.pop("direct_call")
    rejected = data.setdefault("voice_rejected", {})
    if pid not in rejected.setdefault(label, []):
        rejected[label].append(pid)
    rec.write_json(rec.transcript_path, data)
    mark_edited(rec)
    do_export(cfg, rec)
    return {"ok": True, "label": label, "prints_removed": removed}


@registry_locked
def sample_action(cfg: Config, pid: str, action: str, stem: str, label: str, replace: dict | None = None) -> dict:
    """Lidé → Vzorky pro rozpoznávání. exclude: the print leaves recognition (the name in the meeting stays, the
    print does not come back); include: undo that; add: the group becomes a print – a guessed one is confirmed
    first; with `replace` that print goes first, else over the limit the weakest goes (prune). Says why not."""
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    if action == "exclude":
        removed = vp.exclude(pid, stem, label)
        vp.save()
        return {"ok": True, "removed": removed}
    if action == "include":
        vp.include(pid, stem, label)
        vp.save()
        return {"ok": True}
    if action != "add":
        raise ValueError("action: exclude | include | add")
    rec = find_recording(stem, cfg.out_dir)
    data = rec.read_json(rec.transcript_path)
    secs = speech_seconds(data.get("segments") or []).get(label, 0.0)
    if secs < cfg.voiceprints.min_seconds:
        return {"ok": False, "reason": f"mluví jen {secs:.0f} s – otisk potřebuje aspoň "
                                       f"{cfg.voiceprints.min_seconds:.0f} s řeči"}
    vp.include(pid, stem, label)  # added by hand: an earlier exclusion no longer holds
    before = {(p.get("stem"), p.get("label")) for p in vp.people.get(pid, [])}
    gone = set()
    if replace:
        gone = {(replace.get("stem"), replace.get("label"))}
        vp.people[pid] = [p for p in vp.people.get(pid, []) if (p.get("stem"), p.get("label")) not in gone]
    vp.save()
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    guessed = (label in (data.get("voice_matches") or {}) or (data.get("direct_call") or {}).get("label") == label
               or not manual.get(label))
    if guessed:
        confirm_candidate(cfg, stem, label, pid)  # the name for good, and the print
    else:
        enroll_names(cfg, rec, {label: pid})
    after = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    now = {(p.get("stem"), p.get("label")) for p in after.people.get(pid, [])}
    if (stem, label) not in now:
        return {"ok": False, "reason": "skoro stejný jako už uložený vzorek – nový by nic nepřidal"}
    dropped = sorted(before - now - gone)
    return {"ok": True, "dropped": [{"stem": a, "label": b} for a, b in dropped]}


@registry_locked
def forget_print(cfg: Config, pid: str, stem: str | None, label: str | None) -> int:
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    before = vp.count(pid)
    if stem and label:
        vp.people[pid] = [pr for pr in vp.people.get(pid, []) if not (pr.get("stem") == stem and pr.get("label") == label)]
        if not vp.people[pid]:
            del vp.people[pid]
    else:
        vp.forget(pid)
    vp.save()
    return before - vp.count(pid)


def people_rows(cfg: Config) -> dict:
    people = People.load(cfg.out_dir, cfg.people_display)
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    rows = people.to_json()
    from .. import assignments
    assigned = assignments.for_people(cfg)
    for r in rows:
        r["prints"] = vp.count(r["id"])
        sm = assignments.summary(assigned.get(r["id"], []))
        r["candidates"] = sm["review"]
        r["assigned"] = sm
    return {"people": rows, "display_default": people.default_mode, "modes": list(DISPLAY_MODES),
            "path": str(people.path)}


@registry_locked
def _drop_opted_out_prints(cfg: Config, people: People) -> list[str]:
    """Somebody opted out of voice recognition: their prints go right away, not at the next enrolment."""
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    gone = [pid for pid in people.no_voiceprint() if vp.count(pid)]
    for pid in gone:
        vp.forget(pid)
    if gone:
        vp.save()
        log.info("voice prints deleted on request: %s", ", ".join(gone))
    return gone


@registry_locked
def save_people(cfg: Config, rows: list[dict], stem: str | None = None) -> list[dict]:
    people = People.load(cfg.out_dir, cfg.people_display)
    people.replace_all(rows)
    people.save()
    _drop_opted_out_prints(cfg, people)
    if stem:
        rec = find_recording(stem, cfg.out_dir)
        if rec.transcript_path.exists():
            do_export(cfg, rec)
    return people_rows(cfg)["people"]  # with the print counts, so the page shows what is left
