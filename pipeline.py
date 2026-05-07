#!/usr/bin/env python3
"""
pipeline.py
===========

Multi-agent build/review pipeline orchestrator.

This is the state machine that drives a Builder (Claude Code) and a Reviewer
(Gemini) through plan → plan-review → execute → code-review → merge for a
single feature. It is NOT an LLM agent — it is a deterministic Python
process. All intelligence lives in the agents; this script just sequences
turns, validates protocol, and counts rounds.

Hermes' job is to set up the feature directory, then launch this script and
get out of the way.

Architecture
------------

Every feature gets its own directory under `<root>/.hermes/features/<slug>-<id>/`,
containing:
  - state.json       — config: session IDs, model assignments, round caps
  - status.log       — append-only protocol log (whose turn, what phase)
  - conversation.md  — the work: problem statement, plans, reviews
  - findings.json    — structured review verdicts (machine-readable)
  - .lock            — flock target (advisory lock around CLI invocations)

The state machine reads the last meaningful line of status.log, dispatches
to a handler, validates the agent wrote its end-marker and verdict, and
loops. There are exactly four review rounds it will tolerate before
escalating: 2 plan-review rounds, 2 code-review rounds. Past that, exit
with status=ESCALATE and let the human take over.

Usage
-----

    pipeline.py setup --slug contact-form --request "Add a contact form to..."
    pipeline.py run <feature_dir>
    pipeline.py run <feature_dir> --mock        # dry-run with fake CLIs
    pipeline.py status <feature_dir>            # show current state

Typically Hermes calls `setup` then `run`. The script writes a final
`result.json` in the feature dir summarising the outcome, which Hermes
reads to report back to the user.
"""

from __future__ import annotations

import argparse
import atexit
import dataclasses
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"

# ---------------------------------------------------------------- constants --

# How long any single CLI invocation is allowed to take.
CLAUDE_TIMEOUT_SEC = 600
GEMINI_TIMEOUT_SEC = 300

# How many review rounds we tolerate per phase before escalating.
DEFAULT_PLAN_REVIEW_CAP = 2
DEFAULT_CODE_REVIEW_CAP = 2

# Default models. Builder uses opus for plan, sonnet for everything else
# (the resumed session sees the plan regardless).
DEFAULT_BUILDER_PLAN_MODEL = "opus"
DEFAULT_BUILDER_EXEC_MODEL = "sonnet"

# What we consider a clean turn end. The agent writes this exactly. The
# orchestrator validates after every CLI invocation.
END_MARKERS = {
    "builder_plan":       "[Builder plan end]",
    "builder_plan_revise":"[Builder plan revision end]",
    "builder_exec":       "[Builder execution end]",
    "builder_fix":        "[Builder fix end]",
    "reviewer_plan":      "[Reviewer plan review end]",
    "reviewer_code":      "[Reviewer code review end]",
}

# What the agent should emit as the LAST line of its conversation.md block.
EXPECTED_VERDICTS = {
    "builder_plan":       {"PLAN_READY", "NEED_INPUT"},
    "builder_plan_revise":{"PLAN_READY", "NEED_INPUT"},
    "builder_exec":       {"CODE_READY", "NEED_INPUT"},
    "builder_fix":        {"FIXES_APPLIED", "NEED_INPUT"},
    "reviewer_plan":      {"APPROVED", "CHANGES_REQUESTED"},
    "reviewer_code":      {"APPROVED", "CHANGES_REQUESTED"},
}


# ---------------------------------------------------------------- data --

@dataclass
class State:
    """Persisted in state.json. Read at startup, written on every transition."""
    feature_slug: str
    feature_dir: str
    branch_name: str
    project_root: str  # where git lives, where the agents do their work

    builder_session_id: str
    reviewer_session_id: str

    builder_plan_model: str = DEFAULT_BUILDER_PLAN_MODEL
    builder_exec_model: str = DEFAULT_BUILDER_EXEC_MODEL
    builder_max_turns: int = 120

    plan_review_cap: int = DEFAULT_PLAN_REVIEW_CAP
    code_review_cap: int = DEFAULT_CODE_REVIEW_CAP

    plan_review_round: int = 0
    code_review_round: int = 0

    phase: str = "INIT"  # see PHASES below
    final_status: Optional[str] = None  # set when the pipeline terminates
    final_reason: Optional[str] = None


# Phases the state machine moves through. Each is a row in dispatch().
PHASES = [
    "INIT",
    "BUILDER_PLAN",
    "REVIEWER_PLAN_REVIEW",
    "BUILDER_PLAN_REVISE",
    "BUILDER_EXEC",
    "REVIEWER_CODE_REVIEW",
    "BUILDER_FIX",
    "MERGE_READY",
    "DONE",
    "ESCALATE",
    "ERROR",
]

TERMINAL_PHASES = {"DONE", "ESCALATE", "ERROR"}


# ---------------------------------------------------------------- io helpers --

def load_state(feature_dir: Path) -> State:
    raw = json.loads((feature_dir / "state.json").read_text())
    # forgive missing fields by filling defaults from dataclass
    fields = {f.name for f in dataclasses.fields(State)}
    return State(**{k: v for k, v in raw.items() if k in fields})


def save_state(feature_dir: Path, state: State) -> None:
    tmp = feature_dir / "state.json.tmp"
    tmp.write_text(json.dumps(asdict(state), indent=2))
    tmp.replace(feature_dir / "state.json")


