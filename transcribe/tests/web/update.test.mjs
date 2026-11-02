// Nápověda ▾ → Zkontrolovat aktualizace: the page asks the capture app (the tray) through the server, which drops a
// request file; the app answers in a state file. Here the test plays the capture app.
import assert from "node:assert/strict";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { openPage, server, waitFor } from "./page.mjs";

const OUT = process.env.RECORDINGZ_TEST_OUT;
const REQUEST = join(OUT, "recordingz-update.request"), STATE = join(OUT, "recordingz-update.json");

function capture(running) {
  writeFileSync(process.env.RECORDINGZ_TEST_CAPTURE, JSON.stringify({ app: "recordingz", running, recording: false,
                                                                     pid: Number(process.env.RECORDINGZ_TEST_PID) }));
}
/** the capture app: takes the next request and answers it */
async function answer(state, extra = {}) {
  await waitFor(() => existsSync(REQUEST), "the request to the capture app");
  const action = readFileSync(REQUEST, "utf8");
  unlinkSync(REQUEST);
  writeFileSync(STATE, JSON.stringify({ state, current: "2.0.6", available: null, message: "",
                                        at: new Date().toISOString().slice(0, 23), ...extra }));
  return action;
}
const line = p => p.doc.getElementById("updateLine");
/** until the server has seen the capture status file (waitFor does not wait for a promise) */
async function serverSees(running) {
  for (let i = 0; i < 200; i++) {
    if ((await server("/api/update")).running === running) return;
    await new Promise(r => setTimeout(r, 20));
  }
  throw new Error(`the server never saw the capture app ${running ? "running" : "stopped"}`);
}

test("check for updates from the help menu: newer version offered, installing asked; up to date", async t => {
  capture(true);
  await serverSees(true);
  const p = await openPage(t);
  p.$("helpBtn").click();
  p.$("helpMenu").querySelector('[data-help="update"]').click();
  await waitFor(() => p.doc.getElementById("aboutVersion"), "O aplikaci");
  assert.equal(await answer("available", { available: "2.1.0" }), "check", "the menu item checks right away");
  await waitFor(() => /K dispozici je verze 2\.1\.0 \(máte 2\.0\.6\)/.test(line(p).textContent), "the newer version");
  const install = p.doc.getElementById("updateInstall");
  assert.ok(!install.hidden && install.textContent === "Nainstalovat 2.1.0");
  install.click();
  assert.equal(await answer("installing", { available: "2.1.0", message: "Potvrďte instalaci v okně recordingz." }),
               "install");
  await waitFor(() => /Potvrďte instalaci/.test(line(p).textContent), "the app's word on installing");
  assert.ok(install.hidden);

  p.doc.getElementById("updateCheck").click();
  await answer("current");
  await waitFor(() => /Máte nejnovější verzi 2\.0\.6/.test(line(p).textContent), "up to date");
});

test("without the capture app running the page says so and asks nothing", async t => {
  capture(false);
  await serverSees(false);
  const p = await openPage(t);
  p.$("helpBtn").click();
  p.$("helpMenu").querySelector('[data-help="update"]').click();
  await waitFor(() => line(p) && /neběží/.test(line(p).textContent),
                () => `the reason (line: "${line(p) ? line(p).textContent : "none"}", help: ${p.$("helpMain").textContent.slice(0, 80)})`);
  assert.ok(!existsSync(REQUEST), "no request left for an app that is not there");
});
