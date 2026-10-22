---
description: Timebox a task, e.g. /timebox 45m refactor the parser | /timebox @17:30 ship the release | /timebox resume 30m | /timebox status
argument-hint: <duration|@HH:MM|5pm|resume|status|end> [--guard-stop] [--squeeze-at PCT] <goal>
---

Use the timebox skill for this request: $ARGUMENTS

- `status` → run the script's `status` and report it in one line.
- `end` → write/refresh the checkpoint, then end as `paused` unless the goal is done.
- `resume <limit>` → run `resume` and continue from the checkpoint's Resume action.
- Otherwise the first token is the limit; `--guard-stop` and `--squeeze-at PCT` (when scope cutting starts, default 70) are optional; the rest is the goal. Start the timebox, write the plan, and begin work.
