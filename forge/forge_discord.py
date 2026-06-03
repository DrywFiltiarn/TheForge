"""
forge_discord.py — Discord client, PDF generation, message formatting,
                   and approval polling.
"""

import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import requests

from . import forge_config as cfg
from .forge_log import log, log_err, log_warn

# ─── Emoji helpers ────────────────────────────────────────────────────────────

def _encode_emoji(emoji: str) -> str:
    """Normalise an emoji for use in a Discord reaction URL path segment."""
    if "%" in emoji:
        emoji = unquote(emoji)
    if ":" in emoji:
        return emoji
    return quote(emoji, safe="")

# ─── PDF generation ───────────────────────────────────────────────────────────

PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
:root { --bg:#ffffff; --text:#1a1a2e; --muted:#4a5568; --accent:#2563eb; --border:#e2e8f0; --code-bg:#f1f5f9; --heading:#0f172a; }
* { box-sizing:border-box; margin:0; padding:0; }
@page { size:A4; margin:24mm 20mm 24mm 20mm; @bottom-right { content:counter(page); font-size:9pt; color:#94a3b8; } }
body { font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif; font-size:10.5pt; line-height:1.65; color:var(--text); }
h1 { font-size:20pt; font-weight:600; color:var(--heading); margin-bottom:6pt; padding-bottom:8pt; border-bottom:2px solid var(--accent); }
h2 { font-size:14pt; font-weight:600; color:var(--heading); margin-top:18pt; margin-bottom:6pt; padding-bottom:4pt; border-bottom:1px solid var(--border); }
h3 { font-size:11pt; font-weight:600; color:var(--accent); margin-top:12pt; margin-bottom:4pt; text-transform:uppercase; letter-spacing:0.04em; }
p { margin-bottom:8pt; } ul,ol { margin-left:16pt; margin-bottom:8pt; } li { margin-bottom:3pt; }
code { font-family:'JetBrains Mono','Courier New',monospace; font-size:9pt; background:var(--code-bg); padding:1pt 4pt; border-radius:3pt; color:#c7254e; }
pre { background:var(--code-bg); border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:4pt; padding:10pt 12pt; margin:8pt 0; page-break-inside:avoid; }
pre code { font-size:8.5pt; background:none; padding:0; color:var(--text); }
blockquote { border-left:3px solid var(--accent); margin:8pt 0; padding:6pt 12pt; background:#eff6ff; color:var(--muted); }
table { width:100%; border-collapse:collapse; margin:10pt 0; font-size:9.5pt; }
th { background:var(--code-bg); font-weight:600; padding:5pt 8pt; border:1px solid var(--border); text-align:left; }
td { padding:5pt 8pt; border:1px solid var(--border); }
tr:nth-child(even) td { background:#f8fafc; }
strong { font-weight:600; } em { color:var(--muted); }
.header-meta { font-size:9pt; color:var(--muted); margin-bottom:14pt; }
"""


def _markdown_to_pdf(markdown_text: str, title: str = "") -> Optional[bytes]:
    """Convert a markdown string to PDF bytes using weasyprint."""
    try:
        import markdown as md_lib
        html_body = md_lib.markdown(
            markdown_text,
            extensions=["fenced_code", "tables", "codehilite", "toc", "nl2br"],
        )
    except ImportError:
        import html as html_mod
        lines, html_lines, in_code = markdown_text.splitlines(), [], False
        for line in lines:
            if line.startswith("```"):
                if in_code:
                    html_lines.append("</code></pre>"); in_code = False
                else:
                    html_lines.append("<pre><code>"); in_code = True
            elif in_code:
                html_lines.append(html_mod.escape(line))
            elif line.startswith("### "): html_lines.append(f"<h3>{html_mod.escape(line[4:])}</h3>")
            elif line.startswith("## "):  html_lines.append(f"<h2>{html_mod.escape(line[3:])}</h2>")
            elif line.startswith("# "):   html_lines.append(f"<h1>{html_mod.escape(line[2:])}</h1>")
            elif line.startswith(("- ", "* ")): html_lines.append(f"<li>{html_mod.escape(line[2:])}</li>")
            elif line.strip() == "---":  html_lines.append("<hr>")
            elif line.strip():           html_lines.append(f"<p>{html_mod.escape(line)}</p>")
            else:                        html_lines.append("<br>")
        html_body = "\n".join(html_lines)

    title_html  = f"<title>{title}</title>" if title else ""
    clean_title = re.sub(r"\.(pdf|md|txt)$", "", title) if title else "Report"
    full_html = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        f'{title_html}<style>{PDF_CSS}</style></head><body>'
        f'<div class="header-meta">The Forge · {clean_title}</div>'
        f'{html_body}</body></html>'
    )
    try:
        from weasyprint import HTML
        return HTML(string=full_html).write_pdf()
    except Exception as e:
        log_warn(f"weasyprint PDF generation failed: {e}")
        return None


# ─── Discord client ───────────────────────────────────────────────────────────

class DiscordClient:
    BASE = "https://discord.com/api/v10"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = requests.get(f"{self.BASE}{path}", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log_warn(f"Discord GET {path} failed: {e}")
            return None

    def _post(self, path: str, payload: dict) -> Optional[dict]:
        try:
            r = requests.post(f"{self.BASE}{path}", headers=self.headers,
                              json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log_warn(f"Discord POST {path} failed: {e}")
            return None

    def get_channel_id(self, guild_id: str, channel_name: str) -> Optional[str]:
        channels = self._get(f"/guilds/{guild_id}/channels")
        if not channels:
            return None
        for ch in channels:
            if ch.get("name") == channel_name:
                return ch["id"]
        log_warn(f"Discord channel #{channel_name} not found in guild {guild_id}")
        return None

    def send_message(self, channel_id: str, content: str,
                     embeds: Optional[list] = None) -> Optional[str]:
        payload: dict = {}
        if len(content) <= 2000:
            payload["content"] = content
        else:
            payload["embeds"] = [{"description": content[:4096]}]
        if embeds:
            payload["embeds"] = embeds
        result = self._post(f"/channels/{channel_id}/messages", payload)
        return result["id"] if result else None

    def send_file(self, channel_id: str, caption: str,
                  filename: str, file_content: str) -> Optional[str]:
        """Convert markdown to PDF and post as a Discord attachment."""
        pdf_filename = re.sub(r"\.(md|txt|html)$", "", filename) + ".pdf"
        pdf_bytes    = _markdown_to_pdf(file_content, title=pdf_filename)
        try:
            headers = {"Authorization": self.headers["Authorization"]}
            if pdf_bytes:
                files = {"file": (pdf_filename, pdf_bytes, "application/pdf")}
                log(f"Discord send_file: sending PDF ({len(pdf_bytes)} bytes) as {pdf_filename}")
            else:
                log_warn("PDF generation failed — sending as plain text")
                txt_filename = pdf_filename.replace(".pdf", ".txt")
                files = {"file": (txt_filename, file_content.encode("utf-8"), "text/plain; charset=utf-8")}
            data = {"content": caption[:2000]}
            r = requests.post(
                f"{self.BASE}/channels/{channel_id}/messages",
                headers=headers, data=data, files=files, timeout=30,
            )
            r.raise_for_status()
            return r.json()["id"]
        except Exception as e:
            log_warn(f"Discord send_file failed: {e} — falling back to inline message")
            return self.send_message(channel_id, f"{caption}\n\n```\n{file_content[:1800]}\n```")

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        try:
            encoded = _encode_emoji(emoji)
            url     = f"{self.BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
            headers = {k: v for k, v in self.headers.items() if k != "Content-Type"}
            r = requests.put(url, headers=headers, timeout=10)
            if r.status_code == 429:
                retry_after = float(r.json().get("retry_after", 1.0))
                log_warn(f"Discord add_reaction rate-limited — retrying after {retry_after}s")
                time.sleep(retry_after + 0.1)
                r = requests.put(url, headers=headers, timeout=10)
            if r.status_code not in (200, 204):
                log_warn(f"Discord add_reaction HTTP {r.status_code} for emoji {encoded!r}")
            return r.status_code in (200, 204)
        except Exception as e:
            log_warn(f"Discord add_reaction failed: {e}")
            return False

    def get_reactions(self, channel_id: str, message_id: str, emoji: str) -> list[dict]:
        result = self._get(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{_encode_emoji(emoji)}"
        )
        return result if isinstance(result, list) else []

    def get_message(self, channel_id: str, message_id: str) -> Optional[dict]:
        return self._get(f"/channels/{channel_id}/messages/{message_id}")

    def get_recent_messages(self, channel_id: str, after_id: str,
                            limit: int = 10) -> list[dict]:
        result = self._get(
            f"/channels/{channel_id}/messages?after={after_id}&limit={limit}"
        )
        return result if isinstance(result, list) else []


def get_discord() -> Optional[DiscordClient]:
    if not cfg.DISCORD_BOT_TOKEN:
        log_warn("FORGE_DISCORD_TOKEN not set — Discord notifications disabled")
        return None
    return DiscordClient(cfg.DISCORD_BOT_TOKEN)


# ─── Message formatting ───────────────────────────────────────────────────────

def _discord_escape(s: str) -> str:
    """Escape Discord markdown special characters in user-supplied text."""
    for ch in ("\\", "*", "_", "~", "|", "`", ">", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


def format_report_caption(task: dict, section: str, dur: str = "") -> str:
    tid     = task["id"]
    desc    = _discord_escape(task["description"])
    phase   = task.get("phase", "?")
    project = task.get("project", "(unknown)")
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    icon    = "📋" if section == "PLAN" else "📦"
    label   = "Planning" if section == "PLAN" else "Implementation"
    parts   = [
        f"{icon} **{section} REPORT — `{tid}` (Phase {phase})**",
        desc,
        f"Project: `{project}` · Prereqs: `{prereqs}`",
    ]
    if dur:
        parts.append(f"⏱ {label}: `{dur}`")
    parts.append("_Approval request in #forge-approvals_")
    return "\n".join(parts)


def format_plan_approval_request(task: dict, attempt: int,
                                  feedback: str = "", plan_dur: str = "") -> str:
    tid     = task["id"]
    desc    = _discord_escape(task["description"])
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    project = task.get("project", "(unknown)")
    header  = f"**🔐 PLAN APPROVAL — `{tid}`**"
    if attempt > 1:
        header += f" *(revision {attempt})*"
    parts   = [header, desc, f"Project: `{project}` · Prereqs: `{prereqs}`"]
    if plan_dur:
        parts.append(f"⏱ Planning: `{plan_dur}`")
    if feedback:
        parts += ["", f"📝 _Revision feedback: {feedback}_"]
    parts += [
        "", f"_Full plan report attached in #forge-reports → search `{tid}`_",
        "", "✅ approve · ❌ reject (reply with feedback then react)",
    ]
    return "\n".join(parts)


def format_push_approval_request(task: dict, commit_info: dict,
                                  act_dur: str = "") -> str:
    tid     = task["id"]
    desc    = _discord_escape(task["description"])
    project = task.get("project", "(unknown)")
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    parts   = [f"**🔐 PUSH APPROVAL — `{tid}`**", desc,
               f"Project: `{project}` · Prereqs: `{prereqs}`"]
    if act_dur:
        parts.append(f"⏱ Implementation: `{act_dur}`")
    parts += ["", f"_Full implementation report in #forge-reports → search `{tid}`_", ""]
    for repo, info in commit_info.items():
        commits = info.get("commits", [])
        if commits:
            parts.append(f"**{repo}:**")
            parts.append(f"```\n{chr(10).join(commits[:5])}\n```")
            parts.append("")
    parts += [
        "✅ **React to confirm** — task marked complete.",
        "❌ **React to reject** — task marked needs-review, no further action.",
    ]
    return "\n".join(parts)


def format_implementation_caption(task: dict, commit_info: dict,
                                   act_dur: str = "") -> str:
    tid     = task["id"]
    desc    = _discord_escape(task["description"])
    phase   = task.get("phase", "?")
    project = task.get("project", "(unknown)")
    prereqs = ", ".join(task.get("prereqs", [])) or "none"
    parts   = [
        f"**📦 IMPLEMENTATION REPORT — `{tid}` (Phase {phase})**",
        desc, f"Project: `{project}` · Prereqs: `{prereqs}`",
    ]
    if act_dur:
        parts.append(f"⏱ Implementation: `{act_dur}`")
    parts.append("_Push approval request in #forge-approvals_")
    return "\n".join(parts)


# ─── Approval polling ─────────────────────────────────────────────────────────

def wait_for_approval(
    dc: Optional[DiscordClient],
    approvals_channel_id: str,
    message_id: str,
    timeout: int = cfg.APPROVAL_TIMEOUT,
    reports_channel_id: Optional[str] = None,
    report_message_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Poll for ✅ or ❌ reaction on message_id in #forge-approvals.
    Returns (approved: bool, feedback: str).
    Only reactions from FORGE_OWNER_ID are acted upon.
    """
    if dc is None:
        log_warn("Discord not configured — auto-approving")
        return True, ""

    log(f"Waiting for approval on message {message_id} "
        f"(owner: {cfg.FORGE_OWNER_ID}, timeout {timeout}s)...")
    deadline      = time.monotonic() + timeout
    last_reminder = time.monotonic()

    def mirror_reaction(emoji: str) -> None:
        if reports_channel_id and report_message_id:
            dc.add_reaction(reports_channel_id, report_message_id, emoji)

    while time.monotonic() < deadline:
        time.sleep(cfg.APPROVAL_POLL_INTERVAL)

        approvers = dc.get_reactions(approvals_channel_id, message_id, cfg.APPROVE_EMOJI)
        for u in approvers:
            if u.get("bot", False):
                continue
            if u.get("id") == cfg.FORGE_OWNER_ID:
                log(f"✅ Approved by owner ({u.get('username', 'unknown')})")
                mirror_reaction(cfg.APPROVE_EMOJI)
                dc.send_message(
                    approvals_channel_id,
                    f"✅ **Approval registered** — reaction picked up from "
                    f"{u.get('username', 'owner')}. OpenCode is proceeding."
                )
                return True, ""
            else:
                log_warn(f"⚠️  Ignoring ✅ from non-owner user {u.get('id')} ({u.get('username', '?')})")
                dc.send_message(
                    approvals_channel_id,
                    f"⚠️ Reaction from `{u.get('username', u.get('id', '?'))}` ignored — "
                    f"only the server owner can approve."
                )

        rejectors = dc.get_reactions(approvals_channel_id, message_id, cfg.REJECT_EMOJI)
        for u in rejectors:
            if u.get("bot", False):
                continue
            if u.get("id") == cfg.FORGE_OWNER_ID:
                log(f"❌ Rejected by owner ({u.get('username', 'unknown')})")
                feedback = ""
                recent   = dc.get_recent_messages(approvals_channel_id, message_id, limit=5)
                for msg in sorted(recent, key=lambda m: m.get("id", "0")):
                    author = msg.get("author", {})
                    if not author.get("bot", False) and author.get("id") == cfg.FORGE_OWNER_ID:
                        feedback = msg.get("content", "").strip()
                        break
                mirror_reaction(cfg.REJECT_EMOJI)
                if feedback:
                    dc.send_message(
                        approvals_channel_id,
                        f"❌ **Rejection registered** — feedback received: _{feedback}_\n"
                        f"OpenCode will revise the plan."
                    )
                else:
                    dc.send_message(
                        approvals_channel_id,
                        f"❌ **Rejection registered** — no feedback reply found.\n"
                        f"OpenCode will re-plan. Reply with feedback before reacting next time "
                        f"so the revision has direction."
                    )
                return False, feedback
            else:
                log_warn(f"⚠️  Ignoring ❌ from non-owner user {u.get('id')} ({u.get('username', '?')})")
                dc.send_message(
                    approvals_channel_id,
                    f"⚠️ Reaction from `{u.get('username', u.get('id', '?'))}` ignored — "
                    f"only the server owner can reject."
                )

        if time.monotonic() - last_reminder > 300:
            last_reminder = time.monotonic()
            log("Still waiting for owner approval in #forge-approvals...")

    log_warn(f"Approval timeout after {timeout}s")
    return False, "Approval timed out — no response received"