def append_status(feature_dir: Path, line: str) -> None:
    """Atomic append to status.log. Always include a timestamp."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(feature_dir / "status.log", "a") as f:
        f.write(f"{ts}  {line}\n")


def append_conversation(feature_dir: Path, content: str) -> None:
    """For orchestrator-generated content (problem statement, headers).
    The agents write their own [Builder]/[Reviewer] blocks — we don't
    write into those."""
    with open(feature_dir / "conversation.md", "a") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")


def notify_telegram(text: str) -> None:
    """Send a Telegram message. Failures are logged but never propagate."""
    env_path = Path.home() / ".hermes" / "notifier.env"
    if not env_path.exists():
        return
    try:
        config = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()
        token = config.get("TELEGRAM_BOT_TOKEN_NOTIFICATIONS")
        chat_id = config.get("TELEGRAM_CHAT_ID_NOTIFICATIONS")
        if not token or not chat_id:
            return
        import http.client
        body = json.dumps({"chat_id": chat_id, "text": text})
        conn = http.client.HTTPSConnection("api.telegram.org", timeout=10)
        try:
            conn.request("POST", f"/bot{token}/sendMessage",
                         body=body.encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()
    except Exception as e:
        try:
            error_log = Path.home() / ".hermes" / "notifier-errors.log"
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] pipeline notify failed: {e}\n")
        except Exception:
            pass


def builder_has_completed_a_turn(feature_dir: Path) -> bool:
    """Returns True iff status.log contains any Builder end-marker.
    This is the source of truth for whether Claude's session exists.
    status.log is append-only and crash-safe."""
    log = (feature_dir / "status.log").read_text()
    return any(END_MARKERS[kind] in log for kind in [
        "builder_plan", "builder_plan_revise",
        "builder_exec", "builder_fix",
    ])


def reviewer_has_completed_a_turn(feature_dir: Path) -> bool:
    log = (feature_dir / "status.log").read_text()
    return any(END_MARKERS[kind] in log for kind in [
        "reviewer_plan", "reviewer_code",
    ])


@contextmanager
def feature_lock(feature_dir: Path):
    """Advisory lock so accidental concurrent invocations of the script
    against the same feature directory serialize cleanly."""
    lock_file = feature_dir / ".lock"
    lock_file.touch(exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------- logging --

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------- notifier lifecycle --

def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_pid(pid: int) -> None:
    """SIGTERM with brief grace period, then SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        time.sleep(0.1)
        if not _is_pid_alive(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_existing_notifier(feature_dir: Path) -> list[int]:
    """Terminate any prior notifier(s) for this feature_dir.
    Checks the pid file first, then scans for stray processes via pgrep.
    Returns the list of pids that were killed."""
    killed: list[int] = []

    pid_file = feature_dir / "notifier.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid != os.getpid() and _is_pid_alive(pid):
                _kill_pid(pid)
                killed.append(pid)
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)

    # Fallback: catch manually-launched notifiers that bypassed the pid file.
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"notifier.py.*{feature_dir.name}"],
            capture_output=True, text=True, timeout=2,
        )
        for token in result.stdout.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid in killed or pid == os.getpid():
                continue
            if _is_pid_alive(pid):
                _kill_pid(pid)
                killed.append(pid)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if killed:
        log(f"  killed prior notifier(s): {killed}")
    return killed


