#!/usr/bin/env python3
"""
discord_mcp.py — Discord MCP Server for Forge

A lightweight MCP server that exposes Discord operations as tools.
Allows Cline (or any MCP client) to post messages and check reactions
without needing the full Forge orchestrator running.

This is an OPTIONAL component. The main forge.py uses requests directly.
Use this if you want Cline to be able to report to Discord from within
its own session (e.g., for ad-hoc status updates or asking questions).

Protocol: MCP stdio (JSON-RPC 2.0 over stdin/stdout)

Usage (register with Cline):
  cline mcp add discord --command "python forge/discord_mcp.py" \\
    --env FORGE_DISCORD_TOKEN=<token> \\
    --env FORGE_DISCORD_GUILD_ID=<guild_id>
"""

import json
import os
import sys
from typing import Any, Optional
from urllib.parse import quote

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("FORGE_DISCORD_TOKEN", "")
GUILD_ID  = os.environ.get("FORGE_DISCORD_GUILD_ID", "")
DISCORD_API = "https://discord.com/api/v10"

# Hardcoded channel IDs
REPORTS_CHANNEL_ID   = "1508515907952054323"   # #forge-reports  — broadcast only
APPROVALS_CHANNEL_ID = "1508488060298334229"   # #forge-approvals — approval polling

# Owner gate — only this Discord user ID is treated as an authoritative reactor.
# Used in check_approval to verify the reacting user is the server owner.
FORGE_OWNER_ID = "334811986019745792"

HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type": "application/json",
}

# ── Discord helpers ────────────────────────────────────────────────────────────

