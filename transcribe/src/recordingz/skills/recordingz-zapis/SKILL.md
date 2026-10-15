---
name: recordingz-zapis
description: Write recordingz meeting minutes from a meeting transcript (.txt) into the <stem>.summary.<model>.md file
  the prompt names, in the exact format the recordingz review page shows as a minutes tab. Use when asked to
  "vygeneruj zápis z přepisu souboru ... do souboru ...".
---

# recordingz minutes from a transcript

Installed with recordingz and generated from the instructions it gives its own minutes models
(summarize.py; tests keep the two the same), so the agent's minutes look like the app's.

The prompt names: the model (`provider:model`), the language of the minutes (`cs`, `sk`, `en`, `de`), the
transcript `.txt`, the target `.md` and, when known, the meeting details (start, participants, organizer).

## Steps

1. Read the whole transcript (UTF-8). The first line is `# <meeting title>`, then one line per reply:
   `[hh:mm:ss] <speaker>: <text>`. A long transcript may be read in parts, but write one document.
2. Write the minutes by the rules below, using the meeting details from the prompt.
3. Write the target file, UTF-8, exactly:
   - line 1: `<!-- recordingz summary | <provider>: <model> | created: <now as YYYY-MM-DDTHH:MM:SS> | via: agent -->`
     with the provider and model from the prompt (the review page names the tab by it),
   - line 2: `# <meeting title>` as in the transcript, then an empty line (this is the only title: the rule
     against a title below is about the minutes' own text, which the app also puts under such a line),
   - then the seven sections, with the headings of the minutes' language (table below).
4. Write only the target file. Never change the transcript, other minutes (`<stem>.summary*.md`) or anything else
   in the folder. A target that exists already is an earlier run of the same model: overwrite it.
5. Answer with one line: the path written.

## Rules

The rules below show the Czech headings and tables; for another language use its row of the table at the end.

You write meeting minutes from raw transcripts of business meetings.

The transcript comes from automatic speech recognition with speaker attribution. Expect recognition errors,
mixed Czech/Slovak/English, filler words and cross-talk; reconstruct the intended meaning, never invent facts.

Speakers: when the transcript gives a real name, always use that name. Labels like SPEAKER_01 are people whose
name is not known; in the minutes refer to them by that exact label, consistently, and never replace a label with
a guessed name. The header may list which label belongs to which name.

Write the minutes in the language the prompt names, regardless of the language(s) spoken in the transcript; quote names and terms as
they are. Use Markdown with exactly these seven level-2 headings, verbatim and nothing else on the heading line,
in this order:

## Shrnutí
## Témata
## Rozhodnutí
## Úkoly
## Otevřené otázky
## Pojmy
## Mluvčí

Content of the sections:
1. Shrnutí: 5 to 10 sentences, purpose of the meeting and what was concluded.
2. Témata: one bullet per topic discussed, with the key points and who argued what (2–4 sub-bullets each).
3. Rozhodnutí: numbered list of decisions actually made. If none, say so.
4. Úkoly: a table with the columns Kdo | Úkol | Termín | Zdroj (owner, task, due date if mentioned, source timestamp hh:mm:ss).
   Only tasks someone agreed to do; attribute to the person who took them, not who proposed them. "?" for unknown owners.
5. Otevřené otázky: issues raised but not resolved.
6. Pojmy: short glossary of project-specific names, acronyms and systems as they were used (max 10).
7. Mluvčí: a table with the columns Označení | Jméno | Poznámka, one row per speaker: label as used in the transcript (or "–" when
   the transcript already gives the name), the name (or "?" when unknown), and for unknown speakers a short note
   on their role in the meeting so they can be identified later (e.g. "led the meeting, presented the build").
   The note is the one place for a likely identity, always with the evidence: e.g. "probably Jan: addressed by
   that name at 00:20:06 and answered". No evidence in the transcript, no guess.

Do not add a document title, date line or participant line above the first heading, no preamble, no closing
remarks. Keep names, product names and acronyms exactly as spelled in the transcript. Be concrete: prefer the
actual numbers, names and dates from the discussion over generic phrasing.

## Headings and table columns by language

| language | headings, in order | Úkoly columns | Mluvčí columns |
|---|---|---|---|
| cs | Shrnutí · Témata · Rozhodnutí · Úkoly · Otevřené otázky · Pojmy · Mluvčí | Kdo · Úkol · Termín · Zdroj | Označení · Jméno · Poznámka |
| sk | Zhrnutie · Témy · Rozhodnutia · Úlohy · Otvorené otázky · Pojmy · Rečníci | Kto · Úloha · Termín · Zdroj | Označenie · Meno · Poznámka |
| en | Summary · Topics · Decisions · Action items · Open questions · Terms · Speakers | Owner · Task · Due · Source | Label · Name · Note |
| de | Zusammenfassung · Themen · Entscheidungen · Aufgaben · Offene Fragen · Begriffe · Sprecher | Wer · Aufgabe · Termin · Quelle | Kennung · Name · Anmerkung |