def launch_notifier(feature_dir: Path) -> Optional[subprocess.Popen]:
    """Spawn notifier.py as a child process, register cleanup, return Popen.
    Best-effort: returns None on failure without aborting the pipeline."""
    notifier_script = SCRIPT_DIR / "notifier.py"
    if not notifier_script.exists():
        log(f"  notifier.py not found at {notifier_script}; skipping")
        return None

    kill_existing_notifier(feature_dir)

    pid_file = feature_dir / "notifier.pid"
    notifier_log = feature_dir / "notifier.log"
    try:
        proc = subprocess.Popen(
            ["python3", "-u", str(notifier_script), str(feature_dir)],
            stdout=open(notifier_log, "ab"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log(f"  failed to launch notifier: {e}")
        return None

    pid_file.write_text(str(proc.pid))
    log(f"  notifier launched pid={proc.pid}")

    def _cleanup():
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    return proc


# ---------------------------------------------------------------- prompts --

def load_system_prompt(role: str) -> str:
    fname = "builder_system.md" if role == "builder" else "reviewer_system.md"
    return (PROMPTS_DIR / fname).read_text()


def builder_turn_prompt(state: State, kind: str, end_marker: str) -> str:
    """Construct the per-turn user prompt for the Builder.

    `kind` is one of: builder_plan, builder_plan_revise, builder_exec, builder_fix.
    """
    feature_dir = Path(state.feature_dir)
    project_root = Path(state.project_root)

    parts = [
        f"You are working on the git branch `{state.branch_name}` in `{project_root}`.",
        f"The shared protocol files are in `{feature_dir}`.",
        "Read `conversation.md` for the full context, including any prior",
        "Reviewer feedback. Prior turns of yours are also there.",
        "",
    ]

    if kind == "builder_plan":
        parts += [
            "TURN: Produce an implementation plan for the problem statement",
            "at the top of `conversation.md`. Do not write code yet.",
            "Write your plan inside a [Builder]...[/Builder] block in",
            "`conversation.md`. End the block with VERDICT: PLAN_READY.",
        ]
    elif kind == "builder_plan_revise":
        parts += [
            "TURN: The Reviewer requested changes to your plan. Read the",
            "latest [Reviewer] block in `conversation.md` and revise the",
            "plan. Write the revised plan in a NEW [Builder]...[/Builder]",
            "block (do not edit the previous one). End with VERDICT: PLAN_READY.",
        ]
    elif kind == "builder_exec":
        parts += [
            "TURN: The plan has been approved. Implement it now.",
            f"The orchestrator has already checked out the feature branch `{state.branch_name}` for you.",
            f"You are currently ON that branch — do NOT switch branches.",
            "Make your code changes on the current branch.",
            "Run any tests the project supports to verify the build doesn't break.",
            "DO NOT commit — the orchestrator handles commits after your turn validates.",
            "Do not use git checkout, git branch, git push, or git merge.",
            "Then in `conversation.md`, append a [Builder]...[/Builder]",
            "block listing files changed and a brief summary of the change.",
            "Do NOT paste the diff. End the block with VERDICT: CODE_READY.",
        ]
    elif kind == "builder_fix":
        parts += [
            "TURN: The Reviewer requested fixes to the code. Read the latest",
            "[Reviewer] block in `conversation.md` and apply the requested",
            "BLOCKER fixes (you may also address IMPORTANT findings, but",
            "BLOCKERs are required).",
            "Apply the requested fixes to the existing files.",
            "The orchestrator will handle the commit (it will become a new commit,",
            "NOT amended onto the previous one).",
            "DO NOT commit, push, or modify git history.",
            "Then append a [Builder]...[/Builder] block summarising the fixes.",
            "End with VERDICT: FIXES_APPLIED.",
        ]
    else:
        raise ValueError(f"unknown builder turn kind: {kind}")

    parts += [
        "",
        f"When you finish, append exactly this line to `status.log`:",
        f"    {end_marker}",
        "Use bash for the append: ",
        f"    echo '{end_marker}' >> {feature_dir/'status.log'}",
        "Do not write any other lines to status.log.",
    ]
    return "\n".join(parts)


def reviewer_turn_prompt(state: State, kind: str, end_marker: str) -> str:
    """Construct the per-turn user prompt for the Reviewer."""
    feature_dir = Path(state.feature_dir)
    project_root = Path(state.project_root)

    parts = [
        f"The git branch under review is `{state.branch_name}` in `{project_root}`.",
        f"The shared protocol files are in `{feature_dir}`.",
        "Read `conversation.md` for full context. Read prior [Reviewer]",
        "blocks first — do NOT raise issues you raised before unless the",
        "Builder failed to address them.",
        "",
    ]

    if kind == "reviewer_plan":
        parts += [
            f"TURN: Plan review round {state.plan_review_round + 1}.",
            "Review the LATEST [Builder] block (the implementation plan).",
            "Use the BLOCKER / IMPORTANT / NIT structure from your system prompt.",
            "If there are no BLOCKERs, the verdict is APPROVED.",
        ]
    elif kind == "reviewer_code":
        parts += [
            f"TURN: Code review round {state.code_review_round + 1}.",
            f"Review the code changes on branch `{state.branch_name}`.",
            "You can inspect the diff with:",
            f"    cd {project_root} && git diff $(git merge-base HEAD origin/main 2>/dev/null || echo HEAD~1)..HEAD",
            "Or read individual files directly. Do NOT pipe `git diff` to",
            "anything — read files directly when reviewing them.",
            "Use the BLOCKER / IMPORTANT / NIT structure. If no BLOCKERs,",
            "the verdict is APPROVED. Do not invent issues to fill sections.",
        ]
    else:
        raise ValueError(f"unknown reviewer turn kind: {kind}")

    parts += [
        "",
        "Output your review directly to stdout. Use this exact format:",
        "",
        "[Reviewer]",
        "### BLOCKER",
        "(your blockers here, or \"(none)\")",
        "### IMPORTANT",
        "(your important findings here, or \"(none)\")",
        "### NIT",
        "(your nits here, or \"(none)\")",
        'VERDICT: APPROVED',
        "[/Reviewer]",
        "",
        "The output must START with [Reviewer] on its own line and END with",
        "[/Reviewer] on its own line. The verdict line must be exactly",
        '"VERDICT: APPROVED" or "VERDICT: CHANGES_REQUESTED".',
        "DO NOT use shell commands, file writing tools, or any other tools to",
        "write files. Just print the review block to stdout. The orchestrator",
        "will handle persistence.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- CLI runners --

@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool = False


def _run_subprocess(cmd: list[str], cwd: Path, timeout: int,
                    stdin_data: Optional[str] = None,
                    env: Optional[dict] = None) -> CliResult:
    start = time.monotonic()
    try:
        # Only one of input= or stdin= may be non-None.
        proc = subprocess.run(
            cmd, cwd=str(cwd), input=stdin_data,
            stdin=subprocess.DEVNULL if stdin_data is None else None,
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        return CliResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_sec=round(time.monotonic() - start, 2),
        )
    except subprocess.TimeoutExpired as e:
        return CliResult(
            returncode=-1,
            stdout=(e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=(e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
            duration_sec=round(time.monotonic() - start, 2),
            timed_out=True,
        )


def _log_cli_failure(feature_dir: Path, cmd: list[str], result: CliResult,
                     *, extra: str = "") -> None:
    """Write stdout/stderr of a failed CLI invocation for debugging."""
    failure_log = feature_dir / "cli_failures.log"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry = (
        f"\n{'='*60}\n"
        f"timestamp: {ts}\n"
        f"command: {' '.join(cmd[:6])} ... (truncated)\n"
        f"rc: {result.returncode}\n"
        f"timed_out: {result.timed_out}\n"
        f"duration_sec: {result.duration_sec}\n"
    )
    if extra:
        entry += f"extra: {extra}\n"
    entry += (
        f"--- stdout ---\n{result.stdout or '(empty)'}\n"
        f"--- stderr ---\n{result.stderr or '(empty)'}\n"
    )
    with open(failure_log, "a") as f:
        f.write(entry)


def invoke_claude(state: State, prompt: str, *, model: str,
                  end_marker_for_log: str) -> CliResult:
    """Invoke Claude Code with our session ID, system prompt, and turn prompt."""
    sys_prompt = load_system_prompt("builder")
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", sys_prompt,
        "--model", model,
        "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
        "--permission-mode", "auto",
        # Builder max-turns: Claude Code's per-invocation tool-call cap. Features
        # that install native deps (better-sqlite3, sharp, etc.) can need 50-80;
        # full feature builds 80-120. This is the safety net against runaway
        # agents, not a target — most features finish well under it.
        "--max-turns", str(state.builder_max_turns),
    ]
    if builder_has_completed_a_turn(Path(state.feature_dir)):
        cmd += ["--resume", state.builder_session_id]
    else:
        cmd += ["--session-id", state.builder_session_id]

    cwd = Path(state.project_root)
    log(f"  > claude --model {model} (session {state.builder_session_id[:8]}...)")
    # Fix C: strip ANTHROPIC_API_KEY so Claude falls back to credentials file.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = _run_subprocess(cmd, cwd, timeout=CLAUDE_TIMEOUT_SEC, env=env)
    log(f"    rc={result.returncode}  duration={result.duration_sec}s"
        + ("  TIMED OUT" if result.timed_out else ""))

    if result.returncode != 0 or result.timed_out:
        _log_cli_failure(Path(state.feature_dir), cmd, result)
    return result


def extract_reviewer_block(stdout: str) -> Optional[str]:
    """Extract [Reviewer]...[/Reviewer] block from agent stdout.
    Returns the full block including tags, or None if not found."""
    match = re.search(r'\[Reviewer\].*?\[/Reviewer\]', stdout, re.DOTALL)
    return match.group(0) if match else None


def invoke_gemini(state: State, prompt: str, *,
                  end_marker_for_log: str) -> CliResult:
    """Invoke Gemini with the reviewer system prompt and turn prompt."""
    sys_prompt = load_system_prompt("reviewer")
    full_prompt = sys_prompt + "\n\n---\n\n" + prompt
    cmd = ["gemini", "--skip-trust", "--yolo", "-p", full_prompt]
    if reviewer_has_completed_a_turn(Path(state.feature_dir)):
        cmd.insert(3, "--resume")
    cwd = Path(state.project_root)
    completed = reviewer_has_completed_a_turn(Path(state.feature_dir))
    log(f"  > gemini (session resume={completed})")
    result = _run_subprocess(cmd, cwd, timeout=GEMINI_TIMEOUT_SEC)
    log(f"    rc={result.returncode}  duration={result.duration_sec}s"
        + ("  TIMED OUT" if result.timed_out else ""))
    if result.returncode != 0 or result.timed_out:
        _log_cli_failure(Path(state.feature_dir), cmd, result)
    if result.returncode == 0:
        block = extract_reviewer_block(result.stdout)
        if block:
            feature_dir = Path(state.feature_dir)
            with open(feature_dir / "conversation.md", "a", encoding="utf-8") as f:
                f.write("\n" + block + "\n")
            append_status(feature_dir, end_marker_for_log)
    return result


# ---------------------------------------------------------------- mock CLIs --
# Used by --mock for dry-run testing of the state machine without spending tokens.

def _mock_builder(state: State, prompt: str, *, model: str,
                  end_marker_for_log: str) -> CliResult:
    """Pretend to be Claude. Append a canned [Builder] block and the marker."""
    feature_dir = Path(state.feature_dir)
    # Pick a verdict based on the turn kind, inferable from the marker.
    if "plan" in end_marker_for_log:
        verdict = "PLAN_READY"
        body = "Drafted plan: 1) Create form component. 2) Add validation. 3) Persist to localStorage."
    elif "execution" in end_marker_for_log:
        verdict = "CODE_READY"
        body = "Implemented plan. Files changed: ContactForm.tsx, useContactSubmissions.ts, page.tsx. Tests pass."
    elif "fix" in end_marker_for_log:
        verdict = "FIXES_APPLIED"
        body = "Applied requested fixes to ContactForm.tsx. Amended commit."
    else:
        verdict = "PLAN_READY"
        body = "(mock builder turn)"

    block = (
        f"\n[Builder]\n"
        f"({end_marker_for_log} — model={model})\n"
        f"{body}\n"
        f"VERDICT: {verdict}\n"
        f"[/Builder]\n"
    )
    append_conversation(feature_dir, block)
    append_status(feature_dir, end_marker_for_log)
    return CliResult(returncode=0, stdout="(mock)", stderr="", duration_sec=0.1)


def _mock_reviewer(state: State, prompt: str, *,
                   end_marker_for_log: str) -> CliResult:
    """Pretend to be Gemini. First round CHANGES_REQUESTED, then APPROVED.
    Lets us verify the loop logic + round cap without spending tokens."""
    feature_dir = Path(state.feature_dir)
    is_code_review = "code review" in end_marker_for_log
    round_num = state.code_review_round if is_code_review else state.plan_review_round
    if round_num == 0:
        verdict = "CHANGES_REQUESTED"
        blockers = "- Mock blocker: missing input validation on email field."
        importants = "(none)"
        nits = "(none)"
    else:
        verdict = "APPROVED"
        blockers = "(none)"
        importants = "(none)"
        nits = "(none)"
    block = (
        f"\n[Reviewer]\n"
        f"({end_marker_for_log})\n"
        f"### BLOCKER\n{blockers}\n"
        f"### IMPORTANT\n{importants}\n"
        f"### NIT\n{nits}\n"
        f"VERDICT: {verdict}\n"
        f"[/Reviewer]\n"
    )
    append_conversation(feature_dir, block)
    append_status(feature_dir, end_marker_for_log)
    return CliResult(returncode=0, stdout="(mock)", stderr="", duration_sec=0.1)


# Globals swapped by --mock; default to real CLIs.
_INVOKE_BUILDER = invoke_claude
_INVOKE_REVIEWER = invoke_gemini


def use_mock_clis():
    global _INVOKE_BUILDER, _INVOKE_REVIEWER
    _INVOKE_BUILDER = _mock_builder
    _INVOKE_REVIEWER = _mock_reviewer
    log("MOCK MODE: real CLIs will not be called.")


# ---------------------------------------------------------------- validation --

def read_last_block(feature_dir: Path, role: str) -> Optional[str]:
    """Return the contents of the LAST [role]...[/role] block in conversation.md,
    or None if there isn't one. Role is 'Builder' or 'Reviewer'."""
    text = (feature_dir / "conversation.md").read_text()
    pattern = re.compile(rf"\[{role}\](.*?)\[/{role}\]", re.DOTALL)
    matches = pattern.findall(text)
    return matches[-1].strip() if matches else None


def extract_verdict(block_content: str) -> Optional[str]:
    """Pull the VERDICT: <X> from a block. Last match wins."""
    m = list(re.finditer(r"^VERDICT:\s*(\S+)\s*$", block_content, re.MULTILINE))
    return m[-1].group(1) if m else None


def status_log_has_marker(feature_dir: Path, marker: str) -> bool:
    """True if `marker` appears anywhere in status.log (last write only matters,
    but we tolerate prior re-writes for retries)."""
    text = (feature_dir / "status.log").read_text()
    return marker in text


def reviewer_work_is_complete(feature_dir: Path, kind: str) -> bool:
    """Returns True if the Reviewer's [Reviewer] block in conversation.md
    has a valid verdict for this turn kind. Used to rescue the case where
    the Gemini subprocess hangs after completing its review but before
    writing the status.log end-marker."""
    block = read_last_block(feature_dir, "Reviewer")
    if block is None:
        return False
    verdict = extract_verdict(block)
    return verdict in EXPECTED_VERDICTS[kind]


def validate_turn(feature_dir: Path, *, role: str, kind: str,
                  end_marker: str) -> tuple[bool, Optional[str], str]:
    """After an agent turn returns, check it followed the protocol.
    Returns (ok, verdict, reason)."""
    if not status_log_has_marker(feature_dir, end_marker):
        return False, None, f"end-marker {end_marker!r} missing from status.log"
    block = read_last_block(feature_dir, role)
    if block is None:
        return False, None, f"no [{role}] block found in conversation.md"
    verdict = extract_verdict(block)
    if verdict is None:
        return False, None, f"no VERDICT line in last [{role}] block"
    expected = EXPECTED_VERDICTS[kind]
    if verdict not in expected:
        return False, verdict, (f"verdict {verdict!r} not in expected "
                                f"{sorted(expected)} for {kind}")
    return True, verdict, "ok"


def write_findings(feature_dir: Path, verdict: str, kind: str) -> None:
    """Persist the latest reviewer verdict so the orchestrator and humans
    can read it without parsing markdown."""
    findings_path = feature_dir / "findings.json"
    if findings_path.exists():
        data = json.loads(findings_path.read_text())
    else:
        data = {"history": []}
    data["history"].append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "verdict": verdict,
    })
    data["latest"] = {"kind": kind, "verdict": verdict}
    findings_path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------- state machine --

def transition(state: State, new_phase: str, reason: str = "") -> None:
    log(f"  → phase: {state.phase} → {new_phase}  {reason}")
    state.phase = new_phase
    save_state(Path(state.feature_dir), state)
    append_status(Path(state.feature_dir), f"[Orchestrator] phase={new_phase} {reason}")


def transition_and_notify(state: State, new_phase: str, reason: str = "") -> None:
    transition(state, new_phase, reason)
    slug = state.feature_slug
    msg = None
    if new_phase == "BUILDER_PLAN" and state.plan_review_round == 0:
        msg = f"[{slug}] 🚀 Pipeline iniciado"
    elif new_phase == "REVIEWER_PLAN_REVIEW":
        msg = f"[{slug}] 🔍 Revisando plan"
    elif new_phase == "BUILDER_PLAN_REVISE":
        msg = f"[{slug}] ✏️ Revisando plan (round {state.plan_review_round})"
    elif new_phase == "BUILDER_EXEC":
        msg = f"[{slug}] 🛠 Ejecutando código"
    elif new_phase == "REVIEWER_CODE_REVIEW":
        msg = f"[{slug}] 🔍 Revisando código"
    elif new_phase == "BUILDER_FIX":
        msg = f"[{slug}] 🔧 Aplicando fixes (round {state.code_review_round})"
    elif new_phase == "MERGE_READY":
        msg = f"[{slug}] ✅ Listo para merge"
    elif new_phase == "DONE":
        msg = f"[{slug}] 🎉 Pipeline DONE"
    elif new_phase == "ESCALATE":
        reason_text = state.final_reason or reason
        msg = f"[{slug}] ⚠️ Pipeline ESCALATE: {reason_text}"
    elif new_phase == "ERROR":
        reason_text = state.final_reason or reason
        msg = f"[{slug}] ❌ Pipeline ERROR: {reason_text}"
    if msg:
        notify_telegram(msg)


def ensure_feature_branch(state: State) -> None:
    """Checkout or create state.branch_name in state.project_root.
    Idempotent: if already on the right branch, does nothing.
    Raises RuntimeError or subprocess.CalledProcessError on failure.
    """
    cwd = state.project_root
    branch = state.branch_name
    feature_dir = Path(state.feature_dir)

    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()

    if current == branch:
        append_status(feature_dir, f"[Orchestrator] feature branch ensured: {branch}")
        return

    # Try checking out an existing branch first; create if it doesn't exist.
    result = subprocess.run(
        ["git", "checkout", branch],
        cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=cwd, capture_output=True, text=True, check=True
        )

    current_after = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()

    if current_after != branch:
        raise RuntimeError(
            f"ensure_feature_branch: expected {branch!r} but landed on {current_after!r}"
        )

    append_status(feature_dir, f"[Orchestrator] feature branch ensured: {branch}")


def commit_builder_changes(state: State, kind: str) -> bool:
    """Stage and commit any changes the Builder made.
    Returns True if a commit was created, False if nothing to commit.
    Raises if the commit operation fails.
    """
    cwd = state.project_root
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd, capture_output=True, text=True, check=True
    )
    if not status.stdout.strip():
        return False

    messages = {
        "builder_exec": f"feat: {state.feature_slug}",
        "builder_fix":  f"fix: address review feedback ({state.feature_slug})",
    }
    msg = messages.get(kind, f"build: {state.feature_slug} ({kind})")

    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=cwd, check=True)
    return True


