// Zápis: the minutes being written or waiting get a tab of their own ("⏳ ve frontě") listing each one with a button
// to stop it, and all at once when there are more. A file of its own: it holds the queue with a recording.
import assert from "node:assert/strict";
import { test } from "node:test";
import { writeFileSync } from "node:fs";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

function writeCapture(status) {
  writeFileSync(process.env.RECORDINGZ_TEST_CAPTURE, JSON.stringify({ app: "recordingz", pid: Number(process.env.RECORDINGZ_TEST_PID),
                                                                     running: true, ...status }));
}
const rows = p => [...p.doc.querySelectorAll("#docMain .minutesQueue li")];

test("minutes in the queue: their own tab, each one can be stopped, all at once too", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the speakers");
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => p.doc.querySelector("#sumTabs .sumTab"), "the summary tabs");
  assert.ok(!p.$("queueTab"), "nothing waits: no queue tab");

  // a recording holds the local (GPU) minutes, so they stay in the queue
  writeCapture({ recording: true, title: "Porada", stem: "2026-09-30_1827_porada", started: "2026-09-30T18:27:05" });
  await waitFor(() => !p.$("recBadge").hidden, "the recording");
  await waitFor(() => p.$("sumModel").value === "ollama:gemma4:31b", "the local model");
  p.$("sumGen").click(); p.$("sumGen").click();
  await waitFor(() => p.$("queueTab") && rows(p).length === 1, "the queue tab with the new minutes");
  assert.ok(p.$("queueTab").classList.contains("active"), "shown at once");
  assert.match(p.$("queueTab").textContent, /ve frontě \(1\)/);
  assert.match(rows(p)[0].textContent, /Zápis gemma4:31b čeká ve frontě \(1\. v pořadí, až skončí nahrávání\)/);
  assert.ok(!p.$("stopAllMinutes"), "one job: no 'all'");

  await waitFor(() => p.$("sumModel").value === "ollama:gemma4:31b", "the model again");
  p.$("sumGen").click(); p.$("sumGen").click();
  await waitFor(() => rows(p).length === 2 && p.$("stopAllMinutes"), "two of them, and stopping all");
  assert.match(p.$("queueTab").textContent, /ve frontě \(2\)/);

  // the previous minutes: the note over them points to the queue tab
  [...p.doc.querySelectorAll("#sumTabs .sumTab")].find(tb => tb.textContent.startsWith("test")).click();
  await waitFor(() => p.doc.querySelector("#docMain .minutesRunNote [data-queue-tab]"), "the note over the old minutes");
  assert.match(p.doc.querySelector("#docMain .minutesRunNote").textContent, /2 nové zápisy ve frontě/);
  p.doc.querySelector("#docMain .minutesRunNote [data-queue-tab]").click();
  await waitFor(() => rows(p).length === 2, "back on the queue tab");

  rows(p)[0].querySelector("button.stopJob").click();
  await waitFor(() => rows(p).length === 1, "one left");
  await waitFor(() => /zastaven/.test(p.$("status").textContent), "said in the status line");
  p.$("stopAllMinutes")?.click() ?? rows(p)[0].querySelector("button.stopJob").click();
  await waitFor(() => !p.$("queueTab"), "the queue tab gone when nothing waits");
  await waitFor(() => p.doc.querySelector("#sumTabs .sumTab.active")?.textContent.startsWith("test"), "the minutes again");
  writeCapture({ recording: false });
  await waitFor(() => p.$("recBadge").hidden, "the recording over");
});
