// Release notes: "what is new" once after an install or update (closing it marks the version seen), and every
// release's notes in Nápověda → O aplikaci next to the version. A file of its own: it starts with nothing seen.
import assert from "node:assert/strict";
import { test } from "node:test";
import { openPage, server, sleep, waitFor } from "./page.mjs";

test("what is new: shown once with the running version's notes, not again after closing", async t => {
  const notes = await server("/api/release-notes");
  assert.equal(notes.unseen.length, 1, "nothing seen yet: the running version only");
  const p = await openPage(t);
  const box = p.$("whatsNew");
  await waitFor(() => !box.hidden, "the panel opens by itself");
  assert.equal(p.$("whatsNewTitle").textContent, `Co je nového ve verzi ${notes.version}`);
  assert.deepEqual([...p.$("whatsNewMain").querySelectorAll("h2")].map(h => h.firstChild.textContent.trim()), [notes.version]);
  const items = p.$("whatsNewMain").querySelectorAll("li").length;
  assert.ok(items > 1, "the notes as a list");
  assert.equal(items, notes.unseen[0].markdown.split("\n").filter(l => l.startsWith("- ")).length,
               "an item wrapped onto more lines stays one item");
  p.$("whatsNewClose").click();
  assert.ok(box.hidden);
  await waitFor(() => p.requests.includes("POST /api/release-notes/seen"), "closing marks the version seen");
  await sleep(100);
  assert.deepEqual((await server("/api/release-notes")).unseen, []);

  const again = await openPage(t);
  await waitFor(() => again.requests.includes("GET /api/release-notes"), "the page asks again");
  await sleep(200);
  assert.ok(again.$("whatsNew").hidden, "not shown a second time");
});

test("help → O aplikaci: the version and the notes of every release", async t => {
  const notes = await server("/api/release-notes");
  const p = await openPage(t);
  p.$("helpBtn").click();
  p.change(p.$("helpPick"), "about");
  const main = p.$("helpMain");
  await waitFor(() => main.querySelector("#aboutVersion"), "the about page");
  assert.equal(main.querySelector("#aboutVersion").textContent, notes.version);
  assert.deepEqual([...main.querySelectorAll(".releaseNote h2")].map(h => h.firstChild.textContent.trim()),
                   notes.notes.map(n => n.version));
  assert.ok(notes.notes.length >= 2, "older releases too");
});
