# Changes

Notes for each release, newest first, written by hand for the people using it. The heading format is fixed
(`## <version> – <date>`); a test keeps an entry for the current version.

## 2.0.5 – 2026-10-16

- **Test mikrofonu** (tray and Nastavení) judges speech the way the recording does, by the loudness of each half second
  instead of the loudest click: it says "slyší řeč", "jen šum, žádná řeč" (room noise only – speak during the test)
  or "TICHO" (the microphone delivers nothing). The test takes 3 s.
- **Schůzka 1:1** with a cloud transcript: "doplnit mluvčí lokálně podle hlasových otisků" is ticked beforehand when
  the other side has no voice print yet (the local voices give them one) and left off when they have one (a minute of
  the graphics card saved, the minutes sooner). A line under it says which; a box you click stays as you set it.

## 2.0.3 – 2026-10-16

- **What's new** after an update: the review window shows these notes once, and Nápověda → "O aplikaci a co je
  nového" keeps the version and all releases.
- On-site recordings watch the microphone: when it hears no speech in the first 3 minutes, the app says so (is the
  laptop at the table, the microphone on?). After 10 minutes without speech it asks "Schůzka ještě běží?"; with
  no answer within 5 minutes the recording stops and is kept. The minutes are set in Nastavení → Nahrávání
  (0 = never ask).
- A recording without speech no longer gets a transcript invented from the meeting's title and participants: such
  replies are dropped, the transcript says "bez řeči" and no minutes are written for it.

## 2.0.2 – 2026-10-16

- A recording whose sound can no longer be written (full disk, lost network folder) now warns loudly at once and
  keeps warning; before, the tray looked fine and the sound piled up in memory until the recording ended.
- Starting a recording no longer freezes the tray icon while the devices open (slow on a network folder).
- Recovering recordings left by a crash goes on past a damaged one, and a too-short leftover deletes only its own
  sound files, not the whole folder.
- Updates: a stalled download stops after a minute instead of hanging, a broken or old installer is removed from
  the temp folder, and an update prompt that timed out is offered again (only "No" skips that version).
- Names, voice prints and transcripts are saved safely: a save during processing no longer loses the other one's
  change, and a file is never seen half-written. The settings file is written the same way by both apps.
- Renaming a recording that is being processed or recorded is refused, and a rename that fails half-way is undone.
- The review page's API takes only recording names (no paths), and an unexpected error is reported to the page
  instead of dropping the connection. A review server nobody opens stops on its own.
- Processing says the same on every tab (Mluvčí, Přepis, Zápis), in the words of the processing panel.
- The capture log is rotated at 5 MB, and the bundled ffmpeg is preferred to one found on PATH.
- The installer finds a running teamsrec 1.x capture app again and asks it to quit before the update (a typo in its
  path made it miss it).
- The documentation describes the installed suite (default folder `%USERPROFILE%\meetings`, the MSI, the wizard)
  instead of the early development setup.

## 2.0.1 – 2026-10-15

- The capture app's log now says what it took over from teamsrec 1.x on the first start (the settings and the
  registry values); before, those lines were lost because they came before the log was open.

## 2.0.0 – 2026-10-15

- **teamsrec is now recordingz.** The recording app, the transcription and the review window are one project with
  one version, built and released together from [drzdez/recordingz](https://github.com/drzdez/recordingz).
- The update from teamsrec 1.x keeps everything: the settings (`%APPDATA%\teamsrec\teamsrec.toml` is copied to
  `%APPDATA%\recordingz\recordingz.toml`), the recordings folder, the API keys stored on the settings page and the
  `TEAMSREC_*_API_KEY` variables. The program moves to `%LOCALAPPDATA%\Programs\recordingz`; on its first start the
  review window prepares a new Python environment in `%LOCALAPPDATA%\recordingz` (the old `%LOCALAPPDATA%\teamsrec`
  can then be deleted).
- The local review API refuses requests from other web pages, and processing runs one job at a time (from 1.1.9).
