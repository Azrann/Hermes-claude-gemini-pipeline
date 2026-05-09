---
name: claude-gemini-pipeline
description: Drive a feature build through a Builder (Claude Code) + Reviewer (Gemini CLI) collaboration. Use when asked to "build and review a feature," "run the multi-agent pipeline," or any phrasing that combines feature implementation with cross-model code review. The orchestrator handles plan, plan-review, execute, code-review, and merge-readiness as a deterministic Python state machine. The pipeline runs detached for 5–15 minutes; after launching it, end the turn and let the user drive — do not poll, tail, or wait in-conversation.
---

# Claude-Gemini Pipeline (v2.2)

## When to use

Use this skill when the user asks to "build and review a feature," "have Claude implement and Gemini review X," "run the multi-agent pipeline," or any phrasing that combines **feature implementation** with **cross-model code review**.

**Do NOT use** for:

- Simple one-off code edits the user wants you to do directly.
- Baseline scaffolding (initialising a repo, setting up a project, first deploy). If a request mixes scaffolding and feature work, do scaffolding manually first, then enter the pipeline for the feature.
- Research or investigation tasks that don't end in a committed feature branch.

When the user reports a bug or asks for a code fix in a real project, **propose the pipeline first and wait for confirmation** rather than editing files directly.

## Prerequisites

The orchestrator validates these at runtime via `verify_prerequisites()`. Sanity-check before launching:

- `claude` CLI installed and logged in (`claude login` already done; Claude Code v2.1.x or newer).
- `gemini` CLI installed and logged in (`gemini` then `/auth` already done; Gemini CLI v0.40+).
- `git` available and `project_root` is a git repo.
- **Working tree is clean** — `git status --porcelain` must be empty. The orchestrator hard-fails otherwise. This is the most common reason setup is rejected.
- Python 3.10+ (stdlib only).
- Current branch is one where it's safe to commit. The orchestrator will create `feature/<slug>` automatically.

If any are missing, tell the user what to install instead of launching and watching it fail.

---

## Step 0 — Mock mode first (on a fresh machine or after edits)

Mock mode replaces both CLIs with canned fakes. Zero tokens, ~1s wall-clock, exercises the whole state machine including a CHANGES_REQUESTED → APPROVED loop. Run it whenever:

- This is the first time the pipeline runs on a new machine.
- You've edited `pipeline.py`, the prompts, or the validation logic.
- You're debugging a protocol violation and want to confirm the harness still works.

```bash
mkdir -p /tmp/pipeline-mock-test && cd /tmp/pipeline-mock-test && git init -q && git commit --allow-empty -m init -q
python3 pipeline.py setup --slug mock-test --request "Test" --project-root /tmp/pipeline-mock-test
FEATURE_DIR=$(ls -d /tmp/pipeline-mock-test/.hermes/features/mock-test-* | head -1)
python3 pipeline.py run "$FEATURE_DIR" --mock
# → must end in DONE
```

Note the path: features live under `.hermes/features/`, **not** `.features/`.

---

## Step 1 — Setup

```bash
python3 pipeline.py setup \
  --slug <kebab-case-feature-name> \
  --request "<one-paragraph problem statement>" \
  --project-root <absolute path to git repo>
```

Useful flags:

- `--request-file <path>` — file instead of inline text for longer briefs.
- `--branch feature/custom-name` — override default `feature/<slug>`.
- `--builder-plan-model opus` / `--builder-exec-model sonnet` — defaults; override if asked or for budget mode (see Active Gotchas).
- `--builder-max-turns 120` — Builder per-invocation tool-call cap. Default 120.
- `--plan-review-cap 2` / `--code-review-cap 2` — round caps before escalation.

Setup prints the absolute feature directory path on stdout — capture it.

## Step 2 — Run, then hand off

The orchestrator **auto-launches the notifier** as a child process. Do not launch `notifier.py` manually — that produces two notifiers racing on the same `status.log`. If you want notifications off, pass `--no-notifier`.

```bash
# Detach from the parent shell so the pipeline survives terminal closure.
mkdir -p ~/.hermes/logs

setsid python3 pipeline.py run "$FEATURE_DIR" \
    </dev/null \
    >>~/.hermes/logs/pipeline-$(basename "$FEATURE_DIR").log 2>&1 &
PIPELINE_PID=$!
echo "Pipeline PID: $PIPELINE_PID"
```

