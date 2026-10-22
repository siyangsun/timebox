#!/usr/bin/env python3
"""timebox: wall-clock / context / usage budgets, self check-ins, resumable checkpoints.

CLI (run by Claude via Bash; use run.sh, which finds python):
  timebox.py start <dur|HH:MM> --goal "..." [--guard-stop] [--squeeze-at PCT] [--checkin MIN]
  timebox.py status
  timebox.py template                 # print checkpoint skeleton
  timebox.py checkpoint               # validate + stamp .timebox/checkpoint.md
  timebox.py end [--status done|paused]
  timebox.py resume <dur|HH:MM> [--guard-stop] [--checkin MIN]
  timebox.py install-statusline [--uninstall]   # wrap the user's statusLine to capture telemetry

Hooks (run by Claude Code, JSON on stdin):
  timebox.py hook post|pre|stop|prompt|session|precompact|sleeper
Status line (JSON on stdin): timebox.py statusline
"""
import argparse, datetime as dt, json, os, re, shutil, signal, subprocess, sys, time

SCRIPT = os.path.abspath(__file__)
CMD = '"' + os.path.join(os.path.dirname(SCRIPT), "run.sh").replace("\\", "/") + '"'  # launcher finds python on any OS
REQUIRED = ["## Goal", "## Status", "## Done", "## Next", "## Resume"]
PLACEHOLDERS = ["One line. What", "Ordered steps, each", "The exact first action on resume"]
SQUEEZE = 70                        # percent of the budget where CONVERGE (scope cutting) starts; --squeeze-at overrides
CTX_WRAP, USAGE_WRAP = 75, 90      # percent: WRAP fires at whichever budget gets here first
TEL_MAX_AGE = 120                   # seconds; older telemetry is ignored
EXPIRE_AFTER = 3600                 # a "running" timebox this long past its deadline is abandoned
HOME_CFG = lambda: os.path.join(os.path.expanduser("~"), ".claude")

TEMPLATE = """# Timebox checkpoint
## Goal
One line. What "finished" means.

## Status
done | paused at NN% of budget — one-line summary

## Done
- Concrete results, with file paths

## In progress
- What, where (file:line), how far along, what's half-broken

## Next
1. Ordered steps, each ≤15 min, highest value first

## Decisions & dead ends
- Chosen approach and why; what was tried and rejected (so resume doesn't retry it)

## Resume
The exact first action on resume (command to run / file to open).
"""

GUIDE = {
    "plan": "PLAN: write .timebox/plan.md — the Minimum Shippable Outcome (MSO) plus milestones with minute budgets totalling <=70% of the budget. Keep planning under 10% of the budget, then start executing.",
    "execute": "EXECUTE: work milestones in order. The clock changes scope, never rigor: keep verification and root-cause fixes. If you are >25% behind plan, note it in the check-in and reorder; scope cuts wait for CONVERGE.",
    "converge": "CONVERGE: no new scope. Finish or drop the in-progress milestone and drive to the MSO; never cut the MSO. List each cut and why in the check-in. Keep verification.",
    "wrap": "WRAP: write .timebox/checkpoint.md NOW (template: `{cmd} template`), run `{cmd} checkpoint`, then only finish or polish the deliverable (keep verification).",
    "overtime": "DEADLINE PASSED: stop feature work. Make sure .timebox/checkpoint.md is current, run `{cmd} checkpoint`, then `{cmd} end --status done|paused`.",
}


# ---------- paths / state ----------
def root(hook_data=None):
    r = os.environ.get("CLAUDE_PROJECT_DIR")
    if r:
        return r
    d = os.path.abspath((hook_data or {}).get("cwd") or os.getcwd())
    c = d
    while True:  # nearest ancestor that already has a .timebox
        if os.path.isdir(os.path.join(c, ".timebox")):
            return c
        if os.path.dirname(c) == c:
            return d
        c = os.path.dirname(c)

class P:
    def __init__(self, r):
        self.dir = os.path.join(r, ".timebox")
        self.state = os.path.join(self.dir, "state.json")
        self.ckpt = os.path.join(self.dir, "checkpoint.md")
        self.plan = os.path.join(self.dir, "plan.md")
        self.log = os.path.join(self.dir, "log.jsonl")
        self.hist = os.path.join(self.dir, "history")
        self.tel = os.path.join(self.dir, "telemetry.json")
        self.pid = os.path.join(self.dir, "sleeper.pid")

