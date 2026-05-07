---
name: claude-gemini-pipeline
description: "Drive a feature build through a Claude (Builder) and Gemini (Reviewer) collaboration. Use when asked to 'build and review a feature', 'run the multi-agent pipeline', or for Claude-to-build / Gemini-to-review workflows. The orchestrator handles plan, plan-review, execute, code-review, and merge-readiness."
---

# Claude-Gemini Pipeline (v2.2)

## What this does

A two-agent build/review pipeline:

1. **Setup** — `pipeline.py setup` creates a feature directory with session UUIDs, seeds `conversation.md`, and prepares `state.json` / `status.log`.
2. **Run** — `pipeline.py run <feature_dir>` is a pure Python state machine that drives Claude (Builder: Opus for plan, Sonnet for exec) and Gemini (Reviewer) through the build/review loop until DONE, ESCALATE, or ERROR.
3. **Report** — read `result.json` and `findings.json` to surface the outcome.

The orchestrator uses only the Python standard library and runs to completion as a child process. The caller does not need to stay in the loop while it runs.

## Trigger

Use this when the user asks to "build and review a feature," "run the multi-agent pipeline," "have Claude implement and Gemini review X," or any phrasing that combines feature implementation with cross-model code review.

Do NOT use for: simple one-off code edits, baseline scaffolding, or research/investigation tasks that don't end in a merged feature.

## Prerequisites

The orchestrator validates these at runtime, but sanity-check before launching:

- `claude` CLI installed and authenticated (Claude Code v2.1.x or newer).
- `gemini` CLI installed and authenticated (Gemini CLI v0.40 or newer).
- `git` and `gh` available; the project is a git repository.
- Python 3.10+ (only stdlib used).
- The user is on a branch where it's safe to commit (orchestrator creates `feature/<slug>` automatically).

If any are missing, tell the user what to install rather than launching and watching failure.

## Step 0 — Scope validation

Separate baseline scaffolding (initializing a repo, setting up a project, creating the GitHub repo, first deploy) from feature development (adding a contact form, fixing a bug, refactoring a module). The pipeline is for feature development. If the request mixes both, handle scaffolding separately first, then enter the pipeline for the feature.

## Step 1 — Setup

Run:

```bash
python3 pipeline.py setup \
  --slug <kebab-case-feature-name> \
  --request "<one-paragraph problem statement>" \
  --project-root <absolute path to git repo>
```

Optional flags:

- `--request-file <path>` — use a file instead of inline text for longer briefs.
- `--branch feature/custom-name` — override the default `feature/<slug>` branch.
- `--builder-plan-model opus` / `--builder-exec-model sonnet` — defaults; override if asked.
- `--builder-max-turns 120` — per-invocation tool-call cap for the Builder. Default 120.
- `--plan-review-cap 2` / `--code-review-cap 2` — round caps before escalation.

Setup prints the absolute feature directory path on stdout.

## Step 2 — Run

```bash
# Create log directory
mkdir -p ~/.config/pipeline/logs

# Launch pipeline detached from parent shell
setsid python3 pipeline.py run "$FEATURE_DIR" \
    </dev/null \
    >>~/.config/pipeline/logs/pipeline-$(basename "$FEATURE_DIR").log 2>&1 &
PIPELINE_PID=$!

# Launch notifier (optional) detached from parent shell
setsid python3 notifier.py "$FEATURE_DIR" \
    </dev/null \
    >>~/.config/pipeline/logs/notifier-$(basename "$FEATURE_DIR").log 2>&1 &
NOTIFIER_PID=$!

echo "Pipeline PID: $PIPELINE_PID, Notifier PID: $NOTIFIER_PID"
```

- `setsid` detaches from the parent shell.
- `</dev/null` closes stdin.
- `>>logs/...` redirects stdout/stderr.

Typical wall-clock time: 5–15 minutes. Do not interfere while running — locking is advisory and concurrent agents create race conditions.

For progress visibility: `tail -f <feature_dir>/status.log` or `tail -f ~/.config/pipeline/logs/pipeline-<name>.log`.

## Step 3 — Report

When the script exits, read `<feature_dir>/result.json`:

```json
{
  "feature_slug": "contact-form",
  "branch_name": "feature/contact-form",
  "final_status": "DONE",
  "final_reason": null,
  "plan_review_rounds_used": 1,
  "code_review_rounds_used": 1,
  "completed_at": "2026-05-04T11:21:51"
}
```

Branch on `final_status`:

- **`DONE`** — feature is on the branch, code-reviewed and approved. Report the branch name, summarise changes from the latest `[Builder]` block in `conversation.md`, and offer to push/create PR. Do NOT auto-merge.

