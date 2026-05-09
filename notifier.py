#!/usr/bin/env python3
"""
Pipeline Notifier — Level 2
Pattern-matching + LLM enrichment for pipeline events.

Tails status.log and sends real-time notifications via Telegram.
Configuration defaults to ~/.hermes/notifier.env (mode 0600).
Override with --env-path and --error-log.
"""

import argparse
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path

# ═════════════════════════════════════════════════════════════════════════════
# config (overridden by CLI --env-path and --error-log; see main())

ENV_PATH = Path.home() / ".hermes" / "notifier.env"
ERROR_LOG = Path.home() / ".hermes" / "notifier-errors.log"
_current_error_log = ERROR_LOG  # overridden by --error-log

NO_ACTIVITY_TIMEOUT_SEC = 60 * 60  # 60 minutes
SLEEP_INTERVAL_SEC = 1.0
HTTP_TIMEOUT_SEC = 10
HTTP_RETRY_BACKOFF_SEC = 2

OPENROUTER_URL = "openrouter.ai"
LLM_MODEL = "meta-llama/llama-3-8b-instruct"
LLM_MAX_TOKENS = 200
LLM_TEMPERATURE = 0.3

SYSTEM_PROMPT = """You are a notification agent that summarizes pipeline findings in Spanish.

You will receive findings from a code review and you must produce a SHORT summary (max 1 line, ideally under 100 chars) that highlights the top finding.

Rules:
- Output plain text only, no emojis, no markdown, no quotes around output, no preamble.
- If there are BLOCKERs, mention the top BLOCKER specifically with its description.
- If there are no BLOCKERs but IMPORTANT findings, mention the top IMPORTANT.
- If everything is clean (no findings), output literally: sin issues
- If you cannot understand the input, output literally: sin detalles disponibles

Examples:

Input: Verdict: APPROVED. 0 BLOCKERs, 1 IMPORTANT (race condition in localStorage useEffect), 0 NITs.
Output: 1 IMPORTANT: race condition en localStorage useEffect

Input: Verdict: APPROVED. 0 BLOCKERs, 0 IMPORTANT, 0 NITs.
Output: sin issues

Input: Verdict: CHANGES_REQUESTED. 1 BLOCKER (SQL injection in /api/bookings POST handler), 0 IMPORTANT.
Output: BLOCKER: SQL injection en /api/bookings POST handler

Input: Verdict: CHANGES_REQUESTED. 1 BLOCKER (no error handling on database connection), 0 IMPORTANT.
Output: BLOCKER: no error handling en database connection
"""

TERMINAL_PATTERNS = [
    "phase=DONE",
    "phase=ESCALATE",
    "phase=ERROR",
]

# ═════════════════════════════════════════════════════════════════════════════
# 1. load_config

def load_config(env_path: Path) -> dict:
    """Read notifier.env (mode 0600) and return config dict."""
    if not env_path.exists():
        raise FileNotFoundError(f"Missing config file: {env_path}")

    mode = env_path.stat().st_mode
    if (mode & 0o077) != 0:
        raise PermissionError(f"Config file must be 0600 (got {oct(mode)}): {env_path}")

    config = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    required = ["TELEGRAM_BOT_TOKEN_NOTIFICATIONS", "TELEGRAM_CHAT_ID_NOTIFICATIONS", "OPENROUTER_API_KEY"]
    missing = [k for k in required if k not in config or not config[k]]
    if missing:
        raise KeyError(f"Missing required env vars: {', '.join(missing)}")

    return config

# ═════════════════════════════════════════════════════════════════════════════
# 2. tail_follow

def tail_follow(status_log: Path, checkpoint: Path):
    """Yield new lines from status_log. Persists position in checkpoint
    so a relaunched notifier resumes where it left off rather than
    reprocessing the whole log."""
    offset = 0
    if checkpoint.exists():
        try:
            offset = int(checkpoint.read_text().strip() or "0")
        except ValueError:
            offset = 0

    while True:
        # Reopen each iteration so we don't hold a file handle while sleeping
        with open(status_log) as f:
            f.seek(offset)
            line = f.readline()
            while line:
                if line.endswith("\n"):
                    yield line.rstrip("\n")
                    offset = f.tell()
                    checkpoint.write_text(str(offset))
                else:
                    # incomplete line — wait for the rest, don't advance offset
                    break
                line = f.readline()
        time.sleep(SLEEP_INTERVAL_SEC)

# ═════════════════════════════════════════════════════════════════════════════
# 3. extract_slug