def run_builder_turn(state: State, *, kind: str, model: str) -> tuple[bool, Optional[str]]:
    end_marker = END_MARKERS[kind]
    prompt = builder_turn_prompt(state, kind, end_marker)
    feature_dir = Path(state.feature_dir)
    append_status(feature_dir, f"[Orchestrator] dispatching {kind} to builder")
    result = _INVOKE_BUILDER(state, prompt, model=model,
                             end_marker_for_log=end_marker)
    if result.returncode != 0 or result.timed_out:
        return False, (f"builder subprocess failed: rc={result.returncode} "
                       f"timed_out={result.timed_out}")
    ok, verdict, reason = validate_turn(feature_dir, role="Builder",
                                        kind=kind, end_marker=end_marker)
    if not ok:
        return False, f"protocol violation: {reason}"
    if kind in ("builder_exec", "builder_fix"):
        try:
            committed = commit_builder_changes(state, kind)
            if committed:
                append_status(feature_dir, f"[Orchestrator] commit created for {kind}")
            else:
                append_status(feature_dir, f"[Orchestrator] no changes to commit for {kind}")
        except subprocess.CalledProcessError as e:
            return False, f"git commit failed: {e}"
    return True, verdict


def run_reviewer_turn(state: State, *, kind: str) -> tuple[bool, Optional[str]]:
    end_marker = END_MARKERS[kind]
    prompt = reviewer_turn_prompt(state, kind, end_marker)
    feature_dir = Path(state.feature_dir)
    append_status(feature_dir, f"[Orchestrator] dispatching {kind} to reviewer")
    result = _INVOKE_REVIEWER(state, prompt, end_marker_for_log=end_marker)
    if result.returncode != 0 or result.timed_out:
        # Rescue path: Gemini sometimes hangs after completing the review.
        # Since the Reviewer now outputs to stdout, check partial stdout for a
        # valid [Reviewer] block and persist it ourselves if found.
        if result.timed_out:
            block = extract_reviewer_block(result.stdout)
            if block:
                with open(feature_dir / "conversation.md", "a", encoding="utf-8") as f:
                    f.write("\n" + block + "\n")
                append_status(feature_dir, end_marker)
                append_status(feature_dir,
                              f"[Orchestrator] WARNING: Gemini subprocess timed out "
                              f"after completing review; block rescued from partial stdout")
                # Fall through to validation as if the call succeeded
            else:
                return False, (f"reviewer subprocess failed: rc={result.returncode} "
                               f"timed_out={result.timed_out}")
        else:
            return False, (f"reviewer subprocess failed: rc={result.returncode} "
                           f"timed_out={result.timed_out}")
    ok, verdict, reason = validate_turn(feature_dir, role="Reviewer",
                                        kind=kind, end_marker=end_marker)
    if not ok:
        return False, f"protocol violation: {reason}"
    write_findings(feature_dir, verdict, kind)
    return True, verdict


