"""
token_manager.py — 34Hub Bot v2
================================

Core engine: session pool, JWT-aware auto-refresh, anti-burn monitor,
cooldown system, user stats, audit log, guild channel config.

Refresh strategy:
  - Loop fires every REFRESH_INTERVAL seconds (default: 25 min).
  - On each tick, sessions are checked ONE AT A TIME with a 2-second
    pause between HTTP calls — prevents hammering Nakama and avoids
    triggering rate-limits or looking like a DDoS.
  - A session is only refreshed when its bearer is within
    TOKEN_REFRESH_THRESHOLD seconds of expiry (default: 26 min, slightly
    above the 25-min loop interval so nothing slips through).
  - force_refresh() also serializes calls with the same 2s delay.

Anti-burn: refresh_token lifetime is classified into four risk tiers
(LOW / MEDIUM / HIGH / CRITICAL). At CRITICAL a single alert fires and
is persisted so restarts don't re-send it.
"""

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib import error as urllib_error, request

log = logging.getLogger("token_manager")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------
REFRESH_URL_TPL         = "{host}/v2/account/session/refresh"
MAX_CONSECUTIVE_FAIL    = 3
AUDIT_MAX               = 100

BURN_LOW      = 7 * 3600   # >7h  → preferred
BURN_MEDIUM   = 3 * 3600   # >3h
BURN_HIGH     = 1 * 3600   # >1h
# CRITICAL      = <1h  → alert fires

BURN_WEIGHTS = {"LOW": 4, "MEDIUM": 3, "HIGH": 2, "CRITICAL": 1}
BURN_LABELS  = {
    "LOW":      "LOW",
    "MEDIUM":   "MEDIUM",
    "HIGH":     "HIGH",
    "CRITICAL": "CRITICAL",
}

REFRESH_LOOP_INTERVAL           = 25 * 60  # check pool every 25 minutes
INTER_SESSION_DELAY             = 2        # seconds between per-session HTTP calls
TOKEN_REFRESH_THRESHOLD_DEFAULT = 26 * 60  # refresh when bearer has < 26 min left
                                           # (slightly above loop interval so nothing slips)


def _burn_level(rt_seconds_remaining: int) -> str:
    if rt_seconds_remaining > BURN_LOW:    return "LOW"
    if rt_seconds_remaining > BURN_MEDIUM: return "MEDIUM"
    if rt_seconds_remaining > BURN_HIGH:   return "HIGH"
    return "CRITICAL"


# ---------------------------------------------------------------------------
# SESSION
# ---------------------------------------------------------------------------
@dataclass
class Session:
    label: str
    token: str
    refresh_token: str
    dead: bool = False
    consecutive_failures: int = 0
    failure_reason: str = ""
    last_refresh_ts: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    burn_alerted: bool = False
    serve_count: int = 0

    # -- Serialization --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "label":         self.label,
            "token":         self.token,
            "refresh_token": self.refresh_token,
            "serve_count":   self.serve_count,
            "burn_alerted":  self.burn_alerted,
        }

    @staticmethod
    def from_dict(d: dict, index: int) -> "Session":
        return Session(
            label=d.get("label", f"session_{index + 1}"),
            token=d.get("token", ""),
            refresh_token=d.get("refresh_token", ""),
            serve_count=d.get("serve_count", 0),
            burn_alerted=d.get("burn_alerted", False),
        )

    # -- JWT helpers ----------------------------------------------------------

    def token_exp(self) -> int:
        return TokenManager._decode_jwt_exp(self.token)

    def refresh_token_exp(self) -> int:
        return TokenManager._decode_jwt_exp(self.refresh_token)

    def is_token_valid(self) -> bool:
        return self.token_exp() > int(time.time()) + 30

    def is_refresh_token_valid(self) -> bool:
        return self.refresh_token_exp() > int(time.time()) + 30

    def burn_level(self) -> str:
        return _burn_level(max(self.refresh_token_exp() - int(time.time()), 0))

    def token_secs_left(self) -> int:
        return max(self.token_exp() - int(time.time()), 0)

    def rt_secs_left(self) -> int:
        return max(self.refresh_token_exp() - int(time.time()), 0)


