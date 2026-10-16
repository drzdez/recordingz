# recordingz – the transcribe part

The `recordingz` command: post-processing for meeting recordings made by the capture app
([`capture/`](../capture/README.md)) or imported from any audio/video file
(typically a Teams meeting recording): transcription with speaker attribution, speaker naming,
exports (txt/srt) and meeting summaries. Runs locally (WhisperX on an NVIDIA GPU, or on the CPU) or with a cloud
provider (OpenAI, ElevenLabs).

Input/output contract: [recording-format.md](../docs/recording-format.md)
(owned by the capture app; this part reads `format: 1`).

User guide: [docs/user-guide.md](../docs/user-guide.md), installation: [docs/install.md](../docs/install.md), privacy: [docs/privacy.md](../docs/privacy.md) (the review page itself is in Czech). Background: [`lab/FINDINGS.md`](lab/FINDINGS.md) (measurements and decisions), [`docs/hardware-portability.md`](../docs/hardware-portability.md) (other GPUs, CPU, Mac, cloud).

## Usage

Everything works on a recordings folder laid out as `<OUT_DIR>/YYYY/MM/<stem>/<stem>.*`: one folder per recording,
every file in it prefixed with the stem (`<date>_<time>_<title-slug>`). A recording is a `.json` sidecar plus audio
(`_mix.wav`, and `_sys.wav`/`_mic.wav` for live captures); transcripts and minutes are written next to them.

```
recordingz process                     # import everything in <OUT_DIR>/_inbox, then transcribe + export
                                                #   + summarize every recording that has no transcript yet
recordingz process <stem|file>         # the same for one recording or one ad-hoc media file
recordingz process --latest            # import the inbox, then only the newest recording
                                                # (`latest` also works as <stem> in every command)
recordingz import <file> [--title ...] [--start ...] [--participants "A,B"] [--video]
recordingz transcribe <stem|file> [--provider whisperx|openai|elevenlabs] [--language auto|cs|sk|en] [--no-diarize]
recordingz compare-transcribe <stem> --provider openai   # a second transcript next to the main one, to compare
recordingz video <stem> [--participants "A,B"]           # (re)run the Teams video analysis
recordingz label-speakers <stem>       # interactive: SPEAKER_00 -> "Jana Nováková"
recordingz export <stem> [--txt] [--srt]
recordingz summarize <stem> [--provider ollama|anthropic|openai] [--model ...] [--language cs] [--compare]
recordingz review [latest|<stem>]              # local page: listen to each speaker, type names, save
recordingz recognize [<stem>|latest] [--summary]      # match unknown speakers against voice prints collected since
recordingz people list | merge KEEP DROP | forget-voice ID
recordingz rename <stem> "New title"           # renames the folder + files to the new stem
recordingz purge-audio [--older-than 30] [--dry-run]   # delete audio + window videos of finished recordings, keep texts
recordingz list                                # recordings and what each has (A audio, V video, T transcript, S summary)
```

Typical day:

1. Live meeting: the capture app records it into `<OUT_DIR>/2026/09/2026-09-04_1400_tydenni-sync/`.
2. Stored Teams recording: download the `…-Meeting Recording.mp4` and drop it into `<OUT_DIR>/_inbox/`.
3. Run `recordingz process`. Both recordings get `.transcript.json`, `.txt`, `.srt` and `.summary.md`.
4. If a speaker is still `SPEAKER_03` (audio-only recording), run `label-speakers` once; exports are regenerated.