def step(state: State) -> None:
    """Advance the state machine by one phase. Mutates state in place."""
    feature_dir = Path(state.feature_dir)
    log(f"phase: {state.phase}")

    if state.phase == "INIT":
        transition_and_notify(state, "BUILDER_PLAN", "starting plan turn")
        return

    if state.phase == "BUILDER_PLAN":
        ok, verdict = run_builder_turn(state, kind="builder_plan",
                                        model=state.builder_plan_model)
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "NEED_INPUT":
            transition_and_notify(state, "ESCALATE", "builder asked for input")
            return
        transition_and_notify(state, "REVIEWER_PLAN_REVIEW", "plan ready, dispatching review")
        return

    if state.phase == "REVIEWER_PLAN_REVIEW":
        ok, verdict = run_reviewer_turn(state, kind="reviewer_plan")
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "APPROVED":
            transition_and_notify(state, "BUILDER_EXEC", "plan approved")
        else:
            state.plan_review_round += 1
            if state.plan_review_round >= state.plan_review_cap:
                transition_and_notify(state, "ESCALATE",
                           f"plan review hit cap ({state.plan_review_cap})")
            else:
                transition_and_notify(state, "BUILDER_PLAN_REVISE",
                           f"plan review round {state.plan_review_round}")
        return

    if state.phase == "BUILDER_PLAN_REVISE":
        ok, verdict = run_builder_turn(state, kind="builder_plan_revise",
                                        model=state.builder_plan_model)
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "NEED_INPUT":
            transition_and_notify(state, "ESCALATE", "builder asked for input")
            return
        transition_and_notify(state, "REVIEWER_PLAN_REVIEW", "plan revised, re-reviewing")
        return

    if state.phase == "BUILDER_EXEC":
        try:
            ensure_feature_branch(state)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            state.final_reason = str(e)
            transition_and_notify(state, "ERROR", f"reason: {e}")
            return
        ok, verdict = run_builder_turn(state, kind="builder_exec",
                                        model=state.builder_exec_model)
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "NEED_INPUT":
            transition_and_notify(state, "ESCALATE", "builder asked for input")
            return
        transition_and_notify(state, "REVIEWER_CODE_REVIEW", "code ready, dispatching review")
        return

    if state.phase == "REVIEWER_CODE_REVIEW":
        ok, verdict = run_reviewer_turn(state, kind="reviewer_code")
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "APPROVED":
            transition_and_notify(state, "MERGE_READY", "code approved")
        else:
            state.code_review_round += 1
            if state.code_review_round >= state.code_review_cap:
                transition_and_notify(state, "ESCALATE",
                           f"code review hit cap ({state.code_review_cap})")
            else:
                transition_and_notify(state, "BUILDER_FIX",
                           f"code review round {state.code_review_round}")
        return

    if state.phase == "BUILDER_FIX":
        ok, verdict = run_builder_turn(state, kind="builder_fix",
                                        model=state.builder_exec_model)
        if not ok:
            state.final_reason = verdict
            transition_and_notify(state, "ERROR", f"reason: {verdict}")
            return
        if verdict == "NEED_INPUT":
            transition_and_notify(state, "ESCALATE", "builder asked for input")
            return
        transition_and_notify(state, "REVIEWER_CODE_REVIEW", "fixes applied, re-reviewing")
        return

    if state.phase == "MERGE_READY":
        # Pipeline does NOT auto-merge. The human (or Hermes) handles merge
        # so the human keeps a checkpoint of agency over the production branch.
        transition_and_notify(state, "DONE", "ready for human-driven merge")
        return

    raise RuntimeError(f"unknown phase: {state.phase}")


