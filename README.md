# recordingz

Records Microsoft Teams calls (and calls in other apps) on Windows, then transcribes them locally with speaker names
and writes the minutes. A review window shows the transcripts, lets you name the speakers by ear and edit the settings.

- **The capture app** (`recordingz-capture.exe`, [`capture/`](capture/README.md)) – a Windows tray app that notices a
  call and records system audio (the other participants) and your microphone as separate WAV tracks, plus a low-fps
  video of each Teams window for the name labels.
- **The transcribe part** (the `recordingz` CLI, [`transcribe/`](transcribe/README.md)) – transcription with speaker
  attribution (WhisperX + pyannote on an NVIDIA GPU, or a cloud provider), speaker names from the microphone track,
  the Teams video and voice prints, exports (txt/srt) and minutes (a local Ollama model, Claude or OpenAI). It also
  serves the review page.
- **The review window** (`recordingz-review.exe`, [`review/`](review/README.md)) – the review page in a desktop window
  (Tauri); the same page also runs in a browser.

The parts talk only through the recordings folder (one folder per recording with a JSON sidecar, see
[docs/recording-format.md](docs/recording-format.md)), one shared configuration file
(`%APPDATA%\recordingz\recordingz.toml`) and two small status files in `%TEMP%`. The suite was called teamsrec until
2.0; changes per release are in [CHANGELOG.md](CHANGELOG.md).

## Install

