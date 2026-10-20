# Piping QC

An offline study app for Saudi Aramco piping QC inspectors, from material receiving to reinstatement. Welding QC is out of scope.

Pick any of the 34 topics across 9 sections. Each topic has key points as bullets, an animated site scene you can step through, a summary, then a 6-question quiz that unlocks the next topic. Each section ends with a recap (traps, interview line) and a 10-question test. Quizzes include reverse "which standard?" questions. There are also flip flashcards with spaced repetition, a hydrotest pressure calculator (pipe, flange and valve limits with the controlling item shown), a key numbers table and an 8-week plan.

The **Ask** tab answers questions offline. It searches the field book and, once published, your own SAES and SAEP library, then gives a short summary, a cross-check of values that agree or differ between documents, and the clauses it used.

## Install on iPhone

1. Open the GitHub Pages link in **Safari**.
2. Tap **Share**, then **Add to Home Screen**, then **Add**.
3. Open it once from the Home Screen icon while you're online. After that it works without internet.

Progress is stored on the phone. Use **More → Back up your progress** to copy a backup code.

## Standards library for the Ask tab

The SAES and SAEP PDFs are proprietary, so they never go in this public repo. They live in a private repo, and a GitHub Action there turns them into one encrypted file:

1. Zips go into the private repo `piping-qc-docs` as Release assets (or in its `docs/` folder).
2. The Action in that repo (template: `tools/docs-workflow.yml`) runs `tools/build_index.py`. It reads every PDF, splits it into clauses and encrypts the result with AES-256-GCM using the `INDEX_PASSPHRASE` secret.
3. It pushes `kb/manifest.json` (counts only) and `kb/index.bin` (encrypted) here.
4. The app checks the manifest when it's online and offers the new version. After you download it and enter the passphrase once, it works offline.

Scanned PDFs without a text layer are skipped and listed in the Action log.

## Files

| Path | What it is |
| --- | --- |
| `index.html` | The whole app: content, styles and script |
| `sw.js` | Service worker that caches the app for offline use |
| `manifest.webmanifest` | Home Screen name, icon and colours |
| `icon-*.png` | App icons |
| `tools/build_index.py` | Builds the encrypted library from SAES and SAEP zips |
| `tools/docs-workflow.yml` | GitHub Action to copy into the private docs repo |
| `fonts/` | Barlow Condensed, IBM Plex Sans and IBM Plex Mono (SIL Open Font License) |

To publish an update, change the files and bump `CACHE` in `sw.js` (for example `piping-qc-v5`) so phones fetch the new version.

## Source note

Saudi Aramco SAES, SAEP, SAIC, SATIP, SATR, GI and Schedule Q documents are proprietary and revised often. Values here come from a study deck and public codes (ASME B31.3, B16.5, API 598, API RP 578). Check each one against the current revision held by your project or inspection agency.