`setsid` detaches from the parent. `</dev/null` closes stdin (avoids the 3s "no stdin data received" stall). The redirect captures stdout/stderr.

Typical wall-clock: 5–15 minutes. **Do not interfere while running** — the file lock is advisory and concurrent agents race on git operations.

### Hand the run off to the user — do not wait for it

Once the pipeline is launched and detached, **end your turn**. Do not poll `result.json`, do not `tail -f`, do not loop on `pipeline.py status`. The pipeline runs independently as a detached process; sitting in-conversation watching it burns context for no benefit and the user gets no information they don't already have on Telegram.

End the turn with a short handoff message that includes:

- The feature directory path (so the user can copy it back later).
- The PID (so the user can check or kill it).
- That progress notifications will arrive on Telegram.
- That the user should **come back and ask** when they want the report — at that point you'll run Step 3.

Example handoff:

> Pipeline launched for `<slug>` (PID `<pid>`). Feature directory: `<feature_dir>`. You'll see progress on Telegram as it runs through plan → review → exec → review. Typical wall-clock is 5–15 minutes. **Come back and tell me when you want the report** (or if Telegram shows ESCALATE / ERROR before then) and I'll pick it up from `result.json`.

Then stop. Do not add a "I'll check back in N minutes" — there is no N; the user drives.

### When the user returns

When the user comes back saying "it's done" / "check the result" / "what happened" / etc., proceed to Step 3. First confirm the pipeline actually finished:

```bash
test -f "$FEATURE_DIR/result.json" && echo "complete" || echo "still running"
```

- **`result.json` exists** → run Step 3.
- **`result.json` doesn't exist** → pipeline is still running. Show a snapshot and tell the user to come back later:

  ```bash
  python3 pipeline.py status "$FEATURE_DIR"
  ```

  Report the current phase and round counters from the snapshot, then end the turn again. Do not start polling.

If the user asks for live progress mid-run rather than a final report, run `pipeline.py status` once, report what it shows, and end the turn. They have Telegram for the streaming view; you give them point-in-time snapshots on request.

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

- **DONE** — feature is on the branch, code-reviewed and approved. Report the branch name, summarise changes from the latest `[Builder]` block in `conversation.md`, and offer to push / open a PR. **Do not auto-merge.**
- **ESCALATE** — hit a round cap or an agent asked for input. **Not a failure** — human input required. Read `findings.json` and the latest `[Reviewer]` block. Offer: (a) raise the round cap and resume, (b) hand off the half-built branch, or (c) abort and clean up.
- **ERROR** — protocol or subprocess failure. `final_reason` says what. See the Recovery Playbook below.

## Step 4 — Merge (human-driven)

The pipeline does **not** auto-merge. After DONE, optionally:

```bash
cd <project_root>
git push origin <branch_name>
gh pr create --head <branch_name> --base main --title "..." --body "..."
gh pr merge --merge --delete-branch
```

Build the PR body from the latest `[Builder]` block plus a "Reviewed by Gemini" line. Do this only after user confirmation. Then offer to clean up the feature directory.

After DONE, also verify TypeScript yourself if applicable: `npx tsc --noEmit`. Don't trust "TSC OK" claims from the Builder.

---

## Recovery Playbook

Organised by symptom. The state machine is the source of truth — don't edit `conversation.md` or `findings.json` to nudge the pipeline. Edit `state.json` and re-run.

### Symptom: `final_status: ERROR`, `final_reason` mentions `rc=1`

CLI subprocess failed. Check `<feature_dir>/cli_failures.log` for stdout/stderr — it's written by `_log_cli_failure()` after every non-zero return. Common causes:

- **Auth expired** — `claude login` or `gemini /auth` again.
- **Credit exhaustion** (Anthropic) — `You're out of extra usage · resets …`. The plan in `conversation.md` is usually detailed enough to hand off, or wait for reset and resume.
- **Network / quota** on Gemini side.

Recovery: fix the root cause, then patch state and resume (see "Resuming a failed run" below).

### Symptom: `reviewer subprocess failed: rc=-1 timed_out=True`

Gemini hit `GEMINI_TIMEOUT_SEC = 300` before completing. The orchestrator has a rescue path: it parses the partial stdout for a `[Reviewer]...[/Reviewer]` block via `extract_reviewer_block()` and persists it if found. If you're seeing this error in `result.json`, the rescue failed too — the review wasn't in stdout when the timeout fired.

