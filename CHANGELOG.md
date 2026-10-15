# Changes

Notes for each release, newest first, written by hand for the people using it. The heading format is fixed
(`## <version> – <date>`); a test keeps an entry for the current version.

## 2.0.0 – 2026-10-15

- **teamsrec is now recordingz.** The recording app, the transcription and the review window are one project with
  one version, built and released together from [drzdez/recordingz](https://github.com/drzdez/recordingz).
- The update from teamsrec 1.x keeps everything: the settings (`%APPDATA%\teamsrec\teamsrec.toml` is copied to
  `%APPDATA%\recordingz\recordingz.toml`), the recordings folder, the API keys stored on the settings page and the
  `TEAMSREC_*_API_KEY` variables. The program moves to `%LOCALAPPDATA%\Programs\recordingz`; on its first start the
  review window prepares a new Python environment in `%LOCALAPPDATA%\recordingz` (the old `%LOCALAPPDATA%\teamsrec`
  can then be deleted).
- The local review API refuses requests from other web pages, and processing runs one job at a time (from 1.1.9).
