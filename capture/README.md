# recordingz-capture (.NET)

The capture app of recordingz: a .NET 10 port of an earlier Python prototype (no longer kept). It is a Windows tray app
that:

- notices a Teams call, or a call in Zoom, Webex, Slack, a browser meeting and similar apps;
- records the system sound (WASAPI loopback: the other people) and your microphone as separate 16-bit WAV
  tracks;
- records a low-fps video of each Teams window, used later for name labels;
- writes the sidecar `<stem>.json` defined in [docs/recording-format.md](../docs/recording-format.md).
  The transcribe part ([`transcribe/`](../transcribe/README.md)) processes the recording from there.

It also records on-site meetings (room microphone only), playback of stored recordings (system sound only)
and manual recordings. It has a local settings page.

Design and reasons: [docs/dotnet-design.md](../docs/dotnet-design.md).
The review that completed the port from the prototype (historical): [PARITY.md](PARITY.md).

## Requirements

- Windows 10/11 and the .NET 10 SDK (`net10.0-windows`, WinForms).
- NuGet: NAudio 2.2.1 (WASAPI), Tomlyn 2.10.1 (TOML). Both are restored automatically.
- ffmpeg is optional but recommended. Without it there is no `_mix.wav` and no window video. It is looked for
  on `PATH`, in `%RECORDINGZ_FFMPEG_DIR%`, and in the winget install folder
  (`winget install Gyan.FFmpeg`).
- Classic Outlook is optional, for meeting titles and participants (COM; the new Outlook has no COM).

## Build and test

```powershell
cd capture
dotnet build Recordingz.Capture.slnx
dotnet test  Recordingz.Capture.slnx
```

The tests (xUnit) cover the pure logic: detection rules, calendar matching, watchdog/backoff,
sidecar format, naming and rename, the TOML editor, the on-site device policy, updates, the installer's folder and
the takeover from teamsrec 1.x. They need no audio device, Outlook, Teams or ffmpeg.

## Installer (MSI) and releases

```powershell
powershell -File installer\build-msi.ps1   # from the repo root -> installer\bin\x64\Release\en-US\recordingz-<version>-x64.msi
```

The script publishes a self-contained single-file exe (`artifacts\publish`, no .NET runtime needed on the target)
and builds the WiX 6 project in `installer\`: a **per-user** MSI (no admin rights) into
`%LOCALAPPDATA%\Programs\recordingz`, with a Start menu shortcut and a Startup shortcut (autostart). A newer
MSI upgrades an older one in place. This is the capture app alone; `-Suite` builds the whole suite (see the
[repository README](../README.md#build-and-test)).

The installer has one question, the **recordings folder** (`FolderUI.wxs`), prefilled so a user just clicks Next: the
folder the app last used, else `%USERPROFILE%\meetings` (in the profile, writable without admin rights; also the app's
default without any config). The choice goes to `HKCU\Software\recordingz\capture\OutDir`; the app writes it once into
`[recordings] out_dir` of the shared TOML (`Config/InstallerFolder.cs`), after that the TOML wins (Nastavení). An
upgrade skips the question and keeps the folder. `msiexec /i … /qn OUTDIR=E:\rec` sets it silently. If the folder
cannot be created (a missing drive), the app records into the default and logs why. Before replacing the files it asks a running app to quit by dropping
`quit.request` next to the exe (`App/QuitRequest.cs`) and waits up to 90 s; the app refuses during a recording and the
install stops with a message. After the install it starts the app again (`msiexec /i … LAUNCHAPP=0` skips that).

**Updates** (`App/Updater.cs`): at start (after a minute) and then once a day, unless `[capture] update_check = false`,
the app asks `api.github.com/repos/drzdez/recordingz/releases/latest` (public, no token). A higher version
with an `-x64.msi` asset shows **Install version X…** in the tray menu and a balloon (not for a declined version, and
not during a recording – then right after it); **Check for updates** asks at once. Installing: a confirmation, the
MSI downloaded to `%TEMP%` from the releases of drzdez/recordingz only, its size and SHA-256 checked against the release's
`digest`, then `msiexec /i … /passive` – the MSI quits the app and starts the new version. The last check and a
declined version are kept in `HKCU\Software\recordingz\capture` (`UpdateLastCheck`, `UpdateDeclined`).

The version is `<Version>` in `Recordingz.Capture.csproj` (the app reads it from its assembly). GitHub Actions
(`.github/workflows/build.yml` at the repo root) runs the tests and builds the suite MSI on every push to `main`; pushing a
tag `v<version>` also publishes a GitHub Release with the MSI:

```powershell
git tag v<version>; git push origin v<version>
```

## Run

```powershell
.\src\Recordingz.Capture\bin\Debug\net10.0-windows\recordingz-capture.exe
```

The app holds a single-instance mutex (`Local\recordingz`), so only one instance records at a time. A second
start writes `recordingz is already running, exiting` to the log and quits.

Everything goes to `<out_dir>\recordingz.log` (over 5 MB it is moved to `recordingz.log.1` at start).
On startup the app also recovers recordings that a crash left without a sidecar (`Recording/Orphans.cs`).

## Configuration

The config file is `%APPDATA%\recordingz\recordingz.toml`, or the path in `%RECORDINGZ_CONFIG%`. It is shared with
the transcribe part. A missing or broken file gives the defaults. The settings page
(tray → Settings…) edits it in place and keeps comments and every key it does not manage.

```toml
[recordings]
out_dir = 'D:\meetings'        # recordings go to <out_dir>\YYYY\MM\<stem>\ (default ~/meetings)

