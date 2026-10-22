"""Run: pytest tests/  — simulates hook stdin JSON; shifts the clock by editing state.json."""
import json, os, subprocess, sys, time
import pytest

S = os.path.join(os.path.dirname(__file__), "..", "skills", "timebox", "scripts", "timebox.py")
RUN = os.path.join(os.path.dirname(S), "run.sh")
UPPER = f'"{sys.executable}" -c "import sys; sys.stdout.write(sys.stdin.read().upper())"'.replace("\\", "/")  # portable stand-in for a user status line


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"; (home / ".claude").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    return dict(os.environ, CLAUDE_PROJECT_DIR=str(proj), HOME=str(home), USERPROFILE=str(home)), proj, home


def run(env, *args, stdin="{}"):
    return subprocess.run([sys.executable, "-I", S, *args], input=stdin, capture_output=True, text=True, env=env[0], timeout=30)

def hook(env, name, **data):
    r = run(env, "hook", name, stdin=json.dumps(data))
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr

def state(env):
    return json.load(open(env[1] / ".timebox" / "state.json"))

def edit(env, **kw):
    s = state(env); s.update(kw); json.dump(s, open(env[1] / ".timebox" / "state.json", "w"))

def at_frac(env, frac):  # move the clock so `frac` of the budget has elapsed
    s = state(env); total = s["deadline"] - s["started_at"]
    shift = time.time() - s["started_at"] - frac * total
    edit(env, started_at=s["started_at"] + shift, deadline=s["deadline"] + shift)

def tel(env, ctx=None, used=None, resets=None, age=0):
    d = dict(ts=time.time() - age, context_pct=ctx, five_hour={"used_percentage": used, "resets_at": resets} if used is not None else None)
    json.dump(d, open(env[1] / ".timebox" / "telemetry.json", "w"))

def ctx_of(out):
    return out["hookSpecificOutput"]["additionalContext"]

def write_ckpt(env):
    (env[1] / ".timebox" / "checkpoint.md").write_text("## Goal\ng\n## Status\npaused\n## Done\n- a\n## Next\n1. b\n## Resume\nrun x\n")

def start(env, lim="60m", *extra):
    r = run(env, "start", lim, "--goal", "g", *extra); assert r.returncode == 0, r.stderr


def test_limit_parsing(env):
    for ok in ("45m", "1h30m", "1:30", "90", "5pm", "@17:30", "by 6:15pm"):
        assert run(env, "start", ok, "--goal", "g").returncode == 0, ok
        run(env, "end")
    assert run(env, "start", "0m", "--goal", "g").returncode != 0
    assert run(env, "start", "soon", "--goal", "g").returncode != 0


def test_time_phases(env):
    start(env)
    at_frac(env, 0.86)
    _, out, _ = hook(env, "prompt")
    assert "Phase: WRAP" in ctx_of(out)


def test_wrap_on_context_and_staleness(env):
    start(env); at_frac(env, 0.3)
    tel(env, ctx=80)
    assert "Phase: WRAP" in ctx_of(hook(env, "prompt")[1]) and "context 80%" in ctx_of(hook(env, "prompt")[1])
    edit(env, last_phase="execute")
    tel(env, ctx=80, age=300)  # stale: ignored
    c = ctx_of(hook(env, "prompt")[1])
    assert "Phase: EXECUTE" in c and "Context:" not in c


def test_wrap_on_usage_only_if_resets_after_deadline(env):
    start(env); at_frac(env, 0.3)
    dl = state(env)["deadline"]
    tel(env, used=95, resets=dl + 3600)
    assert "Phase: WRAP" in ctx_of(hook(env, "prompt")[1])
    edit(env, last_phase="execute")
    tel(env, used=95, resets=dl - 60)  # resets before we're done: not binding
    assert "Phase: EXECUTE" in ctx_of(hook(env, "prompt")[1])
    tel(env)  # fields absent entirely
    assert hook(env, "prompt")[0] == 0


def test_stop_default_and_guard(env):
    start(env); at_frac(env, 0.3)
    assert hook(env, "stop") == (0, None, "")           # <50%: allowed
    at_frac(env, 0.6)
    _, out, _ = hook(env, "stop"); assert out["decision"] == "block"   # >50%, no checkpoint
    assert hook(env, "stop", stop_hook_active=True)[1] is None          # once per turn
    write_ckpt(env); assert run(env, "checkpoint").returncode == 0
    assert hook(env, "stop")[1] is None                                  # checkpointed: allowed
    run(env, "end"); start(env, "60m", "--guard-stop"); at_frac(env, 0.3)
    assert hook(env, "stop")[1]["decision"] == "block"                  # guard: blocks even early


def test_stop_on_context_budget(env):
    start(env); at_frac(env, 0.1); tel(env, ctx=45)  # 45/75 = 60% of the context budget
    assert hook(env, "stop")[1]["decision"] == "block"


def test_precompact_blocks_once(env):
    start(env)
    assert hook(env, "precompact")[0] == 2
    assert hook(env, "precompact")[0] == 0              # never twice in a row
    write_ckpt(env); run(env, "checkpoint")
    assert hook(env, "precompact")[0] == 0              # fresh checkpoint
    edit(env, last_checkpoint=time.time() - 3 * state(env)["checkin_s"])
    assert hook(env, "precompact")[0] == 2              # stale again, block re-armed by the checkpoint