Check `conversation.md` for a `[Reviewer]` block:

- **Block exists with valid VERDICT** — the review actually completed but the orchestrator was killed mid-write. Append the marker and resume:
  ```bash
  echo "[Reviewer code review end]" >> "$FEATURE_DIR/status.log"
  ```
  Then patch `state.json` (see resuming) and re-run.
- **No block** — the review didn't finish. Raise `GEMINI_TIMEOUT_SEC` (currently 300s in `pipeline.py`) for large codebases and resume.

### Symptom: `protocol violation: no [Reviewer] block found in conversation.md`

**Verify before assuming a bug.** Read `conversation.md`, grep for `[Reviewer]`. If a block IS present, the validator has a bug — preserve the feature directory as evidence and report it. Do NOT contaminate by adding the block manually.

If genuinely missing: Gemini emitted the review to stdout but stdout was empty or malformed. Check `cli_failures.log`. The fix in v2.2 is that the reviewer prompt explicitly says *"Just print the review block to stdout"* and the orchestrator parses stdout — if you've edited `reviewer_system.md` or `reviewer_turn_prompt()` to ask Gemini to write files instead, that change broke the contract. Revert.

### Symptom: `protocol violation: end-marker missing` or `no VERDICT line`

Agent didn't follow protocol. One-shot only — the orchestrator does not retry on protocol violations because they usually indicate a prompt or model issue that won't fix itself.

- Check the latest block in `conversation.md`. If verdict is there but missing the marker, append it manually and resume.
- If the model genuinely went off-script, edit the prompt in `prompts/` and re-run.

### Symptom: Pipeline reached ESCALATE on a round cap

Default caps are 2 plan + 2 code-review rounds. Gemini, like any pedantic reviewer, will always find something — caps turn divergent loops into convergent ones. Options:

- Raise the cap (`--plan-review-cap 3` / `--code-review-cap 3`) and resume — but only if recent reviewer findings look substantive, not nit-picky.
- Hand off the half-built branch for manual finishing.
- Abort and clean up the feature directory.

### Symptom: Notifier dies before the final notification

Best-effort component. If the orchestrator finished DONE but Telegram never said so, the notifier process was killed (e.g., SIGHUP from terminal closure). The orchestrator itself sends phase-transition notifications via `notify_telegram()`, so most events arrive even without the notifier — but review *summaries* (the LLM-enriched ones) come from the notifier only.

**Always check `result.json` directly** — never rely solely on Telegram for completion status.

### Resuming a failed run

A pipeline in `ERROR` exits immediately on re-run. To resume:

1. Inspect `status.log` and `cli_failures.log` to identify the last successful phase.
2. Edit `state.json`:
   - Set `phase` to the phase that should run next (e.g., `BUILDER_EXEC` if exec failed, `BUILDER_FIX` if a code review came back CHANGES_REQUESTED).
   - Set `final_status` and `final_reason` to `null`.
3. Delete `result.json` if it exists.
4. Re-run: `python3 pipeline.py run "$FEATURE_DIR"`.

The orchestrator derives Claude/Gemini session existence from `status.log` (append-only, crash-safe), so a resumed run correctly uses `--resume` for sessions that completed a turn and `--session-id` for fresh ones.

---

## Active Gotchas

These are real failure modes you'll encounter; not bugs to fix.

### Gemini "not running in a trusted directory"

If the repo has never been opened in interactive `gemini` mode, the CLI aborts. The pipeline passes `--skip-trust` already, but if you see this error, you can also export:

```bash
export GEMINI_CLI_TRUST_WORKSPACE=true
```

before launching.

### Anthropic credit exhaustion

Opus is expensive. A single plan turn can exhaust a free/capped tier:

```
You're out of extra usage · resets 7:30pm (UTC)
```

Mitigations:

- Pre-flight: `claude -p "say hi"` before launching to confirm capacity.
- Budget mode: `--builder-plan-model sonnet` (Sonnet for both plan and exec).

### Turn budget

Default `--builder-max-turns 120`. Features that install native deps (`better-sqlite3`, `sharp`, anything with `node-gyp`), create 5+ files, or run a test suite can exceed 30. Symptom: `Error: Reached max turns (N) after productive work.` Bump to 150–200 for heavy features, or pre-install native deps before launching.

### Reviewer timeout on large codebases