[calendar]
outlook = false                # classic Outlook (COM): meeting title + participants

[user]
name = ""

[capture]
prompt_default = "record"      # record = just notify | ask = discard box (keeps on timeout) | skip = box, discards on timeout
onsite_mic = ""                # part of the input device name for on-site meetings ("" = Windows default input)
device_missing = "ask"         # ask | fail | fallback: when onsite_mic is not available
onsite_offer = "never"         # never | calendar | always: record a calendar meeting on site when it starts
onsite_upgrade = true          # an on-site meeting that turns into a Teams call switches to a live recording
onsite_silence_min = 10        # on site: after this many minutes without speech ask "still running?"; no answer in 5 min = stop & keep; 0 = never ask
other_apps = "record"          # record | off: calls outside Teams
tray_open = "app"              # app | web: what a double click on the tray icon opens (the review window or the browser)
review_app = ""                # recordingz-review.exe; "" = the one next to this exe, else %LOCALAPPDATA%\Programs\recordingz-review\
update_check = true            # look for a new release once a day and offer it
```

Fixed constants: poll every 3 s, 10 s call-end grace, 5 s minimum
length, 4 h cap, 30 s playback silence stop, 45 s discard box, 2 fps / 1600×900 window video; on site a warning
when the microphone hears no speech in the first 3 min (speech = RMS above ~-42 dBFS over half a second).

## Output

`<out_dir>\YYYY\MM\<stem>\<stem>_sys.wav`, `_mic.wav`, `_mix.wav` (mono 16 kHz), `_screen<N>.mp4`, and `<stem>.json`.
The stem is `YYYY-MM-DD_HHMM_<slug>`. The sidecar is written last; its existence means the recording is
complete. The full format is in [docs/recording-format.md](../docs/recording-format.md).

## Source layout

```
capture/
  Recordingz.Capture.slnx
  src/Recordingz.Capture/
    Program.cs            entry: config, log, shared mutex, orphan recovery, tray message loop
    Core/Types.cs         shared types: IClock, INotifier, TrackInfo, CalendarItem, ScreenInfo, IAudioSource, Log ...
    App/                  MonitorLoop (3 s state machine + AppLogic pure rules), TrayApp (icon, menu, notifier),
                          Dialogs (MessageBoxTimeout yes/no + discard box, title input), FileLog, Updater,
                          QuitRequest (the installer's quit), ReviewJobs, Presence
    Detection/            CallDetector (join screen, meeting titles, other apps), MicUsers (ConsentStore),
                          WindowTitles (EnumWindows)
    Audio/                Recorder (loopback + mic → WAV, reopen, watchdog fields), Devices (inputs, unavailable,
                          capture sessions → call microphone), MicTest
    Recording/            Watchdog (stall/silent/dead mic, reopen backoff, follow call mic), Finalizer (mix,
                          title fix-up, calendar re-match, rename, sidecar), Mixer (ffmpeg), Orphans (crash recovery)
    Calendar/             Outlook (COM items, pick by title/time, candidates, difflib ratio)
    Screen/               ScreenCapture (PrintWindow per Teams window → ffmpeg x264), ScreenCaptureProcess (child process)
    Contract/             Sidecar (JSON model + writer), Naming (slug, stem, folders, rename)
    Config/               AppConfig (TOML load), ConfigWatcher (reload on change), SettingsModel (page fields +
                          validation), TomlEditor (in-place edit), InstallerFolder, LegacyMigration (from teamsrec 1.x)
    Settings/             SettingsServer (127.0.0.1 HttpListener + JSON API), settings.html (the fallback settings page)
  tests/Recordingz.Capture.Tests/
    xUnit tests per area (App, Audio, Calendar, Config, Contract, Detection, Finalizer, Updater, Watchdog ...)
```

## With the review page

- Edits of recordingz.toml (for example from the review page's Nastavení) apply without a restart
  (`Config/ConfigWatcher.cs`); only a new recordings folder waits for the next start.
- A double click on the tray icon opens the review page through `recordingz-review.exe`, in its window or in the
  browser as `[capture] tray_open` says; a click on the "Saved …" balloon opens it on that recording (`--open <stem>`).
  **Settings…** opens the page's Nastavení; the app's own settings page is only the fallback without
  recordingz-review.exe.
- On every change the app writes `%TEMP%\recordingz-capture.json` (recording or not, title, start, pid), so the review
  page shows a red dot "Nahrávání probíhá" while it records.

Changes per release: [CHANGELOG.md](../CHANGELOG.md).
