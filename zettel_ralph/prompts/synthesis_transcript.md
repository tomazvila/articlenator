# prompts/synthesis_transcript.md — steps for one video transcript

`NOTE_CONTRACT.md` defines the note and every rule; the numbers in brackets are its
sections. Its examples are fictional: never copy their titles, numbers or links.

## Hard rules

1. Write only what the transcript says. No outside knowledge, no invented example. (6)
2. Copy quotes from the file; never from a damaged or `minor:` loop span; garble stays there. (1, 5.1)
3. Copy each number with its unit, period and alternatives, in one clause. (5.2)
4. Keep every hedge and condition the speaker attaches to the claim, in the title too; never add a limit word. (4, 5.3)
5. The verb matches the speech act: option, prediction and "let's say" stay so. (5.3)
6. Multi-speaker video: name a speaker only with `speaker_evidence`; mixed voices or unsure: `not stated`. (3)
7. Title: named step with its number, limit and frame quantity; general title only for a general rule. (4)
8. A `scope` value is a word from the cited passage, or `not stated`. (2)
9. Edit an existing note only to fold (7.1); else a NEW note + `note_link.py`. (7.3, 7.4)
10. Links: `[[Home]]`, a note of this unit, or an `index_query.py` result; never itself. (5.1)
11. One note per tool call; rename with `move_file`, never a second file. (12)
12. Run `verify_claims.py` before clock-out. Fix turn: fix only the named items, or `delete_draft`; no new `--notes`. (12)

Wrong → right (fictional):
- "maybe two easy sessions a week if your fingers hurt": "Recommends Two Easy Sessions A
  Week" → "Says Maybe Two Easy Sessions A Week If Your Fingers Hurt".
- "I row three steady pieces a week": `equipment: "rowing machine"` → `equipment: "not stated"`.
- "from three sessions a week, at most one hard": "Advises Few Hard Sessions", period
  `not stated` → "Allows At Most One Hard Session Of Three Per Week", period `per week`.

## Tools

- You run only `pwd` and `python3 <helper>` with the flags shown below (`python3
  <helper> --help` lists a helper's flags). `python3 -c`, shell commands and other
  scripts are refused.
- Your writes go to a shadow work folder. Nothing reaches the vault until the checks
  pass (contract 12). Read and write notes by their vault path
  (`01 Permanent Notes/<Title>.md`). Rename with `move_file`; remove a draft of this
  unit with `delete_draft`.

## Steps

1. **Read.** Transcript frontmatter (only `asr_quality` decides, contract 1), then the
   whole text. In a conversation, mark who speaks each passage.
2. **Find claims.** A rule, recommendation, option, prediction, observation or quantity
   that a reader applies to training. Keep each claim with its conditions; one note per
   named step. Skip anecdotes, banter, unreadable and damaged spans, and record each
   skip:

   ```
   python3 queue_mark.py --ids vid-<id> --skip-passage "<first 8 words>" --at MM:SS --reason "<why>"
   ```

3. **Write each note** (one tool call per note): Evidence quotes first, then `sources`,
   `scope` with `modality`, title, lead and Details from the quotes only,
   `verification: unverified`.
4. **Dedup.**

   ```
   python3 index_query.py "<claim>" --speaker "<speaker>" --skill "<skill>" --level "<level>" --equipment "<equipment>" --basis "<basis>" --modality "<modality>"
   ```

   Fold only when `fold_check.fold_allowed` is true and no quantity changes (contract
   7.1). Else keep your new note and link (kinds in the table of contract 7.2):

   ```
   python3 note_link.py --from "01 Permanent Notes/<new>.md" --to "01 Permanent Notes/<other>.md" --kind related
   ```

   Use `--kind disagreement` only when your note has `## Disagreement`;
   `--kind supersedes-candidate` only when the other note has no `sources:`.
5. **Record** each note you created or changed:

   ```
   python3 index_add.py --title "<H1>" --file "01 Permanent Notes/<H1>.md" --gist "<lead>" --tags "calisthenics,<topic>" --source vid-<id> --speaker "<speaker>" --skill "<skill>" --level "<level>" --equipment "<equipment>" --basis "<basis>" --modality "<modality>"
   ```

   Exit code 3 = refused: undo the fold, keep a new note, link the two.
6. **Self-check** each note, fix what the report names, run again:

   ```
   python3 verify_claims.py "01 Permanent Notes/<Title>.md" --lit "$ZR_STAGING/lit"
   ```

   The table at the end of contract 12 says what to do for each checker result. Then
   read the note once more against hard rules 4 to 8.
7. **Clock out** with your final note titles and every existing note you folded into
   (several calls in one unit add up; an unlisted shadow note is discarded):

   ```
   python3 queue_mark.py --ids vid-<id> --stage synthesized --notes "<Title A>;<Title B>"
   ```

   No note at all: `python3 queue_mark.py --ids vid-<id> --stage skipped --reason "<why>"`.

Fix turns and review after you finish: contract 12, steps 5 and 6. A fix turn, a
repair turn or a `REVIEW REPAIR TURN.` message lists NOTE, TRANSCRIPT FILE(S),
FAILURES or REJECTED ITEMS, and CITED PASSAGES: read nothing else, fix exactly the
listed items or `delete_draft` the note.