def run_pipeline(feature_dir: Path, *, with_notifier: bool = True) -> int:
    state = load_state(feature_dir)
    log(f"running pipeline for {feature_dir.name} (phase={state.phase})")
    with feature_lock(feature_dir):
        if with_notifier:
            launch_notifier(feature_dir)
        while state.phase not in TERMINAL_PHASES:
            step(state)
        state.final_status = state.phase
        save_state(feature_dir, state)

    # Write a result.json for Hermes to pick up.
    result = {
        "feature_slug": state.feature_slug,
        "feature_dir": state.feature_dir,
        "branch_name": state.branch_name,
        "final_status": state.final_status,
        "final_reason": state.final_reason,
        "plan_review_rounds_used": state.plan_review_round,
        "code_review_rounds_used": state.code_review_round,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (feature_dir / "result.json").write_text(json.dumps(result, indent=2))
    log(f"DONE  status={state.final_status}  reason={state.final_reason}")
    log(f"      result.json written to {feature_dir/'result.json'}")
    return 0 if state.final_status == "DONE" else 1


# ---------------------------------------------------------------- setup --

def verify_prerequisites(project_root: Path) -> None:
    """Fail fast with a clear message if the environment needed by the pipeline is unsuitable."""
    if shutil.which("claude") is None:
        log("❌ claude CLI not found in PATH. Install with: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    if shutil.which("gemini") is None:
        log("❌ gemini CLI not found in PATH. Install with: npm install -g @google/gemini-cli")
        sys.exit(1)

    if shutil.which("git") is None:
        log("❌ git not found in PATH. Install git and try again.")
        sys.exit(1)

    if not project_root.is_dir():
        log(f"❌ project_root no es un directorio: {project_root}")
        sys.exit(1)

    if not (project_root / ".git").exists():
        log(f"❌ project_root no es un repo git: {project_root}")
        sys.exit(1)

    result = subprocess.run(
        ["git", "-C", str(project_root), "status", "--porcelain"],
        capture_output=True, text=True
    )
    if result.stdout.strip():
        log(f"❌ Working tree no está limpio. Cambios sin commitear:\n{result.stdout}Limpia con git status antes de iniciar el pipeline.")
        sys.exit(1)

    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        log(f"❌ No se pudo identificar la rama actual en {project_root}. Asegúrate de estar en un repo git válido.")
        sys.exit(1)


def cmd_setup(args) -> int:
    project_root = Path(args.project_root or os.getcwd()).resolve()
    verify_prerequisites(project_root)

    short_id = uuid.uuid4().hex[:8]
    slug = re.sub(r"[^a-z0-9-]+", "-", args.slug.lower()).strip("-")
    feature_name = f"{slug}-{short_id}"
    feature_dir = project_root / ".hermes" / "features" / feature_name
    feature_dir.mkdir(parents=True, exist_ok=False)

    branch_name = args.branch or f"feature/{slug}"

    state = State(
        feature_slug=slug,
        feature_dir=str(feature_dir),
        branch_name=branch_name,
        project_root=str(project_root),
        builder_session_id=str(uuid.uuid4()),
        reviewer_session_id=str(uuid.uuid4()),
        builder_plan_model=args.builder_plan_model,
        builder_exec_model=args.builder_exec_model,
        builder_max_turns=args.builder_max_turns,
        plan_review_cap=args.plan_review_cap,
        code_review_cap=args.code_review_cap,
    )
    save_state(feature_dir, state)

    # Seed conversation.md with the problem statement.
    problem = args.request
    if args.request_file:
        problem = Path(args.request_file).read_text()
    seed = (
        f"# Feature: {slug}\n\n"
        f"_Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}_\n"
        f"_Branch: `{branch_name}`_\n"
        f"_Builder session: `{state.builder_session_id}`_\n"
        f"_Reviewer session: `{state.reviewer_session_id}`_\n\n"
        f"## Problem Statement\n\n{problem}\n\n"
        f"## Constraints\n\n"
        f"- Branch: `{branch_name}` (do not push or merge from agents).\n"
        f"- Project root: `{project_root}`.\n"
        f"- Verify TypeScript compiles (if applicable) before declaring CODE_READY.\n\n"
        f"---\n\n"
    )
    append_conversation(feature_dir, seed)

    append_status(feature_dir, "[Hermes initialized]")
    append_status(feature_dir, f"[Orchestrator] feature_dir={feature_dir}")

    log(f"setup complete: {feature_dir}")
    print(str(feature_dir))  # so Hermes can capture the path
    return 0


# ---------------------------------------------------------------- status cmd --

def cmd_status(args) -> int:
    feature_dir = Path(args.feature_dir).resolve()
    state = load_state(feature_dir)
    print(f"feature: {state.feature_slug}")
    print(f"  dir:           {state.feature_dir}")
    print(f"  branch:        {state.branch_name}")
    print(f"  phase:         {state.phase}")
    print(f"  plan rounds:   {state.plan_review_round}/{state.plan_review_cap}")
    print(f"  code rounds:   {state.code_review_round}/{state.code_review_cap}")
    print(f"  builder model: plan={state.builder_plan_model} exec={state.builder_exec_model}")
    if state.final_status:
        print(f"  final status:  {state.final_status}")
        if state.final_reason:
            print(f"  reason:        {state.final_reason}")
    print(f"\nlast 10 status.log lines:")
    text = (feature_dir / "status.log").read_text().splitlines()
    for line in text[-10:]:
        print(f"  {line}")
    return 0


# ---------------------------------------------------------------- run cmd --

def cmd_run(args) -> int:
    feature_dir = Path(args.feature_dir).resolve()
    if args.mock:
        use_mock_clis()
    with_notifier = not args.no_notifier and not args.mock
    return run_pipeline(feature_dir, with_notifier=with_notifier)


# ---------------------------------------------------------------- main --

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup", help="initialize a new feature directory")
    sp.add_argument("--slug", required=True, help="short kebab-case feature name")
    sp.add_argument("--request", help="problem statement text (or use --request-file)")
    sp.add_argument("--request-file", help="path to a file with the problem statement")
    sp.add_argument("--branch", help=f"branch name (default: feature/<slug>)")
    sp.add_argument("--project-root", help="git repo root (default: cwd)")
    sp.add_argument("--builder-plan-model", default=DEFAULT_BUILDER_PLAN_MODEL)
    sp.add_argument("--builder-exec-model", default=DEFAULT_BUILDER_EXEC_MODEL)
    sp.add_argument("--builder-max-turns", type=int, default=120,
                    help="max tool-call turns per Builder invocation (default: 120)")
    sp.add_argument("--plan-review-cap", type=int, default=DEFAULT_PLAN_REVIEW_CAP)
    sp.add_argument("--code-review-cap", type=int, default=DEFAULT_CODE_REVIEW_CAP)
    sp.set_defaults(func=cmd_setup)

    rp = sub.add_parser("run", help="run the pipeline against a prepared feature dir")
    rp.add_argument("feature_dir")
    rp.add_argument("--mock", action="store_true",
                    help="use canned fake CLI responses (no tokens spent)")
    rp.add_argument("--no-notifier", action="store_true",
                    help="skip auto-launching the notifier child process")
    rp.set_defaults(func=cmd_run)

    sp2 = sub.add_parser("status", help="show current state of a feature dir")
    sp2.add_argument("feature_dir")
    sp2.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    if args.cmd == "setup" and not (args.request or args.request_file):
        sys.exit("setup: must provide --request or --request-file")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
