"""
forge_log.py — Logging, timestamps, and terminal formatting utilities.

Provides: log(), log_err(), log_warn(), _ts(), _ts_local_display(),
          _log_width(), _wrap_log_lines(), _fmt_duration()
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore

from .forge_config import LOG_FILE

# ─── Timezone ─────────────────────────────────────────────────────────────────

_LOCAL_TZ = _ZoneInfo("Europe/Amsterdam")

def _ts() -> str:
    """Return current local time (Europe/Amsterdam) as ISO 8601 string."""
    return datetime.now(_LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

def _ts_local_display() -> str:
    """Return current local time (Europe/Amsterdam) as HH:MM:SS for log display."""
    return datetime.now(_LOCAL_TZ).strftime("%H:%M:%S")

# ─── Forge log ────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    import shutil as _shutil, textwrap as _tw
    prefix = f"[{_ts()}] [{level}] "
    cont   = " " * len(prefix)
    width  = min(max(_shutil.get_terminal_size(fallback=(120, 40)).columns, 60), 220)
    if len(prefix) + len(msg) <= width:
        line = prefix + msg
    else:
        chunks = _tw.wrap(msg, width=width,
                          initial_indent=prefix,
                          subsequent_indent=cont,
                          break_long_words=False,
                          break_on_hyphens=False)
        line = "\n".join(chunks) if chunks else prefix + msg
    print(line, flush=True)
    with open(cfg.LOG_FILE, "a") as f:
        f.write(line + "\n")

def log_err(msg: str) -> None:
    log(msg, "ERROR")

def log_warn(msg: str) -> None:
    log(msg, "WARN")

# ─── Terminal width and line wrapping ─────────────────────────────────────────

def _log_width() -> int:
    """Return usable terminal column width for log line wrapping."""
    import shutil as _shutil
    try:
        w = _shutil.get_terminal_size(fallback=(120, 40)).columns
        return min(max(w, 60), 220)
    except Exception:
        return 120

def _wrap_log_lines(text: str, first_prefix: str, cont_prefix: str) -> list[str]:
    """
    Wrap a block of text for opencode.log output at terminal width.
    Returns a list of complete lines including newline characters.
    """
    import textwrap as _tw
    width  = _log_width()
    result = []
    for logical_line in text.splitlines():
        if not logical_line:
            result.append(first_prefix.rstrip() + "\n")
            continue
        available = width - len(first_prefix)
        if available < 20:
            result.append(f"{first_prefix}{logical_line}\n")
            continue
        if len(logical_line) <= available:
            result.append(f"{first_prefix}{logical_line}\n")
        else:
            chunks = _tw.wrap(
                logical_line,
                width=width,
                initial_indent=first_prefix,
                subsequent_indent=cont_prefix,
                break_long_words=False,
                break_on_hyphens=False,
            )
            for chunk in chunks:
                result.append(chunk + "\n")
    return result

# ─── Duration formatting ──────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Format a duration into XXs, XXm:YYs, or XXh:YYm:ZZs."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m:{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h:{m:02d}m:{s:02d}s"
