---
name: sync-add-note
description: Leave a cross-device reminder note that surfaces on your OTHER machines the next time they run a sync-down. Use when the user types /sync-add-note or says they want to remember a follow-up task on another device / leave themselves a note for their next session elsewhere (e.g. "remind me to do X when I'm on the Surface"). The note does not surface on the machine that created it.
---

# sync-add-note — leave a cross-device reminder

Records a note in the shared sync folder. It surfaces on the user's *other*
machines the next time they run `/sync-env-down`, never on the machine that made
it. The engine (`sync_engine.py`) lives in the sibling `sync-envs` skill folder.

## How to run the engine

- **Windows:** `py -3 "<claudeSkillsDir>\sync-envs\sync_engine.py" --add-note "TEXT"`
- **macOS/Linux:** `python3 "<claudeSkillsDir>/sync-envs/sync_engine.py" --add-note "TEXT"`

**Run it in the background** (the Bash tool's `run_in_background: true`) — it
writes to the sync folder, which is usually a cloud drive; see the timing note
in `/sync-env-down` if it stalls.

## Procedure

1. **Get the note text.** Use what the user gave after `/sync-add-note`. If they
   just invoked the skill with no text, ask what they want to remember. Keep it a
   single, self-contained reminder — enough to act on from another machine with no
   memory of this conversation. Split multiple unrelated reminders into separate
   `--add-note` calls.
2. **Heads-up on content.** Notes are stored as **plaintext** in the sync folder.
   Don't put secrets (tokens, passwords) in a note; if the user's text contains
   one, flag it and suggest leaving the secret out.
3. **Add it.** Run the engine with `--add-note "TEXT"`. Quote the text; it may
   contain spaces.
4. **Confirm.** Relay the engine's confirmation, including the note id, and remind
   the user it will show up on their other machines on the next `/sync-env-down`
   — not on this one.

## Notes

- One reminder per note. For several follow-ups, call `--add-note` once each.
- `/sync-notes` lists everything currently active across all machines.