def extract_slug(feature_dir: Path) -> str:
    """contact-form-abc12345 → contact-form"""
    name = feature_dir.name
    # Strip trailing -<8hex> suffix
    if len(name) > 9 and name[-9] == "-" and all(c in "0123456789abcdef" for c in name[-8:]):
        return name[:-9]
    return name

# ═════════════════════════════════════════════════════════════════════════════
# 4. classify_event

def classify_event(line: str) -> tuple | None:
    """Return event_type for reviewer summary events, None for everything else.
    Pipeline now emits all phase-transition notifications directly."""
    if "[Reviewer plan review end]" in line:
        return "reviewer_plan_complete"
    if "[Reviewer code review end]" in line:
        return "reviewer_code_complete"
    if "phase=ERROR" in line:
        return "error"
    if "phase=ESCALATE" in line:
        return "escalate"
    if "phase=DONE" in line:
        return "done"
    return None


# ═════════════════════════════════════════════════════════════════════════════
# 5. extract_simple_message


# ═════════════════════════════════════════════════════════════════════════════
# 6. extract_rich_context

def _last_block_between(text: str, start_marker: str, end_marker: str) -> str:
    """Return the last occurrence of text between start_marker and end_marker."""
    blocks = []
    idx = 0
    while True:
        s = text.find(start_marker, idx)
        if s == -1:
            break
        e = text.find(end_marker, s + len(start_marker))
        if e == -1:
            blocks.append(text[s:])
            break
        blocks.append(text[s:e + len(end_marker)])
        idx = e + len(end_marker)
    if not blocks:
        return ""
    return blocks[-1]


def _last_reviewer_block(feature_dir: Path) -> str:
    conv_path = feature_dir / "conversation.md"
    if not conv_path.exists():
        return ""
    text = conv_path.read_text(encoding="utf-8", errors="replace")
    return _last_block_between(text, "[Reviewer", "[/Reviewer]")


def _last_builder_block(feature_dir: Path) -> str:
    conv_path = feature_dir / "conversation.md"
    if not conv_path.exists():
        return ""
    text = conv_path.read_text(encoding="utf-8", errors="replace")
    return _last_block_between(text, "[Builder", "[/Builder]")


def _findings_entry_at(feature_dir: Path, kind: str, index: int) -> dict:
    """Get the N-th findings entry of a given kind (0-indexed).
    kind is 'reviewer_plan' or 'reviewer_code'.
    Returns {} if not enough entries exist yet."""
    findings_path = feature_dir / "findings.json"
    if not findings_path.exists():
        return {}
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        history = data.get("history", []) if isinstance(data, dict) else []
        # Filter by kind
        matching = [e for e in history if e.get("kind") == kind]
        if 0 <= index < len(matching):
            return matching[index]
        return {}
    except Exception:
        return {}


def _findings_summary(entry: dict) -> str:
    lines = []
    verdict = entry.get("verdict") or entry.get("status") or entry.get("result")
    if verdict:
        lines.append(f"Verdict: {verdict}")
    blockers = entry.get("blockers") or entry.get("BLOCKERs") or []
    important = entry.get("important") or entry.get("IMPORTANTs") or []
    nits = entry.get("nits") or entry.get("NITs") or []
    if blockers:
        lines.append(f"BLOCKERs: {len(blockers)} — {blockers[0] if isinstance(blockers, list) else blockers}")
    if important:
        lines.append(f"IMPORTANT: {len(important)} — {important[0] if isinstance(important, list) else important}")
    if nits:
        lines.append(f"NITs: {len(nits)}")
    return "\n".join(lines) if lines else str(entry)


def _extract_verdict(entry: dict) -> str:
    return entry.get("verdict") or entry.get("status") or entry.get("result") or "UNKNOWN"