- **`ESCALATE`** — hit a round cap or agent asked for input. Read `findings.json` and the latest `[Reviewer]` block in `conversation.md` to surface BLOCKERs. Offer to: (a) increase round cap and resume, (b) hand off the half-built branch for manual fixing, or (c) abort and clean up.

- **`ERROR`** — protocol or subprocess failure. The `final_reason` field says what. Common cases:
  - `builder/reviewer subprocess failed: rc=...` — CLI error. Check auth, model availability, network, quota.
  - `reviewer subprocess failed: rc=-1 timed_out=True` — Gemini timed out before end-marker. Check `conversation.md` — the review may be complete there.
  - `protocol violation: end-marker missing` — agent didn't write sentinel to `status.log`.
  - `protocol violation: no VERDICT line` — agent wrote a block but forgot the verdict.

Preserve the feature directory as a debugging artifact. Do not delete without user confirmation.

## Step 4 — Merge (human-driven)

The pipeline does NOT auto-merge. After DONE, optionally:

```bash
cd <project_root>
git push origin <branch_name>
gh pr create --head <branch_name> --base main --title "..." --body "..."
gh pr merge --merge --delete-branch
```

Build the PR body from the latest `[Builder]` block plus a "Reviewed by Gemini" line. Do this only after user confirmation.

After successful merge, offer to clean up the feature directory.

## Configuration knobs

- **Models** — Builder defaults Opus (plan) / Sonnet (exec). Override with `--builder-plan-model` / `--builder-exec-model`. Reviewer is always Gemini (cross-family review is the design point).
- **Round caps** — Default 2 plan + 2 code review rounds. Raise to 3 for hard features. Setting to 1 disables iteration.
- **Mock mode** — `pipeline.py run <dir> --mock` runs with canned fake CLI responses. Zero tokens. Use after edits to prompts or state machine.

## Known Issues / Verified Bugs

| # | Bug | Status |
|---|-----|--------|
| 1 | Prompts path incorrect (`PROMPTS_DIR = SCRIPT_DIR / "prompts"`) | **FIXED** — use `prompts/` subdir |
| 2 | `--permission-mode acceptEdits` removed in Claude Code ≥2.1 | **FIXED** — use `--permission-mode auto` |
| 3 | `bypassPermissions` blocked under root | **FIXED** — `auto` works with root |
| 4 | Insufficient logging on `rc != 0` | **FIXED** — `_log_cli_failure()` writes to `<feature_dir>/cli_failures.log` |
| 5 | Session tracking with booleans in `state.json` fragile on crashes | **REPLACED** — derive from `status.log` via `builder_has_completed_a_turn()` / `reviewer_has_completed_a_turn()` |
| 7 | `ANTHROPIC_API_KEY` in env causes "Invalid API key" | **FIXED** — filter env dict passed to subprocess |
| 8 | 3s stall from "no stdin data received" | **FIXED** — `stdin=subprocess.DEVNULL` when `stdin_data is None` |
| 9 | Claude "You're out of extra usage" credit exhaustion | **DOCUMENTED** — pre-check with `claude -p "say hi"`; use `--builder-plan-model sonnet` for budget mode |
| 10 | `--max-turns 30` insufficient for native deps | **FIXED** — `--builder-max-turns` configurable, default 120 |
| 11 | Gemini timeout at 600s without end-marker | **DOCUMENTED** — verdict may be in `conversation.md`; manually append marker to `status.log` and resume |
| 12 | Gemini "not running in a trusted directory" | **DOCUMENTED** — set `GEMINI_CLI_TRUST_WORKSPACE=true` when launching |
| 13 | Gemini headless mode not writing `[Reviewer]` block to `conversation.md` | **FIXED** — `reviewer_turn_prompt()` now includes explicit write instruction for both `conversation.md` and `status.log` |
| 14 | Notifier dies before final "DONE" notification | **DOCUMENTED** — use `setsid`/`nohup`, or have orchestrator send final notification directly |

## Session-Existence Derivation (v2.2)

**Problem:** Storing session-started booleans in `state.json` is fragile. A crash between the boolean flip and the CLI invocation leaves the state claiming a session exists that doesn't — producing `"No conversation found with session ID: ..."` on relaunch.

**Fix:** Derive session existence from `status.log` (append-only, crash-safe):

```python
def builder_has_completed_a_turn(feature_dir: Path) -> bool:
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
```

In `invoke_claude()`: use `--resume <id>` if True, else `--session-id <id>`.
In `invoke_gemini()`: use `--resume` if True.

Remove session-started fields from `State` dataclass. `load_state()` ignores unknown JSON keys, so existing `state.json` files are forward-compatible.

## Gemini trust workspace (Gemini CLI >= 0.40)

If the repo has never been opened in interactive `gemini` mode, the CLI aborts with:

```
Gemini CLI is not running in a trusted directory.
To proceed, either use --skip-trust, set GEMINI_CLI_TRUST_WORKSPACE=true,
or trust this directory in interactive mode.
```

