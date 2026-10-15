"""Emergency fast track: process a recording on cloud services only.

The normal path is local (WhisperX + pyannote on the GPU, minutes by the local Ollama) and stays the priority. This
module is the separate, opt-in way around it, chosen per job on the review page ("⚡ … rychle přes cloud"), for when
speed or a quiet PC matter more than keeping the meeting on the PC:

- transcription *and* diarization in one request by the cloud service ([transcribe] cloud_provider: ElevenLabs Scribe,
  or OpenAI gpt-4o-transcribe-diarize) instead of WhisperX + pyannote,
- minutes by Claude ([summarize] cloud_model) instead of Ollama, no comparison minutes at all (the big model is
  trusted; a comparison is a choice of the local way, "srovnávací zápis" on the page),
- no analysis of the Teams window videos (local OCR).

The meeting's audio and transcript leave the PC (docs/privacy.md). The cloud returns no voice embeddings, so no voice
prints are matched or stored for such a recording; the microphone track still names the user.

Voice post-processing (`add_voices`, only after a fast-track transcript, started from the page when wanted): the
standard local diarization runs on the recording – one GPU pass, no transcription – and each speaker group of the
cloud transcript gets the voice embedding of the local voice that overlaps it most. Then the standard voice
recognition runs (`pipeline.recognize_voices`), and names confirmed later store prints as usual. The cloud
diarization keeps the groups; the local one only lends its voices. A group that overlaps two local voices about
equally is reported (it may mix two people).

Nothing here changes the configuration on disk: `cloud_only` returns a copy for one job (`run-job --cloud`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from . import settings
from .config import Config
from .recording import Recording, RecordingError
from .voiceprints import _unit

log = logging.getLogger(__name__)

CLOUD_PROVIDERS = ("elevenlabs", "openai")
MIX_SHARE = 0.3  # a cloud group whose second local voice covers this much of its speech may mix two people


def cloud_only(cfg: Config) -> Config:
    """The same configuration for one fast-track job (see the module docstring)."""
    return replace(cfg,
                   transcribe=replace(cfg.transcribe, provider=cfg.transcribe.cloud_provider, diarize=True),
                   summarize=replace(cfg.summarize, provider="anthropic", model=cfg.summarize.cloud_model, compare=()),
                   video=replace(cfg.video, enabled=False))


def cloud_ready(cfg: Config) -> dict:
    """Whether the fast track can run: a key for the cloud transcription and for Claude. Names only, never keys."""
    need = {cfg.transcribe.cloud_provider: f"přepis ({cfg.transcribe.cloud_provider})", "anthropic": "zápis (Claude)"}
    missing = [what for name, what in need.items() if not settings.get_secret(name)]
    return {"ok": not missing, "missing": ", ".join(missing), "provider": cfg.transcribe.cloud_provider,
            "model": cfg.summarize.cloud_model}


SERVICE_NAMES = {"openai": "OpenAI", "elevenlabs": "ElevenLabs", "anthropic": "Claude", "ollama": "Ollama"}


def _uses(cfg: Config) -> dict:
    """What a job with this configuration transcribes and writes the minutes with, in words for the page."""
    t, s = cfg.transcribe, cfg.summarize
    if t.provider == "whisperx":
        asr = f"WhisperX {t.model}" + (" + mluvčí pyannote" if t.diarize else "") + " na grafické kartě"
    else:
        model = t.openai_model if t.provider == "openai" else t.elevenlabs_model
        asr = f"{SERVICE_NAMES.get(t.provider, t.provider)} {model} (i mluvčí, v cloudu)"
    minutes = f"{SERVICE_NAMES.get(s.provider, s.provider)} {s.model}" + (" na grafické kartě" if s.provider == "ollama"
                                                                         else " (v cloudu)")
    compare = [f"{SERVICE_NAMES.get(p, p)} {m}" for p, _, m in (c.partition(":") for c in s.compare)
               if m and (p, m) != (s.provider, s.model)]  # as pipeline.do_summarize_compare: no second copy
    return {"transcribe": asr, "minutes": minutes, "compare": ", ".join(compare)}


def plans(cfg: Config) -> dict:
    """The page's processing offer: what the local way and the fast track use ({"local", "cloud"})."""
    return {"local": _uses(cfg), "cloud": _uses(cloud_only(cfg))}