def test_bash_timeout_cap(env):
    start(env, "10m"); at_frac(env, 0.5)               # 5m left + 60s grace
    _, out, _ = hook(env, "pre", tool_name="Bash", tool_input={"command": "x", "timeout": 600000})
    u = out["hookSpecificOutput"]["updatedInput"]
    assert u["command"] == "x" and 340000 < u["timeout"] < 365000
    assert hook(env, "pre", tool_name="Bash", tool_input={"command": "x", "timeout": 5000})[1] is None
    assert hook(env, "pre", tool_name="Read", tool_input={})[1] is None
    assert hook(env, "pre", tool_name="Bash", tool_input={"command": "x", "run_in_background": True})[1] is None
    at_frac(env, 1.5)                                    # long past: floor, not zero
    assert hook(env, "pre", tool_name="Bash", tool_input={"command": "x"})[1]["hookSpecificOutput"]["updatedInput"]["timeout"] == 10000


def test_statusline_telemetry_and_passthrough(env):
    start(env)
    payload = json.dumps({"workspace": {"project_dir": str(env[1])}, "context_window": {"used_percentage": 42},
                          "rate_limits": {"five_hour": {"used_percentage": 12.5, "resets_at": 1}}})
    r = run(env, "statusline", stdin=payload)
    t = json.load(open(env[1] / ".timebox" / "telemetry.json"))
    assert t["context_pct"] == 42 and t["five_hour"]["used_percentage"] == 12.5 and "⏱" in r.stdout
    # install wraps an existing status line without clobbering; output passes through unchanged
    settings = env[2] / ".claude" / "settings.json"
    settings.write_text(json.dumps({"theme": "x", "statusLine": {"type": "command", "command": UPPER, "padding": 2}}))
    assert run(env, "install-statusline").returncode == 0
    cfg = json.loads(settings.read_text())
    assert cfg["theme"] == "x" and cfg["statusLine"]["padding"] == 2 and "timebox" in cfg["statusLine"]["command"] and (env[2] / ".claude" / "timebox" / "timebox.py").exists()
    r = subprocess.run(cfg["statusLine"]["command"], shell=True, input='{"cwd":"/nope","x":"hello"}', capture_output=True, text=True, env=env[0])
    assert r.stdout.strip() == '{"CWD":"/NOPE","X":"HELLO"}'
    assert "Already installed" in run(env, "install-statusline").stdout  # refreshes the copy
    run(env, "install-statusline", "--uninstall")
    assert json.loads(settings.read_text())["statusLine"]["command"] == UPPER


def test_sleeper_wakes_and_dedupes_and_stops(env):
    start(env, "10m", "--checkin", "0.03")              # 1.8s check-ins
    edit(env, last_checkin=time.time() - 100)
    p = subprocess.Popen([sys.executable, "-I", S, "hook", "sleeper"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env[0])
    time.sleep(0.5)
    assert run(env, "hook", "sleeper").returncode == 0  # duplicate exits at once
    out, err = p.communicate(timeout=15)
    assert p.returncode == 2 and "CHECK-IN" in err
    # a new sleeper is killed by `end`
    p = subprocess.Popen([sys.executable, "-I", S, "hook", "sleeper"], stdin=subprocess.DEVNULL, env=env[0])
    time.sleep(0.5)
    run(env, "end")
    assert p.wait(timeout=10) is not None and not (env[1] / ".timebox" / "sleeper.pid").exists()


def test_expired_timebox_does_not_trap(env):
    start(env); at_frac(env, 0.0)
    s = state(env); edit(env, started_at=s["started_at"] - 7200, deadline=s["deadline"] - 7200)
    assert hook(env, "stop")[1] is None and state(env)["status"] == "expired"
    assert run(env, "start", "5m", "--goal", "g").returncode == 0


def test_checkpoint_rejects_template_and_session_hook(env):
    start(env)
    (env[1] / ".timebox" / "checkpoint.md").write_text(run(env, "template").stdout)
    assert run(env, "checkpoint").returncode != 0
    write_ckpt(env)
    assert "status" in ctx_of(hook(env, "session")[1])   # running: points at `status`, not `resume`
    run(env, "end", "--status", "paused")
    assert "resume" in ctx_of(hook(env, "session")[1])
    assert run(env, "resume", "10m").returncode == 0


def test_squeeze_at(env):
    start(env, "60m", "--squeeze-at", "50"); at_frac(env, 0.6)
    assert "CONVERGE" in ctx_of(hook(env, "prompt")[1])           # 60% is past a 50% squeeze
    edit(env, squeeze=0.7, last_phase="execute", last_checkin=0)
    c = ctx_of(hook(env, "prompt")[1])
    assert "EXECUTE" in c and "scope cuts wait" in c               # default 70%: still no cutting at 60%
    assert run(env, "start", "5m", "--goal", "g", "--squeeze-at", "95").returncode != 0


@pytest.mark.skipif(not __import__("shutil").which("sh"), reason="needs sh (Git Bash on Windows)")
def test_launcher_finds_python(env):
    r = subprocess.run(["sh", RUN, "template"], capture_output=True, text=True, env=env[0], timeout=30)
    assert r.returncode == 0 and "## Resume" in r.stdout
