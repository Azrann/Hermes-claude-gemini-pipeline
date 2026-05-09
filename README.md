# Claude-Gemini Build Pipeline

A two-agent build/review pipeline. Claude Code is the Builder, Gemini CLI is the Reviewer, and a small Python state machine sequences them. Zero dependencies — stdlib only.

This README is for developers editing `pipeline.py`, the prompts, or the notifier. If you're trying to **use** the pipeline, see [SKILL.md](SKILL.md). If you're setting it up on a new machine for the first time, see [INSTALL.md](INSTALL.md).

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | Orchestrator. CLI entry point. State machine. |
| `notifier.py` | Optional Telegram notifier. Auto-launched by `pipeline.py run` unless `--no-notifier`. |
| `prompts/builder_system.md` | Appended to Claude's system prompt every Builder turn. |
| `prompts/reviewer_system.md` | Prepended to Gemini's user prompt every Reviewer turn. |
| `SKILL.md` | LLM-facing runtime guide. |
| `INSTALL.md` | First-time setup. |
| `README.md` | This file. |

## State machine

```
┌───────────────────────────────────────────────┐
│                                                │
│  INIT → BUILDER_PLAN  ──→ REVIEWER_PLAN_REVIEW │
│                              │                 │
│      APPROVED? ──no──→ BUILDER_PLAN_REVISE     │
│              │              │                   │
│              yes            └──→ (loop, cap=2) │
│              ↓                                 │
│         BUILDER_EXEC                           │
│              ↓                                 │
│      REVIEWER_CODE_REVIEW                      │
│              │                                 │
│      APPROVED? ──no──→ BUILDER_FIX             │
│              │              │                   │
│              yes            └──→ (loop, cap=2) │
│              ↓                                 │
│         MERGE_READY → DONE                     │
│                                                │
└───────────────────────────────────────────────┘
```

Implementation lives in `step()`. Each phase is a row of the `if state.phase == "..."` block. Adding or removing a phase is a local change in that function plus the `END_MARKERS` and `EXPECTED_VERDICTS` dicts at the top of the file.

## The protocol

The two agents communicate through three files in the feature directory (`<project_root>/.hermes/features/<slug>-<id>/`):

- **`status.log`** — append-only protocol log. Every turn ends with the agent appending a sentinel line (e.g., `[Builder plan end]`). The orchestrator validates this after every CLI invocation; missing markers trigger `ERROR`. Append-only is load-bearing: it's how the orchestrator derives whether a CLI session exists, crash-safe across restarts.

- **`conversation.md`** — the actual work. Problem statement at the top, followed by alternating `[Builder]...[/Builder]` and `[Reviewer]...[/Reviewer]` blocks. Each block ends with a `VERDICT: <X>` line that the orchestrator parses for routing.

- **`findings.json`** — structured history of reviewer verdicts. Written by the orchestrator (not the Reviewer) after each successful Reviewer turn.

The agents do their actual code-writing work in the project directory; the feature directory is just the protocol channel.

### Who writes what

This trips people up:

- **Builder writes to `conversation.md` and `status.log` directly** via its `Bash`/`Write` tools. The orchestrator validates after the turn returns.
- **Reviewer writes to stdout only.** Its prompt explicitly says *"Just print the review block to stdout."* The orchestrator parses `[Reviewer]...[/Reviewer]` from the captured stdout via `extract_reviewer_block()` and persists it to `conversation.md` itself, then appends the end-marker to `status.log` itself.

This split exists because the previous design (Reviewer writes files directly) produced bug #13 — Gemini in headless mode would emit the review to stdout, never call its file-writing tool, and the orchestrator would find an empty `conversation.md`. Don't undo this without re-solving that.

### Git contract

