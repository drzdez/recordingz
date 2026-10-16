// A recording being processed says the same on every tab, and what it says follows what was chosen: a 1:1 call
// names the other side and writes the minutes at once, the local voices are mentioned, no minutes is "bez zápisu".
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, openPage, waitFor } from "./page.mjs";

const running = plan => ({ current: { id: 7, name: "process", stem: BOARD_NEW, text: "zpracování", started: "08:26",
                                      plan }, queue: [], held: false });

test("while it is processed, Přepis and Zápis say so too; the text follows 1:1, the voices and no minutes", async t => {
  const p = await openPage(t, BOARD_NEW);
  await waitFor(() => p.doc.querySelector("#main .procPanel"), "the processing offer");
  p.window.setJobs(running({ cloud: true, voices: true, minutes_wait: true, minutes: true, only_with: "Marek Hudec" }));
  const speakers = p.$("main").textContent;
  assert.match(speakers, /Právě se zpracovává \(od 08:26\) – přepis přes cloud, mluvčí lokálně podle hlasových otisků, druhá strana pojmenovaná jako Marek Hudec; zápis hned/);
  assert.doesNotMatch(speakers, /zápis počká/, "a 1:1 call does not wait for names");
  for (const sub of ["transcript", "summary"]) {
    p.doc.querySelector(`#subtabs [data-sub="${sub}"]`).click();
    const doc = p.$("docMain").textContent;
    assert.match(doc, /Právě se zpracovává \(od 08:26\) – přepis přes cloud, mluvčí lokálně podle hlasových otisků, druhá strana pojmenovaná jako Marek Hudec; zápis hned/, sub);
    assert.doesNotMatch(doc, /Zpracování spustíte/, `${sub}: not offered again while it runs`);
  }
  p.window.setJobs(running({ cloud: true, minutes_wait: true, minutes: true }));
  assert.match(p.$("docMain").textContent, /přepis přes cloud; zápis počká, až pojmenujete mluvčí \(cloud je nerozpozná\)/,
               "the open tab follows the jobs");
  p.window.setJobs(running({ minutes_wait: true, minutes: false }));
  assert.match(p.$("docMain").textContent, /přepis, rozpoznání mluvčích, bez zápisu/);
  p.window.setJobs({ current: null, queue: [], held: false });
  assert.match(p.$("docMain").textContent, /Zpracování spustíte v záložce Mluvčí/, "nothing runs: offered again");
});
