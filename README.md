# 34Hub Bot v2

Discord bot for distributing Animal Company session tokens from a managed pool.
Built by 34ry0.

---

## What's new in v2

- **Cleaner UI** — consistent embeds, color-coded by severity (green/amber/red/blurple)
- **Role gating** — `ALLOWED_ROLES` env var lets you restrict public commands to specific Discord roles
- **Fixed auto-refresh** — tokens are refreshed when their bearer is within `TOKEN_REFRESH_THRESHOLD` seconds of expiry, never on a blind interval timer
- **`/help`** — shows commands relevant to the caller's permission level
- **Removed emojis** — UI uses text badges: `[OK]`, `[CRIT]`, `[DEAD]` etc.
- **Leaner code** — `token_manager.py` fully refactored; no state duplication

---

## Quick start

```bash
cp .env.example .env
# fill in .env
pip install -r requirements.txt
python bot.py
```

### Docker / Railway

```bash
docker build -t 34hub-bot .
docker run --env-file .env -v /your/data:/app/data 34hub-bot
```

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | Bot token from Discord Dev Portal |
| `ALLOWED_GUILD_IDS` | yes | — | Comma-separated server IDs |
| `ALLOWED_USERS` | yes | — | Comma-separated admin Discord user IDs |
| `ALLOWED_ROLES` | no | (empty = open) | Comma-separated role IDs for public commands |
| `NAKAMA_HOST` | no | animalcompany Nakama | Nakama server base URL |
| `NAKAMA_SERVER_KEY` | no | `6URuTSlDKKfYbuDW` | Nakama server key |
| `COOLDOWN_SECONDS` | no | `0` | Per-user cooldown between `/token` requests (0 = off) |
| `TOKEN_REFRESH_THRESHOLD` | no | `300` | Seconds before bearer expiry to trigger refresh |
| `BURN_ALERT_CHANNEL` | no | `0` | Channel ID for burn alerts (0 = admin DMs) |
| `TOKENS_FILE` | no | `tokens.json` | Path to session pool file |
| `COOLDOWNS_FILE` | no | `cooldowns.json` | Path to cooldown cache |
| `STATS_FILE` | no | `stats.json` | Path to user stats |
| `CHANNELS_FILE` | no | `channels.json` | Path to guild channel config |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Commands

### Public (all members, or role-gated if `ALLOWED_ROLES` is set)

| Command | Description |
|---|---|
| `/token` | Get current session token as `tokens.json` attachment |
| `/mystats` | Your personal request count and cooldown |
| `/leaderboard` | Top 10 requesters |
| `/ping` | Bot latency |
| `/botinfo` | Version, uptime, pool overview |
| `/help` | Command list (shows admin commands to admins) |

### Admin only (`ALLOWED_USERS`)

| Command | Description |
|---|---|
| `/addtoken` | Add a session to the pool |
| `/add_session` | Alias for `/addtoken` |
| `/remove_session` | Remove a session by label |
| `/setchannel` | Lock commands to a specific channel |
| `/removechannel` | Remove channel lock |
| `/force_refresh` | Force-refresh all sessions immediately |
| `/status` | Full system status with per-session detail |
| `/session_health` | Anti-burn dashboard with RT expiry and weights |
| `/poolstats` | Load distribution across sessions |
| `/audit` | Last 25 token deliveries |
| `/clearstats` | Wipe all user stats and audit log |
| `/reset_cooldown` | Remove cooldown for a specific user |
| `/reset_all_cooldowns` | Clear all active cooldowns |

---

## How the refresh system works

The bot checks every **30 seconds** whether any session's bearer token is
within `TOKEN_REFRESH_THRESHOLD` seconds of expiry (default: 5 minutes).
If it is, the bearer is refreshed using the session's `refresh_token`.

The old v1 bug: the bot was calling the refresh endpoint every
`REFRESH_INTERVAL` seconds regardless of token age, burning through
refresh tokens and invalidating live Animal Company sessions.

v2 only touches a token when it actually needs to be refreshed.

---

## Anti-burn system

`refresh_token` lifetime is monitored and classified:

| Level | RT remaining | Weight | Alert |
|---|---|---|---|
| `[OK]` | > 7h | 4x (preferred) | — |
| `[MED]` | > 3h | 3x | — |
| `[HIGH]` | > 1h | 2x | — |
| `[CRIT]` | < 1h | 1x | Yes (once) |

When a session hits CRITICAL, a single alert is sent (channel or DM) and
the flag is persisted so restarts don't re-send it.