- **The orchestrator commits, the Builder must not.** Builder prompts contain `DO NOT commit — the orchestrator handles commits after your turn validates.` This is enforced in prompt language, not in code. Fix turns become separate commits (not amends), which preserves an audit trail of "what the Builder did" vs "what the Reviewer asked for."
- **The orchestrator manages the feature branch.** `ensure_feature_branch()` runs before `BUILDER_EXEC` and is idempotent. The Builder is told *"You are currently ON that branch — do NOT switch branches"* and is denied `git checkout`/`branch`/`push`/`merge`.
- **Auto-merge is deliberately not implemented.** Pipeline ends at `DONE`. Adding auto-merge would be a one-line state machine change, but giving an agent push access to main is a design choice that should be made explicitly, not by default.

## State fields worth flagging

Most of `State` is self-evident. A few that aren't:

- `builder_session_id` / `reviewer_session_id` — UUIDs generated at setup. The Builder ID is passed as `claude --session-id <uuid>` on the first call, then `claude --resume <uuid>` thereafter. Whether to use `--session-id` or `--resume` is **derived from `status.log`**, not stored in `state.json`. See `builder_has_completed_a_turn()` / `reviewer_has_completed_a_turn()`. This makes resumes crash-safe — a partial state.json write can't desync session existence from reality.

- `reviewer_session_id` exists for parity but is currently vestigial. Gemini doesn't accept dictated session IDs; we use `gemini --resume` (no ID argument) for subsequent calls. The field is preserved in case a future Gemini CLI version supports dictated IDs.

- `phase` — current state machine state. Persisted on every transition via `save_state()`, so the script can be killed and restarted (see "Resuming a failed run" in SKILL.md).

- `plan_review_round` / `code_review_round` — incremented after a `CHANGES_REQUESTED` verdict, before the cap check. A cap of 2 means at most 2 review rounds.

## The validation contract

After every CLI invocation, `validate_turn()` checks:

1. The agent appended the expected end-marker to `status.log`.
2. There's a `[Builder]` or `[Reviewer]` block at the end of `conversation.md`.
3. The block ends with a `VERDICT: <X>` line.
4. The verdict is in the expected set for this turn kind.

Failing any of these transitions to `ERROR` with a specific reason. **The agent gets one shot per turn** — protocol violations don't retry, because they usually indicate a prompt or model issue that won't fix itself on retry.

### The Gemini timeout rescue

Gemini sometimes completes a review and then hangs before the subprocess returns. `run_reviewer_turn()` has a rescue path: on `timed_out=True`, it parses the partial stdout for a `[Reviewer]` block and persists it if found. This is not a retry — it's recovering work that already happened.

Anyone reading the validation logic should know this exists. If you tighten the timeout or change how Gemini is invoked, re-test the rescue.

## Editing the prompts

`prompts/builder_system.md` and `prompts/reviewer_system.md` are loaded fresh on every CLI invocation (`load_system_prompt()`). Edit, save, run mock mode to sanity-check. No reload, no restart.

Common reasons to edit:

- **Reviewer is too pedantic** — strengthen the "do not invent issues" language and tighten the BLOCKER definition. Don't reduce the round cap; that hides the symptom.
- **Builder forgets the verdict line** — make the verdict instruction louder; consider moving from system prompt to per-turn user prompt (those are constructed in `builder_turn_prompt()` / `reviewer_turn_prompt()`).
- **End-marker drift** — the agent writes the marker but also extra junk to status.log. Tighten the "do not write any other lines" instruction.

After any prompt edit, run mock mode first, then a cheap real feature, before trusting it on real work. The mock CLIs (`_mock_builder` / `_mock_reviewer`) don't exercise the prompts themselves, but they do verify the validation logic still accepts the protocol shape you've described.

## Mock mode

`--mock` swaps `_INVOKE_BUILDER` and `_INVOKE_REVIEWER` for canned fakes. Zero tokens, ~1s wall-clock. The mock Reviewer returns `CHANGES_REQUESTED` on round 0 and `APPROVED` on round 1, so a single mock run exercises both the loop and the cap-met path.

Use it after any edit to:

- The state machine in `step()`.
- `END_MARKERS`, `EXPECTED_VERDICTS`, or `PHASES`.
- The validation contract in `validate_turn()`.
- The prompt-construction functions (`builder_turn_prompt`, `reviewer_turn_prompt`).

