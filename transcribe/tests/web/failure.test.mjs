// A failed processing stays in sight: a banner pinned with the sub-tabs (Mluvčí, Přepis, Zápis), also after the page
// is opened again, with what to do when a cloud service refused its key; it goes with Skrýt or a run that succeeds.
// (2026-11-02: the ElevenLabs 401 was only in the status line, gone with the next message.)
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { BOARD_NEW, openPage, server, waitFor } from "./page.mjs";

const OUT = process.env.RECORDINGZ_TEST_OUT;
const failNext = (stem, text) =>
  writeFileSync(join(OUT, stem.slice(0, 4), stem.slice(5, 7), stem, `${stem}.fail-next`), text, "utf8");
const REFUSED = 'error: ElevenLabs 401: {"detail":{"type":"authentication_error","code":"unauthorized","message":"Invalid API key"}}';

async function processNow(p) {
  const go = await waitFor(() => p.doc.querySelector("#main .procPanel button.primary-btn"), "the processing offer");
  go.click(); go.click();  // it asks first
}

test("a failed processing: a banner on every tab, kept after reopening, with the key hint; Skrýt removes it", async t => {
  failNext(BOARD_NEW, REFUSED);
  const p = await openPage(t, BOARD_NEW);
  await processNow(p);
  const banner = await waitFor(() => p.doc.getElementById("failBanner"), "the failure banner");
  assert.ok(p.$("subtabs").contains(banner), "pinned with the sub-tabs, not inside one tab");
  assert.match(banner.querySelector(".state").textContent, /⚠ zpracování selhalo/);
  assert.match(banner.querySelector(".hint").textContent, /ElevenLabs odmítl klíč API.*Nastavení → Klíče API/);
  assert.match(banner.querySelector(".why").textContent, /^ElevenLabs 401: .*Invalid API key/, "the service's words, without 'error:'");
  assert.ok([...banner.querySelectorAll("button")].some(b => b.textContent === "Otevřít Nastavení"));
  for (const tab of ["transcript", "summary", "speakers"]) {
    const b = p.doc.querySelector(`#subtabs [data-sub="${tab}"]`);
    if (b) b.click();
    assert.ok(p.doc.getElementById("failBanner"), `still there on ${tab}`);
  }

  const again = await openPage(t, BOARD_NEW);  // the window or the server restarted
  const kept = await waitFor(() => again.doc.getElementById("failBanner"), "the banner after reopening");
  [...kept.querySelectorAll("button")].find(b => b.textContent === "Skrýt").click();
  await waitFor(() => !again.doc.getElementById("failBanner"), "the banner hidden");
  assert.equal((await server(`/api/recordings/${BOARD_NEW}`)).failure, null, "hidden for good");
});

test("a run that succeeds clears the failure; a failure without a known cause has no hint", async t => {
  failNext(BOARD_NEW, "error: disk full");
  const p = await openPage(t, BOARD_NEW);
  await processNow(p);
  const banner = await waitFor(() => p.doc.getElementById("failBanner"), "the failure banner");
  assert.ok(!banner.querySelector(".hint") && !/Otevřít Nastavení/.test(banner.textContent), "no advice to guess");
  await processNow(p);
  await waitFor(() => p.$("status").textContent.startsWith("hotovo"), "the second run done");
  assert.ok(!p.doc.getElementById("failBanner"), "gone once processed");
  assert.equal((await server(`/api/recordings/${BOARD_NEW}`)).failure, null);
});
