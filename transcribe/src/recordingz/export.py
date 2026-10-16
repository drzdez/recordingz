"""Exports from the normalized transcript: .txt (readable) and .srt (subtitles)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .providers.base import Segment

NO_SPEECH = "(bez řeči – nahrávka neobsahuje rozpoznatelnou řeč)"  # instead of the replies of an empty transcript


def _hms(sec: float) -> str:
    return str(timedelta(seconds=int(sec))).rjust(8, "0")


def _srt_ts(sec: float) -> str:
    td = timedelta(seconds=sec)
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{int((sec - total) * 1000):03d}"


def to_txt(segments: list[Segment], title: str | None = None, header: dict | None = None) -> str:
    lines = []
    if title:
        lines.append(f"# {title}")
    for k, v in (header or {}).items():
        lines.append(f"{k}: {v}")
    if lines:
        lines.append("")
    if not segments:
        lines.append(NO_SPEECH)
    for seg in segments:
        who = f"{seg.speaker}: " if seg.speaker else "?: "  # "?" = the diarization gave this reply nobody
        lines.append(f"[{_hms(seg.start)}] {who}{seg.text}")
    return "\n".join(lines) + "\n"


def to_srt(segments: list[Segment]) -> str:
    if not segments:
        return f"1\n{_srt_ts(0)} --> {_srt_ts(5)}\n{NO_SPEECH}\n"
    blocks = []
    for i, seg in enumerate(segments, 1):
        who = f"{seg.speaker}: " if seg.speaker else "?: "
        blocks.append(f"{i}\n{_srt_ts(seg.start)} --> {_srt_ts(seg.end)}\n{who}{seg.text}\n")
    return "\n".join(blocks)


def write_exports(segments: list[Segment], txt_path: Path | None, srt_path: Path | None,
                  title: str | None = None, header: dict | None = None) -> None:
    if txt_path:
        txt_path.write_text(to_txt(segments, title, header), encoding="utf-8")
    if srt_path:
        srt_path.write_text(to_srt(segments), encoding="utf-8")