def extract_rich_context(event: str, feature_dir: Path, line: str = "",
                         event_index: int = 0) -> str:
    if event == "reviewer_plan_complete":
        block = _last_reviewer_block(feature_dir)
        kind = "reviewer_plan"
        entry = _findings_entry_at(feature_dir, kind, event_index)
        ctx_parts = ["Review type: plan review"]
        if entry:
            ctx_parts.append("Findings:\n" + _findings_summary(entry))
        if block:
            ctx_parts.append("Latest reviewer block:\n" + block[-1500:])
        return "\n\n".join(ctx_parts)

    if event == "reviewer_code_complete":
        block = _last_reviewer_block(feature_dir)
        kind = "reviewer_code"
        entry = _findings_entry_at(feature_dir, kind, event_index)
        ctx_parts = ["Review type: code review"]
        if entry:
            ctx_parts.append("Findings:\n" + _findings_summary(entry))
        if block:
            ctx_parts.append("Latest reviewer block:\n" + block[-1500:])
        return "\n\n".join(ctx_parts)

    if event == "error":
        parts = [f"Error line: {line}"]
        # last 20 lines of status.log
        status_path = feature_dir / "status.log"
        if status_path.exists():
            lines = status_path.read_text(encoding="utf-8", errors="replace").splitlines()
            parts.append("Last 20 status lines:\n" + "\n".join(lines[-20:]))
        # cli_failures.log
        failures_path = feature_dir / "cli_failures.log"
        if failures_path.exists():
            parts.append("cli_failures.log:\n" + failures_path.read_text(encoding="utf-8", errors="replace")[-1500:])
        return "\n\n".join(parts)

    if event == "escalate":
        ctx_parts = [f"Escalate line: {line}"]
        # Determine if round cap or NEED_INPUT by looking at recent review/builder blocks
        rev_block = _last_reviewer_block(feature_dir)
        builder_block = _last_builder_block(feature_dir)
        # heuristic: if "NEED_INPUT" in line or builder_block is the most recent with a question
        if "NEED_INPUT" in line.upper() or (builder_block and "?" in builder_block[-500:]):
            if builder_block:
                ctx_parts.append("Latest builder block:\n" + builder_block[-1500:])
        else:
            if rev_block:
                ctx_parts.append("Latest reviewer block:\n" + rev_block[-1500:])
        return "\n\n".join(ctx_parts)

    if event == "done":
        ctx_parts = []
        findings_path = feature_dir / "findings.json"
        if findings_path.exists():
            ctx_parts.append("Full findings:\n" + findings_path.read_text(encoding="utf-8", errors="replace")[-3000:])
        builder_block = _last_builder_block(feature_dir)
        if builder_block:
            ctx_parts.append("Latest builder block:\n" + builder_block[-1500:])
        # result.json for round counts
        result_path = feature_dir / "result.json"
        if result_path.exists():
            ctx_parts.append("Result:\n" + result_path.read_text(encoding="utf-8", errors="replace"))
        return "\n\n".join(ctx_parts)

    return line

# ═════════════════════════════════════════════════════════════════════════════
# 7. enrich_with_llm

def _openrouter_request(config: dict, messages: list) -> str | None:
    """Call OpenRouter chat completions. Return content or None on failure."""
    api_key = config["OPENROUTER_API_KEY"]

    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    })

    http_referer = config.get("HTTP_REFERER", "https://github.com")
    x_title = config.get("X_TITLE", "Pipeline Notifier")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": http_referer,
        "X-Title": x_title,
    }

    def _do_request():
        conn = http.client.HTTPSConnection(OPENROUTER_URL, timeout=HTTP_TIMEOUT_SEC)
        try:
            conn.request("POST", "/api/v1/chat/completions", body=body.encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_body = resp.read().decode("utf-8", errors="replace")
            conn.close()
            return status, resp_body
        finally:
            conn.close()

    status, resp_body = _do_request()

    if 500 <= status < 600:
        time.sleep(HTTP_RETRY_BACKOFF_SEC)
        status, resp_body = _do_request()

    if status != 200:
        log_error(f"OpenRouter HTTP {status}: {resp_body[:500]}")
        return None

    try:
        data = json.loads(resp_body)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log_error(f"OpenRouter parse error: {e} — body: {resp_body[:500]}")
        return None


def enrich_with_llm(event: str, context: str, config: dict,
                    feature_dir: Path = None, event_index: int = 0) -> str | None:
    """Build the message: deterministic prefix from code + LLM-generated detail.
    Returns None if LLM call fails."""

    if event in ("reviewer_plan_complete", "reviewer_code_complete"):
        kind = "reviewer_plan" if event == "reviewer_plan_complete" else "reviewer_code"
        entry = _findings_entry_at(feature_dir, kind, event_index)
        review_label = "plan" if event == "reviewer_plan_complete" else "code review"
        prefix = f"📝 Resumen {review_label}"
        llm_input = _findings_summary(entry)

    elif event == "error":
        prefix = "❌ ERROR"
        llm_input = context

    elif event == "escalate":
        prefix = "⚠️ ESCALATE"
        llm_input = context

    elif event == "done":
        prefix = "🎉 Pipeline completo"
        llm_input = context

    else:
        prefix = "📢 Evento"
        llm_input = context

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": llm_input + "\n\nGenerate the summary."},
    ]
    summary = _openrouter_request(config, messages)
    if summary is None:
        return None

    return f"{prefix}: {summary}"

