// Do koše / Smazat hned / the "v koši" filter: a trashed meeting leaves the list, shows only under the filter with
// the days it has left, comes back with Obnovit; deleting for good asks twice. A file of its own: it deletes.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, BOARD_OLD, WEEKLY, openPage, server, waitFor } from "./page.mjs";

const options = p => [...p.$("pick").options].map(o => o.textContent);
const stateBtn = p => [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("🗑 v koši"));

test("trash: a meeting without a transcript into the trash, seen under 'v koši', restored", async t => {
  const p = await openPage(t, BOARD_NEW);
  const bin = await waitFor(() => p.$("toTrash"), "Do koše under the offer to transcribe");
  assert.ok(p.$("deleteNow"), "and Smazat hned");
  assert.ok(!stateBtn(p), "an empty trash: no filter button");
  bin.click();
  assert.match(p.$("status").textContent, /půjde do koše/, "the first click only asks");
  assert.ok(options(p).some(o => o.includes("Team board") && o.startsWith("2026-09-03")), "nothing moved yet");
  bin.click();
  await waitFor(() => /je v koši/.test(p.$("status").textContent), "trashed");
  assert.ok(!options(p).some(o => o.startsWith("2026-09-03")), "out of the list");
  assert.notEqual(p.$("pick").value, BOARD_NEW, "another meeting is open");

  stateBtn(p).click();
  await waitFor(() => p.doc.querySelector("#main .trashBox"), "the trashed meeting, opened");
  assert.deepEqual(options(p), ["🗑 2026-09-03 09:00 – Team board [smaže se za 30 dní]"], "only the trash, days left");
  assert.match(p.$("main").textContent, /je v koši – smaže se za 30 dní/);
  p.$("trashRestore").click();
  await waitFor(() => /obnoveno z koše/.test(p.$("status").textContent), "restored");
  assert.equal(p.$("pick").value, BOARD_NEW, "the restored meeting is open");
  await waitFor(() => !stateBtn(p), "the trash is empty again");  // a list refresh begun before the restore may land last
  assert.ok((await server("/api/recordings")).some(r => r.stem === BOARD_NEW));
});

test("trash: emptying it and deleting at once both ask first and go for good", async t => {
  const p = await openPage(t, BOARD_OLD);
  const bin = await waitFor(() => p.$("toTrash"), "Do koše under the speakers");
  bin.click(); bin.click();
  await waitFor(() => /je v koši/.test(p.$("status").textContent), "trashed");
  stateBtn(p).click();
  const empty = await waitFor(() => p.$("trashEmpty"), "Vysypat koš");
  assert.match(empty.textContent, /Vysypat koš \(1\)/);
  empty.click();
  assert.equal((await server("/api/trash")).items.length, 1, "the first click deletes nothing");
  empty.click();
  await waitFor(() => /koš vysypán: 1/.test(p.$("status").textContent), "emptied");
  assert.equal((await server("/api/trash")).items.length, 0);
  assert.ok(!stateBtn(p), "no trash filter any more");

  p.$("pick").value = WEEKLY; p.$("pick").dispatchEvent(new p.window.Event("change"));
  const del = await waitFor(() => p.$("deleteNow"), "Smazat hned");
  del.click();
  assert.match(p.$("status").textContent, /bez koše/);
  del.click();
  await waitFor(() => /smazáno natrvalo/.test(p.$("status").textContent), "deleted");
  const left = (await server("/api/recordings")).map(r => r.stem);
  assert.ok(!left.includes(WEEKLY) && !left.includes(BOARD_OLD));
  assert.equal((await server("/api/trash")).items.length, 0, "not in the trash either");
});
