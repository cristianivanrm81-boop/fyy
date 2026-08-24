"""
34Hub Bot v2 — by 34ry0
========================

Improvements over v1:
  - Cleaner embeds with consistent color language (no emojis, icons via text)
  - Role-based permissions: ALLOWED_ROLES env var for non-admin users
  - Better auto-refresh: JWT-aware, never fires on a blind timer
  - /token sends tokens.json as a file attachment (ephemeral)
  - /addtoken / /add_session — admin only
  - /status, /poolstats, /session_health, /audit — admin only
  - /token, /mystats, /leaderboard, /ping, /botinfo — public (role-gated)
  - /setchannel, /removechannel, /force_refresh, /reset_cooldown,
    /reset_all_cooldowns, /clearstats, /remove_session — admin only
  - /help — shows commands relevant to the caller's permission level
"""

import asyncio
import io
import json
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands

from token_manager import TokenManager, BURN_LABELS, BURN_WEIGHTS, BURN_LOW, BURN_MEDIUM, BURN_HIGH

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("34hub")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v:
        raise ValueError(f"Missing env var: {key}")
    return v

def _parse_ids(raw: str, name: str) -> list[int]:
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ValueError(f"{name} must be comma-separated integers")

BOT_TOKEN         = _require("DISCORD_BOT_TOKEN")
ALLOWED_GUILD_IDS = _parse_ids(_require("ALLOWED_GUILD_IDS"), "ALLOWED_GUILD_IDS")
ALLOWED_USERS     = _parse_ids(_require("ALLOWED_USERS"),     "ALLOWED_USERS")

# Optional: role IDs that can use non-admin commands (e.g. /token, /mystats)
# If empty, any server member can use public commands.
_raw_roles = os.getenv("ALLOWED_ROLES", "").strip()
ALLOWED_ROLES = _parse_ids(_raw_roles, "ALLOWED_ROLES") if _raw_roles else []

NAKAMA_HOST       = os.getenv("NAKAMA_HOST",       "https://animalcompany.us-east1.nakamacloud.io")
NAKAMA_SERVER_KEY = os.getenv("NAKAMA_SERVER_KEY", "6URuTSlDKKfYbuDW")
COOLDOWN_SECONDS  = int(os.getenv("COOLDOWN_SECONDS", "0"))
REFRESH_THRESHOLD = int(os.getenv("TOKEN_REFRESH_THRESHOLD", "300"))
TOKENS_FILE       = Path(os.getenv("TOKENS_FILE",    "tokens.json"))
COOLDOWNS_FILE    = Path(os.getenv("COOLDOWNS_FILE", "cooldowns.json"))
STATS_FILE        = Path(os.getenv("STATS_FILE",     "stats.json"))
CHANNELS_FILE     = Path(os.getenv("CHANNELS_FILE",  "channels.json"))
BURN_ALERT_CHANNEL = int(os.getenv("BURN_ALERT_CHANNEL", "0"))

BOT_VERSION  = "2.0.0"
BOT_START_TS = time.time()

manager = TokenManager(
    tokens_file=TOKENS_FILE,
    cooldowns_file=COOLDOWNS_FILE,
    stats_file=STATS_FILE,
    channels_file=CHANNELS_FILE,
    host=NAKAMA_HOST,
    server_key=NAKAMA_SERVER_KEY,
    cooldown_seconds=COOLDOWN_SECONDS,
    refresh_threshold=REFRESH_THRESHOLD,
)

# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# ---------------------------------------------------------------------------
# PERMISSION HELPERS
# ---------------------------------------------------------------------------

def _guild_ok(i: discord.Interaction) -> bool:
    return i.guild_id in ALLOWED_GUILD_IDS

def _is_admin(i: discord.Interaction) -> bool:
    return i.user.id in ALLOWED_USERS

def _has_role(i: discord.Interaction) -> bool:
    """True if ALLOWED_ROLES is empty (open to all) or the user has one."""
    if not ALLOWED_ROLES:
        return True
    if not hasattr(i.user, "roles"):
        return False
    return any(r.id in ALLOWED_ROLES for r in i.user.roles)

