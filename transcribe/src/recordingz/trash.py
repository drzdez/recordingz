"""The trash for whole meetings: a recording's folder moves to <out_dir>/.trash/<stem>/ (outside the YYYY/MM tree, so
no list, search or job sees it), can come back, and goes for good when the trash is emptied – by hand, or by itself
after [retention] trash_days. Deleting for good also drops the voice prints the recording gave.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .fileio import registry_lock
from .recording import Recording, RecordingError, is_sidecar

log = logging.getLogger(__name__)

TRASH = ".trash"
STAMP = "trashed.json"  # next to the recording in the trash: when, and where it came from


def trash_dir(cfg: Config) -> Path:
    return cfg.out_dir / TRASH


def _size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def _forget_prints(cfg: Config, stem: str) -> None:
    from .voiceprints import Voiceprints
    with registry_lock(cfg.out_dir):
        vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
        if vp.forget_stem(stem):
            vp.save()


def _own_folder(cfg: Config, rec: Recording) -> str:
    """The recording's folder relative to out_dir (YYYY/MM/<stem>); anything else is refused before it is touched."""
    try:
        rel = rec.dir.resolve().relative_to(cfg.out_dir.resolve())
    except ValueError:
        raise RecordingError(f"{rec.dir} is not a recording folder under {cfg.out_dir}") from None
    if len(rel.parts) != 3 or rel.parts[0] == TRASH:
        raise RecordingError(f"{rec.dir} is not a recording folder under {cfg.out_dir}")
    return rel.as_posix()


def move_to_trash(cfg: Config, rec: Recording, now: datetime | None = None) -> Path:
    """The recording's folder into the trash; the voice prints stay until it is deleted for good."""
    origin = _own_folder(cfg, rec)
    target = trash_dir(cfg) / rec.dir.name
    if target.exists():  # the same meeting trashed before and restored by hand: the older copy goes
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(rec.dir), str(target))
    (target / STAMP).write_text(json.dumps({"stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"),
                                            "from": origin,
                                            "trashed": (now or datetime.now()).isoformat(timespec="seconds")},
                                           ensure_ascii=False), encoding="utf-8")
    log.info("%s: moved to the trash", rec.stem)
    return target


def delete_now(cfg: Config, rec: Recording) -> None:
    """The recording deleted for good, without the trash: its folder and its voice prints."""
    _own_folder(cfg, rec)
    shutil.rmtree(rec.dir)
    _forget_prints(cfg, rec.stem)
    log.info("%s: deleted for good", rec.stem)


def _stamp(folder: Path) -> dict:
    try:
        return json.loads((folder / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def items(cfg: Config) -> list[dict]:
    """What is in the trash, the most recently trashed first."""
    root = trash_dir(cfg)
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        s = _stamp(folder)
        out.append({"stem": s.get("stem") or folder.name, "title": s.get("title") or folder.name,
                    "start": s.get("start"), "trashed": s.get("trashed") or "", "bytes": _size(folder)})
    return sorted(out, key=lambda i: i["trashed"], reverse=True)


def _folder(cfg: Config, stem: str) -> Path:
    root = trash_dir(cfg)
    for folder in root.iterdir() if root.is_dir() else ():
        if folder.is_dir() and (folder.name == stem or _stamp(folder).get("stem") == stem):
            return folder
    raise RecordingError(f"{stem} is not in the trash")


def _patiently(fn, tries: int = 20, wait_s: float = 0.05):
    """Windows refuses to delete or move a file another thread has open for a moment (the page listing the trash
    reads the stamps): try again briefly instead of failing the restore."""
    import time
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait_s)


def restore(cfg: Config, stem: str) -> str:
    """Back where it was; returns the stem."""
    folder = _folder(cfg, stem)
    s = _stamp(folder)
    target = cfg.out_dir / (s.get("from") or f"{stem[:4]}/{stem[5:7]}/{folder.name}")
    if target.exists():
        raise RecordingError(f"{target} exists already – the meeting cannot go back there")
    _patiently(lambda: (folder / STAMP).unlink(missing_ok=True))
    target.parent.mkdir(parents=True, exist_ok=True)
    _patiently(lambda: shutil.move(str(folder), str(target)))
    if not any(is_sidecar(p) for p in target.glob("*.json")):
        log.warning("%s: restored, but its folder has no sidecar", stem)
    log.info("%s: restored from the trash", stem)
    return s.get("stem") or stem


def delete_from_trash(cfg: Config, stem: str) -> None:
    """One meeting out of the trash for good."""
    shutil.rmtree(_folder(cfg, stem))
    _forget_prints(cfg, stem)
    log.info("%s: deleted from the trash", stem)


def empty(cfg: Config, older_than_days: int | None = None, now: datetime | None = None) -> list[str]:
    """Delete for good what is in the trash (all, or what has been there longer than the days). Returns the stems."""
    now = now or datetime.now()
    gone = []
    for it in items(cfg):
        if older_than_days is not None:
            try:
                if (now - datetime.fromisoformat(it["trashed"])).days < older_than_days:
                    continue
            except ValueError:
                pass  # no date: old enough
        try:
            folder = _folder(cfg, it["stem"])
            shutil.rmtree(folder)
        except (OSError, RecordingError) as e:
            log.warning("%s: not deleted from the trash: %s", it["stem"], e)
            continue
        _forget_prints(cfg, it["stem"])
        gone.append(it["stem"])
    if gone:
        log.info("trash emptied: %d meetings", len(gone))
    return gone


def auto_empty(cfg: Config) -> list[str]:
    """[retention] trash_days > 0: what has been in the trash that long goes. Never fails the caller."""
    if cfg.retention.trash_days <= 0:
        return []
    try:
        return empty(cfg, cfg.retention.trash_days)
    except Exception as e:  # housekeeping
        log.error("trash: %s", e)
        return []
