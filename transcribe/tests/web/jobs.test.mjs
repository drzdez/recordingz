// A background job seen through the server's event stream: started, finished, the page reloads on its own,
// and the history lists what happened, newest first.
import assert from "node:assert/strict";
import { test } from "node:test";
import { writeFileSync } from "node:fs";
import { BOARD_NEW, WEEKLY, openPage, waitFor } from "./page.mjs";

/** what the capture app writes; the test server watches this file (RECORDINGZ_TEST_CAPTURE) */
function writeCapture(status) {
  writeFileSync(process.env.RECORDINGZ_TEST_CAPTURE, JSON.stringify({ app: "recordingz", pid: Number(process.env.RECORDINGZ_TEST_PID),
                                                                     running: true, ...status }));
}

test("transcribing a new recording: live status over SSE, reload when done, history", async t => {
  const p = await openPage(t, BOARD_NEW);
  const go = await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe");
  assert.match(p.doc.querySelector("#main").textContent, /Provede se: přepis a rozpoznání mluvčích/);
  go.click();
  assert.match(p.$("status").textContent, /potvrďte/, "a long job asks first");
  assert.ok(!p.requests.some(r => r.includes("/process")), "nothing started by the first click");
  go.click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.doc.querySelector(".card"), "the page reloading the new transcript by itself");
  await waitFor(() => /^hotovo: přepis a titulky; zápis počká/.test(p.$("status").textContent), "the result in the status line");
  assert.ok(!p.$("quit").disabled, "the page is not busy any more");
  assert.ok(p.doc.querySelector("#main").textContent.includes("Nová nahrávka je přepsaná."));

  assert.equal(p.streams.length, 1, "the page listens to the event stream");
  assert.ok(!p.requests.some(r => r.startsWith("GET /api/status")), "no polling while the stream works");

  p.$("histBtn").click();
  const rows = [...p.$("events").querySelectorAll(".ev .tx")].map(e => e.textContent);
  assert.ok(!p.$("events").hidden);
  assert.match(rows[0], /zpracováno/, "newest first");
  assert.ok(rows.some(r => /zpracování spuštěno/.test(r)));
});

test("Uložit a přegenerovat zápis: pick the model, only that minutes is written, after the job's end event", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speaker cards");
  p.input(p.doc.querySelector('.side [data-key="nick"]'), "Kája");  // an unsaved change
  assert.ok(p.$("dirty").classList.contains("changed"));
  p.$("saveSum").click();
  const sel = await waitFor(() => p.$("minutesModel"), "the chooser of the model");
  assert.ok(!p.requests.some(r => r.startsWith("PUT ")), "nothing saved by opening it");
  await waitFor(() => [...sel.options].some(o => /zastaralý|aktuální/.test(o.textContent)), "this meeting's minutes listed");
  p.change(sel, "anthropic:claude-opus-5-5");
  p.$("minutesGo").click();
  await waitFor(() => p.$("dirty").classList.contains("clean"), "the names saved first");
  await waitFor(() => /zápis claude-opus-5-5 hotový/.test(p.$("status").textContent), "only that minutes, then done");
  assert.ok(!p.$("quit").disabled);
  assert.ok(p.requests.some(r => r.startsWith("POST ") && r.endsWith("/summaries")), "one minutes by the picked model");
  assert.ok(!p.requests.some(r => r.startsWith("GET /api/status")), "no polling while the stream works");
  const tabs = [...p.doc.querySelectorAll("#sumTabs .sumTab")];
  const old = tabs.find(tb => tb.querySelector("span").textContent === "test");
  assert.ok(old.classList.contains("stale") && /zastaralý/.test(old.textContent), "the older minutes is marked outdated");
  assert.ok(!tabs.find(tb => tb.querySelector("span").textContent === "claude-opus-5-5").classList.contains("stale"));
});

test("transcribing again from scratch: armed button, REST call, the page reloads the new speakers", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelectorAll(".card").length === 3, "the three speakers");
  const again = await waitFor(() => p.button("Přepsat znovu od nuly"), "the button");
  again.click();
  assert.ok(!p.$("quit").disabled, "the first click only arms it");
  p.button("Potvrdit nový přepis").click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.$("status").textContent === "hotovo: nový přepis, mluvčí jsou znovu k pojmenování", "the job done");
  assert.ok(!p.$("quit").disabled);
  assert.ok(p.requests.includes(`POST /api/recordings/${WEEKLY}/process`));
  await waitFor(() => p.doc.querySelector("#main").textContent.includes("Nová nahrávka je přepsaná."), "the new transcript");
});