def _channel_ok(i: discord.Interaction) -> bool:
    if not i.guild_id:
        return True
    ch = manager.get_guild_channel(i.guild_id)
    if ch is None:
        return _is_admin(i)   # no channel set — only admins until one is configured
    return i.channel_id == ch

def _wrong_channel_msg(i: discord.Interaction) -> str:
    ch = manager.get_guild_channel(i.guild_id) if i.guild_id else None
    if ch:
        return f"This command can only be used in <#{ch}>."
    return "No channel configured. An admin must run `/setchannel` first."

# ---------------------------------------------------------------------------
# FORMATTING HELPERS
# ---------------------------------------------------------------------------

def _fmt_dur(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:   return f"{h}h {m}m {s}s"
    if m:   return f"{m}m {s}s"
    return f"{s}s"

def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))

def _burn_icon(level: str, dead: bool = False) -> str:
    if dead: return "[DEAD]"
    return {"LOW": "[OK]", "MEDIUM": "[MED]", "HIGH": "[HIGH]", "CRITICAL": "[CRIT]"}.get(level, level)

# ---------------------------------------------------------------------------
# EMBED FACTORY
# ---------------------------------------------------------------------------

# Color palette
C_BRAND   = 0x2B2D31   # dark neutral — brand
C_SUCCESS = 0x3BA55D   # green
C_WARN    = 0xFAA81A   # amber
C_DANGER  = 0xED4245   # red
C_INFO    = 0x5865F2   # blurple
C_GOLD    = 0xFFD700