# ---- one processing, a choice per step: the transcript (with the speakers) and the minutes, each on this PC or in
# the cloud; the defaults follow the settings. The emergency fast track above is both in the cloud.

def _cloud_asr(cfg: Config) -> str:
    """The cloud transcription service of the choice: the configured one when it is a cloud one, else the fast track's."""
    return cfg.transcribe.provider if cfg.transcribe.provider != "whisperx" else cfg.transcribe.cloud_provider


def _local_minutes(cfg: Config) -> str:
    """The Ollama model of the local choice: the configured one, else the first Ollama comparison model; "" = none."""
    s = cfg.summarize
    if s.provider == "ollama":
        return s.model
    return next((m for p, _, m in (c.partition(":") for c in s.compare) if p == "ollama" and m), "")


def _cloud_minutes(cfg: Config) -> tuple[str, str]:
    """(provider, model) of the cloud choice: the configured one when it is a cloud one, else Claude of the fast track."""
    s = cfg.summarize
    return (s.provider, s.model) if s.provider != "ollama" else ("anthropic", s.cloud_model)


def choose(cfg: Config, transcribe: str = "", minutes: str = "") -> Config:
    """The configuration for one job with the page's choices: transcribe / minutes = "local" | "cloud" | "" (as set).
    A cloud transcript brings its own speakers (diarization) and skips the window videos (local OCR), as the fast
    track does."""
    if transcribe == "local":
        cfg = replace(cfg, transcribe=replace(cfg.transcribe, provider="whisperx"))
    elif transcribe == "cloud":
        cfg = replace(cfg, transcribe=replace(cfg.transcribe, provider=_cloud_asr(cfg), diarize=True),
                      video=replace(cfg.video, enabled=False))
    if minutes == "local":
        model = _local_minutes(cfg)
        if not model:
            raise ValueError("lokální model zápisu není nastavený (Nastavení → Zápis)")
        cfg = replace(cfg, summarize=replace(cfg.summarize, provider="ollama", model=model))
    elif minutes == "cloud":
        provider, model = _cloud_minutes(cfg)
        cfg = replace(cfg, summarize=replace(cfg.summarize, provider=provider, model=model))
    return cfg


def choices(cfg: Config) -> dict:
    """What the page offers per step, with the defaults from the settings and whether a cloud key is missing.
    Names only, never keys."""
    t, s = cfg.transcribe, cfg.summarize
    asr = _cloud_asr(cfg)
    asr_model = t.openai_model if asr == "openai" else t.elevenlabs_model
    mp, mm = _cloud_minutes(cfg)
    local_model = _local_minutes(cfg)

    def key(name: str) -> str:
        return "" if settings.get_secret(name) else f"chybí klíč {SERVICE_NAMES.get(name, name)} (Nastavení → Klíče API)"
    compare = [f"{SERVICE_NAMES.get(p, p)} {m}" for p, _, m in (c.partition(":") for c in s.compare) if m]
    return {
        "transcribe": {
            "default": "local" if t.provider == "whisperx" else "cloud",
            "local": {"label": f"WhisperX {t.model}" + (" + mluvčí pyannote" if t.diarize else ""),
                      "missing": ""},
            "cloud": {"label": f"{SERVICE_NAMES.get(asr, asr)} {asr_model}, i mluvčí (odchází zvuk)",
                      "missing": key(asr), "service": SERVICE_NAMES.get(asr, asr)},
        },
        "minutes": {
            "default": "off" if not s.enabled else "local" if s.provider == "ollama" else "cloud",
            "local": {"label": f"Ollama {local_model}" if local_model else "Ollama",
                      "missing": "" if local_model else "lokální model zápisu není nastavený (Nastavení → Zápis)"},
            "cloud": {"label": f"{SERVICE_NAMES.get(mp, mp)} {mm} (odchází text přepisu)", "missing": key(mp),
                      "service": SERVICE_NAMES.get(mp, mp)},
        },
        "compare": ", ".join(compare),  # the comparison minutes, when some are set
        "compare_auto": s.compare_auto,  # "srovnávací zápis" ticked beforehand (with local minutes)
    }