def _get(path: str) -> Optional[Any]:
    try:
        r = requests.get(f"{DISCORD_API}{path}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _post(path: str, payload: dict) -> Optional[Any]:
    try:
        r = requests.post(f"{DISCORD_API}{path}", headers=HEADERS,
                          json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _put(path: str) -> tuple[bool, int]:
    """PUT request. Returns (success, http_status_code)."""
    try:
        headers = {k: v for k, v in HEADERS.items() if k != "Content-Type"}
        r = requests.put(f"{DISCORD_API}{path}", headers=headers, timeout=10)
        return r.status_code in (200, 204), r.status_code
    except Exception as e:
        return False, -1

def _encode_emoji(emoji: str) -> str:
    """
    Normalise an emoji for use in a Discord reaction URL path segment.

    Discord expects the raw Unicode character percent-encoded at the byte level.
    Callers may pass either:
      - The raw character:  "✅"  or "❌"
      - Percent-encoded:    "%E2%9C%85"  or "%E2%9D%8C"
      - Named (custom):     "name:id"

    We decode any existing percent-encoding first, then re-encode cleanly
    so that requests does not double-encode the path.
    """
    from urllib.parse import unquote
    # If it looks like it's already percent-encoded, decode it first
    if "%" in emoji:
        emoji = unquote(emoji)
    # Custom guild emoji "name:id" — pass through without encoding
    if ":" in emoji:
        return emoji
    # Standard Unicode emoji — encode to percent-encoded UTF-8
    return quote(emoji, safe="")

def get_channel_id(channel_name: str) -> Optional[str]:
    channels = _get(f"/guilds/{GUILD_ID}/channels")
    if isinstance(channels, list):
        for ch in channels:
            if ch.get("name") == channel_name:
                return ch["id"]
    return None

# ── MCP Tool implementations ───────────────────────────────────────────────────

def tool_send_message(channel_name: str, content: str) -> dict:
    """Post a message to a Discord channel by name."""
    channel_id = get_channel_id(channel_name)
    if not channel_id:
        return {"success": False, "error": f"Channel #{channel_name} not found"}

    # Discord content limit: 2000 chars
    if len(content) > 2000:
        content = content[:1997] + "..."

    result = _post(f"/channels/{channel_id}/messages", {"content": content})
    if isinstance(result, dict) and "id" in result:
        return {"success": True, "message_id": result["id"], "channel_id": channel_id}
    return {"success": False, "error": str(result)}

def tool_send_embed(channel_name: str, title: str, description: str,
                    color: int = 0x4AF0A0) -> dict:
    """Post an embed message to a Discord channel."""
    channel_id = get_channel_id(channel_name)
    if not channel_id:
        return {"success": False, "error": f"Channel #{channel_name} not found"}

    description = description[:4096] if len(description) > 4096 else description
    payload = {
        "embeds": [{
            "title": title[:256],
            "description": description,
            "color": color,
        }]
    }
    result = _post(f"/channels/{channel_id}/messages", payload)
    if isinstance(result, dict) and "id" in result:
        return {"success": True, "message_id": result["id"], "channel_id": channel_id}
    return {"success": False, "error": str(result)}

def tool_add_reaction(channel_id: str, message_id: str, emoji: str) -> dict:
    """
    Add a reaction to a message.
    emoji can be the raw Unicode character (✅ ❌), percent-encoded (%E2%9C%85),
    or a custom guild emoji in name:id format.
    """
    encoded = _encode_emoji(emoji)
    success, status = _put(
        f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
    )
    if success:
        return {"success": True, "emoji_sent": encoded}
    return {"success": False, "http_status": status,
            "emoji_sent": encoded,
            "hint": "403 = missing Add Reactions permission; 400 = unknown emoji"}

def tool_get_reactions(channel_id: str, message_id: str, emoji: str) -> dict:
    """Get users who reacted with emoji. Returns list of users (excluding bots)."""
    result = _get(f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}")
    if isinstance(result, list):
        humans = [u for u in result if not u.get("bot", False)]
        return {"success": True, "count": len(humans), "users": humans}
    return {"success": False, "error": str(result)}

def tool_check_approval(channel_id: str, message_id: str) -> dict:
    """
    Check if a message has been approved (✅) or rejected (❌) by the owner.

    Only reactions from FORGE_OWNER_ID are acted upon. Reactions from any
    other user ID are reported in 'ignored' but do not affect the result.
    Returns: {approved: bool|null, rejected: bool|null, feedback: str}
    null means no qualifying owner reaction found yet.
    """
    approve = _get(f"/channels/{channel_id}/messages/{message_id}/reactions/{_encode_emoji('✅')}")
    reject  = _get(f"/channels/{channel_id}/messages/{message_id}/reactions/{_encode_emoji('❌')}")

    ignored = []

    # Filter to owner-only, non-bot
    def owner_reactions(reaction_list: Any) -> list[dict]:
        out = []
        for u in (reaction_list if isinstance(reaction_list, list) else []):
            if u.get("bot", False):
                continue
            if u.get("id") == FORGE_OWNER_ID:
                out.append(u)
            else:
                ignored.append({"id": u.get("id"), "username": u.get("username")})
        return out

    approve_owner = owner_reactions(approve)
    reject_owner  = owner_reactions(reject)

    if approve_owner:
        return {
            "approved": True, "rejected": False, "feedback": "",
            "approver": approve_owner[0].get("username", "unknown"),
            "approver_id": approve_owner[0].get("id"),
            "ignored_reactors": ignored,
        }

    if reject_owner:
        # Feedback: only look at messages from the owner after the approval request
        recent = _get(f"/channels/{channel_id}/messages?after={message_id}&limit=10")
        feedback = ""
        if isinstance(recent, list):
            for msg in sorted(recent, key=lambda m: m.get("id", "0")):
                author = msg.get("author", {})
                if not author.get("bot", False) and author.get("id") == FORGE_OWNER_ID:
                    feedback = msg.get("content", "").strip()
                    break
        return {
            "approved": False, "rejected": True, "feedback": feedback,
            "rejector": reject_owner[0].get("username", "unknown"),
            "rejector_id": reject_owner[0].get("id"),
            "ignored_reactors": ignored,
        }

    return {"approved": None, "rejected": None, "feedback": "",
            "ignored_reactors": ignored}

def tool_list_channels() -> dict:
    """List all text channels in the configured guild."""
    channels = _get(f"/guilds/{GUILD_ID}/channels")
    if isinstance(channels, list):
        text_channels = [{"id": c["id"], "name": c["name"]}
                         for c in channels if c.get("type") == 0]
        return {"success": True, "channels": text_channels}
    return {"success": False, "error": str(channels)}

def tool_get_channel_id(channel_name: str) -> dict:
    """Resolve a channel name to its ID."""
    cid = get_channel_id(channel_name)
    if cid:
        return {"success": True, "channel_id": cid, "channel_name": channel_name}
    return {"success": False, "error": f"Channel #{channel_name} not found"}

# ── MCP Protocol ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "discord_send_message",
        "description": "Post a plain-text message to a Discord channel by name. "
                       "Use for status updates, progress reports, or simple notifications.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "The Discord channel name (without #), e.g. 'forge-reports'"
                },
                "content": {
                    "type": "string",
                    "description": "The message content (max 2000 characters)"
                }
            },
            "required": ["channel_name", "content"]
        }
    },
    {
        "name": "discord_send_embed",
        "description": "Post a formatted embed message to a Discord channel. "
                       "Use for structured reports with a title and rich description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "The Discord channel name (without #)"
                },
                "title": {
                    "type": "string",
                    "description": "Embed title (max 256 characters)"
                },
                "description": {
                    "type": "string",
                    "description": "Embed body text in markdown (max 4096 characters)"
                },
                "color": {
                    "type": "integer",
                    "description": "Hex color as integer (default: 0x4AF0A0 green)",
                    "default": 4911264
                }
            },
            "required": ["channel_name", "title", "description"]
        }
    },
    {
        "name": "discord_add_reaction",
        "description": "Add a reaction emoji to a Discord message. "
                       "Use %E2%9C%85 for ✅ and %E2%9D%8C for ❌.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Discord channel ID"},
                "message_id": {"type": "string", "description": "Discord message ID"},
                "emoji": {"type": "string", "description": "URL-encoded emoji, e.g. %E2%9C%85"}
            },
            "required": ["channel_id", "message_id", "emoji"]
        }
    },
    {
        "name": "discord_check_approval",
        "description": "Check if a message has been approved (✅) or rejected (❌) by a human. "
                       "Returns approved/rejected/null and any rejection feedback from replies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Discord channel ID"},
                "message_id": {"type": "string", "description": "Discord message ID"}
            },
            "required": ["channel_id", "message_id"]
        }
    },
    {
        "name": "discord_list_channels",
        "description": "List all text channels in the configured Discord server. "
                       "Useful to verify channel names before posting.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "discord_get_channel_id",
        "description": "Resolve a Discord channel name to its channel ID. "
                       "Required before calling discord_add_reaction or discord_check_approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "Channel name without # prefix"
                }
            },
            "required": ["channel_name"]
        }
    },
]

