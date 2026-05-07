# Claude-Gemini Build Pipeline

A two-agent build/review pipeline. Claude Code is the Builder, Gemini CLI
is the Reviewer, and a tiny Python state machine sequences them.

Zero dependencies. Uses only the Python standard library.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | The orchestrator. CLI entry point. State machine. |
| `prompts/builder_system.md` | Appended to Claude's system prompt every Builder turn. |
| `prompts/reviewer_system.md` | Prepended to Gemini's user prompt every Reviewer turn. |
| `README.md` | This file. |

## How it works

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

The state machine is in `pipeline.py:step()`. Each phase is a row of the
`if state.phase == "..."` block. Adding or removing a phase is a local
change in that function plus the `END_MARKERS` / `EXPECTED_VERDICTS`
dicts at the top.

## Running it

### 1. Setup a feature

```bash
python3 pipeline.py setup --slug contact-form \
  --request "Build a contact form with localStorage" \
  --project-root /path/to/repo
```

This creates a feature directory and prints its path.

### 2. Run the pipeline

```bash
python3 pipeline.py run /path/to/repo/.features/contact-form-abc12345
```

### Dry-run (mock mode)

`--mock` swaps the real CLI invocations for canned fakes. The mock
Reviewer returns `CHANGES_REQUESTED` on round 0 and `APPROVED` on round 1,
so the loop logic exercises both paths every time. No tokens spent.

```bash
python3 pipeline.py run <dir> --mock
```

Use mock mode after any edit to the state machine, prompts, or validation
logic to confirm the harness still works end-to-end. Takes about 1 second.

### Inspecting state

```bash
python3 pipeline.py status <feature_dir>
```

Shows current phase, round counters, model assignment, and the last 10
lines of `status.log`.

## The protocol

The two agents communicate through three files in the feature directory:

- **`status.log`** — append-only protocol log. Every turn ends with the
  agent appending a sentinel line (e.g., `[Builder plan end]`). The
  orchestrator validates this after every CLI invocation; missing markers
  trigger a protocol-violation error.

- **`conversation.md`** — the actual work. Problem statement at the top,
  followed by alternating `[Builder]...[/Builder]` and `[Reviewer]...[/Reviewer]`
  blocks. Each block ends with a `VERDICT: <X>` line that the orchestrator
  parses for routing decisions.

- **`findings.json`** — structured history of reviewer verdicts. Written
  by the orchestrator after each Reviewer turn.

The agents also do their actual work in the project directory (writing
code, committing, etc.) — the feature directory is just the protocol
channel.

## The state.json fields

Most are obvious; a few worth flagging:

- `builder_session_id` / `reviewer_session_id` — UUIDs. The Builder one
  is passed as `claude --session-id <uuid>` on the first call, then
  `claude --resume <uuid>` thereafter. Gemini doesn't accept dictated
  IDs, so we just track whether its session has been started yet and
  use `gemini --resume -p` for subsequent calls.

- `builder_session_started` / `reviewer_session_started` — boolean,
  flipped after the first successful invocation. Distinguishes "create"
  from "resume" calls.

- `phase` — current state machine state. Persisted on every transition,
  so the script could in principle be killed and restarted.

- `plan_review_round` / `code_review_round` — counters. Cap is checked
  AFTER incrementing, so a cap of 2 means at most 2 review rounds.

## The validation contract

After every CLI invocation, the orchestrator runs `validate_turn()`:

1. Did the agent append the expected end-marker to `status.log`?
2. Is there a `[Builder]` or `[Reviewer]` block at the end of `conversation.md`?
3. Does that block end with a `VERDICT: <X>` line?
4. Is the verdict in the expected set for this turn kind?

Failing any of these transitions to `ERROR` with a specific reason. The
agent gets one shot per turn; we don't retry on protocol violations
because protocol violations usually indicate a prompt or model issue
that won't fix itself on retry.

## Editing the prompts

`prompts/builder_system.md` and `prompts/reviewer_system.md` are loaded
fresh on every CLI invocation. Edit, save, run mock mode to sanity-check,
done. No reload, no restart.

The most common reason to edit them:

- **Reviewer is too pedantic** — strengthen the "do not invent issues"
  language and the BLOCKER definition.
- **Builder forgets to write the verdict line** — make the verdict
  instruction louder; consider moving it from the system prompt to the
  per-turn user prompt.
- **End-marker drift** — the agent writes the marker but also extra junk
  to status.log. Tighten the "do not write any other lines" instruction.

After any prompt edit, run mock mode first, then a real cheap feature,
before trusting it on real work.

## Authentication and limits

The pipeline assumes:

- `claude` is logged in (Pro/Max/Team plan or API key).
- `gemini` is logged in (`/auth` already done).

If either is unauthenticated, the subprocess will fail with a useful
stderr that gets surfaced as `ERROR`. The orchestrator does not try to
fix auth problems.

Token usage per feature, rough order of magnitude:

- 1 Opus/Sonnet plan turn (small, maybe 5–10K tokens).
- 0–2 plan revisions (similar).
- 1 Sonnet exec turn (large, 20–100K tokens depending on feature size).
- 0–2 Sonnet fix turns (smaller than exec).
- 2–4 Gemini review turns (10–30K tokens each).

For a typical feature, expect a few dollars of API spend if you're on
metered billing. On an unlimited plan or Enterprise seat, this is well
within normal use.

## Known sharp edges

- **Resume-from-mid-run is untested.** If you kill `pipeline.py run` mid-build
  and restart it, the script will read `state.json`, find the current
  phase, and try to step from there. The state machine SHOULD handle this
  — every transition saves state — but failure modes have not been
  exercised carefully (e.g., what if the agent already wrote a marker but
  the orchestrator died before transitioning?). If you need this, write
  the test first.

- **Concurrent invocations of the same feature** are blocked by `flock`,
  but two different features running concurrently in the same project
  could race on git operations. Don't do this. One feature at a time per
  repo.

- **The Reviewer is told not to use shell tools, but it still tries.**
  Gemini speculatively attempts `run_shell_command` even when reviewing.
  The current `--yolo` flag suppresses the prompts; if Google ever
  changes this behavior, the prompts may need updating.

- **Auto-merge is deliberately not implemented.** The pipeline ends at
  `DONE` and hands off to a human for final review and merge. Adding
  auto-merge would be a one-line addition to the state machine, but
  giving an agent push access to main is a design choice that should be
  made explicitly, not by default.

## Adding a new phase

If you want to add, say, a "post-merge smoke test" phase:

1. Add the phase name to the `PHASES` list and to `TERMINAL_PHASES` if
   it ends the run.
2. If it involves an agent turn, add an entry to `END_MARKERS` and
   `EXPECTED_VERDICTS`.
3. If it's a Builder turn, extend `builder_turn_prompt()` with a new
   `kind` branch.
4. Add a new `if state.phase == "...":` block in `step()`.
5. Run mock mode. Make sure the new phase is reachable and terminates.

## Why no LLM in the orchestrator

The orchestrator is deterministic on purpose. Earlier iterations had an
LLM agent drive the loop, which meant every routing decision cost tokens,
was nondeterministic, and resisted testing. Moving the loop to Python
made the pipeline 10× faster, infinitely more debuggable, and free to
regression-test via `--mock`.

If you find yourself wanting to add LLM-driven logic to the orchestrator
(e.g., "have GPT decide whether the verdict really means APPROVED"),
stop. The agents already make those calls inside their turns; the
orchestrator is plumbing, not a third reviewer.
