// "Schůzka 1:1: <user> a …" before processing: a direct call's other side, prefilled from the call window and
// confirmed by one tick, goes with the job; the Přepis tab says who the call was with. A file of its own: it turns
// the fixture's unprocessed recording into a live 1:1 call.
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { BOARD_NEW, openPage, server, waitFor } from "./page.mjs";

const OUT = dirname(process.env.RECORDINGZ_TEST_CONFIG);

/** the fixture's unprocessed recording as a live call with Marek Hudec, recorded with the user's microphone */
async function makeCall() {
  const dir = join(OUT, "2026", "09", BOARD_NEW);
  const file = join(dir, `${BOARD_NEW}.json`);
  const sc = JSON.parse(readFileSync(file, "utf-8"));
  writeFileSync(file, JSON.stringify({ ...sc, source: "live", title: "Marek Hudec", title_source: "window", participants: [],
                                       tracks: { mic: { file: `${BOARD_NEW}_mic.wav` } } }));
  writeFileSync(join(dir, `${BOARD_NEW}_mic.wav`), "RIFF");
  await server("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ values: { "user.name": "Petra Malá" } }) });  // the mic is the user's (a new name: the server reloads)
}

test("a 1:1 call in the cloud: the local voices ticked for somebody without a voice print, off for one with it", async t => {
  await makeCall();
  mkdirSync(join(OUT, "_speakers"), { recursive: true });  // Petr Svoboda has a print, Marek Hudec is new
  writeFileSync(join(OUT, "_speakers", "voiceprints.json"), JSON.stringify({ format: 1, model: "m", people: {
    "petr-svoboda": [{ v: [1, ...Array(255).fill(0)], stem: "2026-09-02_0900_team-board", label: "SPEAKER_01" }] } }));
  await server("/api/secrets/elevenlabs", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: "k" }) });
  t.after(async () => {
    await server("/api/secrets/elevenlabs", { method: "DELETE" });
    rmSync(join(OUT, "_speakers", "voiceprints.json"));
  });
  const p = await openPage(t, BOARD_NEW);
  const m = (await waitFor(() => p.doc.querySelector("#main .procPanel"), "the processing offer"));
  const voices = m.querySelector("input.cloudVoicesBox"), why = m.querySelector(".voicesWhy");
  const name = m.querySelector("input.callWith");
  const type = text => { name.value = text; name.dispatchEvent(new p.window.Event("input", { bubbles: true })); };
  m.querySelector('input[name="procAsr"][value="cloud"]').click();
  assert.ok(!voices.checked && why.hidden, "not a 1:1 call yet: nothing chosen for the user");
  m.querySelector("input.callOnly").click();
  assert.ok(voices.checked, "Marek has no print: the local voices give him one");
  assert.equal(why.textContent, "Marek Hudec zatím hlasový otisk nemá – doplnění mu ho při zpracování vytvoří");
  type("petr  SVOBODA");
  assert.ok(!voices.checked, "Petr has one: a minute of GPU saved");
  assert.match(why.textContent, /už hlasový otisk má/);
  type("Péťa");
  assert.ok(!voices.checked, "by his nickname too");
  m.querySelector('input[name="procAsr"][value="local"]').click();
  assert.ok(why.hidden, "a local transcript has the voices anyway");
  m.querySelector('input[name="procAsr"][value="cloud"]').click();
  voices.click();
  type("Marek Hudec"); type("Petr Svoboda");
  assert.ok(voices.checked, "a box the user clicked stays as they set it");
  assert.match(m.querySelector(".procWhat").textContent, /mluvčí pak lokálně podle hlasových otisků/);
});

test("a direct call: the other side prefilled, one tick confirms it for the processing", async t => {
  await makeCall();
  const p = await openPage(t, BOARD_NEW);
  const call = await waitFor(() => p.doc.querySelector("#main .callBox"), "the call box");
  assert.match(call.textContent, /^Schůzka 1:1jen vy a jeden další\s*Petra Malá a/);
  assert.equal(call.querySelector("input.callWith").value, "Marek Hudec", "the other side from the call window");
  assert.ok(!call.querySelector("input.callOnly").checked, "not confirmed until ticked");

  p.doc.querySelector('#subtabs [data-sub="transcript"]').click();
  await waitFor(() => /^Nahrávka ještě nemá přepis\.Zpracování spustíte v záložce Mluvčí\.$/.test(p.$("docMain").textContent),
                "the Přepis tab says only where");
  p.$("toSpeakers").click();
  assert.ok(!p.$("main").hidden, "back on Mluvčí");

  const what = () => p.doc.querySelector("#main .procWhat").textContent;
  assert.match(what(), /titulky, zápis na tomto PC, jen když se poznají všichni mluvčí/);
  call.querySelector("input.callOnly").click();
  assert.match(what(), /titulky, druhá strana pojmenovaná jako Marek Hudec, zápis na tomto PC hned\.$/);
  call.querySelector("input.callWith").value = "";
  call.querySelector("input.callWith").dispatchEvent(new p.window.Event("input", { bubbles: true }));
  assert.ok(!call.querySelector("input.callOnly").checked, "no name: not confirmed");
  call.querySelector("input.callWith").value = "Marek Hudec";
  call.querySelector("input.callWith").dispatchEvent(new p.window.Event("input", { bubbles: true }));
  assert.ok(call.querySelector("input.callOnly").checked, "typing a name ticks it");
  const bodies = [];
  p.window.fetch = (orig => (url, init) => {
    if (String(url).endsWith("/process")) bodies.push(JSON.parse(init.body));
    return orig(url, init);
  })(p.window.fetch);
  const go = p.doc.querySelector("#main .procPanel button.primary-btn");
  go.click(); go.click();  // it asks first
  await waitFor(() => bodies.length, "the processing started");
  assert.equal(bodies[0].only_with, "Marek Hudec", "confirmed: only the user and Marek");
});