The MSI on [the releases page](https://github.com/drzdez/recordingz/releases/latest) is the whole suite for Windows:
the capture app, the review window and the transcribe part, uv and ffmpeg, per user and without admin rights. The
window prepares the Python environment on its first start and then runs a setup wizard. The capture app looks for a
new release once a day and offers it. Details, and the manual setup from the sources:
[docs/install.md](docs/install.md).

## How it is used

Start it once (the MSI puts it into autostart) and forget about it. It sits in the tray:

1. When Teams takes the microphone, recording starts at once and the tray shows a notification
   (`prompt_default = "ask"` shows a *discard?* box instead). The join screen does not count as a call: Teams holds
   the microphone there only for the device preview.
2. While recording, the tray icon is red and shows the elapsed time. Tray menu: **Stop & keep**, **Abort & delete**.
3. Recording stops by itself ~10 s after the call ends (hard cap 4 h). Too short recordings are discarded.
4. Files land in one folder per recording, `<out_dir>/YYYY/MM/<stem>/`: `<stem>_sys.wav`, `_mic.wav`, `_mix.wav`,
   `_screen<N>.mp4` and the `<stem>.json` sidecar (stem = `<date>_<time>_<title-slug>`).
5. A double click on the tray icon opens the review page; there (or with `recordingz process`) the recording is
   transcribed, the speakers named and the minutes written.

Calls outside Teams: when Zoom, Webex, Slack, Discord, WhatsApp, Skype or Signal takes the microphone, or a browser
does while a meeting is open in it (Google Meet, Zoom/Webex/Teams on the web, Jitsi, Whereby), the call is recorded
the same way. Window capture stays Teams-only; `other_apps = "off"` limits recording to Teams.

Other modes in the tray menu: **Record on-site meeting** (the room microphone only; with `onsite_offer` a meeting
from the Outlook calendar can start it by itself), **Record now** (no detection) and **Record playback** (stored
Teams recordings played at normal speed: the system track only, stops after 30 s of silence). Recordings from
elsewhere (e.g. a downloaded Teams recording) go into `<out_dir>/_inbox/` or through `recordingz import`.

When an audio device stops delivering data (a Bluetooth headset falling asleep), the streams are reopened with a
growing pause, the tray icon turns yellow and a warning is shown. The calendar of classic Outlook (local, COM) gives
the meeting title and participants when `[calendar] outlook = true`.

Using it in detail: [docs/user-guide.md](docs/user-guide.md).

## Configuration

One file for all parts: `%APPDATA%\recordingz\recordingz.toml` (or the path in `RECORDINGZ_CONFIG`). Every setting
can be changed on the review page (**Nastavení**), which keeps the comments; the capture app picks up changes by
itself. A few keys:

```toml
[recordings]
out_dir = "D:/meetings"      # the recordings folder (default ~/meetings); _inbox lives inside it

[user]
name = "Jan Novák"           # the microphone track of a live recording is this person

[calendar]
outlook = true               # classic Outlook on this PC (COM, local): meeting title + participants

[capture]
prompt_default = "record"    # record = only a notification | ask = a "discard?" box | skip = the box discards
other_apps = "record"        # calls in other apps and browser meetings: record | off
onsite_mic = "Pole mikrofonu"  # part of the input device name for on-site meetings; empty = default input

[transcribe]
provider = "whisperx"        # whisperx (local) | openai | elevenlabs (cloud, sends the audio)

[summarize]
provider = "ollama"          # ollama (local) | anthropic | openai
```

The full table is in [docs/install.md](docs/install.md#4-configuration). API keys never go into the file: they are
read from the environment or the Windows Credential Manager, where the settings page stores them.

## Repository layout

| path | what it is |
|---|---|
| [`capture/`](capture/README.md) | the capture app: .NET 10, WinForms tray, NAudio (WASAPI), xUnit tests; [PARITY.md](capture/PARITY.md) is the (historical) review of the port from the earlier Python prototype |
| [`transcribe/`](transcribe/README.md) | the transcribe part: Python 3.12 package `recordingz` (uv), the review page (`src/recordingz/web/`), pytest + jsdom page tests, `lab/` experiments, launcher `bin\recordingz.cmd` |
| [`review/`](review/README.md) | the review window: Tauri (Rust), a thin shell around the review page |
| `installer/` | the WiX 6 per-user MSI: `build-msi.ps1`, `stage-suite.ps1` |
| [`docs/`](docs) | user guide, installation, privacy, the recording format, design notes |
| `.github/workflows/build.yml` | CI: all tests and the suite MSI; a tag `v<version>` publishes a release |
| [`CHANGELOG.md`](CHANGELOG.md) | changes per release |

## Build and test

All parts share one version: `<Version>` in `capture/src/Recordingz.Capture/Recordingz.Capture.csproj`,
kept equal with the others by `transcribe/tests/test_version.py`.

From the repository root:

```powershell
# capture app (.NET 10 SDK)
cd capture
dotnet build Recordingz.Capture.slnx
dotnet test  Recordingz.Capture.slnx
cd ..

# transcribe part (uv; the GPU extras need an NVIDIA driver 570+)
cd transcribe
uv sync --extra all
uv run pytest                  # the page tests in tests/web need Node.js and `npm ci` there, otherwise they skip
cd ..

# review window (Rust + Node.js)
cd review
npm install
npm run build                  # -> src-tauri\target\release\recordingz-review.exe
cargo test --manifest-path src-tauri\Cargo.toml
cd ..

# the suite MSI
powershell -File installer\stage-suite.ps1 -Version <version>    # -> artifacts\suite
powershell -File installer\build-msi.ps1 -Suite artifacts\suite
```

`build-msi.ps1` without `-Suite` builds an MSI of the capture app alone. The GitHub workflow runs the same scripts on
every push to `main`; pushing a tag `v<version>` also publishes a GitHub Release with the MSI, which is where the
installed app finds its updates.

## Documentation

- [User guide](docs/user-guide.md) · [Installation and configuration](docs/install.md) · [Privacy](docs/privacy.md)
- [Recording format](docs/recording-format.md) and its [JSON schema](docs/recording.schema.json) – the contract
  between the capture app and the transcribe part
- [.NET design of the capture app](docs/dotnet-design.md) · [Linux, Mac and tablets](docs/cross-platform-design.md) ·
  [Running on different hardware](docs/hardware-portability.md)

## Consent

Local capture is invisible to other participants. Tell them you are recording; [docs/privacy.md](docs/privacy.md)
has a note to send to colleagues.
