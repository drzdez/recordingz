// "Schůzka 1:1: <user> a …" before processing: a direct call's other side, prefilled from the call window and
// confirmed by one tick, goes with the job; the Přepis tab says who the call was with. A file of its own: it turns
// the fixture's unprocessed recording into a live 1:1 call.
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { BOARD_NEW, openPage, server, waitFor } from "./page.mjs";

test("a direct call: the other side prefilled, one tick confirms it for the processing", async t => {
  const dir = join(dirname(process.env.RECORDINGZ_TEST_CONFIG), "2026", "09", BOARD_NEW);
  const file = join(dir, `${BOARD_NEW}.json`);
  const sc = JSON.parse(readFileSync(file, "utf-8"));
  writeFileSync(file, JSON.stringify({ ...sc, source: "live", title: "Marek Hudec", title_source: "window", participants: [],
                                       tracks: { mic: { file: `${BOARD_NEW}_mic.wav` } } }));
  writeFileSync(join(dir, `${BOARD_NEW}_mic.wav`), "RIFF");
  await server("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ values: { "user.name": "Petra Malá" } }) });  // the mic is the user's (a new name: the server reloads)
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