**Fix:** Set `GEMINI_CLI_TRUST_WORKSPACE=true` in the environment when launching:

```bash
GEMINI_CLI_TRUST_WORKSPACE=true python3 pipeline.py run /path/to/feature_dir
```

## Credit exhaustion (Anthropic quota)

When a Claude Code account is on a free/capped tier, Opus is expensive. A plan turn can consume enough quota that the subsequent exec turn fails with:

```
You're out of extra usage · resets 7:30pm (UTC)
```

**Mitigations:**
- Run `claude -p "say hi"` before launching to confirm capacity.
- For budget-constrained setups, override `--builder-plan-model sonnet`.
- If hit mid-pipeline, the plan in `conversation.md` is usually detailed enough to hand off.

## Turn budget (`--max-turns` / `builder_max_turns`)

Default: `--max-turns 120` (configurable via `--builder-max-turns`). For features requiring:
- Installing native Node modules (e.g. `better-sqlite3`, `node-gyp`)
- Creating 5+ new files
- Running tests + committing

30 turns may be insufficient. Symptom: `Error: Reached max turns (30)` after productive work.

**Mitigations:**
- Use `--builder-max-turns 120` for features with native deps.
- Break large features into smaller pipeline runs.
- Pre-install native dependencies before launching.

## Reviewer timeout (Gemini)

Gemini has `GEMINI_TIMEOUT_SEC = 600`. For large codebases (15+ files), Gemini may spend 8+ minutes reading and writing the review, then be killed before appending the end-marker to `status.log`.

**Symptom:**
- `result.json`: `"reviewer subprocess failed: rc=-1 timed_out=True"`
- `conversation.md`: complete `[Reviewer]` block with `VERDICT:`
- `status.log`: NO `[Reviewer code review end]`

**Recovery:** Manually complete the turn:

```bash
echo "[Reviewer code review end]" >> <feature_dir>/status.log
```

Then patch `state.json`: set `phase` to next phase (`BUILDER_FIX` if CHANGES_REQUESTED, `MERGE_READY` if APPROVED), clear `final_status`/`final_reason`, delete `result.json`, and resume.

## Resuming a failed pipeline run

If the pipeline fails mid-build (credit exhaustion, network blip, turn limit) and the root cause is fixed, resume from where it left off:

```json
// state.json
{
  "phase": "ERROR",
  "final_status": "ERROR",
  "final_reason": "builder subprocess failed: rc=1 timed_out=False"
}
```

**Do NOT** run `pipeline.py run` against this directory as-is — the state machine sees `ERROR` and exits immediately.

**Recovery:**

1. Inspect `status.log` and `cli_failures.log` to identify the last successful phase.
2. Patch `state.json`:
   - Set `phase` to the phase that should run next (e.g. `"BUILDER_EXEC"`).
   - Set `final_status` and `final_reason` to `null`.
3. Remove `result.json` if it exists.
4. Re-run: `pipeline.py run <feature_dir>`.

The derivation logic reads `status.log`, so a resumed run correctly uses `--resume` when a prior turn succeeded, and `--session-id` when starting fresh.

**CRITICAL — Do NOT contaminate artifacts.** When a run fails with `protocol violation: no [Reviewer] block found`, first verify whether the block is actually missing (read `conversation.md`, grep for `[Reviewer]`) before concluding the validator has a bug. Archiving the contaminated feature directory preserves evidence for bug investigation.

## Review block missing from conversation.md (Gemini CLI headless)

**Symptom:**
- `result.json`: `"protocol violation: no [Reviewer] block found in conversation.md"`
- `status.log` has the end-marker
- `conversation.md` has **zero** `[Reviewer]` occurrences
- Gemini exited `rc=0`
- `cli_failures.log` does not exist

**Root cause:** Gemini CLI in headless mode (`-p` prompt) receives a prompt telling it to "append your review inside a `[Reviewer]...[/Reviewer]` block" but the turn prompt only gives the explicit shell command for `status.log`. The prompt does NOT give an explicit tool-write command for `conversation.md`. Gemini may emit the review to stdout (captured by `capture_output=True`), but the orchestrator ignores stdout — it only validates `conversation.md`.

**Status:** Fixed in v2.2 — `reviewer_turn_prompt()` now includes explicit write instructions for both `conversation.md` and `status.log`.

## Environment hygiene for subprocess calls

When invoking `claude` or `gemini` from Python via `subprocess.run()`, construct a clean env dict:

```python
env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
proc = subprocess.run(cmd, cwd=cwd, env=env, ...)
```

This prevents inherited env vars from overriding the CLI's own credentials. Also pass `stdin=subprocess.DEVNULL` when there's no piped input, to avoid the 3-second "no stdin data received" stall.

## Bootstrap checklist

