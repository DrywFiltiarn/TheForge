"""
forge_opencode.py — OpenCode CLI subprocess management: agent sync, command
                    building, NDJSON log parsing, and session execution.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import forge_config as cfg
from .forge_log import (
    log, log_err, log_warn,
    _ts, _ts_local_display, _log_width, _wrap_log_lines, _fmt_duration,
)
from .forge_discord import DiscordClient

def _opencode_agents_dir() -> Path:
    """
    Return the platform-appropriate OpenCode global agents directory.
    Linux/macOS: ~/.config/opencode/agents/
    Windows:     %APPDATA%/opencode/agents/
    Created if absent.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / "opencode" / "agents"
    else:
        d = Path.home() / ".config" / "opencode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_opencode_agents() -> None:
    """
    Sync forge-plan.md and forge-act.md from agents/ to the OpenCode global
    agents directory (~/.config/opencode/agents/).
    agents/ is the single source of truth. Files are only written when content
    has changed, so re-runs are cheap.
    """
    agents_dir = _opencode_agents_dir()
    for name in (f"{cfg.AGENT_PLAN_NAME}.md", f"{cfg.AGENT_ACT_NAME}.md"):
        src = cfg.AGENTS_DIR / name
        dst = agents_dir / name
        if not src.exists():
            log_warn(f"Agent file not found: {src} — OpenCode PLAN/ACT mode may fail")
            continue
        src_text = src.read_text(encoding="utf-8")
        if dst.exists() and dst.read_text(encoding="utf-8") == src_text:
            continue  # already up to date
        dst.write_text(src_text, encoding="utf-8")
        log(f"Synced agent file → {dst}")


def build_opencode_cmd(prompt: str, plan_mode: bool, cwd: Path,
                       model_id: Optional[str] = None) -> list[str]:
    """
    Build the opencode run command list.
    plan_mode=True  → forge-plan agent (read-only, plan report only)
    plan_mode=False → forge-act agent (full permissions, implementation)
    """
    agent = cfg.AGENT_PLAN_NAME if plan_mode else cfg.AGENT_ACT_NAME
    cmd = [
        cfg.OPENCODE_BIN, "run",
        "--format", "json",
        "--thinking",
        "--dangerously-skip-permissions",
        "--dir", str(cwd),
        "--agent", agent,
    ]
    if model_id:
        cmd.extend(["--model", model_id])
    cmd.append(prompt)
    return cmd


def _summarise_command(cmd: str) -> str:
    """
    Return a short human-readable description of a shell command for opencode.log.
    Avoids truncating mid-token by recognising common patterns.
    """
    import re as _re
    s = cmd.strip()

    # Bare redirect: > /path/to/file  (agent writing a file with no command)
    m = _re.match(r'^>\s*(\S+)', s)
    if m:
        return f"write → {m.group(1).rsplit('/', 1)[-1]}"

    # cat > file (heredoc write)
    m = _re.match(r'cat\s*>\s*(\S+)', s)
    if m:
        return f"write → {m.group(1).rsplit('/', 1)[-1]}"

    # python3 -c "..." or python3 << 'EOF'  (inline python)
    if _re.match(r'python3?\s+(-c\s+|<<)', s):
        for line in s.splitlines():
            m = _re.search(r"open\([\"']([^\"']+)[\"']", line)
            if m:
                return f"python → {m.group(1).rsplit('/', 1)[-1]}"
        return "python inline"

    # python3 /path/to/script.py
    m = _re.match(r'python3?\s+(\S+\.py)', s)
    if m:
        return m.group(1).rsplit('/', 1)[-1]

    # cargo <subcommand> [args]
    m = _re.match(r'(cargo\s+\w+(?:\s+-p\s+\S+)?(?:\s+--\S+)*)', s)
    if m:
        return m.group(1)

    # git <subcommand> [args]
    m = _re.match(r'(git\s+\S+(?:\s+\S+){0,3})', s)
    if m:
        return m.group(1)

    # find / grep / pytest / tee — show as-is up to 80 chars
    if _re.match(r'(find|grep|pytest|tee|ls|cp|mv|rm|mkdir|touch)\s', s):
        return s

    # Default: return full command; _wrap_log_lines handles terminal wrapping
    return s