def handle_call_tool(name: str, arguments: dict) -> dict:
    """Dispatch tool calls to implementations."""
    if not BOT_TOKEN:
        return {"content": [{"type": "text", "text": json.dumps({
            "error": "FORGE_DISCORD_TOKEN not set"
        })}]}

    if name == "discord_send_message":
        result = tool_send_message(arguments["channel_name"], arguments["content"])
    elif name == "discord_send_embed":
        result = tool_send_embed(
            arguments["channel_name"],
            arguments["title"],
            arguments["description"],
            arguments.get("color", 0x4AF0A0),
        )
    elif name == "discord_add_reaction":
        result = tool_add_reaction(
            arguments["channel_id"],
            arguments["message_id"],
            arguments["emoji"],
        )
    elif name == "discord_check_approval":
        result = tool_check_approval(
            arguments["channel_id"],
            arguments["message_id"],
        )
    elif name == "discord_list_channels":
        result = tool_list_channels()
    elif name == "discord_get_channel_id":
        result = tool_get_channel_id(arguments["channel_name"])
    else:
        result = {"error": f"Unknown tool: {name}"}

    return {
        "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
    }

def handle_request(req: dict) -> Optional[dict]:
    """Handle a single MCP JSON-RPC request."""
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "discord-forge",
                    "version": "1.0.0",
                    "description": "Discord integration for Forge orchestrator"
                }
            }
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = handle_call_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    # Unknown method
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }
    return None

def main() -> None:
    """Run the MCP server on stdio."""
    # Stderr goes to a log, stdout is the MCP protocol
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            response = handle_request(req)
            if response is not None:
                print(json.dumps(response), flush=True)
        except json.JSONDecodeError as e:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"}
            }
            print(json.dumps(error_resp), flush=True)
        except Exception as e:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Internal error: {e}"}
            }
            print(json.dumps(error_resp), flush=True)

if __name__ == "__main__":
    main()