test("while recordingz records, a red dot and the title show next to the status line", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  assert.ok(p.$("recBadge").hidden, "nothing recorded at first");
  writeCapture({ recording: true, title: "Plánování sprintu", stem: "2026-09-30_1827_planovani-sprintu",
                 source: "onsite", started: "2026-09-30T18:27:05" });
  await waitFor(() => !p.$("recBadge").hidden, "the red dot");
  assert.equal(p.$("recText").textContent, "Nahrávání probíhá · Plánování sprintu · od 18:27 · na místě");
  writeCapture({ recording: false });
  await waitFor(() => p.$("recBadge").hidden, "the dot gone when the recording ends");
});

test("a job started elsewhere (or before a reload) shows as running, a second one as queued", async t => {
  const base = process.env.RECORDINGZ_REVIEW_URL;
  const post = (path, body) => fetch(new URL(path, base), { method: "POST", headers: { "Content-Type": "application/json" },
                                                         body: JSON.stringify(body) }).then(r => r.json());
  // from scratch: the fake takes 2 s, so the page (slow to open on a busy machine) still finds it running
  const a = await post(`/api/recordings/${BOARD_NEW}/process`, { force: true });
  const b = await post(`/api/recordings/${WEEKLY}/summaries`, { provider: "anthropic", model: "claude-opus-5-5" });
  assert.equal(a.position, 1);
  assert.equal(b.position, 2, "queued, not refused");
  const p = await openPage(t, WEEKLY);  // like a reload while the jobs run
  // the second job runs 0.3 s: every text the line shows is kept, polling could miss it
  const seen = [];
  new p.window.MutationObserver(() => seen.push(p.$("jobText").textContent))
    .observe(p.$("jobText"), { childList: true, characterData: true, subtree: true });
  await waitFor(() => !p.$("jobBadge").hidden, "the running job on a page that did not start it");
  assert.match(p.$("jobText").textContent, /běží: .*nový přepis od nuly spuštěno · od \d\d:\d\d · ve frontě: 1/);
  await waitFor(() => p.$("jobBadge").hidden, "nothing left", 10000);
  assert.ok(seen.some(x => x.includes("zápis claude-opus-5-5 se generuje") && !x.includes("ve frontě")),
            `the queued one running next, seen: ${JSON.stringify(seen)}`);
});

test("the history closes with a click anywhere outside it, or Esc", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  p.$("histBtn").click();
  assert.ok(!p.$("events").hidden);
  p.$("events").dispatchEvent(new p.window.MouseEvent("click", { bubbles: true }));
  assert.ok(!p.$("events").hidden, "a click inside keeps it open");
  p.doc.querySelector(".card").dispatchEvent(new p.window.MouseEvent("click", { bubbles: true }));
  assert.ok(p.$("events").hidden, "a click elsewhere closes it");
  p.$("histBtn").click();
  p.doc.dispatchEvent(new p.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.ok(p.$("events").hidden, "Esc closes it");
});

test("an armed action goes back by itself or with Zrušit, and does nothing", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  const b = await waitFor(() => p.button("Přepsat znovu od nuly"), "an armed button");
  b.click();
  const cancel = b.nextElementSibling;
  assert.equal(cancel.textContent, "Zrušit");
  cancel.click();
  assert.equal(b.textContent, "Přepsat znovu od nuly");
  assert.equal(p.$("status").textContent, "", "the question leaves the status line");
  b.click();
  await waitFor(() => b.textContent === "Přepsat znovu od nuly", "back after 6 s", 12000);  // a 6 s timer: room for a busy machine
  assert.ok(!p.requests.some(r => r.startsWith("POST ") && r.endsWith("/process")), "nothing happened");

  p.$("saveSum").click();  // the minutes: a chooser, closed by Zrušit or Esc without doing anything
  await waitFor(() => p.$("minutesPick"), "the chooser");
  p.doc.dispatchEvent(new p.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.ok(!p.$("minutesPick"), "Esc closes it");
  assert.ok(!p.requests.some(r => r.startsWith("PUT ")), "nothing saved");
});

