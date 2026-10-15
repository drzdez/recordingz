// "Uložit jako…" in the Zápis tab: a copy of the minutes into a folder remembered by the meeting name.
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

test("save a copy, and the next time the same folder is offered", async t => {
  const folder = mkdtempSync(join(tmpdir(), "recordingz-export-"));
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speakers");
  p.doc.querySelector('#subtabs [data-sub="summary"]').click();
  const exp = await waitFor(() => p.$("sumExport"), "the Uložit jako button");
  p.window.fetch = (orig => (url, init) => String(url).includes("pick-folder")  // no Windows dialog in a test
    ? Promise.resolve(new Response(JSON.stringify({ folder }), { headers: { "Content-Type": "application/json" } }))
    : orig(url, init))(p.window.fetch);
  exp.click();
  await waitFor(() => p.$("exportFolder") && p.$("exportFolder").value === folder, "first time: the folder dialog");
  p.button("Uložit", p.$("exportBox")).click();
  await waitFor(() => /kopie (zápisu )?uložena/.test(p.$("status").textContent), "saved").catch(e => { throw new Error(e.message + " – status: " + p.$("status").textContent); });
  assert.equal(readdirSync(folder).length, 1);
  assert.match(readdirSync(folder)[0], /^\d{4}-\d{2}-\d{2} \d{4} /, "date and time first, to sort");
  const again = await waitFor(() => p.$("sumExportAgain"), "Uložit jako minule");
  assert.match(again.title, new RegExp(folder.replace(/\\/g, "\\\\")));
  again.click();
  await waitFor(() => readdirSync(folder).length === 2, "one click: a second copy in the same folder");
  p.$("sumExport").click();
  await waitFor(() => p.$("exportFolder") && p.$("exportFolder").value === folder, "the remembered folder offered");
});

test("📄 Soubor ▾ of the minutes: open, save as, copy the path or the file; model and generate stay out", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speakers");
  p.doc.querySelector('#subtabs [data-sub="summary"]').click();
  const menu = await waitFor(() => p.$("sumFileMenu"), "the file menu");
  const tools = p.doc.querySelector("#sumBar .sumTools");
  assert.deepEqual([...tools.children].filter(e => e.tagName === "BUTTON").map(b => b.id), ["sumDefault"],
                   "outside the menus: the model and its star");
  assert.deepEqual([...p.$("sumMakeMenu").querySelectorAll(".menuItems button")].map(b => b.id),
                   ["sumGen", "sumAgain", "sumPrompt"], "✎ Zápis ▾: generate, regenerate the shown one, the prompt");
  p.$("sumMakeMenu").open = true;
  p.$("sumGen").click();
  assert.ok(p.$("sumMakeMenu").open, "asking to confirm: the menu stays open for the second click");
  assert.deepEqual([...menu.querySelectorAll(".menuItems button")].map(b => b.id),  // saved before: Uložit jako minule
                   ["sumOpen", "sumExportAgain", "sumExport", "sumCopyPath", "sumCopyFile"]);
  assert.ok(!menu.open, "folded");
  menu.querySelector("summary").click();
  assert.ok(menu.open);
  let copied = "";
  Object.defineProperty(p.window.navigator, "clipboard", { value: { writeText: async s => { copied = s; } }, configurable: true });
  p.$("sumCopyPath").click();
  assert.ok(!menu.open, "an item done: the menu closes");
  await waitFor(() => /cesta zkopírována/.test(p.$("status").textContent), "the path copied");
  assert.ok(copied.endsWith(`\\${WEEKLY}.summary.md`) && copied.length > 40, copied);  // the full path of the shown minutes
  p.$("sumCopyFile").click();
  await waitFor(() => /je ve schránce/.test(p.$("status").textContent), "the file on the clipboard");
  p.$("sumPrompt").click();  // a prompt for another tool, by the model chosen in the bar
  await waitFor(() => /prompt ve schránce/.test(p.$("status").textContent), "the prompt copied");
  const chosen = p.$("sumModel").value.split(":").slice(1).join(":");
  assert.ok(chosen, "a model is chosen");
  const slug = chosen.replace(/[^A-Za-z0-9]+/g, "-").toLowerCase();
  const [head, from, to, rest] = copied.split(/ z přepisu souboru | do souboru |; postupuj podle skillu /);
  assert.equal(head, `vygeneruj zápis modelem ${p.$("sumModel").value} v jazyce cs`, copied);
  assert.ok(from.endsWith(`\\${WEEKLY}.txt`) && from.length > 40, from);
  assert.ok(to.endsWith(`\\${WEEKLY}.summary.${slug}.md`) && to.length > 40, to);
  assert.match(rest, /\\skills\\recordingz-zapis\\SKILL\.md\. Údaje o schůzce: začátek 2026-09-04T14:00:00; účastníci /, "the installed skill and the meeting");
  assert.match(p.$("sumOpen").textContent, /Otevřít/);
  p.$("sumOpen").click();
  // "v <program>" where .md has one (the Windows default or Nastavení), else "ve výchozí aplikaci" (CI)
  await waitFor(() => /otevřeno ve? .*summary\.md/.test(p.$("status").textContent), "opened");
  menu.open = true;
  p.doc.body.click();
  assert.ok(!menu.open, "a click elsewhere closes it");
});

test("📄 Soubor ▾ above the transcript: open it, copy the path or the file", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speakers");
  p.doc.querySelector('#subtabs [data-sub="transcript"]').click();
  const menu = await waitFor(() => p.$("txtFileMenu"), "the transcript's file menu");
  assert.deepEqual([...menu.querySelectorAll(".menuItems button")].map(b => b.id), ["txtOpen", "txtCopyPath", "txtCopyFile"]);
  let copied = "";
  Object.defineProperty(p.window.navigator, "clipboard", { value: { writeText: async s => { copied = s; } }, configurable: true });
  p.$("txtCopyPath").click();
  await waitFor(() => copied.endsWith(`\\${WEEKLY}.txt`), "the transcript's path");
  p.$("txtOpen").click();
  await waitFor(() => /otevřeno ve výchozí aplikaci: .*\.txt/.test(p.$("status").textContent), "opened");
});