Before first real pipeline use:

```bash
# 1. Prompts exist
ls prompts/
# → builder_system.md  reviewer_system.md

# 2. Python syntax is valid
python3 -c "import ast; ast.parse(open('pipeline.py').read())"

# 3. Mock mode works end-to-end
mkdir -p /tmp/pipeline-mock-test && cd /tmp/pipeline-mock-test && git init
python3 pipeline.py setup --slug mock-test --request "Test" --project-root /tmp/pipeline-mock-test
FEATURE_DIR=$(ls -d /tmp/pipeline-mock-test/.features/mock-test-* | head -1)
python3 pipeline.py run "$FEATURE_DIR" --mock
# → must end in DONE
```

## Pitfalls

1. **Never pipe `git diff` to Gemini via stdin.** Gemini gets confused by diff markers and hallucinates issues. The pipeline avoids this by having the Reviewer read source files directly.

2. **Round caps exist for a reason.** Gemini, like any pedantic reviewer with no memory, will always find something. The severity tags (BLOCKER/IMPORTANT/NIT) plus the round cap turn this from a divergent loop into a convergent one.

3. **Don't trust "TSC OK" claims from agents.** If the project has TypeScript, verify compilation yourself after DONE: `npx tsc --noEmit`.

4. **The localStorage data-loss pattern.** If the feature involves a useEffect that writes to localStorage AND another that reads on mount, the Reviewer should flag the race condition.

5. **Delegate fixes to the pipeline.** When the user reports a bug or asks for a code fix, do NOT start editing files directly. Propose the pipeline first and wait for confirmation.

## Notifier component (notifier.py)

Optional Telegram notifier that tails `status.log` and sends real-time updates. Launch it alongside `pipeline.py run` in a separate process.

### Architecture: deterministic prefix + LLM summary

The notifier produces messages where **emoji + verdict are 100% deterministic** (from `findings.json`) while the **detail text is LLM-generated** (summarizing findings in Spanish).

**Pattern:** Split responsibility:
- **Python code** reads `findings.json`, extracts verdict, builds prefix (`✅ Plan APROBADO` / `⚠️ Plan CHANGES_REQUESTED`).
- **LLM** receives only `_findings_summary(entry)` and outputs a 1-line plain-text summary.
- **Concatenation** in Python: `f"{prefix}. {summary}"`.

This pattern — *deterministic shell + LLM detail* — should be reused anywhere an LLM is asked to produce structured output where certain fields must be ground-truth accurate.

### Event indexing for multi-round reviews

`findings.json` accumulates a `history` array with mixed `kind` values. The notifier tracks counters per event type and filters by `kind` before indexing. Always filter by `kind` before indexing — `history[-1]` is wrong for multi-round pipelines because entries interleave plan and code reviews.

### Notifier configuration

The notifier reads `~/.config/pipeline/notifier.env` (mode 0600) with these variables:

```
TELEGRAM_BOT_TOKEN_NOTIFICATIONS=<bot-token>
TELEGRAM_CHAT_ID_NOTIFICATIONS=<chat-id>
OPENROUTER_API_KEY=<api-key>
```

### Launching

```bash
python3 notifier.py <feature_dir>
```

Tails `status.log`, persists read offset in `.notifier_position`, and exits on terminal states (`DONE`, `ESCALATE`, `ERROR`) or inactivity timeout.

### Notifier process death (missed final notification)

**Symptom:** Pipeline finishes DONE but "Pipeline completo" message never arrives.

**Root cause:** The notifier dies before the orchestrator writes the final lines (e.g., terminal closure sends SIGHUP).

**Solutions:**
1. **Orchestrator sends final notification (recommended)** — add `send_telegram` at the end of `pipeline.py` after writing `result.json`.
2. **Daemonize the notifier** — launch with `setsid`/`nohup` instead of foreground.
3. **Retry watcher in notifier** — after the tail loop ends, check if `result.json` exists but was not yet notified, and send before exiting.

**Workaround:** Always check `result.json` directly — do not rely solely on Telegram for completion status.

## File layout

```
pipeline.py              # the orchestrator
notifier.py              # Telegram notification agent (optional)
prompts/
  ├── builder_system.md    # appended to Claude's system prompt every turn
  └── reviewer_system.md   # prepended to Gemini's prompt every turn
README.md                # developer-facing notes
SKILL.md                 # this file
```

Inside each project, the orchestrator creates:

```
<project_root>/.features/<slug>-<short-uuid>/
├── state.json              # session IDs, models, round counters, phase
├── status.log              # append-only protocol log
├── conversation.md         # problem statement + [Builder]/[Reviewer] blocks
├── findings.json           # structured review verdict history
├── result.json             # written at termination
└── .lock                   # advisory lock (flock target)
```

One feature, one directory. Keep them around as a build archive.