# ═════════════════════════════════════════════════════════════════════════════
# 8. pattern_match_fallback

def pattern_match_fallback(event: str, context: str, feature_dir: Path,
                           event_index: int = 0) -> str:
    if event == "reviewer_plan_complete":
        entry = _findings_entry_at(feature_dir, "reviewer_plan", event_index)
        verdict = _extract_verdict(entry)
        return f"📝 Resumen plan: {verdict}"

    if event == "reviewer_code_complete":
        entry = _findings_entry_at(feature_dir, "reviewer_code", event_index)
        verdict = _extract_verdict(entry)
        return f"📝 Resumen code review: {verdict}"

    if event == "error":
        # extract raw reason from context
        m = re.search(r'reason=([^\s\n]+)', context)
        reason = m.group(1) if m else context.splitlines()[0] if context else "unknown"
        return f"❌ Pipeline ERROR: {reason}"

    if event == "escalate":
        m = re.search(r'reason=([^\s\n]+)', context)
        reason = m.group(1) if m else context.splitlines()[0] if context else "unknown"
        return f"⚠️ Pipeline ESCALATE: {reason}"

    if event == "done":
        return "🎉 Pipeline completo (sin resumen LLM)"

    return f"📢 Evento: {event}"

# ═════════════════════════════════════════════════════════════════════════════
# 9. send_telegram

def send_telegram(text: str, config: dict) -> bool:
    token = config["TELEGRAM_BOT_TOKEN_NOTIFICATIONS"]
    chat_id = config["TELEGRAM_CHAT_ID_NOTIFICATIONS"]
    url = f"/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })
    try:
        conn = http.client.HTTPSConnection("api.telegram.org", timeout=HTTP_TIMEOUT_SEC)
        conn.request("POST", url, body=body.encode("utf-8"), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return status == 200
    except Exception as e:
        log_error(f"Telegram send error: {e}")
        return False

# ═════════════════════════════════════════════════════════════════════════════
# 10. log_error

# ERROR_LOG is set by main() via the --error-log arg
def log_error(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(_current_error_log, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

# ═════════════════════════════════════════════════════════════════════════════
# 11. main

def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline Notifier — tails status.log and sends Telegram notifications")
    parser.add_argument("feature_dir", help="Path to the feature directory")
    parser.add_argument("--env-path", default=str(ENV_PATH), help="Path to notifier.env (default: ~/.hermes/notifier.env)")
    parser.add_argument("--error-log", default=str(ERROR_LOG), help="Path to error log (default: ~/.hermes/notifier-errors.log)")
    args = parser.parse_args()

    feature_dir_path = Path(args.feature_dir).resolve()
    if not feature_dir_path.exists():
        print(f"ERROR: Feature dir does not exist: {args.feature_dir}", file=sys.stderr)
        return 1

    env_path = Path(args.env_path)
    try:
        config = load_config(env_path)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        return 1

    slug = extract_slug(feature_dir_path)
    status_log = feature_dir_path / "status.log"
    checkpoint = feature_dir_path / ".notifier_position"

    print(f"Notifier started for {slug}")
    print(f"Tailing: {status_log}")

    last_activity = time.time()
    exit_code = 0

    event_counts = {
        "reviewer_plan_complete": 0,
        "reviewer_code_complete": 0,
    }

    for line in tail_follow(status_log, checkpoint):
        last_activity = time.time()

        classification = classify_event(line)
        if classification is None:
            continue

        key = classification

        if key in ("reviewer_plan_complete", "reviewer_code_complete", "error", "escalate", "done"):
            event_index = 0
            if key in event_counts:
                event_index = event_counts[key]
                event_counts[key] += 1

            context = extract_rich_context(key, feature_dir_path, line, event_index)
            llm_msg = enrich_with_llm(key, context, config,
                                      feature_dir=feature_dir_path, event_index=event_index)
            if llm_msg:
                prefixed = f"[{slug}] {llm_msg}"
                send_telegram(prefixed, config)
            else:
                fallback = pattern_match_fallback(key, context, feature_dir_path, event_index)
                prefixed = f"[{slug}] {fallback}"
                send_telegram(prefixed, config)

        # Check terminal — after sending, exit immediately
        if any(tp in line for tp in TERMINAL_PATTERNS):
            print(f"Terminal state detected: {line}")
            exit_code = 0
            break

        # Inactivity timeout
        if time.time() - last_activity > NO_ACTIVITY_TIMEOUT_SEC:
            send_telegram(f"[{slug}] ⏱️ Pipeline parece colgado", config)
            exit_code = 1
            break

    print("Notifier exiting.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
