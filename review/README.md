# recordingz review window (Tauri)

The review page as a desktop window. It is a thin shell on purpose: the page (`transcribe/src/recordingz/web/index.html`)
and its REST + Server-Sent Events server (`transcribe/src/recordingz/web/review.py`) are exactly the ones the browser uses, so every feature is
written once and works in both.

What the shell does (`src-tauri/src/main.rs`, `env.rs`):

1. finds the running review server through its lock file (`%TEMP%\recordingz-review.json`), or starts one without a
   browser (`recordingz review --no-browser`): `RECORDINGZ_TRANSCRIBE_CMD` if set, else the installed suite's
   Python environment, else `transcribe\bin\recordingz.cmd` of the checkout it was built from, else `recordingz` on
   PATH. In the installed suite (`transcribe\` and `uv.exe` next to the exe) the first start, and the first start
   after an update, builds that environment with uv in `%LOCALAPPDATA%\recordingz\env`, with uv's progress in the
   window;
2. shows the page in a native window (WebView2) with `?app=desktop`, which tells the page it runs here;
3. opens links to anything but the server (help on GitHub, links in minutes) in the default browser;
4. when the window closes, stops the server it started itself (a server that was already running, for example for a
   browser tab, keeps running). While a job runs the server refuses and ends by itself after the job.

`recordingz-review.exe --browser` opens the page in the default browser instead (no window) and exits; a second start
of the window only brings the open one to the front. `--settings` opens the page's Nastavení (the tray's Settings…),
`--wizard` its setup wizard (the tray's Setup wizard…), `--open <stem>` one recording (a click on the capture app's "Saved …" balloon; the open window switches to it). The tray icon of the capture app starts one or the other on a
double click, as `[capture] tray_open` (`app` | `web`) says.

What the page does differently with `?app=desktop`: no Zavřít button (closing the window does it) and no
`target="_blank"` on links (the window routes them). Covered by `transcribe/tests/web/desktop.test.mjs`.

The page gets one Tauri call, nothing else: when a warning appears while the window is not in front (a recording
started while processing runs, the server is gone), it flashes the window in the taskbar
(`requestUserAttention`). `withGlobalTauri` exposes `window.__TAURI__` and `src-tauri/capabilities/review-page.json`
allows only that permission, only for pages from `http://127.0.0.1`.

## Build and run

Needs Rust (rustup, MSVC toolchain), Node.js and WebView2 (part of Windows 11).

```
cd review
npm install
npm run build          # -> src-tauri\target\release\recordingz-review.exe
npm run dev            # debug build with a console
cargo test --manifest-path src-tauri\Cargo.toml
```

The icon set in `src-tauri/icons` is generated from `icon-source.png` with `npm run icons`. The build is an .exe
only (`bundle.active = false`); the suite MSI (`installer/` at the repository root) installs it next to the capture app.
