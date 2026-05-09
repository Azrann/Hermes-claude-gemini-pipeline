# Installation

One-time setup for a new machine. After this, see [SKILL.md](SKILL.md) for runtime usage and [README.md](README.md) for architecture.

Estimated time: 15–25 minutes, mostly waiting for npm and creating Telegram credentials.

---

## 1. System prerequisites

- **Python 3.10+** (stdlib only, no `pip install` step):
  ```bash
  python3 --version  # must be ≥ 3.10
  ```
- **Node.js 18+** (for the two CLIs):
  ```bash
  node --version
  ```
- **git**:
  ```bash
  git --version
  ```
- **gh** (optional, only if you'll use the post-DONE PR creation step):
  ```bash
  gh --version
  ```

If any are missing, install them via your package manager before continuing.

---

## 2. Claude Code CLI

The Builder agent.

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

`claude login` opens a browser flow. Sign in with the Anthropic account that holds your Pro/Max/Team plan or API credit.

Verify:

```bash
claude -p "say hi"
```

Should print a one-line greeting. If you get `Invalid API key`, see the troubleshooting note at the bottom of this file about `ANTHROPIC_API_KEY` in your shell env.

**Plan considerations.** The pipeline defaults to Opus for plan turns and Sonnet for exec/fix. On metered billing a single feature costs a few dollars; on a Max plan or Enterprise seat it's well within normal use. If you're on a capped tier, set `--builder-plan-model sonnet` when launching.

---

## 3. Gemini CLI

The Reviewer agent.

```bash
npm install -g @google/gemini-cli
gemini
```

This drops you into the interactive REPL. Run:

```
/auth
```

and complete the browser flow. Then exit (`/quit`).

**Trust the workspace.** Gemini CLI ≥ 0.40 refuses to run in directories it hasn't seen interactively. Either:

- Open the project once interactively (`cd <project_root> && gemini`, accept trust prompt, `/quit`), **or**
- Export `GEMINI_CLI_TRUST_WORKSPACE=true` in your shell rc file:
  ```bash
  echo 'export GEMINI_CLI_TRUST_WORKSPACE=true' >> ~/.bashrc  # or ~/.zshrc
  ```

The pipeline already passes `--skip-trust`, so this is belt-and-suspenders, but the env var is more reliable across CLI version bumps.

Verify:

```bash
gemini -p "say hi"
```

Should print a one-line greeting.

---

## 4. Notifier configuration (optional but recommended)

The pipeline can send Telegram notifications throughout the run — phase transitions from the orchestrator, plus LLM-enriched review summaries from the notifier child process. Without these, you'll need to `cat result.json` and grep `conversation.md` manually.

If you don't want notifications, skip this section and pass `--no-notifier` when launching.

### 4a. Create a Telegram bot

1. Open Telegram, message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow prompts, name it whatever you want.
3. BotFather replies with a token like `7891234567:AAH...`. Save it.
4. Send any message to your new bot from your account, then visit:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat":{"id": <NUMBER>}` in the JSON. Save that number — it's your chat ID.

### 4b. Create an OpenRouter API key

The notifier uses OpenRouter (Llama 3 8B by default) to generate Spanish summaries of review findings. Cheap — a few cents per pipeline run.

1. Sign up at [openrouter.ai](https://openrouter.ai).
2. Add credit ($5 lasts a long time at this usage rate).
3. Create an API key in the dashboard. Save it.

### 4c. Write the config files

Two files, two paths. The orchestrator reads `~/.hermes/notifier.env` for phase notifications; the notifier child process reads `~/.config/pipeline/notifier.env` for review summaries.

Create both with mode `0600`:

```bash
mkdir -p ~/.hermes ~/.config/pipeline

cat > ~/.config/pipeline/notifier.env <<'EOF'
TELEGRAM_BOT_TOKEN_NOTIFICATIONS=7891234567:AAH...your-token...
TELEGRAM_CHAT_ID_NOTIFICATIONS=123456789
OPENROUTER_API_KEY=sk-or-v1-...your-key...
EOF

chmod 600 ~/.config/pipeline/notifier.env

# The orchestrator reads from ~/.hermes/notifier.env. Symlink to keep them in sync:
ln -s ~/.config/pipeline/notifier.env ~/.hermes/notifier.env
```

The notifier hard-fails if the file isn't `0600` — this is intentional, the file holds three credentials.

Verify by sending a test message:

```bash
source ~/.config/pipeline/notifier.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN_NOTIFICATIONS}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID_NOTIFICATIONS}" \
  -d "text=pipeline notifier test"
```

You should see "pipeline notifier test" arrive in your Telegram chat.

---

## 5. Pipeline files

Clone or copy the pipeline into a directory of your choice. The layout you need:

```
<pipeline_dir>/
├── pipeline.py
├── notifier.py
└── prompts/
    ├── builder_system.md
    └── reviewer_system.md
```

You'll invoke `python3 <pipeline_dir>/pipeline.py ...` from anywhere, pointing `--project-root` at the git repo you want the agents to work in.

There is no install step for the pipeline itself — it's pure stdlib Python. Verify the files parse:

```bash
python3 -c "import ast; ast.parse(open('<pipeline_dir>/pipeline.py').read())"
python3 -c "import ast; ast.parse(open('<pipeline_dir>/notifier.py').read())"
```

---

## 6. Smoke test (mock mode)

This runs the full state machine end-to-end without spending tokens or touching the real CLIs. It's the install-verification step.

```bash
mkdir -p /tmp/pipeline-smoke && cd /tmp/pipeline-smoke
git init -q
git commit --allow-empty -m init -q

python3 <pipeline_dir>/pipeline.py setup \
  --slug smoke-test \
  --request "Smoke test" \
  --project-root /tmp/pipeline-smoke

FEATURE_DIR=$(ls -d /tmp/pipeline-smoke/.hermes/features/smoke-test-* | head -1)
python3 <pipeline_dir>/pipeline.py run "$FEATURE_DIR" --mock
```

Expected:

- `result.json` exists with `"final_status": "DONE"`.
- `conversation.md` has alternating `[Builder]` / `[Reviewer]` blocks ending in `VERDICT: APPROVED`.
- `status.log` has all the expected end-markers.
- The whole thing took about 1 second.

If any of those fail, the install isn't complete — see Troubleshooting below.

Mock mode does **not** call Telegram or OpenRouter, so a working smoke test doesn't verify notifier credentials. Use the curl test in section 4c for that.

Clean up:

```bash
rm -rf /tmp/pipeline-smoke
```

---

## 7. First real run

Once mock mode passes and the curl notifier test works, you're ready. From a clean git repo:

```bash
cd <your-real-repo>
git status  # must be clean — the orchestrator hard-fails on a dirty tree

python3 <pipeline_dir>/pipeline.py setup \
  --slug hello-world \
  --request "Add a HELLO.md file with the text 'hello world'" \
  --project-root "$PWD"
```

Pick a trivially small feature for the first run so a single Opus plan turn + Sonnet exec turn finishes in 2–3 minutes. After that, see [SKILL.md](SKILL.md) for the launch-and-handoff workflow.

---

## Troubleshooting

### `claude -p "say hi"` returns `Invalid API key`

You have `ANTHROPIC_API_KEY` set in your environment, and it's wrong or stale. The pipeline strips this var before invoking Claude (so it falls back to the credentials file from `claude login`), but for the verification command above, either unset it:

```bash
unset ANTHROPIC_API_KEY
claude -p "say hi"
```

or set it correctly. The credentials file written by `claude login` lives at `~/.claude/`.

### `gemini -p "say hi"` returns trust-workspace error

You haven't completed step 3's trust setup. Either open the project interactively once or export `GEMINI_CLI_TRUST_WORKSPACE=true`.

### Smoke test fails with `❌ project_root no es un directorio`

You ran `setup` before `git init`. The orchestrator's `verify_prerequisites()` requires a `.git` directory in `--project-root`.

### Smoke test fails with `❌ Working tree no está limpio`

The directory has uncommitted changes. The orchestrator refuses to launch on a dirty tree to avoid mixing manual and agent commits. Either commit or stash before running.

### Smoke test passes but real run hangs

Most common cause: `claude` or `gemini` is unauthenticated and the subprocess is waiting on a login prompt that never arrives. Run `claude -p "say hi"` and `gemini -p "say hi"` directly to confirm both work non-interactively.

### Telegram test message arrives but pipeline notifications don't

You configured one of the two notifier env files but not the other (or the symlink). The orchestrator and the notifier child process read different paths — see section 4c. `ls -la ~/.hermes/notifier.env ~/.config/pipeline/notifier.env` should show both pointing to a real file with mode `0600`.

### `protocol violation: ...` errors on real runs

Not an install issue — see the Recovery Playbook in [SKILL.md](SKILL.md).