def _write_opencode_log(clf, event: dict, token_buf: list[str],
                        task_id: str = "", mode: str = "",
                        session_tokens: dict = None,
                        session_start: float = 0.0) -> None:
    """
    Write a human-readable line to opencode.log for a single OpenCode NDJSON event.

    Event types:
      step_start       — suppressed (noise)
      text             — model prose output
      reasoning        — thinking block; dark grey, visually distinct from prose
      tool_use         — tool call + result pair
      step_finish      — token accounting; emits on stop or context threshold crossing
      session.compacted — auto-compaction fired; orange warning
      error            — session error; red, propagated to forge.log
    """
    if session_tokens is None:
        session_tokens = {}

    etype = event.get("type", "")

    raw_ts = event.get("timestamp")
    if isinstance(raw_ts, (int, float)) and raw_ts > 1_000_000_000_000:
        ts = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).astimezone(_LOCAL_TZ).strftime("%H:%M:%S")
    else:
        ts = _ts_local_display()

    def flush_tokens() -> None:
        if token_buf:
            t = "".join(x for x in token_buf if not x.startswith("\x00")).strip()
            if t:
                clf.write(f"  {t}\n")
                clf.flush()
            token_buf.clear()

    # step_start: suppressed
    if etype == "step_start":
        flush_tokens()

    # text: model prose and final answer
    elif etype == "text":
        flush_tokens()
        part = event.get("part", {})
        text = part.get("text", "").strip()
        if text:
            clf.write("\n")
            for out_line in _wrap_log_lines(text, "  ", "    "):
                clf.write(out_line)
            clf.flush()

    # reasoning: thinking block — dark grey
    elif etype == "reasoning":
        flush_tokens()
        part  = event.get("part", {})
        rtext = part.get("text", "").strip()
        if rtext:
            DIM   = "\033[90m"
            RESET = "\033[0m"
            timing  = part.get("time", {})
            t_start = timing.get("start", 0)
            t_end   = timing.get("end",   0)
            dur_ms  = (t_end - t_start) if (t_start and t_end) else 0
            dur_str = f" ({dur_ms}ms)" if dur_ms else ""
            clf.write(f"\n{DIM}  ~ thinking{dur_str}\n")
            for out_line in _wrap_log_lines(rtext, "  ~ ", "    "):
                clf.write(out_line)
            clf.write(f"  ~{RESET}\n")
            clf.flush()

    # tool_use: call + result pair
    elif etype == "tool_use":
        flush_tokens()
        part      = event.get("part", {})
        tool_name = part.get("tool", "?")
        state     = part.get("state", {})
        inp       = state.get("input", {})
        out       = state.get("output", "")
        timing    = state.get("time", {})
        title     = state.get("title", "")

        t_start = timing.get("start", 0)
        t_end   = timing.get("end",   0)
        dur_ms  = (t_end - t_start) if (t_start and t_end) else 0
        dur_str = f" {dur_ms}ms" if dur_ms else ""

        if tool_name == "read":
            hint = f" {inp.get('filePath', '')}"
        elif tool_name in ("read_files", "readFiles"):
            paths = inp.get("paths", inp.get("files", []))
            hint  = f" {', '.join(str(p) for p in paths[:3])}" if isinstance(paths, list) else f" {paths}"
        elif tool_name in ("edit", "write", "create"):
            hint = f" {inp.get('filePath', inp.get('path', ''))}"
        elif tool_name in ("bash", "run_commands", "execute_command"):
            cmds    = inp.get("commands", [])
            raw_cmd = str(cmds[0]) if (isinstance(cmds, list) and cmds) else str(inp.get("command", inp.get("cmd", "")))
            hint    = f" $ {_summarise_command(raw_cmd)}"
        elif tool_name in ("glob", "grep", "search"):
            q    = inp.get("pattern", inp.get("query", inp.get("glob", "")))
            hint = f" {str(q)!r}"
        elif tool_name == "list":
            hint = f" {inp.get('path', '')}"
        else:
            hint = ""
            for key in ("filePath", "path", "command", "url", "query", "description"):
                if key in inp:
                    hint = f" {str(inp[key])}"
                    break

        call_line = f"  [{ts}] {tool_name}{hint}{dur_str}"
        call_cont = " " * (2 + 1 + 8 + 1 + 1)
        for out_line in _wrap_log_lines(call_line, "", call_cont):
            clf.write(out_line)

        status_val = state.get("status", "")
        if status_val == "error" or (isinstance(out, str) and out.lower().startswith("error")):
            err_text = str(out) if isinstance(out, str) else str(state.get("error", ""))
            for out_line in _wrap_log_lines(err_text, "       X ", "         "):
                clf.write(out_line)
        elif tool_name in ("read", "read_files", "readFiles") and isinstance(out, str):
            lc = out.count("\n")
            clf.write(f"       + {title or 'file'} ({lc} lines)\n")
        elif tool_name in ("bash", "run_commands", "execute_command") and isinstance(out, str):
            lines = [l for l in out.splitlines() if l.strip()]
            if lines:
                for out_line in _wrap_log_lines(lines[0], "       + ", "         "):
                    clf.write(out_line)
                for l in lines[1:]:
                    for out_line in _wrap_log_lines(l, "         ", "         "):
                        clf.write(out_line)
            else:
                clf.write("       +\n")
        elif isinstance(out, str) and out.strip():
            lines = out.strip().splitlines()
            for out_line in _wrap_log_lines(lines[0], "       + ", "         "):
                clf.write(out_line)
            for l in lines[1:]:
                if l.strip():
                    for out_line in _wrap_log_lines(l, "         ", "         "):
                        clf.write(out_line)
        else:
            clf.write("       +\n")
        clf.flush()

    # step_finish: token accounting; emit only on stop or threshold crossing
    elif etype == "step_finish":
        flush_tokens()
        part   = event.get("part", {})
        reason = part.get("reason", "?")
        toks   = part.get("tokens", {})
        cost   = part.get("cost", 0.0)

        inp_step = int(toks.get("input",     0))
        out_step = int(toks.get("output",    0))
        rsn_step = int(toks.get("reasoning", 0))
        cache    = toks.get("cache", {})
        cr_step  = int(cache.get("read",  0))
        cw_step  = int(cache.get("write", 0))

        session_tokens["input_total"]     = session_tokens.get("input_total",     0) + inp_step
        session_tokens["output_total"]    = session_tokens.get("output_total",    0) + out_step
        session_tokens["reasoning_total"] = session_tokens.get("reasoning_total", 0) + rsn_step
        session_tokens["cache_read"]      = session_tokens.get("cache_read",      0) + cr_step
        session_tokens["cache_write"]     = session_tokens.get("cache_write",     0) + cw_step
        session_tokens["cost_total"]      = session_tokens.get("cost_total",      0.0) + (cost or 0.0)
        session_tokens["steps"]           = session_tokens.get("steps",           0) + 1

        ctx_used  = session_tokens["input_total"]
        ctx_total = cfg.OPENCODE_CONTEXT_WINDOW
        pct       = (ctx_used / ctx_total) * 100.0 if ctx_total else 0.0
        prev_pct  = session_tokens.get("_last_logged_pct", 0.0)
        _update_context_display(task_id, mode, pct, ctx_used, ctx_total,
                                 session_start=session_start)

        if reason == "stop":
            rsn_str = f"  rsn={rsn_step:,}" if rsn_step else ""
            cr_str  = f"  cr={cr_step:,}"   if cr_step  else ""
            clf.write(
                f"\n  [{ts}] done"
                f"  in={session_tokens['input_total']:,}"
                f"  out={session_tokens['output_total']:,}"
                f"{rsn_str}{cr_str}"
                f"  ctx={pct:.1f}%\n"
            )
            session_tokens["_last_logged_pct"] = pct
        elif pct >= 65 and prev_pct < 65:
            clf.write(f"  [{ts}] CONTEXT {pct:.1f}% ({ctx_used:,}/{ctx_total:,}) -- APPROACHING LIMIT\n")
            session_tokens["_last_logged_pct"] = pct
        elif pct >= 50 and prev_pct < 50:
            clf.write(f"  [{ts}] context {pct:.1f}% ({ctx_used:,}/{ctx_total:,})\n")
            session_tokens["_last_logged_pct"] = pct
        clf.flush()

    # session.compacted: auto-compaction — orange warning
    elif etype == "session.compacted":
        flush_tokens()
        ORANGE = "\033[38;5;208m"
        RESET  = "\033[0m"
        tokens_before = int(event.get("tokensBefore", session_tokens.get("input_total", 0)))
        tokens_after  = int(event.get("tokensAfter",  0))
        if tokens_before > 0 and tokens_after > 0:
            reduction = 100.0 * (1 - tokens_after / tokens_before)
            detail = (f"context compacted: {tokens_before:,} → {tokens_after:,} tokens "
                      f"({reduction:.1f}% reduction)")
        elif tokens_before > 0:
            detail = f"context compacted from ~{tokens_before:,} tokens (post-compaction size unknown)"
        else:
            detail = "context compacted (token counts unavailable)"
        clf.write(f"\n{ORANGE}  [{ts}] ⚡ COMPACTION — {detail}{RESET}\n")
        clf.flush()
        log_warn(f"[{task_id}] OpenCode auto-compaction fired — {detail}")
        _write_compaction_log(task_id, mode, tokens_before, tokens_after)
        if tokens_after > 0:
            session_tokens["input_total"] = tokens_after
            _update_context_display(task_id, mode,
                                    (tokens_after / cfg.OPENCODE_CONTEXT_WINDOW) * 100.0,
                                    tokens_after, cfg.OPENCODE_CONTEXT_WINDOW,
                                    session_start=session_start)

    # error: always visible in red; propagated to forge.log
    elif etype == "error":
        flush_tokens()
        err_obj = event.get("error", {})
        if isinstance(err_obj, dict):
            name    = err_obj.get("name", "UnknownError")
            data    = err_obj.get("data", {})
            message = data.get("message", str(err_obj)) if isinstance(data, dict) else str(data)
        else:
            name    = "error"
            message = str(err_obj)
        RED   = "\033[91m"
        RESET = "\033[0m"
        clf.write(f"\n{RED}  [{ts}] ERROR {name}: {message}{RESET}\n")
        clf.flush()
        log_err(f"[{task_id}] OpenCode session error -- {name}: {message[:160]}")

    # unhandled event types
    else:
        try:
            with open(cfg.OPENCODE_SKIPPED_LOG_FILE, "a") as skf:
                skf.write(f"[{ts}] type={etype!r} keys={list(event.keys())} "
                          f"raw={json.dumps(event)[:200]}\n")
        except Exception:
            pass


