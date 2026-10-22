# timebox — Claude Code plugin

Gives Claude a wall-clock budget it can't ignore. MIT licensed.

- **Three budgets.** Wall-clock time, context window, and 5-hour plan usage. The injected status line shows all three; context and usage come from the status line (see Install).
- **Clock in context.** Hooks inject the status after tool calls (on a check-in interval) and on every user message.
- **Self check-ins.** Every ~1/6 of the budget (3–15 min), a background `asyncRewake` hook wakes Claude, who logs progress vs. plan in `.timebox/plan.md` and re-plans.
- **Phases.** PLAN (0–10%) → EXECUTE → CONVERGE (70% by default, `--squeeze-at PCT`; no new scope, cuts allowed) → WRAP → OVERTIME. WRAP fires at whichever comes first: 85% of time, 75% of context, or 5-hour usage ≥90% with a reset after the deadline.
- **Advise and save, no jail.** Stop blocks once per turn only if there's no checkpoint past 50% of any budget (`--guard-stop` for the strict version). PreCompact blocks once if the checkpoint is stale. Bash calls get their `timeout` capped to the time left.
- **Resumable artifact.** `.timebox/checkpoint.md` (Goal / Status / Done / In progress / Next / Decisions & dead ends / Resume) is validated by the script. `resume` archives it and starts a new clock, and new sessions get it injected automatically.

## Install
```
/plugin marketplace add siyangsun/timebox
/plugin install timebox
```
Or try it without installing: `claude --plugin-dir ./timebox`.

Requires Python 3 (stdlib only): `python3`, `python` or `py` on PATH; the launcher `scripts/run.sh` picks the first that works. macOS and Linux are tested; Windows needs Git Bash (Claude Code's default shell there) and is covered by CI but less battle-tested.

Then, once, so context/usage are visible:
```
"<plugin>/skills/timebox/scripts/run.sh" install-statusline    # wraps your statusLine; --uninstall restores it
```
It copies the script to `~/.claude/timebox/` so the status line survives plugin updates; re-run after upgrading to refresh it. A project-level `statusLine` overrides the user one and bypasses this. Without the wrapper, only time is tracked.

## Use
```
/timebox 45m refactor the parser
/timebox @17:30 --guard-stop ship the release notes
/timebox status
/timebox end
/timebox resume 30m
```
Or just say "you have 40 minutes to…". The skill triggers on time limits.

**Auto mode:** the classifier can't see hook output, so it may refuse Claude's writes to `plan.md` / `checkpoint.md`. A plugin can't ship the fix; add this once to `~/.claude/settings.json` (or `/permissions` → Auto mode tab), keeping `"$defaults"`:
```json
"autoMode": { "allow": ["$defaults", "Writing or editing .timebox/plan.md and .timebox/checkpoint.md in the project directory is allowed: they are the user-installed timebox plugin's own state files, which the user asked Claude to maintain via /timebox"] }
```

Tests: `pytest tests/`. Add `.timebox/` to `.gitignore` unless you want the checkpoints versioned.