Don't use it as a substitute for a real cheap feature run when changing the system prompts themselves — the mock CLIs don't read the prompts.

## Notifier architecture

Two senders, deliberately separate:

- **Orchestrator** (`pipeline.py:notify_telegram()`) sends phase-transition notifications synchronously inside `transition_and_notify()`. Reads `~/.hermes/notifier.env`. These are deterministic strings — emoji + slug + phase name.

- **Notifier child process** (`notifier.py`) tails `status.log`, classifies events, and sends LLM-enriched review summaries via OpenRouter (default: Llama 3 8B). Reads `~/.config/pipeline/notifier.env`. The split-responsibility pattern — Python builds a deterministic prefix from `findings.json`, LLM generates a 1-line plain-text summary in Spanish — keeps verdicts ground-truth accurate while detail text reads naturally.

The notifier is auto-launched by `run_pipeline()` via `launch_notifier()`, with `atexit` cleanup. `--no-notifier` skips the launch. `kill_existing_notifier()` cleans up stale notifiers from prior runs (via pid file + `pgrep` fallback) before launching a new one.

If you're consolidating the two config files into one path, change `notify_telegram()` to read the same path as `notifier.py` and update INSTALL.md accordingly.

## Token usage per feature

Rough order of magnitude:

- 1 Opus plan turn — 15–30K tokens (prompt + system prompt + small output).
- 0–2 plan revisions — similar.
- 1 Sonnet exec turn — 30–150K tokens depending on feature size and tool-call count.
- 0–2 Sonnet fix turns — usually smaller than exec.
- 2–4 Gemini review turns — 10–40K tokens each, scales with codebase size during code review.

Metered billing: a few dollars per typical feature. Max plan or Enterprise: well within normal use.

## Adding a new phase

For a hypothetical "post-merge smoke test" phase:

1. Add to `PHASES` (and `TERMINAL_PHASES` if it ends the run).
2. If it's an agent turn, add an entry to `END_MARKERS` and `EXPECTED_VERDICTS`.
3. If it's a Builder turn, extend `builder_turn_prompt()` with a new `kind` branch. Same for `reviewer_turn_prompt()` if Reviewer.
4. Add an `if state.phase == "...":` block in `step()`. Wire `transition_and_notify()` to a sensible Telegram message in `transition_and_notify()`'s phase→message map.
5. Run mock mode. Confirm the new phase is reachable, terminates, and doesn't break existing transitions.

## Sharp edges

- **Concurrent invocations of the same feature** are blocked by `flock` on `<feature_dir>/.lock`. Two *different* features running concurrently in the same project will race on git operations — the lock doesn't cover that. One feature at a time per repo.

- **Resume-from-mid-run works** in v2.2 because session existence derives from `status.log` (append-only), but the recovery procedure is human-driven (edit `state.json`, delete `result.json`, re-run). See SKILL.md's Recovery Playbook for the steps. The orchestrator does not auto-resume on launch.

- **The Reviewer is told to use `git diff` directly** in `reviewer_turn_prompt()`, not via stdin. Earlier docs warned against piping diffs to Gemini — that warning still applies if you change the prompt. Diff markers confuse the model.

- **`--yolo` suppresses Gemini's tool-confirmation prompts.** If a future CLI version changes this flag's behaviour, the Reviewer turn will start hanging on confirmation prompts and the prompts may need updating.

## Why no LLM in the orchestrator

The orchestrator is deterministic on purpose. Earlier iterations had an LLM agent drive the loop, which meant every routing decision cost tokens, was nondeterministic, and resisted testing. Moving the loop to Python made the pipeline an order of magnitude faster, debuggable with `print()`, and free to regression-test via `--mock`.

If you find yourself wanting to add LLM-driven logic to the orchestrator (e.g., "have a model decide whether the verdict really means APPROVED"), stop. The agents already make those calls inside their turns. The orchestrator is plumbing, not a third reviewer.
