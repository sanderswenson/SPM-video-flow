# To do (picked up from the hook-production session, 2026-10-19)

State at hand-off: hook production is committed as `21e515b` (catalog `vflow/data/hooks.toml`, `vflow/hooks.py`,
`vflow hooks` / `vflow variants`, the app's Hooks panel, 52 tests passing). Everything was verified **without a Claude
key**; the Claude hook writer has only run against mocks. The README section "Hooks: film ten openings, get ten
videos" describes the intended flow.

## Run the Claude pass for real

- [ ] `vflow hooks "C:\Users\swens\Downloads\Mission Walkthrough Voiceover.mp4"` from the repo folder (the cached plan in
      `work/` is reused, so it is only the one API call). Judge the ten hooks against the rules in `hooks.HOOKS_SYSTEM`:
      every figure in the transcript, 6-20 word spoken lines in the speaker's voice, 3-8 word titles, cold-open ranges
      that are complete thoughts, strongest first, no two hooks making the same promise. Tune the system prompt, the user
      prompt built in `write_with_claude`, or its validation; rerun; commit. Note the cost of one run.
- [ ] Check the editor pass + hooks together on a fresh clip with the key on (the app path: drop a clip, let it finish,
      open Hooks, render two variants).

## Design decisions still open

- [ ] Should every `vflow edit` / app job write the hook sheet automatically? Today it is on demand (Hooks button,
      `vflow hooks`) to avoid an extra Claude call per clip.
- [ ] Filming ergonomics: one continuous take holding all ten hooks, split by pauses and matched to the sheet's lines, vs
      today's one file per hook (`01.mov`, `02.mov`, ... or any name starting with the hook number). Decide after filming a
      real batch.
- [ ] Defaults: `hooks_count = 10` (setting) and at most 2 patterns per family (`per_family` in `hooks.shortlist`, code
      only). Revisit once real sheets exist; maybe expose the family cap.

## Quality follow-ups seen while testing

- [ ] Local (no-key) drafts lean on the headline figure: on the sample, "$5,500" appears in 7 of 10 lines. Rotate slot
      values across money mentions, or cap how many hooks may use the same slot value, or rank before/after and timeframe
      templates earlier.
- [ ] The payoff-line picker (`hooks.analyze`, "payoff") chose "$5,500 worth of work into a property renting for $3,250."
      where the editor pass had picked "we rent it out at $3,600 a month within 14 days". When the plan already carries an
      editor-pass `hook_range`, consider preferring it as the default cold open.
- [ ] The hook title shows for at most 4 s (`pipeline.overlay_events`) even when a filmed take runs 6 s; consider holding it
      for the whole lead.
- [ ] A filmed take is face-detected and framed on its own (`work/<clip>/hooks/<take>/subjects.json`). Verified only with a
      stand-in cut from the same footage; try a real phone take (different lens, exposure, room). Loudness is normalized
      over the whole concat, so a quiet phone take is not levelled separately from the body: check, and if needed run
      loudnorm per source before the join.
- [ ] Whisper on a short take can stretch one word over a pause (a 3 s hold on "PROPERTY" was seen inside the stand-in).
      Consider capping a caption word's on-screen time in `captions.group_words` or splitting such words.
- [ ] App: a variant job reads "Waiting to render" until its first stage reports (cosmetic). The Hooks panel was driven in
      the browser (`native: false`, typed paths); the native pywebview dialog path (`pick-clips`) is untested.
- [ ] Full dress rehearsal: ten real phone takes through the app, all ten variants rendered, watch them back to back.

## Housekeeping

- Stand-in filmed take for tests: `C:\Users\swens\claude\vflow\work\fake-hooks\01 payoff.mp4` (7 s, portrait, cut from the
  sample at 5:22).
- Test outputs that can be deleted: `out/*_hook01-*`, `out/*_hook06-*`, `out/*_hooks.md` in the repo (gitignored), and
  `C:\Users\swens\Videos\vflow\*_hook01-*`, `*_hook03-*`, `*_hooks.md` from the app run.
- The app's `settings.json` was removed after the test (it had not existed before); the app's work dir
  `%LOCALAPPDATA%\vflow\work\Mission Walkthrough Voiceover\` now holds `hooks.json` and `hooks/01_payoff/` from the test.
- `vflow/.claude/launch.json` (parent folder) has the `vflow-ui` preview config: the app in browser mode on port 8765.