def run_opencode(
    prompt: str,
    plan_mode: bool,
    cwd: Path,
    task_id: str,
    dc: Optional["DiscordClient"],
    approvals_channel_id: Optional[str],
    attempt_number: int = 1,
    model_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Run OpenCode CLI with retry logic for llama.cpp failures.
    Returns (success: bool, output_text: str).

    model_id is passed via --model to select the llama-swap variant:
      openai-compatible/Qwen3.6-35B-A3B:planning — PLAN sessions (forge-plan agent)
      openai-compatible/Qwen3.6-35B-A3B:coding   — ACT sessions  (forge-act agent)

    OpenCode output is parsed and written to cfg.OPENCODE_LOG_FILE in human-readable form.
    Monitor live with: tail -f forge/opencode.log
    Context usage:    tail -f forge/context.log
    """
    cmd        = build_opencode_cmd(prompt, plan_mode, cwd, model_id=model_id)
    mode_label = "PLAN" if plan_mode else "ACT"
    model_label = model_id or "default"
    log(f"[{task_id}] Running OpenCode {mode_label} mode — model: {model_label} "
        f"(timeout {cfg.OPENCODE_TIMEOUT}s, attempt {attempt_number})")
    log(f"[{task_id}] OpenCode output → {cfg.OPENCODE_LOG_FILE}")

    full_output = ""

    for attempt in range(1, cfg.OPENCODE_RETRIES + 1):
        if attempt > 1:
            delay = cfg.OPENCODE_RETRY_DELAY * attempt
            msg = (f"⚠️ `{task_id}` OpenCode {mode_label} attempt {attempt}/{cfg.OPENCODE_RETRIES} "
                   f"— waiting {delay}s (llama.cpp may have crashed)")
            log_warn(msg)
            if dc and approvals_channel_id:
                dc.send_message(approvals_channel_id, msg)
            time.sleep(delay)

        text_output:    list[str] = []
        token_buf:      list[str] = []
        session_tokens: dict      = {}
        session_start:  float     = time.monotonic()
        exit_code = -1

        with open(cfg.OPENCODE_LOG_FILE, "a") as clf:
            _hdr = f"[{_ts()}] [{task_id}] OpenCode {mode_label} — attempt {attempt}/{cfg.OPENCODE_RETRIES}"
            _sep = "─" * len(_hdr)
            clf.write(f"\n{_sep}\n{_hdr}\n{_sep}\n")
        _update_context_display(task_id, mode_label, 0.0, 0, cfg.OPENCODE_CONTEXT_WINDOW,
                                session_start=session_start)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            with open(cfg.OPENCODE_LOG_FILE, "a") as clf:
                for line in proc.stdout:
                    raw = line.rstrip()
                    try:
                        event = json.loads(raw)
                        _write_opencode_log(clf, event, token_buf,
                                            task_id=task_id, mode=mode_label,
                                            session_tokens=session_tokens,
                                            session_start=session_start)
                        etype = event.get("type", "")
                        # Collect text output for plan extraction
                        if etype == "text":
                            part = event.get("part", {})
                            t = part.get("text", "")
                            if t:
                                text_output.append(t)
                    except json.JSONDecodeError:
                        if raw:
                            clf.write(f"{raw}\n")
                            clf.flush()
                            text_output.append(raw)

                if token_buf:
                    clf.write("  " + "".join(token_buf).strip() + "\n")
                    clf.flush()
                    token_buf.clear()

            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            log_err(f"[{task_id}] OpenCode {mode_label} timed out after {cfg.OPENCODE_TIMEOUT}s")
            exit_code = -1
        except FileNotFoundError:
            log_err(f"OpenCode binary not found: {cfg.OPENCODE_BIN}")
            log_err("Install with: npm install -g opencode-ai")
            sys.exit(1)

        # Write session token summary to opencode.log
        with open(cfg.OPENCODE_LOG_FILE, "a") as clf:
            clf.write(f"[{_ts()}] [{task_id}] OpenCode {mode_label} exited: {exit_code}\n")
            if session_tokens:
                clf.write(
                    f"[{_ts()}] [{task_id}] Session tokens — "
                    f"in={session_tokens.get('input_total', 0):,}  "
                    f"out={session_tokens.get('output_total', 0):,}  "
                    f"rsn={session_tokens.get('reasoning_total', 0):,}  "
                    f"cache_r={session_tokens.get('cache_read', 0):,}  "
                    f"cache_w={session_tokens.get('cache_write', 0):,}  "
                    f"cost=${session_tokens.get('cost_total', 0.0):.4f}  "
                    f"steps={session_tokens.get('steps', 0)}\n"
                )

        full_output = "\n".join(text_output)

        if exit_code == 0:
            log(f"[{task_id}] OpenCode {mode_label} completed successfully")
            return True, full_output

        log_err(f"[{task_id}] OpenCode {mode_label} exited with code {exit_code}")

    return False, full_output