def load(p):
    try:
        with open(p.state) as f:
            s = json.load(f)
    except Exception:
        return None
    if s.get("status") == "running" and time.time() - s["deadline"] > EXPIRE_AFTER:
        s["status"] = "expired"  # session died mid-timebox; don't trap the next one
        save(p, s)
    return s

def atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    for i in range(5):  # Windows refuses to replace a file another process has open
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if i == 4: raise
            time.sleep(0.05)

def save(p, s):
    atomic_json(p.state, s)

def log(p, **kw):
    os.makedirs(p.dir, exist_ok=True)
    kw["t"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(p.log, "a") as f:
        f.write(json.dumps(kw) + "\n")

def telemetry(p, now):
    try:
        with open(p.tel) as f:
            t = json.load(f)
        return t if now - t["ts"] <= TEL_MAX_AGE else {}
    except Exception:
        return {}


# ---------- time helpers ----------
def parse_deadline(s, start):
    s = s.strip().lower()
    # Clock time: "@17:30", "by 17:30", "5pm", "5:30pm". Bare "1:30" is a duration (1h30m).
    m = re.fullmatch(r"(?:@|by\s+|at\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s) or \
        re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", s)
    if m:
        h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
        if ap == "pm" and h < 12: h += 12
        if ap == "am" and h == 12: h = 0
        t = dt.datetime.fromtimestamp(start).replace(hour=h, minute=mi, second=0, microsecond=0)
        if t.timestamp() <= start:
            t += dt.timedelta(days=1)
        return t.timestamp()
    m = re.fullmatch(r"(\d+):(\d{2})", s)
    if m:
        secs = int(m[1]) * 3600 + int(m[2]) * 60
    elif not re.fullmatch(r"(\s*\d+(\.\d+)?\s*[hms]?\s*)+", s):
        sys.exit(f"Bad limit '{s}'. Durations: 45m, 1h30m, 1:30, 90 (minutes). Clock times: @17:30, 5pm, 'by 17:30'.")
    else:
        secs = 0.0
        for num, _, unit in re.findall(r"(\d+(\.\d+)?)\s*([hms]?)", s):
            secs += float(num) * {"h": 3600, "m": 60, "s": 1, "": 60}[unit]
    if secs <= 0:
        sys.exit(f"Limit '{s}' must be longer than zero.")
    return start + secs

def fm(sec):
    if abs(sec) < 60:
        return f"{int(abs(sec))}s"
    m = int(round(abs(sec) / 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"

def clock(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")

def phase_of(frac, squeeze=SQUEEZE / 100):
    ph = "plan"
    for t, name in [(0.0, "plan"), (0.10, "execute"), (squeeze, "converge"), (0.85, "wrap"), (1.0, "overtime")]:
        if frac >= t:
            ph = name
    return ph

def snapshot(s, p, now=None):
    now = now or time.time()
    total = s["deadline"] - s["started_at"]
    el = now - s["started_at"]
    frac = el / total if total > 0 else 1.0
    ph = phase_of(frac, s.get("squeeze", SQUEEZE / 100))
    if ph == "plan" and s.get("resumed"):
        ph = "execute"  # resumed sessions use the checkpoint's Next list as the plan
    tel = telemetry(p, now)
    ctx = tel.get("context_pct")
    fh = tel.get("five_hour") or {}
    u, ru = fh.get("used_percentage"), fh.get("resets_at")
    use = u if u is not None and ru and ru > s["deadline"] else None  # binds only if it won't reset before the deadline
    why = []
    if ctx is not None and ctx >= CTX_WRAP: why.append(f"context {ctx:.0f}%")
    if use is not None and use >= USAGE_WRAP: why.append(f"5h usage {use:.0f}%")
    if why and ph in ("plan", "execute", "converge"):
        ph = "wrap"
    budget = max(frac, (ctx or 0) / CTX_WRAP, (use or 0) / USAGE_WRAP)  # 1.0 == WRAP in any budget
    return dict(now=now, total=total, elapsed=el, left=s["deadline"] - now, frac=frac, phase=ph,
                ctx=ctx, usage=u, why=why, budget=budget)

def status_line(s, snap):
    left = f"{fm(snap['left'])} left" if snap["left"] >= 0 else f"{fm(snap['left'])} OVER"
    ck = s.get("last_checkpoint")
    ck_s = f"{fm(snap['now'] - ck)} ago" if ck else "never"
    extra = ""
    if snap["ctx"] is not None: extra += f" Context: {snap['ctx']:.0f}%."
    if snap["usage"] is not None: extra += f" 5h usage: {snap['usage']:.0f}%."
    why = f" (budget trigger: {', '.join(snap['why'])})" if snap["why"] else ""
    return (f"[timebox] {fm(snap['elapsed'])} / {fm(snap['total'])} ({snap['frac']:.0%}) — {left}, "
            f"deadline {clock(s['deadline'])}. Phase: {snap['phase'].upper()}{why}. Last checkpoint: {ck_s}.{extra} "
            f"Goal: {s['goal']}")

def guide(ph):
    return GUIDE[ph].format(cmd=CMD)


# ---------- sleeper (asyncRewake) ----------
def _alive(pid):
    if os.name != "nt":
        os.kill(pid, 0)
        return True
    import ctypes  # os.kill(pid, 0) means CTRL_C_EVENT on Windows
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE

def pid_alive(p):
    try:
        pid = int(open(p.pid).read())
        return pid if _alive(pid) else None
    except Exception:
        return None

def stop_sleeper(p):
    pid = pid_alive(p)
    if pid and pid != os.getpid():
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
    try: os.remove(p.pid)
    except OSError: pass


# ---------- CLI ----------
def new_state(args, goal, session=1, old=None):
    old = old or {}
    sq = args.squeeze_at if args.squeeze_at is not None else old.get("squeeze", SQUEEZE / 100) * 100
    if not 10 <= sq < 85:
        sys.exit("--squeeze-at must be between 10 and 84 (percent of the budget).")
    start = time.time()
    deadline = parse_deadline(args.duration, start)
    total = deadline - start
    checkin = (args.checkin * 60) if args.checkin else max(180, min(900, total / 6))
    return dict(goal=goal, guard_stop=bool(args.guard_stop or old.get("guard_stop")), squeeze=sq / 100, status="running", started_at=start,
                deadline=deadline, checkin_s=checkin, grace_s=min(300, total * 0.1), last_checkin=start,
                last_phase="plan", last_checkpoint=None, precompact_blocked=False, session=session)

def cmd_start(p, args):
    s = load(p)
    if s and s["status"] == "running":
        sys.exit(f"A timebox is already running (goal: {s['goal']}). End it first: {CMD} end --status paused")
    s = new_state(args, args.goal)
    save(p, s)
    log(p, ev="start", goal=s["goal"], deadline=clock(s["deadline"]))
    snap = snapshot(s, p)
    print(status_line(s, snap))
    print(f"Check-ins every {fm(s['checkin_s'])}. Converge (scope cuts allowed) at {clock(s['started_at'] + s['squeeze'] * snap['total'])}, "
          f"wrap at {clock(s['started_at'] + 0.85 * snap['total'])} (or earlier at {CTX_WRAP}% context / {USAGE_WRAP}% 5h usage).")
    print(guide("plan"))

def cmd_status(p, args):
    s = load(p)
    if not s:
        sys.exit("No timebox in this project.")
    if s["status"] != "running":
        print(f"Timebox {s['status']} (goal: {s['goal']}).")
        if os.path.exists(p.ckpt):
            print(f"Checkpoint: {p.ckpt}. Resume with: {CMD} resume <duration>")
        return
    snap = snapshot(s, p)
    print(status_line(s, snap))
    print(guide(snap["phase"]))

def validate_ckpt(p):
    if not os.path.exists(p.ckpt):
        return ["the file (not written yet)"]
    txt = open(p.ckpt).read()
    bad = [h for h in REQUIRED if h.lower() not in txt.lower()]
    if any(x in txt for x in PLACEHOLDERS):
        bad.append("real content (template placeholder text is still there)")
    return bad

def cmd_checkpoint(p, args):
    s = load(p)
    missing = validate_ckpt(p)
    if missing:
        sys.exit(f"Checkpoint incomplete at {p.ckpt}: missing {', '.join(missing)}. Template: {CMD} template")
    if s:
        s.update(last_checkpoint=time.time(), precompact_blocked=False)
        save(p, s)
    log(p, ev="checkpoint")
    print(f"Checkpoint OK ({p.ckpt}).")

def cmd_end(p, args):
    s = load(p)
    if not s:
        sys.exit("No timebox in this project.")
    missing = validate_ckpt(p)
    if args.status == "paused" and missing:
        sys.exit(f"Can't pause without a valid checkpoint (missing {', '.join(missing)}). Write {p.ckpt} first.")
    snap = snapshot(s, p)
    s.update(status=args.status, ended_at=snap["now"])
    save(p, s)
    stop_sleeper(p)
    log(p, ev="end", status=args.status, frac=round(snap["frac"], 2))
    print(f"Timebox {args.status} after {fm(snap['elapsed'])} of {fm(snap['total'])} ({snap['frac']:.0%}).")
    if args.status == "paused":
        print(f"Resume later with: {CMD} resume <duration>")

def cmd_resume(p, args):
    old = load(p)
    if not os.path.exists(p.ckpt):
        sys.exit(f"No checkpoint at {p.ckpt}; nothing to resume.")
    if old and old["status"] == "running":
        sys.exit(f"A timebox is still running. End it first: {CMD} end --status paused")
    os.makedirs(p.hist, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy(p.ckpt, os.path.join(p.hist, f"checkpoint-{stamp}.md"))
    goal = (old or {}).get("goal", "resumed task")
    s = new_state(args, goal, session=(old or {}).get("session", 0) + 1, old=old)
    s.update(last_phase="execute", resumed=True)
    save(p, s)
    log(p, ev="resume", session=s["session"], deadline=clock(s["deadline"]))
    print(status_line(s, snapshot(s, p)))
    print("Resuming from checkpoint (treat its 'Next' list as the plan; do its 'Resume' action first):\n")
    print(open(p.ckpt).read())


# ---------- status line + install ----------
def cmd_statusline():
    raw = sys.stdin.read()
    d = {}
    try:
        d = json.loads(raw)
        proj = (d.get("workspace") or {}).get("project_dir") or d.get("cwd") or ""
        if os.path.isdir(os.path.join(proj, ".timebox")):  # only while a timebox exists here
            cw, rl = d.get("context_window") or {}, d.get("rate_limits") or {}
            atomic_json(os.path.join(proj, ".timebox", "telemetry.json"), dict(
                ts=time.time(), context_pct=cw.get("used_percentage"),
                five_hour=rl.get("five_hour"), seven_day=rl.get("seven_day")))
    except Exception:
        pass
    try:
        orig = json.load(open(os.path.join(HOME_CFG(), "timebox-orig-statusline.json"))).get("statusLine")
    except Exception:
        orig = None
    if orig and orig.get("command"):
        try:
            r = subprocess.run(orig["command"], shell=True, input=raw, capture_output=True, text=True, timeout=5)
            sys.stdout.write(r.stdout)
        except Exception:
            pass
        return
    try:  # no user status line: show a minimal one
        p = P((d.get("workspace") or {}).get("project_dir") or d.get("cwd") or os.getcwd())
        s = load(p)
        if s and s["status"] == "running":
            print(f"⏱ {fm(s['deadline'] - time.time())} left · {s['goal'][:40]}")
    except Exception:
        pass

def cmd_install(args):
    cfg_dir = HOME_CFG()
    settings, orig_f = os.path.join(cfg_dir, "settings.json"), os.path.join(cfg_dir, "timebox-orig-statusline.json")
    stable = os.path.join(cfg_dir, "timebox", "timebox.py")  # survives plugin updates; re-run install to refresh it
    try:
        cfg = json.load(open(settings))
    except FileNotFoundError:
        cfg = {}
    cur = cfg.get("statusLine")
    if args.uninstall:
        try:
            prev = json.load(open(orig_f)).get("statusLine")
        except Exception:
            sys.exit("No saved original status line; nothing to restore.")
        if prev: cfg["statusLine"] = prev
        else: cfg.pop("statusLine", None)
        atomic_json(settings, cfg); os.remove(orig_f); shutil.rmtree(os.path.dirname(stable), ignore_errors=True)
        return print("Restored your original status line.")
    os.makedirs(os.path.dirname(stable), exist_ok=True)
    shutil.copy(SCRIPT, stable)
    if cur and "timebox" in (cur.get("command") or "") and os.path.exists(orig_f):
        return print(f"Already installed; refreshed {stable}.")
    if os.path.exists(settings):
        shutil.copy(settings, settings + ".timebox-bak")
    atomic_json(orig_f, {"statusLine": cur})
    q = lambda s: '"' + s.replace("\\", "/") + '"'
    cfg["statusLine"] = {**(cur or {}), "type": "command", "command": f"{q(sys.executable or 'python3')} {q(stable)} statusline"}
    atomic_json(settings, cfg)
    print(f"Installed. Wraps your previous status line ({(cur or {}).get('command', 'none')}). "
          f"Backup: {settings}.timebox-bak. Re-run after upgrading the plugin to refresh the copy at {stable}; undo with --uninstall.")


# ---------- hooks ----------
def out(obj):
    print(json.dumps(obj))
    sys.exit(0)

def checkin_msgs(p, s, snap, force=False):
    """Status + check-in prompt when due / phase changed (or forced); records it in state."""
    msg = [status_line(s, snap)]
    changed = snap["phase"] != s.get("last_phase")
    due = snap["now"] - s.get("last_checkin", 0) >= s["checkin_s"]
    if changed or due or force:
        msg.append(guide(snap["phase"]))
        msg.append(f"CHECK-IN (keep it under 1 minute): append 2-3 lines to {p.plan} under '## Check-ins': "
                   f"what finished since the last check-in, ahead/on/behind plan, and the adjustment (cut, reorder, or continue).")
        ck = s.get("last_checkpoint")
        if snap["budget"] > 0.3 and (not ck or snap["now"] - ck > 2 * s["checkin_s"]):
            msg.append(f"Also refresh {p.ckpt} and run `{CMD} checkpoint` — an interruption now would lose work.")
        s.update(last_checkin=snap["now"], last_phase=snap["phase"])
        save(p, s)
        log(p, ev="checkin", phase=snap["phase"], frac=round(snap["frac"], 2))
        return msg, True
    return msg, False

def sleeper(p):
    """asyncRewake hook: sleep to the next check-in / deadline, then exit 2 to wake Claude."""
    s = load(p)
    if not s or s["status"] != "running" or pid_alive(p):
        sys.exit(0)
    me = os.getpid()
    os.makedirs(p.dir, exist_ok=True)
    open(p.pid, "w").write(str(me))
    try:
        while True:
            s = load(p)
            if not s or s["status"] != "running" or pid_alive(p) != me:
                sys.exit(0)
            now = time.time()
            nxt = s["last_checkin"] + s["checkin_s"]
            if now < s["deadline"] < nxt:
                nxt = s["deadline"]
            nxt = max(nxt, now + 30 if s["checkin_s"] >= 30 else now + s["checkin_s"])  # let the sync hook consume a due check-in first
            while time.time() < nxt:
                time.sleep(min(1, max(0.05, nxt - time.time())))
                s = load(p)
                if not s or s["status"] != "running" or pid_alive(p) != me:
                    sys.exit(0)
            snap = snapshot(s, p)
            if snap["now"] - s["last_checkin"] < s["checkin_s"] - 1 and not (snap["left"] <= 0 and s["last_phase"] != "overtime"):
                continue  # a sync hook already checked in
            msg, _ = checkin_msgs(p, s, snap, force=True)
            print("\n".join(msg), file=sys.stderr)
            sys.exit(2)
    finally:
        if pid_alive(p) == me:
            os.remove(p.pid)

def hook(p, event, data):
    s = load(p)
    if event == "session":
        if s and s["status"] in ("running", "paused", "expired") and os.path.exists(p.ckpt):
            txt = open(p.ckpt).read()[:4000]
            nxt = (f"It is still running: check `{CMD} status`." if s["status"] == "running"
                   else f"To continue it: {CMD} resume <duration>.")
            out({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                 f"[timebox] A timeboxed task is {s['status']} (goal: {s['goal']}). Latest checkpoint:\n\n{txt}\n\n"
                 f"{nxt} Mention this to the user before starting unrelated work."}})
        sys.exit(0)

    if not s or s["status"] != "running":
        sys.exit(0)
    if event == "sleeper":
        sleeper(p)
    snap = snapshot(s, p)

    if event == "pre":  # cap Bash's own timeout so one command can't outlive the deadline
        ti = data.get("tool_input") or {}
        if data.get("tool_name") != "Bash" or ti.get("run_in_background"):
            sys.exit(0)
        cap = max(10000, int((snap["left"] + s["grace_s"]) * 1000))  # floor: checkpoint commands must still run
        if (ti.get("timeout") or 120000) <= cap:
            sys.exit(0)
        out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**ti, "timeout": cap}}})

    if event == "precompact":
        ck = s.get("last_checkpoint") or 0
        if snap["now"] - ck <= s["checkin_s"] or s.get("precompact_blocked"):
            sys.exit(0)
        s["precompact_blocked"] = True  # never block twice in a row
        save(p, s)
        print(f"Timebox is running and the checkpoint is {'missing' if not ck else fm(snap['now'] - ck) + ' old'}. "
              f"Before compaction: write {p.ckpt} (`{CMD} template`) and run `{CMD} checkpoint`, then compact again.", file=sys.stderr)
        sys.exit(2)

    if event == "stop":
        if data.get("stop_hook_active"):
            sys.exit(0)  # one block per turn, never loop
        ck = s.get("last_checkpoint") or 0
        if s.get("guard_stop"):
            if snap["now"] - ck < s["checkin_s"] and snap["phase"] in ("wrap", "overtime"):
                sys.exit(0)
        elif ck or snap["budget"] < 0.5:
            sys.exit(0)
        left = f"{fm(snap['left'])} left" if snap["left"] > 0 else "deadline passed"
        out({"decision": "block", "reason":
             f"Timebox still running ({left}, goal: {s['goal']}). Don't stop silently. Either keep working, or: "
             f"write {p.ckpt} (`{CMD} template`), run `{CMD} checkpoint`, then `{CMD} end --status done|paused`. "
             f"If you're waiting on the user, checkpoint first, then end as paused."})

    # post / prompt: periodic + phase-change check-ins
    msg, fired = checkin_msgs(p, s, snap)
    if event == "post" and not fired:
        sys.exit(0)
    name = "PostToolUse" if event == "post" else "UserPromptSubmit"
    out({"hookSpecificOutput": {"hookEventName": name, "additionalContext": "\n".join(msg)}})


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "statusline":
        return cmd_statusline()
    if len(sys.argv) >= 3 and sys.argv[1] == "hook":
        try:
            data = json.load(sys.stdin)
        except Exception:
            data = {}
        try:
            hook(P(root(data)), sys.argv[2], data)
        except SystemExit:
            raise
        except Exception as e:  # a broken hook must never break the session
            print(f"timebox hook error: {e}", file=sys.stderr)
            sys.exit(0)
        return

    ap = argparse.ArgumentParser(prog="timebox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "resume"):
        sp = sub.add_parser(name)
        sp.add_argument("duration")
        if name == "start":
            sp.add_argument("--goal", required=True)
        sp.add_argument("--guard-stop", action="store_true", help="Stop hook blocks until checkpointed in WRAP/OVERTIME")
        sp.add_argument("--checkin", type=float, help="minutes between check-ins")
        sp.add_argument("--squeeze-at", type=float, help="percent of budget where scope cutting starts (default 70)")
    sub.add_parser("status"); sub.add_parser("template"); sub.add_parser("checkpoint")
    e = sub.add_parser("end"); e.add_argument("--status", choices=["done", "paused"], default="done")
    i = sub.add_parser("install-statusline"); i.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    if args.cmd == "install-statusline":
        return cmd_install(args)
    p = P(root())
    if args.cmd == "template":
        print(TEMPLATE); return
    {"start": cmd_start, "status": cmd_status, "checkpoint": cmd_checkpoint,
     "end": cmd_end, "resume": cmd_resume}[args.cmd](p, args)


if __name__ == "__main__":
    main()