# ---------------------------------------------------------------------------
# TOKEN MANAGER
# ---------------------------------------------------------------------------
class TokenManager:
    def __init__(
        self,
        *,
        tokens_file: Path,
        cooldowns_file: Path,
        stats_file: Path,
        channels_file: Path,
        host: str,
        server_key: str,
        cooldown_seconds: int = 0,
        refresh_threshold: int = TOKEN_REFRESH_THRESHOLD_DEFAULT,
        # legacy param kept for backwards compat — no longer drives refresh timing
        refresh_interval: int = 300,
    ):
        self.tokens_file    = tokens_file
        self.cooldowns_file = cooldowns_file
        self.stats_file     = stats_file
        self.channels_file  = channels_file
        self.refresh_url    = REFRESH_URL_TPL.format(host=host.rstrip("/"))
        self.server_key     = server_key
        self.cooldown_seconds  = cooldown_seconds
        self.refresh_threshold = refresh_threshold
        self.refresh_interval  = refresh_interval   # stored but unused for timing

        self._sessions:    list[Session] = []
        self._pool_lock    = asyncio.Lock()
        self._pool_failed  = False

        self._cooldowns:   dict[str, int] = {}
        self._cooldown_dirty = False

        self._user_stats:  dict[str, dict] = {}
        self._global_count = 0
        self._audit_log:   list[dict] = []
        self._stats_dirty  = False

        self._guild_channels: dict[str, int] = {}
        self._channels_dirty = False

        self._burn_alert_cb: Optional[Callable] = None

        self._refresh_task:    Optional[asyncio.Task] = None
        self._flush_task:      Optional[asyncio.Task] = None
        self._burn_task:       Optional[asyncio.Task] = None
        self._stats_task:      Optional[asyncio.Task] = None

    # -----------------------------------------------------------------------
    # LIFECYCLE
    # -----------------------------------------------------------------------

    async def start(self):
        await self._load_sessions()
        await self._load_cooldowns()
        await self._load_stats()
        await self._load_channels()

        # Proactive pass right at startup — refresh anything that needs it now
        await self._refresh_all_if_needed()

        self._refresh_task = asyncio.create_task(self._refresh_loop(),     name="token-refresh")
        self._flush_task   = asyncio.create_task(self._cooldown_flush(),   name="cd-flush")
        self._burn_task    = asyncio.create_task(self._burn_monitor(),     name="burn-monitor")
        self._stats_task   = asyncio.create_task(self._stats_flush(),      name="stats-flush")

        alive = sum(1 for s in self._sessions if not s.dead)
        log.info(f"TokenManager started — {len(self._sessions)} sessions, {alive} alive")

    async def stop(self):
        for t in (self._refresh_task, self._flush_task, self._burn_task, self._stats_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        await self._do_flush_cooldowns()
        await self._do_flush_stats()
        await self._do_flush_channels()
        log.info("TokenManager stopped cleanly")

    # -----------------------------------------------------------------------
    # PUBLIC — POOL
    # -----------------------------------------------------------------------

    async def get_tokens(self) -> Optional[dict]:
        async with self._pool_lock:
            alive = [s for s in self._sessions if not s.dead]
            if not alive:
                self._pool_failed = True
                return None
            session = self._weighted_pick(alive)

        # Refresh inline if bearer is expired/near-expiry
        if not session.is_token_valid():
            async with session.lock:
                if not session.is_token_valid():
                    await self._refresh_session(session)

        if not session.token or session.dead:
            return None

        session.serve_count += 1
        return {"token": session.token, "refresh_token": session.refresh_token}

    async def add_session(self, token: str, refresh_token: str, label: str = "") -> tuple[bool, str]:
        if token.count(".") != 2 or refresh_token.count(".") != 2:
            return False, "Invalid JWT format — must have 3 dot-separated parts."

        async with self._pool_lock:
            label = label or f"session_{len(self._sessions) + 1}"
            for s in self._sessions:
                if s.refresh_token == refresh_token:
                    return False, f"Session already exists in pool (label: `{s.label}`)."
            session = Session(label=label, token=token, refresh_token=refresh_token)
            self._sessions.append(session)

        # Refresh if bearer is already expired but RT is still valid
        if not session.is_token_valid() and session.is_refresh_token_valid():
            async with session.lock:
                await self._refresh_session(session)

        await self._save_sessions()
        alive = sum(1 for s in self._sessions if not s.dead)
        self._pool_failed = alive == 0
        await self._check_burn_levels()
        return True, f"Session `{label}` added. Pool: {len(self._sessions)} total, {alive} active."

    async def remove_session(self, label: str) -> tuple[bool, str]:
        async with self._pool_lock:
            before = len(self._sessions)
            self._sessions = [s for s in self._sessions if s.label != label]
            if len(self._sessions) == before:
                return False, f"No session found with label `{label}`."

        await self._save_sessions()
        alive = sum(1 for s in self._sessions if not s.dead)
        self._pool_failed = len(self._sessions) == 0 or alive == 0
        return True, f"Session `{label}` removed. Pool: {len(self._sessions)} total, {alive} active."

    async def force_refresh(self) -> bool:
        """Refresh every session sequentially with a 2s gap between calls."""
        any_ok = False
        first  = True
        for session in list(self._sessions):
            if not first:
                await asyncio.sleep(INTER_SESSION_DELAY)
            first = False
            async with session.lock:
                ok = await self._refresh_session(session)
                if ok:
                    any_ok = True
                    session.dead = False
                    session.consecutive_failures = 0
                    session.failure_reason = ""
        alive = sum(1 for s in self._sessions if not s.dead)
        self._pool_failed = alive == 0
        return any_ok

    def get_pool_status(self) -> list[dict]:
        now = int(time.time())
        return [
            {
                "label":          s.label,
                "dead":           s.dead,
                "token_ok":       s.is_token_valid(),
                "token_left":     s.token_secs_left(),
                "rt_ok":          s.is_refresh_token_valid(),
                "rt_left":        s.rt_secs_left(),
                "failures":       s.consecutive_failures,
                "failure_reason": s.failure_reason,
                "serve_count":    s.serve_count,
                "burn":           s.burn_level() if not s.dead else "DEAD",
            }
            for s in self._sessions
        ]

    def get_session_stats(self) -> list[dict]:
        return [
            {"label": s.label, "serve_count": s.serve_count, "dead": s.dead}
            for s in self._sessions
        ]

    def get_burn_status(self) -> list[dict]:
        now = int(time.time())
        return [
            {
                "label":       s.label,
                "dead":        s.dead,
                "burn":        s.burn_level() if not s.dead else "DEAD",
                "rt_left":     s.rt_secs_left(),
                "alerted":     s.burn_alerted,
                "failures":    s.consecutive_failures,
                "serve_count": s.serve_count,
            }
            for s in self._sessions
        ]

    def is_pool_failing(self) -> tuple[bool, str]:
        if not self._sessions:
            return True, "No sessions in pool"
        if self._pool_failed:
            reasons = [s.failure_reason for s in self._sessions if s.dead and s.failure_reason]
            return True, ("; ".join(reasons[:2]) or "All sessions are down")
        return False, ""

    # -----------------------------------------------------------------------
    # COOLDOWNS
    # -----------------------------------------------------------------------

    def check_cooldown(self, user_id: str) -> tuple[bool, int]:
        if self.cooldown_seconds <= 0:
            return False, 0
        last = self._cooldowns.get(user_id)
        if last is None:
            return False, 0
        elapsed = int(time.time()) - last
        if elapsed < self.cooldown_seconds:
            return True, self.cooldown_seconds - elapsed
        return False, 0

    def set_cooldown(self, user_id: str):
        self._cooldowns[user_id] = int(time.time())
        self._cooldown_dirty = True

    def reset_cooldown(self, user_id: str) -> bool:
        if user_id in self._cooldowns:
            del self._cooldowns[user_id]
            self._cooldown_dirty = True
            return True
        return False

    def reset_all_cooldowns(self) -> int:
        n = len(self._cooldowns)
        self._cooldowns.clear()
        self._cooldown_dirty = True
        return n

    def get_active_cooldown_count(self) -> int:
        return len(self._cooldowns)

    # -----------------------------------------------------------------------
    # STATS
    # -----------------------------------------------------------------------

    def record_request(self, user_id: str, username: str):
        now = int(time.time())
        entry = self._user_stats.setdefault(user_id, {"count": 0, "last_ts": now, "username": username})
        entry["count"] += 1
        entry["last_ts"] = now
        entry["username"] = username
        self._global_count += 1
        self._audit_log.append({"user_id": user_id, "username": username, "ts": now})
        if len(self._audit_log) > AUDIT_MAX:
            self._audit_log = self._audit_log[-AUDIT_MAX:]
        self._stats_dirty = True

    def get_user_stat(self, user_id: str) -> Optional[dict]:
        return self._user_stats.get(user_id)

    def get_top_users(self, n: int = 10) -> list[dict]:
        entries = [{"user_id": uid, **v} for uid, v in self._user_stats.items()]
        return sorted(entries, key=lambda x: x["count"], reverse=True)[:n]

    def get_global_count(self) -> int:
        return self._global_count

    def get_audit_log(self) -> list[dict]:
        return list(reversed(self._audit_log))

    def clear_stats(self):
        self._user_stats.clear()
        self._global_count = 0
        self._audit_log.clear()
        self._stats_dirty = True

    # -----------------------------------------------------------------------
    # GUILD CHANNELS
    # -----------------------------------------------------------------------

    def set_guild_channel(self, guild_id: int, channel_id: int):
        self._guild_channels[str(guild_id)] = channel_id
        self._channels_dirty = True

    def get_guild_channel(self, guild_id: int) -> Optional[int]:
        return self._guild_channels.get(str(guild_id))

    def remove_guild_channel(self, guild_id: int) -> bool:
        key = str(guild_id)
        if key in self._guild_channels:
            del self._guild_channels[key]
            self._channels_dirty = True
            return True
        return False

    # -----------------------------------------------------------------------
    # BURN ALERT CALLBACK
    # -----------------------------------------------------------------------

    def set_burn_alert_callback(self, cb: Callable):
        self._burn_alert_cb = cb

    # -----------------------------------------------------------------------
    # INTERNAL — REFRESH
    # -----------------------------------------------------------------------

    async def _refresh_loop(self):
        """Fire every 25 minutes. Sessions are refreshed sequentially with a
        2-second gap between HTTP calls — no parallel bursts to Nakama."""
        while True:
            try:
                await asyncio.sleep(REFRESH_LOOP_INTERVAL)
                await self._refresh_all_if_needed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[refresh_loop] {e}")
                await asyncio.sleep(60)   # back off on unexpected error

    async def _refresh_all_if_needed(self):
        """Check each session sequentially. 2-second pause between HTTP calls
        so we never send a burst of refresh requests to Nakama at once."""
        sessions = list(self._sessions)
        first = True
        for s in sessions:
            if not first:
                await asyncio.sleep(INTER_SESSION_DELAY)
            first = False
            try:
                await self._check_session(s)
            except Exception as e:
                log.error(f"[refresh] Unexpected error on '{s.label}': {e}")
        alive = sum(1 for s in self._sessions if not s.dead)
        if self._sessions:
            self._pool_failed = alive == 0

    async def _check_session(self, session: Session):
        """Refresh this session only if its bearer is near expiry."""
        if session.dead:
            return
        if session.token_secs_left() >= self.refresh_threshold:
            return
        async with session.lock:
            # Double-check inside lock to avoid concurrent duplicate refreshes
            if session.token_secs_left() >= self.refresh_threshold:
                return
            await self._refresh_session(session)

    async def _refresh_session(self, session: Session) -> bool:
        if not session.refresh_token:
            session.failure_reason = "No refresh_token"
            session.dead = True
            return False

        if not session.is_refresh_token_valid():
            session.failure_reason = "refresh_token expired — add a new session with /addtoken"
            session.dead = True
            log.warning(f"[pool] '{session.label}': {session.failure_reason}")
            await self._save_sessions()
            return False

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._http_refresh, session.refresh_token)
            if data:
                session.token         = data["token"]
                session.refresh_token = data["refresh_token"]
                session.last_refresh_ts = time.time()
                session.consecutive_failures = 0
                session.failure_reason = ""
                session.dead = False
                exp = self._decode_jwt_exp(data["token"])
                log.info(
                    f"[pool] '{session.label}' refreshed — "
                    f"new bearer expires {time.strftime('%H:%M:%S UTC', time.gmtime(exp))}"
                )
                await self._save_sessions()
                return True
            session.failure_reason = "Nakama returned no token in response"
        except urllib_error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            session.failure_reason = f"HTTP {e.code}: {body[:120]}"
            log.error(f"[pool] '{session.label}' refresh HTTP error: {session.failure_reason}")
        except Exception as e:
            session.failure_reason = str(e)[:120]
            log.error(f"[pool] '{session.label}' refresh exception: {e}")

        session.consecutive_failures += 1
        if session.consecutive_failures >= MAX_CONSECUTIVE_FAIL:
            session.dead = True
            log.error(
                f"[pool] '{session.label}' DEAD after {session.consecutive_failures} failures: "
                f"{session.failure_reason}"
            )
        return False

    # -----------------------------------------------------------------------
    # INTERNAL — BURN MONITOR
    # -----------------------------------------------------------------------

    async def _burn_monitor(self):
        while True:
            try:
                await asyncio.sleep(300)
                await self._check_burn_levels()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[burn_monitor] {e}")

    async def _check_burn_levels(self):
        for s in list(self._sessions):
            if s.dead:
                continue
            level = s.burn_level()
            if level == "CRITICAL" and not s.burn_alerted:
                s.burn_alerted = True
                await self._save_sessions()
                log.warning(f"[burn] '{s.label}' CRITICAL — refresh_token < 1h remaining")
                if self._burn_alert_cb:
                    try:
                        await self._burn_alert_cb(s, level)
                    except Exception as e:
                        log.error(f"[burn] alert callback error: {e}")
            elif level != "CRITICAL" and s.burn_alerted:
                s.burn_alerted = False
                await self._save_sessions()
                log.info(f"[burn] '{s.label}' risk returned to {level}")

    # -----------------------------------------------------------------------
    # INTERNAL — FLUSH LOOPS
    # -----------------------------------------------------------------------

    async def _cooldown_flush(self):
        while True:
            try:
                await asyncio.sleep(60)
                if self._cooldown_dirty:
                    await self._do_flush_cooldowns()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[cd_flush] {e}")

    async def _stats_flush(self):
        while True:
            try:
                await asyncio.sleep(120)
                if self._stats_dirty:
                    await self._do_flush_stats()
                if self._channels_dirty:
                    await self._do_flush_channels()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[stats_flush] {e}")

    # -----------------------------------------------------------------------
    # DISK — SESSIONS
    # -----------------------------------------------------------------------

    async def _load_sessions(self):
        loop = asyncio.get_event_loop()
        self._sessions = await loop.run_in_executor(None, self._sync_load_sessions)

    async def _save_sessions(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_save_sessions)

    def _sync_load_sessions(self) -> list[Session]:
        try:
            if not self.tokens_file.exists():
                log.warning("tokens.json not found — starting with empty pool")
                return []
            raw = json.loads(self.tokens_file.read_text())
            # Migrate legacy single-session format
            if isinstance(raw, dict):
                if "token" in raw and "refresh_token" in raw:
                    log.info("Migrating legacy tokens.json to pool format")
                    raw = [{"label": "session_1", "token": raw["token"], "refresh_token": raw["refresh_token"]}]
                    self._atomic_write(self.tokens_file, raw)
                else:
                    return []
            if not isinstance(raw, list):
                return []
            sessions = []
            for i, entry in enumerate(raw):
                if "token" in entry and "refresh_token" in entry:
                    sessions.append(Session.from_dict(entry, i))
                else:
                    log.warning(f"Skipping incomplete entry {i} in tokens.json")
            log.info(f"Loaded {len(sessions)} sessions from disk")
            return sessions
        except Exception as e:
            log.error(f"Failed to load tokens.json: {e}")
            return []

    def _sync_save_sessions(self):
        self._atomic_write(self.tokens_file, [s.to_dict() for s in self._sessions])

    # -----------------------------------------------------------------------
    # DISK — COOLDOWNS / STATS / CHANNELS
    # -----------------------------------------------------------------------

    async def _load_cooldowns(self):
        loop = asyncio.get_event_loop()
        self._cooldowns = await loop.run_in_executor(None, self._sync_load_json, self.cooldowns_file, {})

    async def _do_flush_cooldowns(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._atomic_write, self.cooldowns_file, self._cooldowns)
        self._cooldown_dirty = False

    async def _load_stats(self):
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._sync_load_json, self.stats_file, {})
        self._user_stats   = data.get("user_stats", {})
        self._global_count = data.get("global_count", 0)
        self._audit_log    = data.get("audit_log", [])

    async def _do_flush_stats(self):
        loop = asyncio.get_event_loop()
        data = {
            "user_stats":   self._user_stats,
            "global_count": self._global_count,
            "audit_log":    self._audit_log,
        }
        await loop.run_in_executor(None, self._atomic_write, self.stats_file, data)
        self._stats_dirty = False

    async def _load_channels(self):
        loop = asyncio.get_event_loop()
        self._guild_channels = await loop.run_in_executor(None, self._sync_load_json, self.channels_file, {})

    async def _do_flush_channels(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._atomic_write, self.channels_file, self._guild_channels)
        self._channels_dirty = False

    # -----------------------------------------------------------------------
    # DISK HELPERS
    # -----------------------------------------------------------------------

    def _sync_load_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception as e:
            log.warning(f"Failed to read {path}: {e}")
        return default

    def _atomic_write(self, path: Path, data):
        """Write via temp file then atomic rename — no corrupt files on crash."""
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except Exception as e:
            log.error(f"Failed to write {path}: {e}")

    # -----------------------------------------------------------------------
    # HTTP
    # -----------------------------------------------------------------------

    def _http_refresh(self, refresh_tok: str) -> Optional[dict]:
        basic = base64.b64encode(f"{self.server_key}:".encode()).decode()
        body  = json.dumps({"token": refresh_tok}).encode()
        req = request.Request(
            self.refresh_url,
            data=body,
            method="POST",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Basic {basic}",
            },
        )
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data if "token" in data else None

    # -----------------------------------------------------------------------
    # WEIGHTED SELECTION
    # -----------------------------------------------------------------------

    def _weighted_pick(self, sessions: list[Session]) -> Session:
        import random
        weights = [BURN_WEIGHTS.get(s.burn_level(), 1) for s in sessions]
        total   = sum(weights)
        r       = random.uniform(0, total)
        cumul   = 0
        for s, w in zip(sessions, weights):
            cumul += w
            if r <= cumul:
                return s
        return sessions[-1]

    # -----------------------------------------------------------------------
    # UTILITIES
    # -----------------------------------------------------------------------

    @staticmethod
    def _decode_jwt_exp(jwt_token: str) -> int:
        if not jwt_token:
            return 0
        try:
            payload_b64 = jwt_token.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
            exp = payload.get("exp", 0)
            return exp if exp else int(time.time()) + 999_999
        except Exception:
            return int(time.time()) + 999_999