`GEMINI_TIMEOUT_SEC = 300` in `pipeline.py`. For 15+ file reviews, Gemini may take 8+ minutes reading and writing. The rescue path (parsing partial stdout) usually saves it, but if not, raise the constant or split the feature.

### Environment hygiene

The orchestrator strips `ANTHROPIC_API_KEY` from the subprocess env so Claude Code falls back to its credentials file. If you've added other env vars that override CLI auth, filter them similarly in `_run_subprocess()` callers.

---

## Design contracts (don't break these)

These are load-bearing. If you edit prompts or the state machine, preserve them.

- **The orchestrator commits, the Builder must not.** Builder prompts say *"DO NOT commit — the orchestrator handles commits after your turn validates."* Fix turns become separate commits, not amends. Removing this from prompts breaks the audit trail.
- **The Reviewer writes to stdout, not files.** Reviewer prompts say *"Just print the review block to stdout."* The orchestrator parses `[Reviewer]...[/Reviewer]` from stdout via `extract_reviewer_block()` and persists it itself. Asking Gemini to write `conversation.md` directly was the v2.1 design and produced bug #13.
- **Never pipe `git diff` to Gemini via stdin.** Diff markers confuse it. The reviewer prompt tells Gemini to run `git diff` itself or read files directly. Preserve.
- **Caps prevent divergent loops.** Setting caps to 1 disables iteration; raise to 3 only for genuinely hard features.
- **One feature per repo at a time.** Different features running concurrently in the same project race on git operations. The `.lock` file only protects within a single feature directory.

---

## Configuration knobs

| Flag | Default | Purpose |
|---|---|---|
| `--builder-plan-model` | `opus` | Plan turn model. Use `sonnet` for budget mode. |
| `--builder-exec-model` | `sonnet` | Exec/fix turn model. |
| `--builder-max-turns` | `120` | Per-invocation tool-call cap for Builder. |
| `--plan-review-cap` | `2` | Plan-review rounds before ESCALATE. |
| `--code-review-cap` | `2` | Code-review rounds before ESCALATE. |
| `--mock` | off | Replace real CLIs with fakes. Zero tokens. |
| `--no-notifier` | off | Don't auto-launch the notifier child process. |

The Reviewer is always Gemini — cross-family review is the design point.

---

## File layout

```
pipeline.py              # orchestrator, CLI entry point, state machine
notifier.py              # optional Telegram notifier (auto-launched by run)
prompts/
  builder_system.md      # appended to Claude's system prompt every Builder turn
  reviewer_system.md     # prepended to Gemini's user prompt every Reviewer turn
README.md                # developer notes (deeper than this skill)
SKILL.md                 # this file
```

Inside each project, the orchestrator creates:

```
<project_root>/.hermes/features/<slug>-<short-uuid>/
├── state.json              # session IDs, models, round counters, phase
├── status.log              # append-only protocol log (source of truth for session existence)
├── conversation.md         # problem statement + [Builder]/[Reviewer] blocks
├── findings.json           # structured review verdict history
├── result.json             # written at termination
├── cli_failures.log        # stdout/stderr of any failed CLI invocations
├── notifier.log            # notifier child process output
├── notifier.pid            # pid file for cleanup
├── .notifier_position      # tail offset (notifier resumes from here)
└── .lock                   # advisory flock target
```

One feature, one directory. Keep them around as a build archive — they're the debugging artefact.

---

## Notifier configuration

The notifier reads `~/.config/pipeline/notifier.env` (mode 0600):

```
TELEGRAM_BOT_TOKEN_NOTIFICATIONS=<bot-token>
TELEGRAM_CHAT_ID_NOTIFICATIONS=<chat-id>
OPENROUTER_API_KEY=<api-key>
```

Note: the orchestrator itself sends phase-transition notifications via a separate `notify_telegram()` helper that reads `~/.hermes/notifier.env`. **These are two senders with two config files.** If you only configure one, you'll get partial notifications. Either symlink them or maintain both.

---

## Why no LLM in the orchestrator

The orchestrator is deterministic on purpose. Earlier iterations had an LLM drive the loop, which made every routing decision cost tokens, was nondeterministic, and resisted testing. Moving the loop to Python made the pipeline 10× faster and free to regression-test via `--mock`. The agents make intelligent calls inside their turns; the orchestrator is plumbing, not a third reviewer.

If you want to add LLM-driven logic to the orchestrator, stop. Add it to the agents' prompts instead.
