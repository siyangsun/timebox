---
name: timebox
description: Runs a task against a wall-clock time limit (and context/usage budgets) with periodic self check-ins, scope cutting, a timely wrap-up, and a resumable checkpoint file. Use when the user gives a time limit or deadline ("you have 45 minutes", "finish by 5pm", "timebox this"), or asks to resume a timeboxed task.
---

# Timebox

A clock you can't ignore. Hooks inject remaining time, context % and 5-hour usage % into your context as you work, wake you for check-ins, cap Bash timeouts at the deadline, and stop compaction or a silent stop when there is no recent checkpoint. They advise and save; they never lock you out. Your job is to plan to the clock, re-plan at each check-in, and always leave a resumable checkpoint.

`TB` below means: `"${CLAUDE_PLUGIN_ROOT}/skills/timebox/scripts/run.sh"` (the status messages print the absolute path; use that).

## Start

1. Parse the limit and goal from the request. Duration (`45m`, `1h30m`, `1:30`, `90` = minutes) or clock time (`@17:30`, `5pm`, `"by 6:15pm"`).
2. Add `--guard-stop` only if the user wants you blocked from ending a turn until you've checkpointed ("don't stop until...").
3. Run `TB start <limit> --goal "<one line>" [--guard-stop] [--squeeze-at PCT]`. `--squeeze-at` (default 70) is when scope cutting starts; raise it if the user wants full scope for longer.
4. Write `.timebox/plan.md` in under 10% of the budget:
   - **MSO (Minimum Shippable Outcome):** the smallest result that's still useful if time runs out.
   - **Milestones:** ordered by value, each with a minute budget. Total ≤70% of the budget; the rest is buffer + wrap.
   - **Cut list:** what gets dropped first if behind.
   - `## Check-ins` heading (empty).

## Phases

Hooks inject the current phase (PLAN → EXECUTE → CONVERGE → WRAP → OVERTIME) with its rule. WRAP can fire early on context or usage, not just time. Context and usage figures need the status-line wrapper (`TB install-statusline`, run once); without it only time is tracked.

## Check-ins

When a `[timebox] … CHECK-IN` message appears (it also wakes you if you were idle), take under 1 minute:
append 2–3 lines to `## Check-ins` in plan.md — what finished, ahead/on/behind, the adjustment. Then act on it. The clock changes scope, never rigor: keep verification and root-cause fixes. Cut scope only from CONVERGE on, and list each cut and why in plan.md. Don't ask the user's permission for those cuts.

Run `TB status` any time you want the clock.

## Checkpoint (the resumable artifact)

`.timebox/checkpoint.md` must be readable cold, by a fresh session with no memory of this one. Get the skeleton with `TB template`. Required sections: Goal, Status, Done, Next, Resume (plus In progress, Decisions & dead ends). Rules:

- Paths and line numbers, not descriptions. Commands, not intentions.
- "Next" steps ≤15 min each, ordered by value.
- Keep it under ~80 lines. It's an index into the work, not a copy of it; the real work lives in project files.

After writing it, run `TB checkpoint` (validates sections, stamps the time). Refresh it whenever a check-in tells you to, and always in WRAP.

## Ending

- Finished: write the checkpoint (Status: done), `TB end --status done`.
- Out of time or blocked on the user: checkpoint, `TB end --status paused`.
- Final message to the user: what's done, what's left, how much of the budget was used, and how to resume.

If you need user input mid-task, checkpoint and pause first. The Stop and PreCompact hooks block once with instructions if you haven't checkpointed; follow them.

## Resume

`TB resume <new limit>` prints the checkpoint and starts a new clock. Append a fresh `## Check-ins` section to plan.md. A new session in a project with a paused timebox gets the checkpoint injected automatically.

## Files

State lives in `.timebox/`. You write `plan.md` and `checkpoint.md`; the script manages the rest (`history/` holds archived checkpoints).
