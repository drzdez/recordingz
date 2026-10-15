// Processing a recording: one button, the transcript and the minutes each on this PC or in the cloud (defaults from
// the settings), "Provede se: …" following the choices; Uložit a přegenerovat zápis on a recording without a
// transcript starts the whole processing. A file of its own: it transcribes the fixture's only unprocessed recording.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, openPage, server, waitFor } from "./page.mjs";

const put = values => server("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ values }) });
const radio = (root, name, value) => root.querySelector(`input[name="${name}"][value="${value}"]`);
const catchBodies = (p, answer) => {
  const bodies = [];
  p.window.fetch = (orig => (url, init) => {
    if (!String(url).endsWith("/process")) return orig(url, init);
    bodies.push(JSON.parse(init.body));
    return answer ? Promise.resolve(new Response(JSON.stringify(answer), { headers: { "Content-Type": "application/json" } }))
                  : orig(url, init);
  })(p.window.fetch);
  return bodies;
};

test("one button; each step on this PC or in the cloud; a cloud step without its key is off, with the reason", async t => {
  const p = await openPage(t, BOARD_NEW);
  const go = await waitFor(() => p.doc.querySelector("#main .procPanel button.primary-btn"), "the processing offer");
  const main = p.$("main");
  assert.equal(go.textContent, "Přepsat a zpracovat");
  assert.ok(!main.querySelector("button.cloud-btn"), "no second button");
  assert.doesNotMatch(main.textContent, /není co kontrolovat/);
  assert.ok(radio(main, "procAsr", "local").checked && radio(main, "procMin", "local").checked, "the settings: all local");
  const panel = main.querySelector(".procPanel");
  assert.equal(panel.dataset.way, "local", "all local: 🖥 Lokální lit");
  assert.ok(radio(main, "procAsr", "cloud").closest(".col-cloud") && radio(main, "procAsr", "local").closest(".col-local"),
            "this PC on the left, the cloud on the right");
  assert.ok(radio(main, "procAsr", "cloud").disabled && /chybí klíč ElevenLabs/.test(main.textContent), "no key: off, says why");
  assert.ok(radio(main, "procMin", "cloud").disabled && /chybí klíč Claude/.test(main.textContent));
  const what = () => main.querySelector(".procWhat").textContent;
  assert.match(what(), /^Provede se: přepis a rozpoznání mluvčích na tomto PC \(zhruba \d+ min, grafická karta bude vytížená\), titulky, zápis na tomto PC, jen když se poznají všichni mluvčí/);
  radio(main, "procMin", "off").click();
  assert.match(what(), /titulky, bez zápisu/);
  assert.ok(main.querySelector("input.minutesAnywayBox").disabled, "no minutes: 'zápis hned' does not apply");
  radio(main, "procMin", "local").click();
  main.querySelector("input.minutesAnywayBox").click();
  assert.match(what(), /zápis na tomto PC hned, i když se nepoznají všichni mluvčí/);
  main.querySelector("input.minutesAnywayBox").click();

  for (const name of ["elevenlabs", "anthropic"])
    await server(`/api/secrets/${name}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: "k" }) });
  t.after(async () => { for (const name of ["elevenlabs", "anthropic"]) await server(`/api/secrets/${name}`, { method: "DELETE" }); });
  const q = await openPage(t, BOARD_NEW);
  const m = (await waitFor(() => q.doc.querySelector("#main .procPanel"), "with the keys")).parentElement;
  const way = () => m.querySelector(".procPanel").dataset.way;
  m.querySelector('.procHead[data-col="cloud"]').click();
  assert.ok(radio(m, "procAsr", "cloud").checked && radio(m, "procMin", "cloud").checked, "the heading picks its column");
  assert.equal(way(), "cloud", "⚡ Rychlé lit");
  assert.ok(!m.querySelector("input.compareBox") || !m.querySelector("input.compareBox").checked);
  radio(m, "procMin", "local").click();
  assert.equal(way(), "", "mixed: neither");
  m.querySelector('.procHead[data-col="local"]').click();
  assert.equal(way(), "local");
  const voices = m.querySelector("input.cloudVoicesBox");
  assert.ok(voices.disabled, "the local voices: only for a cloud transcript");
  radio(m, "procAsr", "cloud").click();
  assert.ok(!voices.disabled);
  assert.match(m.querySelector(".procWhat").textContent,
               /přepis i mluvčí v cloudu \(ElevenLabs – zvuk schůzky odejde z počítače\), titulky, zápis na tomto PC, až pojmenujete mluvčí \(cloud je nepozná\)/);
  radio(m, "procMin", "cloud").click();
  const bodies = catchBodies(q, { ok: false, error: "jen zkouška" });
  const b = m.querySelector(".procPanel button.primary-btn");
  b.click();
  assert.match(q.$("status").textContent, /zvuk schůzky půjde do ElevenLabs, text přepisu do Claude/, "the second click asks what leaves");
  b.click();
  await waitFor(() => bodies.length, "asked to process");
  assert.deepEqual([bodies[0].transcribe, bodies[0].minutes_with, bodies[0].minutes], ["cloud", "cloud", true]);
});

test("without a transcript no Uložit below, only the panel's button; once processed, the names are saved below", async t => {
  const p = await openPage(t, BOARD_NEW);
  const go = await waitFor(() => p.doc.querySelector("#main .procPanel button.primary-btn"), "a recording without a transcript");
  assert.ok(p.$("speakerButtons").hidden, "no Uložit / Uložit a přegenerovat zápis: nothing to save or regenerate yet");
  go.click(); go.click();  // it asks first
  await waitFor(() => p.requests.some(r => r === `POST /api/recordings/${BOARD_NEW}/process`), "the processing started");
  await waitFor(() => p.doc.querySelector(".card"), "the new speakers are shown");
  assert.ok(!p.$("speakerButtons").hidden, "the names: saved below");
});

test("Přepsat znovu od nuly: folded, the same choices; nevytvářet goes without minutes", async t => {
  const p = await openPage(t, BOARD_NEW);  // transcribed by the test above
  const again = await waitFor(() => p.button("Přepsat znovu od nuly"), "the re-transcribe bar");
  const bar = again.closest("details.redoBar");
  assert.ok(bar && !bar.open, "folded under its summary");
  const anyway = bar.querySelector("input.minutesAnywayBox");
  assert.ok(radio(bar, "procMin", "local").checked && !anyway.disabled, "minutes by [summarize] enabled");
  radio(bar, "procMin", "off").click();
  assert.ok(anyway.disabled, "no minutes: 'zápis hned' does not apply");
  const bodies = catchBodies(p);
  again.click(); again.click();  // it asks first
  await waitFor(() => bodies.length, "the processing started");
  assert.deepEqual([bodies[0].minutes, bodies[0].force, bodies[0].transcribe], [false, true, "local"]);
  await waitFor(() => /hotovo: nový přepis/.test(p.$("status").textContent), "done");
});

test("srovnávací zápis: offered with comparison models, ticked with local minutes; unticked, this run goes without", async t => {
  await put({ "summarize.compare": ["ollama:qwen3:8b"] });
  t.after(() => put({ "summarize.compare": [] }));
  const p = await openPage(t, BOARD_NEW);
  const again = await waitFor(() => p.button("Přepsat znovu od nuly"), "the re-transcribe bar");
  const bar = again.closest("details.redoBar");
  const cmp = bar.querySelector("input.compareBox");
  assert.ok(cmp && cmp.checked, "offered, ticked: the minutes are local");
  assert.match(bar.querySelector(".procWhat").textContent, /srovnávací zápis \(Ollama qwen3:8b\)/);
  radio(bar, "procMin", "off").click();
  assert.ok(cmp.disabled, "no minutes: no comparison either");
  radio(bar, "procMin", "local").click();
  cmp.click();
  assert.doesNotMatch(bar.querySelector(".procWhat").textContent, /srovnávací/);
  const bodies = catchBodies(p);
  again.click(); again.click();
  await waitFor(() => bodies.length, "the processing started");
  assert.equal(bodies[0].compare, false, "this run without the comparison");
  await waitFor(() => /hotovo: nový přepis/.test(p.$("status").textContent), "done");
});

test("Vytvářet alternativní zápis pro porovnání off: srovnávací zápis offered unticked, ticked it is sent", async t => {
  await put({ "summarize.compare": ["ollama:qwen3:8b"], "summarize.compare_auto": false });
  t.after(() => put({ "summarize.compare": [], "summarize.compare_auto": true }));
  const p = await openPage(t, BOARD_NEW);
  const again = await waitFor(() => p.button("Přepsat znovu od nuly"), "the re-transcribe bar");
  const bar = again.closest("details.redoBar");
  const cmp = bar.querySelector("input.compareBox");
  assert.ok(radio(bar, "procMin", "local").checked && cmp && !cmp.checked, "local minutes, yet not ticked beforehand");
  radio(bar, "procMin", "off").click(); radio(bar, "procMin", "local").click();
  assert.ok(!cmp.checked, "nor when the minutes come back to local");
  cmp.click();
  const bodies = catchBodies(p, { ok: false, error: "jen zkouška" });
  again.click(); again.click();
  await waitFor(() => bodies.length, "asked to process");
  assert.equal(bodies[0].compare, true, "ticked: the comparison for this run");
});