def _embed(
    title: str,
    description: str = "",
    color: int = C_BRAND,
    footer: str = "34Hub Bot v2 by 34ry0",
    ts: bool = True,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    if ts:
        e.timestamp = discord.utils.utcnow()
    e.set_footer(text=footer)
    return e

# ---------------------------------------------------------------------------
# TOKEN FILE BUILDER
# ---------------------------------------------------------------------------

def _token_file(tokens: dict) -> discord.File:
    payload = {
        "token":         tokens["token"],
        "refresh_token": tokens["refresh_token"],
        "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cooldown_s":    COOLDOWN_SECONDS,
        "source":        "34Hub Bot v2",
    }
    raw = json.dumps(payload, indent=2).encode()
    return discord.File(io.BytesIO(raw), filename="tokens.json")

# ---------------------------------------------------------------------------
# /token  — public (role-gated)
# ---------------------------------------------------------------------------

@tree.command(name="token", description="Get the current session token")
async def cmd_token(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _channel_ok(i):
        await i.response.send_message(_wrong_channel_msg(i), ephemeral=True); return
    if not _is_admin(i) and not _has_role(i):
        await i.response.send_message("You don't have permission to use this command.", ephemeral=True); return

    await i.response.defer(ephemeral=True)

    uid = str(i.user.id)
    on_cd, left = manager.check_cooldown(uid)
    if on_cd:
        e = _embed("Cooldown Active", f"You can request again in **{_fmt_dur(left)}**.", C_WARN)
        await i.followup.send(embed=e, ephemeral=True); return

    failing, reason = manager.is_pool_failing()
    if failing:
        e = _embed(
            "Token System Unavailable",
            f"All pool sessions are down.\nReason: `{reason}`\n\nAn admin must add a session with `/addtoken`.",
            C_DANGER,
        )
        await i.followup.send(embed=e, ephemeral=True); return

    tokens = await manager.get_tokens()
    if not tokens:
        e = _embed("Temporary Error", "Token unavailable right now. Try again in a moment.", C_WARN)
        await i.followup.send(embed=e, ephemeral=True); return

    manager.set_cooldown(uid)
    manager.record_request(uid, str(i.user))

    cd_text = _fmt_dur(COOLDOWN_SECONDS) if COOLDOWN_SECONDS > 0 else "None"
    e = _embed(
        "Token Delivered",
        (
            "Your `tokens.json` is attached below.\n\n"
            f"**Cooldown:** `{cd_text}`\n"
            f"**Status:** Active\n"
            f"**Served by:** 34Hub Pool"
        ),
        C_SUCCESS,
    )
    await i.followup.send(embed=e, file=_token_file(tokens), ephemeral=True)
    log.info(f"Token delivered to {i.user} ({i.user.id})")

# ---------------------------------------------------------------------------
# /setchannel  — admin
# ---------------------------------------------------------------------------

@tree.command(name="setchannel", description="Set the channel for bot commands [Admin]")
@app_commands.describe(channel="Channel to lock commands to")
async def cmd_setchannel(i: discord.Interaction, channel: discord.TextChannel):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    manager.set_guild_channel(i.guild_id, channel.id)
    e = _embed("Channel Configured", f"Commands are now locked to {channel.mention}.", C_SUCCESS)
    await i.response.send_message(embed=e, ephemeral=True)
    log.info(f"Guild {i.guild_id} channel set to {channel.id}")

# ---------------------------------------------------------------------------
# /removechannel  — admin
# ---------------------------------------------------------------------------

@tree.command(name="removechannel", description="Remove channel lock [Admin]")
async def cmd_removechannel(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    removed = manager.remove_guild_channel(i.guild_id)
    msg = "Channel lock removed. Commands are now unrestricted." if removed else "No channel was configured."
    e = _embed("Channel Removed" if removed else "Nothing to Remove", msg, C_INFO)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /addtoken  — admin
# ---------------------------------------------------------------------------

@tree.command(name="addtoken", description="Add a session to the pool [Admin]")
@app_commands.describe(
    token="Bearer token (JWT)",
    refresh_token="Refresh token (JWT)",
    label="Label to identify this session (optional)",
)
async def cmd_addtoken(i: discord.Interaction, token: str, refresh_token: str, label: str = ""):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    await i.response.defer(ephemeral=True)
    ok, msg = await manager.add_session(token, refresh_token, label)
    color = C_SUCCESS if ok else C_DANGER
    e = _embed("Add Session — " + ("OK" if ok else "Failed"), msg, color)
    await i.followup.send(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /add_session  — admin (alias kept for backwards compat)
# ---------------------------------------------------------------------------

@tree.command(name="add_session", description="Add a session to the pool [Admin]")
@app_commands.describe(token="Bearer token", refresh_token="Refresh token", label="Label (optional)")
async def cmd_add_session(i: discord.Interaction, token: str, refresh_token: str, label: str = ""):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    await i.response.defer(ephemeral=True)
    ok, msg = await manager.add_session(token, refresh_token, label)
    color = C_SUCCESS if ok else C_DANGER
    e = _embed("Add Session — " + ("OK" if ok else "Failed"), msg, color)
    await i.followup.send(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /remove_session  — admin
# ---------------------------------------------------------------------------

@tree.command(name="remove_session", description="Remove a session from the pool [Admin]")
@app_commands.describe(label="Label of the session to remove")
async def cmd_remove_session(i: discord.Interaction, label: str):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    ok, msg = await manager.remove_session(label)
    color = C_SUCCESS if ok else C_DANGER
    e = _embed("Remove Session — " + ("OK" if ok else "Failed"), msg, color)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /force_refresh  — admin
# ---------------------------------------------------------------------------

@tree.command(name="force_refresh", description="Force refresh all sessions now [Admin]")
async def cmd_force_refresh(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    await i.response.defer(ephemeral=True)
    ok = await manager.force_refresh()
    pool  = manager.get_pool_status()
    alive = sum(1 for s in pool if not s["dead"])
    color = C_SUCCESS if ok else C_DANGER
    msg   = f"Refresh {'complete' if ok else 'failed'}. {alive}/{len(pool)} sessions active."
    e = _embed("Force Refresh", msg, color)
    await i.followup.send(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /status  — admin
# ---------------------------------------------------------------------------

@tree.command(name="status", description="System status overview [Admin]")
async def cmd_status(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    pool          = manager.get_pool_status()
    failing, fail = manager.is_pool_failing()
    uptime        = int(time.time() - BOT_START_TS)
    ch_id         = manager.get_guild_channel(i.guild_id) if i.guild_id else None
    alive         = sum(1 for s in pool if not s["dead"])

    health_line = f"DOWN: {fail}" if failing else "Healthy"

    lines = [
        f"Health:           {health_line}",
        f"Pool:             {alive}/{len(pool)} active",
        f"Cooldown:         {_fmt_dur(COOLDOWN_SECONDS) if COOLDOWN_SECONDS else 'Disabled'}",
        f"Refresh threshold:{REFRESH_THRESHOLD}s before expiry",
        f"Active cooldowns: {manager.get_active_cooldown_count()}",
        f"Tokens delivered: {manager.get_global_count()}",
        f"Uptime:           {_fmt_dur(uptime)}",
        f"Latency:          {round(client.latency * 1000)}ms",
        f"Channel lock:     {'#' + str(ch_id) if ch_id else 'None'}",
        "",
        "-- Sessions --",
    ]

    for s in pool:
        icon  = _burn_icon(s["burn"], s["dead"])
        token_str = f"{_fmt_dur(s['token_left'])} left" if s["token_ok"] else "EXPIRED"
        rt_str    = f"{_fmt_dur(s['rt_left'])} left"    if s["rt_ok"]    else "EXPIRED"
        lines.append(f"  {icon} {s['label']}  (uses: {s['serve_count']})")
        lines.append(f"    bearer:  {token_str}")
        lines.append(f"    refresh: {rt_str}")
        if s["failure_reason"]:
            lines.append(f"    error:   {s['failure_reason'][:70]}")

    color = C_DANGER if failing else C_SUCCESS
    e = _embed("System Status", "```\n" + "\n".join(lines) + "\n```", color)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /session_health  — admin
# ---------------------------------------------------------------------------

@tree.command(name="session_health", description="Anti-burn dashboard [Admin]")
async def cmd_session_health(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    rows = manager.get_burn_status()
    if not rows:
        e = _embed("Session Health", "No sessions in pool.", C_WARN)
        await i.response.send_message(embed=e, ephemeral=True); return

    lines = []
    for r in rows:
        icon = _burn_icon(r["burn"], r["dead"])
        rt_h, rt_m = divmod(r["rt_left"] // 60, 60)
        rt_str = f"{rt_h}h {rt_m}m remaining" if not r["dead"] else "n/a"
        weight = BURN_WEIGHTS.get(r["burn"], 0) if not r["dead"] else 0
        alert  = " [alerted]" if r["alerted"] else ""
        lines.append(f"  {icon:<10} {r['label']}{alert}")
        lines.append(f"    RT: {rt_str}  |  weight: {weight}x  |  uses: {r['serve_count']}")
        if r["failures"]:
            lines.append(f"    failures: {r['failures']}")
        lines.append("")

    lines += [
        "-- Burn Thresholds --",
        f"  [OK]   > {BURN_LOW   // 3600}h  -> weight 4x",
        f"  [MED]  > {BURN_MEDIUM // 3600}h  -> weight 3x",
        f"  [HIGH] > {BURN_HIGH  // 3600}h  -> weight 2x",
        f"  [CRIT] < {BURN_HIGH  // 3600}h  -> weight 1x + alert",
    ]

    alert_dest = f"<#{BURN_ALERT_CHANNEL}>" if BURN_ALERT_CHANNEL else "Admin DMs"
    e = _embed(
        "Session Health — Anti-Burn",
        f"Alerts: {alert_dest}\n```\n" + "\n".join(lines) + "\n```",
        C_INFO,
    )
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /poolstats  — admin
# ---------------------------------------------------------------------------

@tree.command(name="poolstats", description="Load distribution across sessions [Admin]")
async def cmd_poolstats(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    rows = manager.get_session_stats()
    if not rows:
        e = _embed("Pool Stats", "No sessions in pool.", C_WARN)
        await i.response.send_message(embed=e, ephemeral=True); return

    total = sum(r["serve_count"] for r in rows) or 1
    lines = []
    for r in rows:
        pct  = (r["serve_count"] / total) * 100
        bar  = "#" * int(pct / 5) + "." * (20 - int(pct / 5))
        icon = "[DEAD]" if r["dead"] else "[OK]  "
        lines.append(f"  {icon} {r['label']}")
        lines.append(f"    [{bar}] {pct:.1f}% ({r['serve_count']} uses)")

    lines.append("")
    lines.append(f"Total deliveries: {total}")
    e = _embed("Pool Stats — Load Distribution", "```\n" + "\n".join(lines) + "\n```", C_INFO)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /audit  — admin
# ---------------------------------------------------------------------------

@tree.command(name="audit", description="Last 25 token deliveries [Admin]")
async def cmd_audit(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    entries = manager.get_audit_log()[:25]
    if not entries:
        e = _embed("Audit Log", "No deliveries on record.", C_WARN)
        await i.response.send_message(embed=e, ephemeral=True); return

    lines = [f"  {_fmt_ts(e['ts'])}  {e['username']} ({e['user_id']})" for e in entries]
    e = _embed("Audit Log", "```\n" + "\n".join(lines) + "\n```", C_INFO)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /clearstats  — admin
# ---------------------------------------------------------------------------

@tree.command(name="clearstats", description="Wipe all user stats and audit log [Admin]")
async def cmd_clearstats(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    manager.clear_stats()
    e = _embed("Stats Cleared", "All counters and audit log entries have been reset.", C_SUCCESS)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /reset_cooldown  — admin
# ---------------------------------------------------------------------------

@tree.command(name="reset_cooldown", description="Remove a specific user's cooldown [Admin]")
@app_commands.describe(user_id="Discord user ID")
async def cmd_reset_cooldown(i: discord.Interaction, user_id: str):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    removed = manager.reset_cooldown(user_id)
    if removed:
        e = _embed("Cooldown Removed", f"User `{user_id}` can use `/token` again.", C_SUCCESS)
    else:
        e = _embed("No Cooldown Found", f"User `{user_id}` had no active cooldown.", C_WARN)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /reset_all_cooldowns  — admin
# ---------------------------------------------------------------------------

@tree.command(name="reset_all_cooldowns", description="Clear all active cooldowns [Admin]")
async def cmd_reset_all_cooldowns(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _is_admin(i):
        await i.response.send_message("Admin only.", ephemeral=True); return

    n = manager.reset_all_cooldowns()
    e = _embed("Cooldowns Cleared", f"**{n}** cooldown(s) removed.", C_SUCCESS)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /mystats  — public
# ---------------------------------------------------------------------------

@tree.command(name="mystats", description="See how many tokens you have requested")
async def cmd_mystats(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _channel_ok(i):
        await i.response.send_message(_wrong_channel_msg(i), ephemeral=True); return
    if not _is_admin(i) and not _has_role(i):
        await i.response.send_message("You don't have permission to use this command.", ephemeral=True); return

    uid  = str(i.user.id)
    stat = manager.get_user_stat(uid)
    if not stat:
        e = _embed("Your Stats", "You haven't requested any tokens yet.", C_INFO)
        await i.response.send_message(embed=e, ephemeral=True); return

    on_cd, left = manager.check_cooldown(uid)
    cd_str = f"`{_fmt_dur(left)}`" if on_cd else "Ready"

    e = _embed("Your Stats", color=C_INFO)
    e.add_field(name="Requests",      value=f"`{stat['count']}`",            inline=True)
    e.add_field(name="Last Request",  value=f"`{_fmt_ts(stat['last_ts'])}`", inline=True)
    e.add_field(name="Cooldown",      value=cd_str,                          inline=True)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /leaderboard  — public
# ---------------------------------------------------------------------------

@tree.command(name="leaderboard", description="Top 10 users by token requests")
async def cmd_leaderboard(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _channel_ok(i):
        await i.response.send_message(_wrong_channel_msg(i), ephemeral=True); return
    if not _is_admin(i) and not _has_role(i):
        await i.response.send_message("You don't have permission to use this command.", ephemeral=True); return

    top = manager.get_top_users(10)
    if not top:
        e = _embed("Leaderboard", "No requests recorded yet.", C_INFO)
        await i.response.send_message(embed=e, ephemeral=True); return

    medals = {0: "1st", 1: "2nd", 2: "3rd"}
    lines = []
    for idx, entry in enumerate(top):
        rank = medals.get(idx, f"{idx + 1}th")
        lines.append(f"**{rank}** — {entry['username']} — `{entry['count']}` requests")

    e = _embed(
        "Leaderboard",
        "\n".join(lines),
        C_GOLD,
        footer=f"Global total: {manager.get_global_count()} tokens | 34Hub Bot v2",
    )
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /botinfo  — public
# ---------------------------------------------------------------------------

@tree.command(name="botinfo", description="Bot version and system info")
async def cmd_botinfo(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return
    if not _channel_ok(i):
        await i.response.send_message(_wrong_channel_msg(i), ephemeral=True); return

    uptime = int(time.time() - BOT_START_TS)
    pool   = manager.get_pool_status()
    alive  = sum(1 for s in pool if not s["dead"])

    e = _embed("34Hub Bot v2", color=C_BRAND)
    e.add_field(name="Version",         value=f"`{BOT_VERSION}`",                       inline=True)
    e.add_field(name="Uptime",          value=f"`{_fmt_dur(uptime)}`",                  inline=True)
    e.add_field(name="Latency",         value=f"`{round(client.latency * 1000)}ms`",    inline=True)
    e.add_field(name="Sessions",        value=f"`{alive}/{len(pool)} active`",          inline=True)
    e.add_field(name="Tokens Served",   value=f"`{manager.get_global_count()}`",        inline=True)
    e.add_field(name="Cooldown",        value=f"`{_fmt_dur(COOLDOWN_SECONDS) or 'Off'}`", inline=True)
    e.add_field(
        name="Commands",
        value=(
            "`/token` `/mystats` `/leaderboard` `/ping` `/botinfo` `/help`\n"
            "`/addtoken` `/add_session` `/remove_session` `/setchannel`\n"
            "`/removechannel` `/force_refresh` `/status` `/session_health`\n"
            "`/poolstats` `/audit` `/clearstats` `/reset_cooldown` `/reset_all_cooldowns`"
        ),
        inline=False,
    )
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /help  — public, shows contextual commands
# ---------------------------------------------------------------------------

@tree.command(name="help", description="Command list")
async def cmd_help(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return

    public_cmds = (
        "`/token`              — Get a session token\n"
        "`/mystats`            — Your request stats\n"
        "`/leaderboard`        — Top 10 requesters\n"
        "`/ping`               — Bot latency\n"
        "`/botinfo`            — Version and status\n"
        "`/help`               — This message"
    )

    e = _embed("34Hub Bot — Commands", color=C_BRAND)
    e.add_field(name="Public", value=public_cmds, inline=False)

    if _is_admin(i):
        admin_cmds = (
            "`/addtoken`           — Add a session to the pool\n"
            "`/add_session`        — Alias for /addtoken\n"
            "`/remove_session`     — Remove a session by label\n"
            "`/setchannel`         — Lock commands to a channel\n"
            "`/removechannel`      — Remove channel lock\n"
            "`/force_refresh`      — Force refresh all sessions now\n"
            "`/status`             — Full system status\n"
            "`/session_health`     — Anti-burn dashboard\n"
            "`/poolstats`          — Load distribution\n"
            "`/audit`              — Last 25 deliveries\n"
            "`/clearstats`         — Wipe stats and audit log\n"
            "`/reset_cooldown`     — Reset one user's cooldown\n"
            "`/reset_all_cooldowns`— Clear all cooldowns"
        )
        e.add_field(name="Admin Only", value=admin_cmds, inline=False)

    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# /ping  — public
# ---------------------------------------------------------------------------

@tree.command(name="ping", description="Check bot latency")
async def cmd_ping(i: discord.Interaction):
    if not _guild_ok(i):
        await i.response.send_message("Not an authorized server.", ephemeral=True); return

    lat   = round(client.latency * 1000)
    color = C_SUCCESS if lat < 150 else (C_WARN if lat < 400 else C_DANGER)
    e = _embed("Pong", f"Latency: **`{lat}ms`**", color, ts=False)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------------------------------------------------------------------
# BURN ALERT CALLBACK
# ---------------------------------------------------------------------------

async def _on_burn_alert(session, level: str):
    rt_left = session.rt_secs_left()
    h, rem  = divmod(rt_left, 3600)
    m       = rem // 60
    rt_str  = f"{h}h {m}m" if h else f"{m}m"

    lines = [
        f"Session : {session.label}",
        f"Risk    : CRITICAL",
        f"RT left : {rt_str}",
    ]
    e = discord.Embed(
        title="Burn Alert — 34Hub",
        description=(
            "```\n" + "\n".join(lines) + "\n```\n"
            f"The `refresh_token` for **`{session.label}`** is expiring soon.\n"
            "Use `/addtoken` to add a replacement before it dies."
        ),
        color=C_DANGER,
        timestamp=discord.utils.utcnow(),
    )
    e.set_footer(text="34Hub Bot v2 — Anti-Burn System")

    if BURN_ALERT_CHANNEL:
        ch = client.get_channel(BURN_ALERT_CHANNEL)
        if ch:
            await ch.send(embed=e)
            return

    for uid in ALLOWED_USERS:
        try:
            user = await client.fetch_user(uid)
            await user.send(embed=e)
        except Exception as ex:
            log.warning(f"[burn] Could not DM admin {uid}: {ex}")

# ---------------------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    manager.set_burn_alert_callback(_on_burn_alert)
    await manager.start()

    # Sync directo a cada guild — instantáneo, sin esperar 1h de propagación global
    for guild_id in ALLOWED_GUILD_IDS:
        guild = discord.Object(id=guild_id)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)

    log.info("=" * 55)
    log.info(f"34Hub Bot v{BOT_VERSION} — {client.user}")
    log.info(f"Guilds:    {ALLOWED_GUILD_IDS}")
    log.info(f"Admins:    {ALLOWED_USERS}")
    log.info(f"Roles:     {ALLOWED_ROLES or 'open'}")
    log.info(f"Cooldown:  {COOLDOWN_SECONDS}s | Refresh threshold: {REFRESH_THRESHOLD}s")
    log.info(f"Burn alerts -> {'channel ' + str(BURN_ALERT_CHANNEL) if BURN_ALERT_CHANNEL else 'admin DMs'}")
    log.info("=" * 55)


@client.event
async def on_error(event, *args, **kwargs):
    log.error(f"Error in event '{event}':")
    traceback.print_exc()


@client.event
async def on_disconnect():
    log.warning("Disconnected from Discord")


@client.event
async def on_resumed():
    log.info("Reconnected to Discord")

# ---------------------------------------------------------------------------
# SIGNAL HANDLING
# ---------------------------------------------------------------------------

def _handle_signal(sig, frame):
    log.info(f"Signal {sig} — shutting down...")
    asyncio.get_event_loop().call_soon_threadsafe(lambda: asyncio.create_task(_shutdown()))


async def _shutdown():
    await manager.stop()
    await client.close()

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)
    log.info("Starting 34Hub Bot v2...")
    try:
        client.run(BOT_TOKEN, log_handler=None)
    except Exception as e:
        log.critical(f"Fatal: {e}")
        traceback.print_exc()
        sys.exit(1)