def needs_voices(data: dict) -> bool:
    """A transcript made by the fast track (a cloud provider) that has no voice embeddings yet."""
    return data.get("provider") in CLOUD_PROVIDERS and not data.get("speaker_embeddings")


def group_voices(segments: list[dict], turns: list[tuple[float, float, str]], embeddings: dict[str, list[float]]
                 ) -> tuple[dict[str, list[float]], dict[str, dict]]:
    """For each speaker group of the transcript, the embedding of the local voice that overlaps its replies most,
    and the groups whose speech is split between two local voices ({group: {"voices", "share"}})."""
    overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in segments:
        group = s.get("speaker")
        if not group:
            continue
        a, b = float(s["start"]), float(s["end"])
        for start, end, voice in turns:
            o = min(b, end) - max(a, start)
            if o > 0:
                overlap[group][voice] += o
    chosen: dict[str, list[float]] = {}
    mixed: dict[str, dict] = {}
    for group, row in overlap.items():
        ranked = sorted(row.items(), key=lambda kv: -kv[1])
        total = sum(row.values())
        if ranked and ranked[0][0] in embeddings:
            u = _unit(list(embeddings[ranked[0][0]]))  # stored as unit vectors (cosine = dot product)
            if u:
                chosen[group] = [round(x, 6) for x in u]
        if len(ranked) > 1 and total and ranked[1][1] / total >= MIX_SHARE:
            mixed[group] = {"voices": [v for v, _ in ranked[:2]], "share": round(ranked[1][1] / total, 2)}
    return chosen, mixed


def add_voices(cfg: Config, rec: Recording) -> dict:
    """The voice post-processing of a fast-track transcript (see the module docstring). Returns
    {"groups": n with a voice, "matches": voice recognitions, "mixed": groups that may mix two people}."""
    from .pipeline import recognize_voices
    from .providers.whisperx_provider import diarize_audio
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    if data.get("provider") not in CLOUD_PROVIDERS:
        raise RecordingError(f"{rec.stem}: voices are added only to a fast-track (cloud) transcript")
    import whisperx
    audio = rec.mix_path if rec.mix_path and rec.mix_path.exists() else None
    if audio is None:
        raise RecordingError(f"{rec.stem}: no audio to take the voices from")
    from .timings import step
    with step("lokální rozlišení mluvčích"):
        dia, embeddings = diarize_audio(whisperx.load_audio(str(audio)), cfg.transcribe.diarize_model,
                                        cfg.transcribe.device)
    turns = [(float(r.start), float(r.end), str(r.speaker)) for r in dia.itertuples()]
    voices, mixed = group_voices(data.get("segments") or [], turns, embeddings or {})
    data["speaker_embeddings"] = voices
    data["voices_from"] = {"model": cfg.transcribe.diarize_model, "mixed": mixed}
    data["diarize_model"] = data.get("diarize_model") or cfg.transcribe.diarize_model
    rec.write_json(rec.transcript_path, data)
    log.info("%s: voices added to %d cloud speaker groups%s", rec.stem, len(voices),
             f", possibly mixed: {', '.join(mixed)}" if mixed else "")
    with step("poznání po hlase"):
        matches = recognize_voices(cfg, rec) if cfg.voiceprints.enabled and voices else {}
    return {"groups": len(voices), "matches": len(matches), "mixed": sorted(mixed)}