Speaker names come from, in priority order: the microphone track of a live capture (that track is the user and
nothing else), the Teams video (the highlighted tile's name label), voice prints, and pyannote diarization as the
fallback. The microphone outranks the video because Teams never draws the speaking outline around the local
user's own tile: while the user talks, the outline stays on whoever spoke before them. Names read from a live
window count only when they match a participant of the meeting - OCR of a tile label invents people, and an
anonymous `SPEAKER_03` that voice prints or one click can name is worth more than a made-up one.

## Review page API

The review page (`recordingz review`, 127.0.0.1 only) talks to its server over a REST API: resources in
the path (`/api/recordings/{stem}`, `/api/recordings/{stem}/speakers/{label}`, `/api/people/{id}` ...), proper methods
(GET / PUT / POST / DELETE), errors as `{"error": ...}` with 400/404/405/409. The whole API is described as OpenAPI 3.1
at `/api/openapi.json` (source: `web/openapi.py`) - the contract a different client, or a .NET server replacing this
one, is built against. What the server does (jobs started and finished, names saved, merges, errors) streams to the
page as **Server-Sent Events** on `/api/events`; the browser reconnects by itself and gets what it missed
(`Last-Event-ID`), with polling of `/api/status` only as a fallback.

## Desktop app

The same review page also runs as a desktop window (Tauri, `review/` at the repository root): it finds or starts the review server and
shows the very same page, so the browser and the app share everything - page, API, server - and differ only in how
links out and closing work. Build: `cd review && npm install && npm run build`; details in
[review/README.md](../review/README.md).

## Privacy

Voice prints are biometric data of colleagues: they are **off by default** (`config --init` asks), stored only
after a person confirms a name, kept only on this machine, and anyone can opt out (the People tab deletes their
prints at once and no new ones are made). What leaves the machine: nothing, except the audio with a cloud
transcription provider and the transcript text when the minutes are written through Claude or OpenAI. Details and a note to send to the team: [docs/privacy.md](../docs/privacy.md).

## Configuration

One TOML file shared with the capture app: `%APPDATA%\recordingz\recordingz.toml` (`~/.config/recordingz/recordingz.toml`
elsewhere). Every value can be overridden by a CLI flag; the `RECORDINGZ_CONFIG` environment variable points to a
different file.

```toml
[user]
name = "Jan Novák"          # live recordings: the diarization label that matches the mic track gets this name

[people]
display = "nick"                 # exports and minutes print: first | full | nick (nick without a nickname -> first)

[calendar]
outlook = true                   # classic Outlook on this PC (COM): meeting title + participants (imports by start time)

[recordings]
out_dir = "D:/meetings"          # the recordings folder (default ~/meetings); _inbox lives inside it

[transcribe]
provider = "whisperx"            # whisperx (local) | openai | elevenlabs (cloud: the audio goes to that service)
language = "auto"                # auto | cs | sk | en
languages = ["cs", "sk", "en"]   # auto detects only among these
per_speaker_language = true      # auto: a speaker who talks another of `languages` is transcribed in it
device = "cuda"                  # cuda | cpu
model = "large-v3"
compute_type = "float16"         # float16 | int8_float16 | int8  (pick by VRAM, see docs/hardware-portability.md)
batch_size = 16
align = true                     # word timestamps; needs an alignment model for the language
diarize = true                   # pyannote; skipped automatically when the video timeline covers the recording
diarize_model = "pyannote/speaker-diarization-community-1"
glossary = ["Atlas", "SLA", "Kubernetes", "BPMN", "Entra ID"]   # goes into the ASR prompt with title + participants

[video]
enabled = true                   # analyse Teams video on import; ignored when the file has no video stream
fps = 2

[summarize]
enabled = true                   # run as part of `process`; a failure (no model, no key) is logged, not fatal
provider = "ollama"              # ollama = local GPU, everything stays on the machine | anthropic = Claude API | openai
model = "gemma4:31b"             # ollama: `ollama pull gemma4:31b` (20 GB, fits 24 GB VRAM) | anthropic: claude-opus-5
language = "cs"                  # language of the minutes, independent of the meeting language
ollama_think = false             # thinking mode for local models: slower, sometimes better
compare = ["anthropic:claude-opus-5"]   # optional: extra summaries for comparison -> <stem>.summary.claude-opus-5.md
```

Most of this can be edited on the review page (**Nastavení**): every field with its meaning and default, written
back into the TOML with comments kept (`settings.py`, `GET/PUT /api/settings`). The capture app reloads its keys
from the file by itself; a new `out_dir` applies after a restart.

Secrets are never in the config file. API keys (Claude, OpenAI, ElevenLabs) are read from the environment first,
then from the Windows Credential Manager (service `recordingz`, via `keyring`), where the settings page stores them;
the page shows only whether a key is set:

- HuggingFace (pyannote models): `hf auth login` once; accept the terms of `pyannote/speaker-diarization-community-1`.
- Claude API (only with `provider = "anthropic"`): the settings page, or the `RECORDINGZ_ANTHROPIC_API_KEY` environment variable. The
  tool-specific name keeps the key away from other Anthropic software on the machine (Claude Code would otherwise
  offer to bill against a plain `ANTHROPIC_API_KEY`); without it the SDK defaults apply.
- Ollama (default): install Ollama, `ollama pull gemma4:31b`; no keys, the transcript never leaves the machine.

## Installation

The usual install is the suite MSI ([docs/install.md](../docs/install.md)). From source (development):

```
git clone https://github.com/drzdez/recordingz
cd recordingz/transcribe
uv sync --extra all            # venv with whisperx + CUDA 12.8 torch (Windows/Linux) + video analysis
uv run recordingz config --init
```

Requirements: Python 3.12 (uv fetches it), NVIDIA driver 570+ for the CUDA 12.8 wheels, `ffmpeg` on PATH
(`winget install Gyan.FFmpeg`) or `RECORDINGZ_FFMPEG_DIR` pointing at its `bin` folder, and a HuggingFace login
(`hf auth login`) with the terms of `pyannote/speaker-diarization-community-1` accepted. Models (~5 GB) download
on first use into the HuggingFace cache.

`bin\recordingz.cmd` runs the checkout's venv and finds the WinGet ffmpeg by itself; add `bin` to PATH to use the command from anywhere.

Tests: `uv run pytest`. The review page is tested too (`tests/web/*.test.mjs`: the page in jsdom against the real
review server); that needs Node.js and a one-time `npm ci` in `tests/web`, otherwise those tests are skipped.

## Lab scripts

The experiments the CLI grew out of are kept in `lab/` and still run on their own:

```
cd lab
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-lab.txt

# 1. audio out of any recording (mono 16 kHz)
ffmpeg -i "meeting.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le samples/meeting_mix.wav

# 2. transcription + alignment + diarization; outputs in out/<name>/
.venv/Scripts/python run_whisperx.py samples/meeting_mix.wav --name meeting --language auto ^
    --initial-prompt "Schůzka o Atlasu. Témata: SLA, Kubernetes, BPMN."

# 3. speaker names from the Teams video, applied to the transcript -> out/video/transcript_named.txt
.venv/Scripts/python video_speakers.py "meeting.mp4" --out out/video ^
    --transcript out/meeting/transcript.json --names "Jana Nováková,Petr Svoboda"
```

`run_whisperx.py --help` lists all knobs (model, compute type, batch, VAD, speaker count bounds). On Windows the
WinGet ffmpeg is not on Git Bash's PATH; see `lab/README.md`.

## Stack

- Python 3.12, `uv`, CLI via `typer`, config via TOML (`src/recordingz/`)
- Providers behind one interface producing `<stem>.transcript.json`: `whisperx` (local, CUDA or CPU),
  `openai` and `elevenlabs` (cloud, `providers/`); `fasttrack.py` is the opt-in path through cloud services only
- `video_speakers`: frame sampling + accent-colour label detection + easyocr
- `mic_speakers`: the diarization label that coincides with microphone activity becomes the user (`[user] name`)
- `people.py`: registry `<OUT_DIR>/_speakers/people.json` (first, last, nick, per-person display override);
  `speakers.json` stores person ids, the display form is applied at export/summary time
- `web/`: the review page. `review.py` = a stdlib HTTP server on 127.0.0.1 with a REST API and Server-Sent Events
  (see "Review page API"; OpenAPI in `openapi.py`), calling the data functions in `views.py` (a recording) and
  `people_views.py` (people, voice prints) and the job queue in `jobs.py`; `index.html` + `static/review.css` +
  `static/review.js` = one page of vanilla JS (`@ts-check` + JSDoc types), no build step, no framework. Writes only `<stem>.speakers.json`, then regenerates exports and (on request) the summary.
- `voiceprints.py`: the diarization embeddings (pyannote community-1 via whisperx `return_embeddings`) are stored
  under a person when a label gets a name (`_speakers/voiceprints.json`, max 10 per person, the user's own from
  the mic track); unnamed labels of new recordings are named when cosine similarity >= `[voiceprints] threshold`
  and ahead of the runner-up by `margin`; the match is written to `speakers.json` + `voice_matches` in the transcript
- `summarize`: one prompt (summary / topics / decisions / action items / open questions / terms / speakers); transcripts
  that do not fit the local model's context (`ollama_max_ctx`, sized to stay on the GPU) are summarized in parts
  and merged; three backends in
  `llm.py`: Ollama REST (`/api/chat`, context sized to the transcript), the Anthropic SDK (streaming, cached system
  prompt) and the OpenAI Chat Completions API (`openai_url`)
