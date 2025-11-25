#!/usr/bin/env python3
"""
GPU Scheduler - Production-grade GPU resource allocation system
Handles bidding, scheduling, usage monitoring, and credit management
"""

import base64
import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

# ==============================================================================
# CONFIGURATION
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
STATIC_DIR = BASE_DIR / "static"
TZ = ZoneInfo("America/New_York")
NUM_GPUS = 8
HOURS_PER_DAY = 24
SESSION_COOKIE = "gpu_sched_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 150_000
RELEASE_REFUND_CREDITS = 0.34
ROLLOVER_PERCENTAGE = 0.5

# Day status constants
CURRENT_DAY_STATUS = "executing"
OPEN_DAY_STATUS = "open"
FINAL_DAY_STATUS = "final"

# Backward compatibility aliases
CURRENT_WEEK_STATUS = CURRENT_DAY_STATUS
NEXT_WEEK_STATUS = OPEN_DAY_STATUS
FINAL_WEEK_STATUS = FINAL_DAY_STATUS

# ==============================================================================
# GLOBAL STATE
# ==============================================================================

state_lock = threading.RLock()
slot_locks: Dict[str, threading.Lock] = {}
slot_locks_lock = threading.Lock()
state: Dict[str, Any] = {}
sessions: Dict[str, Dict[str, Any]] = {}

# GPU usage tracking: {week_key: {slot_key: {gpu_index: {username: count}}}}
gpu_usage_tracking: Dict[str, Dict[str, Dict[int, Dict[str, int]]]] = {}
gpu_tracking_lock = threading.Lock()

# Real-time GPU usage tracking for current hour: {gpu_index: [usernames]}
live_gpu_usage: Dict[int, List[str]] = {}
live_gpu_timestamp: Optional[datetime] = None
live_usage_lock = threading.Lock()


# ==============================================================================
# UTILITY FUNCTIONS
# ==============================================================================

def ensure_dirs() -> None:
    """Create necessary directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)


def now_et() -> datetime:
    """Return current time in Eastern timezone."""
    return datetime.now(tz=TZ)


def get_day_transition_hour() -> int:
    """
    Get the configured hour when days transition (0-23).

    A logical day starts at this hour and runs for 24 hours.
    For example, if transition hour is 6:
    - Day "2024-01-15" runs from 06:00 Jan 15 to 05:59 Jan 16
    - Hours are numbered 0-23, where hour 0 is 06:00-07:00
    """
    with state_lock:
        return state.get("config", {}).get("day_transition_hour", 0)


def set_day_transition_hour(hour: int) -> None:
    """Set the day transition hour (0-23)."""
    if not (0 <= hour <= 23):
        raise ValueError("Transition hour must be between 0 and 23")

    with state_lock:
        if "config" not in state:
            state["config"] = {}
        state["config"]["day_transition_hour"] = hour
        save_state()


def day_start_for(dt: datetime) -> datetime:
    """
    Get the start of the logical day containing dt.

    A logical day starts at the configured transition hour and runs for 24 hours.
    If the current hour is before the transition hour, we're still in yesterday's logical day.

    Examples (if transition hour is 6):
    - 2024-01-15 08:00 → 2024-01-15 06:00 (day "2024-01-15")
    - 2024-01-15 03:00 → 2024-01-14 06:00 (day "2024-01-14")
    """
    dt = dt.astimezone(TZ)
    transition_hour = get_day_transition_hour()

    # If current hour is before transition hour, we're still in yesterday's logical day
    if dt.hour < transition_hour:
        dt = dt - timedelta(days=1)

    return dt.replace(hour=transition_hour, minute=0, second=0, microsecond=0)


def format_day(dt: datetime) -> str:
    """Format datetime as day key (YYYY-MM-DD)."""
    return dt.strftime("%Y-%m-%d")


def parse_day(day_key: str) -> datetime:
    """
    Parse day key to datetime with the configured transition hour applied.

    For example, if transition hour is 6:
    - parse_day("2025-11-25") → 2025-11-25 06:00:00
    """
    dt = datetime.strptime(day_key, "%Y-%m-%d").replace(tzinfo=TZ)
    transition_hour = get_day_transition_hour()
    return dt.replace(hour=transition_hour, minute=0, second=0, microsecond=0)


def day_close_time(day_start: datetime) -> datetime:
    """
    Return the cutoff time (end of logical day) for a given day start.

    The logical day ends one second before the next day's transition hour.
    For example, if transition hour is 6:
    - Day starting at 2024-01-15 06:00 ends at 2024-01-16 05:59:59
    """
    # Add 24 hours to get to next transition, then subtract 1 second
    return day_start + timedelta(hours=24, seconds=-1)


def logical_hour_to_calendar_hour(logical_hour: int) -> int:
    """
    Convert logical hour index (0-23) to calendar hour (0-23).

    Logical hours start from the transition hour. For example, if transition is 6:
    - Logical hour 0 → Calendar hour 6
    - Logical hour 18 → Calendar hour 0 (wraps around)
    """
    transition = get_day_transition_hour()
    return (transition + logical_hour) % 24


def calendar_hour_to_logical_hour(calendar_hour: int, on_current_day: bool = True) -> int:
    """
    Convert calendar hour (0-23) to logical hour index (0-23).

    Args:
        calendar_hour: The calendar hour (0-23)
        on_current_day: If True, assumes hour is on or after transition (current logical day).
                       If False, assumes hour is before transition (next logical day).

    For example, if transition is 6:
    - Calendar hour 6 → Logical hour 0
    - Calendar hour 0 → Logical hour 18
    """
    transition = get_day_transition_hour()
    if on_current_day:
        return (calendar_hour - transition) % 24
    else:
        # Hour is on next calendar day but same logical day
        return (calendar_hour + 24 - transition) % 24


def format_logical_hour(logical_hour: int) -> str:
    """
    Format a logical hour index as a time range string.

    For example, if transition hour is 6:
    - Logical hour 0 → "06:00-07:00"
    - Logical hour 18 → "00:00-01:00"
    """
    start_calendar_hour = logical_hour_to_calendar_hour(logical_hour)
    end_calendar_hour = (start_calendar_hour + 1) % 24
    return f"{start_calendar_hour:02d}:00-{end_calendar_hour:02d}:00"


# Backward compatibility aliases
def week_start_for(dt: datetime) -> datetime:
    """Deprecated: Use day_start_for instead."""
    return day_start_for(dt)


def format_week(dt: datetime) -> str:
    """Deprecated: Use format_day instead."""
    return format_day(dt)


def parse_week(week_key: str) -> datetime:
    """Deprecated: Use parse_day instead."""
    return parse_day(week_key)


def week_close_time(week_start: datetime) -> datetime:
    """Deprecated: Use day_close_time instead."""
    return day_close_time(week_start)


def slot_id(day_str: str, hour: int) -> str:
    """Generate slot ID from day string and hour."""
    return f"{day_str}T{hour:02d}:00"


def guess_mime_type(path: Path) -> str:
    """Guess MIME type from file extension."""
    mime_types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }
    return mime_types.get(path.suffix, "application/octet-stream")


# ==============================================================================
# AUTHENTICATION & SESSION MANAGEMENT
# ==============================================================================

def hash_password(password: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    """Hash password with PBKDF2-SHA256. Returns (salt_hex, hash_hex)."""
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, PASSWORD_ITERATIONS
    )
    return salt_hex, hashed.hex()


def verify_password(password: str, user: Dict[str, Any]) -> bool:
    """Verify password against user's stored hash."""
    salt_hex = user["salt"]
    _, hashed = hash_password(password, salt_hex)
    return secrets.compare_digest(hashed, user["password_hash"])


def refresh_sessions() -> None:
    """Remove expired sessions."""
    expire_before = datetime.utcnow().timestamp() - SESSION_TTL_SECONDS
    expired = [
        sid for sid, meta in sessions.items() if meta["issued_at"] < expire_before
    ]
    for sid in expired:
        sessions.pop(sid, None)


def create_session(username: str) -> str:
    """Create new session for username. Returns session ID."""
    refresh_sessions()
    session_id = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    sessions[session_id] = {
        "username": username,
        "issued_at": datetime.utcnow().timestamp(),
    }
    return session_id


def get_session_user(handler: BaseHTTPRequestHandler) -> Optional[Dict[str, Any]]:
    """Extract and validate session from request. Returns user dict or None."""
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return None

    cookie = SimpleCookie()
    cookie.load(cookie_header)
    if SESSION_COOKIE not in cookie:
        return None

    session_id = cookie[SESSION_COOKIE].value
    refresh_sessions()
    session_meta = sessions.get(session_id)
    if not session_meta:
        return None

    username = session_meta["username"]
    user = state["users"].get(username)
    if not user or not user.get("enabled", True):
        return None

    # Renew session
    session_meta["issued_at"] = datetime.utcnow().timestamp()
    return user


def destroy_session(handler: BaseHTTPRequestHandler) -> None:
    """Remove session from active sessions."""
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return

    cookie = SimpleCookie()
    cookie.load(cookie_header)
    if SESSION_COOKIE not in cookie:
        return

    session_id = cookie[SESSION_COOKIE].value
    sessions.pop(session_id, None)


# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================

def load_state() -> None:
    """Load state from disk or create fresh state."""
    global state, gpu_usage_tracking
    ensure_dirs()
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        # Migrate from old "weeks" format to new "days" format
        if "weeks" in state and "days" not in state:
            print("Migrating from weeks to days format...")
            state["days"] = state.pop("weeks")
            # Update keys from "week_start" to "day_start"
            for day_data in state["days"].values():
                if "week_start" in day_data and "day_start" not in day_data:
                    day_data["day_start"] = day_data.pop("week_start")

        # Ensure days dict exists
        if "days" not in state:
            state["days"] = {}

        # Restore GPU usage tracking data from state
        with gpu_tracking_lock:
            saved_tracking = state.get("gpu_usage_tracking", {})
            # Convert string keys back to integers for GPU indices
            for day_key, slots in saved_tracking.items():
                if day_key not in gpu_usage_tracking:
                    gpu_usage_tracking[day_key] = {}
                for slot_key, gpus in slots.items():
                    if slot_key not in gpu_usage_tracking[day_key]:
                        gpu_usage_tracking[day_key][slot_key] = {}
                    # GPU indices are stored as strings in JSON, convert back to int
                    for gpu_str, user_counts in gpus.items():
                        gpu_index = int(gpu_str)
                        gpu_usage_tracking[day_key][slot_key][gpu_index] = user_counts
    else:
        state = {
            "users": {},
            "days": {},
            "bid_log": [],
            "policy": {"hourly_gpu_cap": None, "reserved_slots": {}},
            "gpu_usage_tracking": {},
        }
        create_default_users()
        save_state()


def save_state() -> None:
    """Atomically save state to disk."""
    ensure_dirs()

    # Include GPU usage tracking data in state before saving
    with gpu_tracking_lock:
        # Convert gpu_usage_tracking to JSON-serializable format
        # (int keys become strings in JSON)
        tracking_for_json = {}
        for week_key, slots in gpu_usage_tracking.items():
            tracking_for_json[week_key] = {}
            for slot_key, gpus in slots.items():
                tracking_for_json[week_key][slot_key] = {}
                for gpu_index, user_counts in gpus.items():
                    # Store GPU index as string for JSON compatibility
                    tracking_for_json[week_key][slot_key][str(gpu_index)] = user_counts

        state["gpu_usage_tracking"] = tracking_for_json

    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def create_default_users() -> None:
    """Create default user accounts."""
    users = [
        ("eb", "admin"),
        ("kl2792", "user"),
        ("yushupan", "user"),
        ("ml", "user"),
        ("kevinmxia", "user"),
        ("adiba.ejaz", "user"),
        ("adam2392", "user"),
        ("kasra", "user"),
        ("dplecko", "user"),
        ("aurghya", "user"),
        ("junzhez", "user"),
        ("shreyas", "user"),
        ("jgw2140", "user"),
        ("inwoo", "user"),
        ("aa5506", "user"),
        ("msj2164", "user"),
        ("pk2819", "user"),
        ("ta2432", "user"),
        ("ar", "user"),
    ]

    for username, role in users:
        salt, password_hash = hash_password(username)
        state["users"][username] = {
            "username": username,
            "salt": salt,
            "password_hash": password_hash,
            "role": role,
            "weekly_budget": 100,
            "balance": 100.0,
            "rollover_applied": 0,
            "last_refill_week": None,
            "enabled": True,
            "last_login": None,
        }


# ==============================================================================
# WEEK MANAGEMENT
# ==============================================================================

def find_day_by_status(status: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Find day with given status. Returns (day_key, day_data) or None."""
    for key, day in state.get("days", {}).items():
        if day.get("status") == status:
            return key, day
    return None


def find_days_by_status(status: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Find all days with given status. Returns list of (day_key, day_data)."""
    results = []
    for key, day in state.get("days", {}).items():
        if day.get("status") == status:
            results.append((key, day))
    return sorted(results, key=lambda x: x[0])


# Backward compatibility
def find_week_by_status(status: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Deprecated: Use find_day_by_status instead."""
    return find_day_by_status(status)


def ensure_day_exists(day_start: datetime, status: str = "future") -> Dict[str, Any]:
    """Ensure day exists in state. Create if missing. Returns day data."""
    day_key = format_day(day_start)

    # Initialize days dict if using old state with "weeks"
    if "days" not in state:
        state["days"] = {}

    existing = state["days"].get(day_key)

    if existing:
        # Preserve historical data - don't overwrite status if day has data
        has_data = any(
            any(gpu["winner"] for gpu in slot["gpu_prices"])
            for slot in existing.get("slots", {}).values()
        )
        if not has_data:
            existing["status"] = status
        existing.setdefault("finalized_at", None)
        return existing

    # Create new day with empty slots (24 hours)
    slots: Dict[str, Dict[str, Any]] = {}
    day_str = day_start.strftime("%Y-%m-%d")
    for hour in range(HOURS_PER_DAY):
        slot_key = slot_id(day_str, hour)
        slots[slot_key] = {
            "gpu_prices": [
                {"gpu": gpu, "price": 0, "winner": None, "bids": []}
                for gpu in range(NUM_GPUS)
            ]
        }

    state["days"][day_key] = {
        "day_start": day_key,
        "status": status,
        "slots": slots,
        "finalized_at": None,
    }
    return state["days"][day_key]


# Backward compatibility
def ensure_week_exists(week_start: datetime, status: str = "future") -> Dict[str, Any]:
    """Deprecated: Use ensure_day_exists instead."""
    return ensure_day_exists(week_start, status)


def initialize_days() -> None:
    """Initialize calendar with current day + next 6 days. Only called on first run."""
    now = now_et()
    current = day_start_for(now)

    # Initialize days dict if needed
    if "days" not in state:
        state["days"] = {}

    # Check if we're starting fresh
    is_fresh_start = len(state.get("days", {})) == 0

    # Create current day (executing)
    ensure_day_exists(current, status=CURRENT_DAY_STATUS)

    # Create next 6 days (open for bidding)
    for offset in range(1, 7):
        next_day = current + timedelta(days=offset)
        ensure_day_exists(next_day, status=OPEN_DAY_STATUS)

    # Only reset balances on fresh start
    if is_fresh_start:
        for user in state["users"].values():
            user["balance"] = float(user.get("weekly_budget", 100))
            user["rollover_applied"] = 0

    save_state()


# Backward compatibility
def initialize_calendar() -> None:
    """Deprecated: Use initialize_days instead."""
    initialize_days()


def maybe_auto_advance(now: datetime) -> Optional[Dict[str, Any]]:
    """Auto-advance day cycle if midnight passed. Handles multi-day catchup."""
    result = None
    max_iterations = 10

    for _ in range(max_iterations):
        current_entry = find_day_by_status(CURRENT_DAY_STATUS)

        if not current_entry:
            break

        current_key, _ = current_entry
        cutoff = day_close_time(parse_day(current_key))

        if now < cutoff:
            break

        result = advance_day_cycle(now_override=now)

    return result


def advance_day_cycle(now_override: Optional[datetime] = None) -> Dict[str, Any]:
    """Finalize current day, add daily budget, and promote next day to current."""
    now = now_override or now_et()
    current_entry = find_day_by_status(CURRENT_DAY_STATUS)
    open_days = find_days_by_status(OPEN_DAY_STATUS)

    if not open_days:
        return {"error": "No open days to promote."}

    # Get first open day (tomorrow)
    next_day_key, next_day_data = open_days[0]

    if not current_entry:
        current_key = format_day(day_start_for(now))
        current_entry = (
            current_key,
            ensure_day_exists(parse_day(current_key), status=CURRENT_DAY_STATUS),
        )

    current_key, current_day = current_entry

    # Calculate credits owed by winners of the day becoming current
    payouts: Dict[str, float] = {}
    for slot in next_day_data["slots"].values():
        for entry in slot["gpu_prices"]:
            winner = entry["winner"]
            if winner:
                payouts[winner] = payouts.get(winner, 0.0) + float(entry["price"])

    # Deduct credits from winners
    for username, amount in payouts.items():
        user = state["users"].get(username)
        if user:
            balance = float(user.get("balance", 0))
            user["balance"] = max(0.0, balance - amount)

    # Add daily budget to ALL users (NO rollover)
    for user in state["users"].values():
        if not user.get("enabled", True):
            continue

        daily_budget = float(user.get("weekly_budget", 100))
        current_balance = float(user.get("balance", 0))
        user["balance"] = current_balance + daily_budget
        user["rollover_applied"] = 0  # No rollover in daily system
        user["last_refill_week"] = next_day_key  # Keep for compatibility

    # Archive old current day
    current_day["status"] = FINAL_DAY_STATUS
    if not current_day.get("finalized_at"):
        current_day["finalized_at"] = now.isoformat()

    # Promote first open day to current
    next_day_data["status"] = CURRENT_DAY_STATUS
    next_day_data["finalized_at"] = now.isoformat()

    # Create new 6th open day (5 days from tomorrow since tomorrow is becoming current)
    new_current_start = parse_day(next_day_key)
    new_open_day_start = new_current_start + timedelta(days=6)
    new_open_day_key = format_day(new_open_day_start)

    # Clean up if exists
    if "days" in state and new_open_day_key in state["days"]:
        state["days"].pop(new_open_day_key)

    new_open_day = ensure_day_exists(new_open_day_start, status=OPEN_DAY_STATUS)

    save_state()

    return {
        "ok": True,
        "current_day": next_day_data.get("day_start", next_day_key),
        "new_open_day": new_open_day.get("day_start", new_open_day_key),
    }


# Backward compatibility
def advance_week_cycle(now_override: Optional[datetime] = None) -> Dict[str, Any]:
    """Deprecated: Use advance_day_cycle instead."""
    return advance_day_cycle(now_override)


def update_system_state() -> None:
    """
    Ensure current day + next 6 days exist.
    Auto-advance if midnight passed.
    Finalize past GPU slots with actual usage.
    """
    now = now_et()

    # Initialize days dict if needed (for migration from weeks)
    if "days" not in state:
        state["days"] = {}

    current_entry = find_day_by_status(CURRENT_DAY_STATUS)
    open_days = find_days_by_status(OPEN_DAY_STATUS)

    # Ensure current day exists
    if not current_entry:
        current_day_start = day_start_for(now)
        ensure_day_exists(current_day_start, status=CURRENT_DAY_STATUS)
        current_entry = (format_day(current_day_start), state["days"][format_day(current_day_start)])

    # Ensure we have 6 open days
    current_key, _ = current_entry
    current_day_start = parse_day(current_key)

    # Get all dates that should be open (tomorrow through day+6)
    expected_open_days = []
    for offset in range(1, 7):
        day_start = current_day_start + timedelta(days=offset)
        expected_open_days.append(format_day(day_start))

    # Create missing open days
    for day_key in expected_open_days:
        if day_key not in state["days"]:
            ensure_day_exists(parse_day(day_key), status=OPEN_DAY_STATUS)
        elif state["days"][day_key].get("status") != OPEN_DAY_STATUS:
            # Fix status if wrong
            state["days"][day_key]["status"] = OPEN_DAY_STATUS

    maybe_auto_advance(now)
    finalize_past_gpu_slots()


# ==============================================================================
# SLOT LOCKING
# ==============================================================================

def get_slot_lock_key(week_key: str, slot_key: str, gpu_index: int) -> str:
    """Generate unique key for slot lock."""
    return f"{week_key}|{slot_key}|{gpu_index}"


def get_slot_lock(lock_key: str) -> threading.Lock:
    """Get or create lock for specific slot."""
    with slot_locks_lock:
        if lock_key not in slot_locks:
            slot_locks[lock_key] = threading.Lock()
        return slot_locks[lock_key]


# ==============================================================================
# GPU MONITORING
# ==============================================================================

def process_gpu_status(
    handler: BaseHTTPRequestHandler, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Process GPU usage data from monitoring daemon. Requires bearer token auth.
    
    Expected payload format:
    {
        "timestamp": "2025-11-06T20:07:38-05:00",  # Optional - for validation only
        "usage": {
            "0": ["user1", "user2"],
            "1": [],
            "2": ["user3"],
            ...
        }
    }
    
    SERVER TIME is the source of truth. Daemon timestamp is only used for validation.
    """
    auth_header = handler.headers.get("Authorization", "")
    expected_token = os.environ.get("GPU_MONITOR_TOKEN", "")

    if not expected_token:
        return {"error": "GPU monitoring not configured - GPU_MONITOR_TOKEN not set."}

    if not auth_header.startswith("Bearer "):
        return {"error": "Missing or invalid authorization token."}

    provided_token = auth_header[7:]
    if not secrets.compare_digest(provided_token, expected_token):
        return {"error": "Invalid authorization token."}

    # Validate payload structure
    usage_dict = payload.get("usage")
    
    if not isinstance(usage_dict, dict):
        return {"error": "Missing or invalid 'usage' field - must be an object."}

    # Get SERVER time - this is the authoritative source
    now = now_et()
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    
    # Validate daemon timestamp if provided (for clock skew detection)
    timestamp_str = payload.get("timestamp")
    daemon_time = None
    clock_skew_seconds = 0
    
    if timestamp_str:
        try:
            daemon_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            daemon_time = daemon_time.astimezone(TZ)
            clock_skew_seconds = abs((now - daemon_time).total_seconds())
            
            # Warn if clock skew is significant (>5 minutes) but still process
            if clock_skew_seconds > 300:
                print(f"WARNING: Clock skew detected: {clock_skew_seconds:.0f}s between server and daemon")
        except (ValueError, AttributeError) as e:
            # Timestamp is malformed but not critical - continue with server time
            print(f"WARNING: Invalid daemon timestamp: {e}")

    # Use SERVER time to determine which slot to update
    week_start = week_start_for(now)
    week_key = format_week(week_start)
    day_str = now.strftime("%Y-%m-%d")
    hour = now.hour
    slot_key = slot_id(day_str, hour)

    # Update live usage tracking (always current hour by definition)
    with live_usage_lock:
        global live_gpu_usage, live_gpu_timestamp
        live_gpu_usage.clear()
        for gpu_str, users in usage_dict.items():
            try:
                gpu_index = int(gpu_str)
                if 0 <= gpu_index < NUM_GPUS and isinstance(users, list):
                    live_gpu_usage[gpu_index] = [u for u in users if u]  # Filter empty strings
            except (ValueError, TypeError):
                continue
        live_gpu_timestamp = now  # Use SERVER time

    # Record usage samples for historical tracking (using SERVER time)
    processed = 0
    with gpu_tracking_lock:
        if week_key not in gpu_usage_tracking:
            gpu_usage_tracking[week_key] = {}
        if slot_key not in gpu_usage_tracking[week_key]:
            gpu_usage_tracking[week_key][slot_key] = {}

        # Process each GPU's usage
        for gpu_str, users in usage_dict.items():
            try:
                gpu_index = int(gpu_str)
                if gpu_index < 0 or gpu_index >= NUM_GPUS:
                    continue
                if not isinstance(users, list):
                    continue
            except (ValueError, TypeError):
                continue

            if gpu_index not in gpu_usage_tracking[week_key][slot_key]:
                gpu_usage_tracking[week_key][slot_key][gpu_index] = {}

            user_counts = gpu_usage_tracking[week_key][slot_key][gpu_index]
            
            # Increment count for each user currently using this GPU
            for username in users:
                if username:  # Skip empty strings
                    user_counts[username] = user_counts.get(username, 0) + 1
                    processed += 1

    return {
        "ok": True,
        "processed": processed,
        "slot": slot_key,
        "server_time": now.isoformat(),
        "clock_skew": f"{clock_skew_seconds:.1f}s" if daemon_time else "N/A",
        "message": f"Recorded {processed} GPU usage samples for slot {slot_key}",
    }


def finalize_past_gpu_slots() -> int:
    """
    Finalize GPU usage for completed slots.
    Determines actual user based on most frequent usage.
    Returns count of finalized slots.
    """
    now = now_et()
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    finalized_count = 0

    with gpu_tracking_lock:
        with state_lock:
            days_dict = state.get("days", {})
            for day_key in list(gpu_usage_tracking.keys()):
                day = days_dict.get(day_key)
                if not day or day["status"] not in {
                    CURRENT_DAY_STATUS,
                    FINAL_DAY_STATUS,
                }:
                    continue

                for slot_key in list(gpu_usage_tracking[day_key].keys()):
                    try:
                        day_str, time_str = slot_key.split("T")
                        slot_datetime = datetime.strptime(
                            f"{day_str}T{time_str}", "%Y-%m-%dT%H:%M"
                        ).replace(tzinfo=TZ)
                    except (ValueError, AttributeError):
                        continue

                    slot_end = slot_datetime + timedelta(hours=1)
                    if slot_end > current_hour_start:
                        continue

                    slot = day["slots"].get(slot_key)
                    if not slot:
                        continue

                    for gpu_index, user_counts in gpu_usage_tracking[day_key][
                        slot_key
                    ].items():
                        if gpu_index >= len(slot["gpu_prices"]):
                            continue

                        entry = slot["gpu_prices"][gpu_index]

                        if "actual_user" in entry:
                            continue

                        if user_counts:
                            actual_user = max(user_counts.items(), key=lambda x: x[1])[
                                0
                            ]
                        else:
                            actual_user = None

                        entry["actual_user"] = actual_user
                        finalized_count += 1

            # Cleanup old tracking data - keep current day + 7 days back
            current_day_key = format_day(day_start_for(now))
            days_to_keep = set()
            for offset in range(-7, 1):  # Keep 7 days back through today
                day_start = day_start_for(now) + timedelta(days=offset)
                days_to_keep.add(format_day(day_start))

            for day_key in list(gpu_usage_tracking.keys()):
                if day_key not in days_to_keep:
                    del gpu_usage_tracking[day_key]

    if finalized_count > 0:
        save_state()

    return finalized_count


# ==============================================================================
# USER OPERATIONS
# ==============================================================================

def user_summary(user: Dict[str, Any]) -> Dict[str, Any]:
    """Generate user summary for API responses."""
    committed = committed_for_user(user["username"])
    balance_display = int(user["balance"])
    return {
        "username": user["username"],
        "role": user["role"],
        "balance": balance_display,
        "weekly_budget": user["weekly_budget"],
        "rollover_applied": user.get("rollover_applied", 0),
        "committed": committed,
    }


def committed_for_user(username: str) -> int:
    """Calculate total credits committed by user in all open days."""
    open_days = find_days_by_status(OPEN_DAY_STATUS)
    if not open_days:
        return 0

    total = 0
    for _, day_data in open_days:
        for slot in day_data["slots"].values():
            for gpu_entry in slot["gpu_prices"]:
                if gpu_entry["winner"] == username:
                    total += gpu_entry["price"]
    return total


def determine_open_week_key() -> Optional[str]:
    """Deprecated: Use find_days_by_status(OPEN_DAY_STATUS) instead. Returns first open day key for compatibility."""
    open_days = find_days_by_status(OPEN_DAY_STATUS)
    if open_days:
        return open_days[0][0]  # Return first open day key
    return None


def create_user_account(
    username: str, password: str, role: str = "user", weekly_budget: int = 100
) -> Dict[str, Any]:
    """Create new user account."""
    if not username:
        raise ValueError("Username is required.")
    if username in state["users"]:
        raise ValueError("Username already exists.")
    if not password:
        raise ValueError("Password is required.")

    salt, password_hash = hash_password(password)
    weekly_budget = max(0, int(weekly_budget))

    user = {
        "username": username,
        "salt": salt,
        "password_hash": password_hash,
        "role": role,
        "weekly_budget": weekly_budget,
        "balance": float(weekly_budget),
        "rollover_applied": 0,
        "last_refill_week": None,
        "enabled": True,
        "last_login": None,
    }

    state["users"][username] = user
    save_state()
    return user


def set_user_password(username: str, password: str) -> Dict[str, Any]:
    """Change user password."""
    user = state["users"].get(username)
    if not user:
        raise ValueError("User not found.")
    if not password:
        raise ValueError("Password is required.")

    salt, password_hash = hash_password(password)
    user["salt"] = salt
    user["password_hash"] = password_hash
    save_state()
    return user


# ==============================================================================
# BIDDING OPERATIONS
# ==============================================================================

def place_bid(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Place single bid on GPU slot."""
    week_key = payload.get("week")
    slot_key = payload.get("slot")
    gpu_index = payload.get("gpu")

    if week_key is None or slot_key is None or gpu_index is None:
        return {"error": "Missing week, slot, or gpu."}

    try:
        gpu_index = int(gpu_index)
    except (TypeError, ValueError):
        return {"error": "Invalid GPU index."}

    if gpu_index < 0 or gpu_index >= NUM_GPUS:
        return {"error": "GPU index out of range."}

    lock_key = get_slot_lock_key(week_key, slot_key, gpu_index)
    slot_lock = get_slot_lock(lock_key)

    with slot_lock:
        with state_lock:
            day = state.get("days", {}).get(week_key)
            if not day:
                return {"error": "Day not found."}
            if day["status"] != OPEN_DAY_STATUS:
                return {"error": "Bidding is closed for this day."}

            slot = day["slots"].get(slot_key)
            if not slot:
                return {"error": "Slot not found."}

            entry = slot["gpu_prices"][gpu_index]
            policy = state.get("policy", {})
            reserved = policy.get("reserved_slots", {}).get(week_key, [])

            if f"{slot_key}_gpu{gpu_index}" in reserved:
                return {"error": "Slot is reserved by admins."}

            current_price = entry["price"]
            new_price = current_price + 1

            committed = committed_for_user(user["username"])
            available_balance = int(user["balance"])

            if entry["winner"] == user["username"]:
                committed -= current_price

            if committed + new_price > available_balance:
                return {"error": "Insufficient credits to hold this slot at close."}

            # Find users who were outbid by this bid (had bids but now aren't the winner)
            previous_winner = entry.get("winner")
            outbid_users = set()
            for bid in entry["bids"]:
                bid_username = bid.get("username")
                if bid_username and bid_username != user["username"]:
                    outbid_users.add(bid_username)

            entry["price"] = new_price
            entry["winner"] = user["username"]
            timestamp = now_et().isoformat()
            entry["bids"].append(
                {"username": user["username"], "price": new_price, "timestamp": timestamp}
            )

            # Add outbid notification for all users who were outbid
            slot_id = f"{week_key}|{slot_key}|{gpu_index}"
            for outbid_username in outbid_users:
                outbid_user = state.get("users", {}).get(outbid_username)
                if outbid_user:
                    if "outbid_notification_queue" not in outbid_user:
                        outbid_user["outbid_notification_queue"] = []
                    if slot_id not in outbid_user["outbid_notification_queue"]:
                        outbid_user["outbid_notification_queue"].append(slot_id)
                        print(f"ADDED TO QUEUE: {slot_id} for user {outbid_username}")

            state["bid_log"].append(
                {
                    "username": user["username"],
                    "week": week_key,
                    "slot": slot_key,
                    "gpu": gpu_index,
                    "price": new_price,
                    "timestamp": timestamp,
                }
            )

            if len(state["bid_log"]) > 500:
                state["bid_log"] = state["bid_log"][-500:]

            save_state()
            return {"ok": True, "price": new_price, "winner": user["username"]}


def place_bulk_bids(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Place multiple bids atomically - all succeed or all fail."""
    bids = payload.get("bids", [])
    if not bids or not isinstance(bids, list):
        return {"error": "Missing or invalid bids array."}

    if len(bids) == 0:
        return {"error": "No bids provided."}

    # Sort and deduplicate
    sorted_bids = sorted(
        bids, key=lambda b: (b.get("week", ""), b.get("slot", ""), b.get("gpu", 0))
    )

    seen_slots: Set[Tuple[Any, Any, Any]] = set()
    unique_bids = []
    for bid in sorted_bids:
        slot_tuple = (bid.get("week"), bid.get("slot"), bid.get("gpu"))
        if slot_tuple not in seen_slots:
            seen_slots.add(slot_tuple)
            unique_bids.append(bid)

    sorted_bids = unique_bids

    if len(sorted_bids) == 0:
        return {"error": "No valid bids after removing duplicates."}

    # Get lock keys
    lock_keys = []
    for bid in sorted_bids:
        week_key = bid.get("week")
        slot_key = bid.get("slot")
        gpu_index = bid.get("gpu")

        if week_key is None or slot_key is None or gpu_index is None:
            return {"error": "Each bid must have week, slot, and gpu."}

        try:
            gpu_index = int(gpu_index)
        except (TypeError, ValueError):
            return {"error": f"Invalid GPU index: {gpu_index}"}

        if gpu_index < 0 or gpu_index >= NUM_GPUS:
            return {"error": f"GPU index out of range: {gpu_index}"}

        lock_keys.append(get_slot_lock_key(week_key, slot_key, gpu_index))

    # Deduplicate lock keys
    seen_keys: Set[str] = set()
    unique_lock_keys = []
    for key in lock_keys:
        if key not in seen_keys:
            seen_keys.add(key)
            unique_lock_keys.append(key)

    locks = [get_slot_lock(key) for key in unique_lock_keys]

    # Acquire all locks
    for lock in locks:
        lock.acquire()

    try:
        with state_lock:
            # Validate all bids
            validations = []
            for bid in sorted_bids:
                week_key = bid["week"]
                slot_key = bid["slot"]
                gpu_index = int(bid["gpu"])

                day = state.get("days", {}).get(week_key)
                if not day:
                    return {"error": f"Day not found: {week_key}"}
                if day["status"] != OPEN_DAY_STATUS:
                    return {"error": f"Bidding is closed for day {week_key}"}

                slot = day["slots"].get(slot_key)
                if not slot:
                    return {"error": f"Slot not found: {slot_key}"}

                entry = slot["gpu_prices"][gpu_index]
                policy = state.get("policy", {})
                reserved = policy.get("reserved_slots", {}).get(week_key, [])

                if f"{slot_key}_gpu{gpu_index}" in reserved:
                    return {"error": f"Slot {slot_key} GPU {gpu_index} is reserved."}

                current_price = entry["price"]
                new_price = current_price + 1

                validations.append(
                    {
                        "week_key": week_key,
                        "slot_key": slot_key,
                        "gpu_index": gpu_index,
                        "entry": entry,
                        "current_price": current_price,
                        "new_price": new_price,
                        "is_mine": entry["winner"] == user["username"],
                    }
                )

            # Check total cost
            committed = committed_for_user(user["username"])
            available_balance = int(user["balance"])
            total_cost = 0

            for v in validations:
                if v["is_mine"]:
                    committed -= v["current_price"]
                total_cost += v["new_price"]

            if committed + total_cost > available_balance:
                return {"error": "Insufficient credits for all bids."}

            # Execute all bids
            timestamp = now_et().isoformat()
            results = []

            for v in validations:
                entry = v["entry"]

                # Find users who were outbid by this bid
                outbid_users = set()
                for bid in entry["bids"]:
                    bid_username = bid.get("username")
                    if bid_username and bid_username != user["username"]:
                        outbid_users.add(bid_username)

                entry["price"] = v["new_price"]
                entry["winner"] = user["username"]
                entry["bids"].append(
                    {
                        "username": user["username"],
                        "price": v["new_price"],
                        "timestamp": timestamp,
                    }
                )

                # Add outbid notification for all users who were outbid
                slot_id = f"{v['week_key']}|{v['slot_key']}|{v['gpu_index']}"
                for outbid_username in outbid_users:
                    outbid_user = state.get("users", {}).get(outbid_username)
                    if outbid_user:
                        if "outbid_notification_queue" not in outbid_user:
                            outbid_user["outbid_notification_queue"] = []
                        if slot_id not in outbid_user["outbid_notification_queue"]:
                            outbid_user["outbid_notification_queue"].append(slot_id)
                            print(f"ADDED TO QUEUE: {slot_id} for user {outbid_username}")

                state["bid_log"].append(
                    {
                        "username": user["username"],
                        "week": v["week_key"],
                        "slot": v["slot_key"],
                        "gpu": v["gpu_index"],
                        "price": v["new_price"],
                        "timestamp": timestamp,
                    }
                )

                results.append(
                    {
                        "slot": v["slot_key"],
                        "gpu": v["gpu_index"],
                        "price": v["new_price"],
                    }
                )

            if len(state["bid_log"]) > 500:
                state["bid_log"] = state["bid_log"][-500:]

            save_state()
            return {"ok": True, "bids": results, "count": len(results)}

    finally:
        for lock in reversed(locks):
            lock.release()


def undo_bid(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Undo a bid - only allowed if slot was empty or already owned by user.
    """
    week_key = payload.get("week")
    slot_key = payload.get("slot")
    gpu_index = payload.get("gpu")
    previous_winner = payload.get("previousWinner")
    previous_price = payload.get("previousPrice", 0)

    if week_key is None or slot_key is None or gpu_index is None:
        return {"error": "Missing week, slot, or gpu."}

    try:
        gpu_index = int(gpu_index)
    except (TypeError, ValueError):
        return {"error": "Invalid GPU index."}

    if gpu_index < 0 or gpu_index >= NUM_GPUS:
        return {"error": "GPU index out of range."}

    lock_key = get_slot_lock_key(week_key, slot_key, gpu_index)
    slot_lock = get_slot_lock(lock_key)

    with slot_lock:
        with state_lock:
            day = state.get("days", {}).get(week_key)
            if not day:
                return {"error": "Day not found."}
            if day["status"] != OPEN_DAY_STATUS:
                return {"error": "Cannot undo bid - day is not open for bidding."}

            slot = day["slots"].get(slot_key)
            if not slot:
                return {"error": "Slot not found."}

            entry = slot["gpu_prices"][gpu_index]

            if entry["winner"] != user["username"]:
                return {"error": "You don't own this slot - cannot undo."}

            if previous_winner and previous_winner != user["username"]:
                return {"error": "Cannot undo - you outbid another user."}

            entry["winner"] = previous_winner
            entry["price"] = previous_price

            if entry["bids"] and entry["bids"][-1]["username"] == user["username"]:
                entry["bids"].pop()

            save_state()
            return {"ok": True, "reverted": True}


# ==============================================================================
# RELEASE OPERATIONS
# ==============================================================================

def release_slot(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Release a slot from current executing day.
    Only allowed for future slots (at least 1 hour away).
    Returns 50% of slot price as refund.
    """
    day_key = payload.get("week")  # Keep "week" for API compatibility
    slot_key = payload.get("slot")
    gpu_index = payload.get("gpu")

    # Validate input
    if day_key is None or slot_key is None or gpu_index is None:
        return {"error": "Missing day, slot, or gpu."}

    try:
        gpu_index = int(gpu_index)
    except (TypeError, ValueError):
        return {"error": "Invalid GPU index."}

    if gpu_index < 0 or gpu_index >= NUM_GPUS:
        return {"error": "GPU index out of range."}

    # Acquire slot lock
    lock_key = get_slot_lock_key(day_key, slot_key, gpu_index)
    slot_lock = get_slot_lock(lock_key)

    with slot_lock:
        with state_lock:
            now = now_et()

            # Validate day exists and is executing
            day = state.get("days", {}).get(day_key)
            if not day:
                return {"error": "Day not found."}

            if day["status"] != CURRENT_DAY_STATUS:
                return {"error": "Can only release slots from the current executing day."}

            # Validate slot exists
            slot = day["slots"].get(slot_key)
            if not slot:
                return {"error": "Slot not found."}

            if gpu_index >= len(slot["gpu_prices"]):
                return {"error": "Invalid GPU index."}

            entry = slot["gpu_prices"][gpu_index]

            # Validate ownership BEFORE checking time
            if entry["winner"] != user["username"]:
                return {"error": "You don't own this slot."}

            # Validate slot time is in the future
            try:
                day_str, time_str = slot_key.split("T")
                slot_datetime = datetime.strptime(
                    f"{day_str}T{time_str}", "%Y-%m-%dT%H:%M"
                ).replace(tzinfo=TZ)
            except (ValueError, AttributeError) as e:
                return {"error": f"Invalid slot key format: {e}"}

            # Must be at least 1 hour away
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            if slot_datetime < next_hour:
                return {"error": "Cannot release slots that have started or are starting within the next hour."}

            # All validations passed - perform release with 50% refund
            slot_price = float(entry["price"])
            refund = slot_price * 0.5  # 50% refund

            # Update user balance
            current_balance = float(user.get("balance", 0))
            user["balance"] = current_balance + refund

            # Clear slot
            entry["winner"] = None
            entry["price"] = 0

            save_state()

            return {
                "ok": True,
                "released": True,
                "refund": refund,
                "new_balance": user["balance"],  # Return float for precision
            }


def release_slots_bulk(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Release multiple slots at once. All must be owned by user and in future.
    Returns total refund amount based on successfully released slots.
    """
    slots_to_release = payload.get("slots", [])
    if not isinstance(slots_to_release, list) or len(slots_to_release) == 0:
        return {"error": "No slots provided."}

    # Validate input format
    validated = []
    for item in slots_to_release:
        week_key = item.get("week")
        slot_key = item.get("slot")
        gpu_index = item.get("gpu")

        if week_key is None or slot_key is None or gpu_index is None:
            return {"error": "Missing week, slot, or gpu in one or more items."}

        try:
            gpu_index = int(gpu_index)
        except (TypeError, ValueError):
            return {"error": "Invalid GPU index."}

        if gpu_index < 0 or gpu_index >= NUM_GPUS:
            return {"error": "GPU index out of range."}

        validated.append(
            {"week_key": week_key, "slot_key": slot_key, "gpu_index": gpu_index}
        )

    # Collect locks
    locks = []
    for v in validated:
        lock_key = get_slot_lock_key(v["week_key"], v["slot_key"], v["gpu_index"])
        locks.append(get_slot_lock(lock_key))

    # Acquire all locks
    for lock in locks:
        lock.acquire()

    try:
        with state_lock:
            now = now_et()
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            released_count = 0

            # Validate and release each slot
            for v in validated:
                # Validate week
                week = state.get("days", {}).get(v["week_key"])
                if not week or week["status"] != CURRENT_WEEK_STATUS:
                    continue

                # Validate slot time
                try:
                    day_str, time_str = v["slot_key"].split("T")
                    slot_datetime = datetime.strptime(
                        f"{day_str}T{time_str}", "%Y-%m-%dT%H:%M"
                    ).replace(tzinfo=TZ)
                except (ValueError, AttributeError):
                    continue

                if slot_datetime < next_hour:
                    continue

                # Validate slot exists
                slot = week["slots"].get(v["slot_key"])
                if not slot:
                    continue

                if v["gpu_index"] >= len(slot["gpu_prices"]):
                    continue

                entry = slot["gpu_prices"][v["gpu_index"]]

                # Validate ownership
                if entry["winner"] != user["username"]:
                    continue

                # Release slot
                entry["winner"] = None
                entry["price"] = 0
                released_count += 1

            # Calculate and apply refund
            total_refund = released_count * RELEASE_REFUND_CREDITS

            current_balance = float(user.get("balance", 0))
            user["balance"] = current_balance + total_refund

            save_state()

            return {
                "ok": True,
                "released_count": released_count,
                "total_refund": total_refund,
                "new_balance": int(user["balance"]),
            }

    finally:
        for lock in reversed(locks):
            lock.release()


# ==============================================================================
# ADMIN OPERATIONS
# ==============================================================================

def update_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update user settings (admin only)."""
    username = payload.get("username")
    if not username:
        return {"error": "Username is required."}

    user = state["users"].get(username)
    if not user:
        return {"error": "User not found."}

    if "weekly_budget" in payload:
        try:
            weekly_budget = int(payload["weekly_budget"])
        except (TypeError, ValueError):
            return {"error": "Weekly budget must be an integer."}
        user["weekly_budget"] = max(0, weekly_budget)

    if "balance_delta" in payload:
        try:
            delta = int(payload["balance_delta"])
        except (TypeError, ValueError):
            return {"error": "Balance adjustment must be an integer."}
        current_balance = float(user.get("balance", 0))
        user["balance"] = max(0.0, current_balance + delta)

    if "enabled" in payload:
        user["enabled"] = bool(payload["enabled"])

    save_state()
    return {"ok": True, "user": user_summary(user)}


def bulk_update_users(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Apply balance_delta and/or weekly_budget to ALL users (admin only)."""
    count = 0

    if "balance_delta" in payload:
        try:
            delta = int(payload["balance_delta"])
        except (TypeError, ValueError):
            return {"error": "Balance adjustment must be an integer."}
        for user in state["users"].values():
            current_balance = float(user.get("balance", 0))
            user["balance"] = max(0.0, current_balance + delta)
            count += 1

    if "weekly_budget" in payload:
        try:
            weekly_budget = int(payload["weekly_budget"])
        except (TypeError, ValueError):
            return {"error": "Weekly budget must be an integer."}
        for user in state["users"].values():
            user["weekly_budget"] = max(0, weekly_budget)
            count += 1

    save_state()
    return {"ok": True, "message": f"Updated {len(state['users'])} users."}


def create_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create new user (admin only)."""
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or username
    weekly_budget = payload.get("weekly_budget", 100)
    role = payload.get("role", "user")

    if role not in {"user", "admin"}:
        return {"error": "Role must be 'user' or 'admin'."}

    try:
        user = create_user_account(
            username, password, role=role, weekly_budget=int(weekly_budget)
        )
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}

    return {"ok": True, "user": user_summary(user)}


def reset_user_password(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reset user password (admin only)."""
    username = payload.get("username")
    password = payload.get("password")

    if not username or not password:
        return {"error": "Username and password are required."}

    try:
        user = set_user_password(username, password)
    except ValueError as exc:
        return {"error": str(exc)}

    return {"ok": True, "user": user_summary(user)}


def change_password(current_user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Change own password (any user)."""
    old_password = payload.get("old_password")
    new_password = payload.get("new_password")

    if not old_password or not new_password:
        return {"error": "Old and new passwords are required."}

    if not verify_password(old_password, current_user):
        return {"error": "Old password is incorrect."}

    try:
        set_user_password(current_user["username"], new_password)
    except ValueError as exc:
        return {"error": str(exc)}

    return {"ok": True}


def update_policy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update system policy (admin only)."""
    policy = state.setdefault("policy", {})

    if "hourly_gpu_cap" in payload:
        cap = payload["hourly_gpu_cap"]
        if cap is None:
            policy["hourly_gpu_cap"] = None
        else:
            try:
                cap = int(cap)
            except (TypeError, ValueError):
                return {"error": "Cap must be an integer or null."}
            policy["hourly_gpu_cap"] = max(1, cap)

    save_state()
    return {"ok": True, "policy": policy}


def cleanup_old_weeks(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove old weeks from state (admin only)."""
    keep_count = payload.get("keep_count", 0)
    if not isinstance(keep_count, int) or keep_count < 0:
        return {"error": "keep_count must be a non-negative integer."}

    current_entry = find_week_by_status(CURRENT_WEEK_STATUS)
    next_entry = find_week_by_status(NEXT_WEEK_STATUS)

    protected_weeks: Set[str] = set()
    if current_entry:
        protected_weeks.add(current_entry[0])
    if next_entry:
        protected_weeks.add(next_entry[0])

    all_weeks = sorted(state.get("days", {}).keys(), reverse=True)
    weeks_to_keep = set(protected_weeks)
    other_weeks = [w for w in all_weeks if w not in protected_weeks]

    if keep_count > 0:
        weeks_to_keep.update(other_weeks[:keep_count])

    deleted = []
    for week_key in all_weeks:
        if week_key not in weeks_to_keep:
            deleted.append(week_key)
            state.get("days", {}).pop(week_key)

    save_state()

    return {
        "ok": True,
        "deleted_count": len(deleted),
        "deleted_weeks": deleted,
        "kept_count": len(weeks_to_keep),
    }


def list_admin_users() -> List[Dict[str, Any]]:
    """List all users with admin info."""
    now_week = determine_open_week_key()
    users = []

    for user in state["users"].values():
        committed = committed_for_user(user["username"]) if now_week else 0
        users.append(
            {
                "username": user["username"],
                "role": user["role"],
                "weekly_budget": user["weekly_budget"],
                "balance": user["balance"],
                "rollover_applied": user.get("rollover_applied", 0),
                "last_refill_week": user.get("last_refill_week"),
                "enabled": user.get("enabled", True),
                "committed": committed,
            }
        )

    return users


def list_weeks() -> List[Dict[str, Any]]:
    """List all weeks with metadata."""
    weeks = []
    for week_key, week in sorted(state.get("days", {}).items()):
        week_start = parse_week(week_key)
        open_at = week_start
        close_at = week_close_time(week_start)
        weeks.append(
            {
                "week_start": week_key,
                "status": week["status"],
                "finalized_at": week.get("finalized_at"),
                "open_at": open_at.isoformat(),
                "close_at": close_at.isoformat(),
                "day": week_key,
            }
        )
    return weeks


def export_week_csv(week_key: str) -> Optional[str]:
    """Export week schedule as CSV."""
    week = state.get("days", {}).get(week_key)
    if not week or week["status"] not in {FINAL_WEEK_STATUS, CURRENT_WEEK_STATUS}:
        return None

    lines = ["slot_id,gpu_index,start_time_utc,end_time_utc,winner_username,final_price"]

    for slot_key, slot_data in sorted(week["slots"].items()):
        day_str, time_str = slot_key.split("T")
        start_local = datetime.strptime(f"{day_str}T{time_str}", "%Y-%m-%dT%H:%M").replace(
            tzinfo=TZ
        )
        start_utc = start_local.astimezone(ZoneInfo("UTC"))
        end_utc = start_utc + timedelta(hours=1)

        for entry in slot_data["gpu_prices"]:
            lines.append(
                ",".join(
                    [
                        f"{slot_key}_gpu{entry['gpu']}",
                        str(entry["gpu"]),
                        start_utc.isoformat(),
                        end_utc.isoformat(),
                        entry["winner"] or "",
                        str(entry["price"]),
                    ]
                )
            )

    return "\n".join(lines)


def export_usage_tracking_csv(week_key: str) -> Optional[str]:
    """
    Export detailed GPU usage tracking data for a week as CSV.
    Shows assigned users vs actual users based on monitoring data.
    """
    week = state.get("days", {}).get(week_key)
    if not week or week["status"] not in {FINAL_WEEK_STATUS, CURRENT_WEEK_STATUS}:
        return None

    lines = [
        "slot_id,gpu_index,start_time_utc,end_time_utc,assigned_user,actual_user,"
        "match_status,all_users_detected,sample_counts"
    ]

    # Get tracking data for this week
    with gpu_tracking_lock:
        week_tracking = gpu_usage_tracking.get(week_key, {})

    for slot_key, slot_data in sorted(week["slots"].items()):
        day_str, time_str = slot_key.split("T")
        start_local = datetime.strptime(f"{day_str}T{time_str}", "%Y-%m-%dT%H:%M").replace(
            tzinfo=TZ
        )
        start_utc = start_local.astimezone(ZoneInfo("UTC"))
        end_utc = start_utc + timedelta(hours=1)

        # Get tracking samples for this slot
        slot_tracking = week_tracking.get(slot_key, {})

        for entry in slot_data["gpu_prices"]:
            gpu_index = entry["gpu"]
            assigned_user = entry.get("winner") or ""
            actual_user = entry.get("actual_user", "")

            # Get all users who used this GPU (from tracking samples)
            gpu_samples = slot_tracking.get(gpu_index, {})
            all_users = ", ".join(f"{user}({count})" for user, count in sorted(
                gpu_samples.items(), key=lambda x: x[1], reverse=True
            )) if gpu_samples else ""

            # Determine match status
            if not assigned_user and not actual_user:
                match_status = "empty"
            elif not assigned_user and actual_user:
                match_status = "squatter"
            elif assigned_user and not actual_user:
                match_status = "no_show"
            elif assigned_user == actual_user:
                match_status = "match"
            else:
                match_status = "mismatch"

            # Format sample counts for CSV
            sample_counts = ";".join(f"{user}:{count}" for user, count in sorted(
                gpu_samples.items(), key=lambda x: x[1], reverse=True
            )) if gpu_samples else ""

            lines.append(
                ",".join(
                    [
                        f"{slot_key}_gpu{gpu_index}",
                        str(gpu_index),
                        start_utc.isoformat(),
                        end_utc.isoformat(),
                        assigned_user,
                        str(actual_user) if actual_user is not None else "",
                        match_status,
                        f'"{all_users}"' if all_users else "",
                        f'"{sample_counts}"' if sample_counts else "",
                    ]
                )
            )

    return "\n".join(lines)


def clear_demo_data() -> Dict[str, Any]:
    """Clear demo winner/price data from current executing week (admin only)."""
    # Find the current executing week
    current_entry = find_week_by_status(CURRENT_WEEK_STATUS)

    if not current_entry:
        return {"error": "No executing week found."}

    week_key, week_data = current_entry

    # Clear all winner/price data but keep slot structure and actual_user tracking
    cleared_count = 0
    for slot_data in week_data["slots"].values():
        for gpu_entry in slot_data["gpu_prices"]:
            if gpu_entry["winner"] or gpu_entry["price"] > 0:
                gpu_entry["winner"] = None
                gpu_entry["price"] = 0
                gpu_entry["bids"] = []
                # Keep "actual_user" - that's from real GPU tracking
                cleared_count += 1

    save_state()

    return {
        "ok": True,
        "week": week_key,
        "cleared": cleared_count,
        "message": f"Cleared {cleared_count} demo assignments from {week_key}. Real GPU tracking data preserved.",
    }


def populate_demo_data() -> Dict[str, Any]:
    """Populate demo winner/price data in current executing week (admin only).
    Creates realistic block assignments where users get multiple GPUs for continuous periods."""
    import random
    from datetime import datetime, timedelta

    # Find the current executing week
    current_entry = find_week_by_status(CURRENT_WEEK_STATUS)

    if not current_entry:
        return {"error": "No executing week found."}

    week_key, week_data = current_entry
    week_start = parse_week(week_key)

    # Users to assign (excluding admin)
    users = [u for u in state["users"].keys() if state["users"][u]["role"] != "admin"]
    if not users:
        return {"error": "No users available for demo assignments."}

    random.seed(42)  # Reproducible demo data
    assigned_count = 0
    now = now_et()
    timestamp = now.isoformat()

    # Create block assignments
    # Each block: user gets multiple GPUs for several consecutive hours
    num_blocks = random.randint(15, 25)  # 15-25 blocks per week

    for _ in range(num_blocks):
        # Pick a user
        user = random.choice(users)

        # Pick a starting day (favor weekdays)
        if random.random() < 0.7:
            day_offset = random.randint(0, 4)  # Mon-Fri
        else:
            day_offset = random.randint(0, 6)  # Any day

        # Pick starting hour (favor work hours)
        if random.random() < 0.7:
            start_hour = random.randint(8, 14)  # Start 8am-2pm
        else:
            start_hour = random.randint(0, 20)

        # Pick duration (2-8 hours, favoring longer blocks)
        duration = random.choices([2, 3, 4, 5, 6, 7, 8], weights=[1, 2, 3, 4, 3, 2, 1])[0]

        # Pick number of GPUs (1-4, favoring 2-3)
        num_gpus = random.choices([1, 2, 3, 4], weights=[2, 5, 5, 2])[0]

        # Pick which GPUs (consecutive is more realistic)
        start_gpu = random.randint(0, NUM_GPUS - num_gpus)
        gpu_list = list(range(start_gpu, start_gpu + num_gpus))

        # Assign the block
        for hour_offset in range(duration):
            hour = start_hour + hour_offset
            if hour >= 24:  # Don't wrap to next day
                break

            day_start = week_start + timedelta(days=day_offset)
            day_str = day_start.strftime("%Y-%m-%d")
            slot_key = slot_id(day_str, hour)

            if slot_key not in week_data["slots"]:
                continue

            slot_data = week_data["slots"][slot_key]

            for gpu_idx in gpu_list:
                gpu_entry = slot_data["gpu_prices"][gpu_idx]

                # Skip if already assigned
                if gpu_entry["winner"]:
                    continue

                price = random.randint(1, 4)

                gpu_entry["winner"] = user
                gpu_entry["price"] = price
                gpu_entry["bids"] = [{
                    "username": user,
                    "price": price,
                    "timestamp": timestamp
                }]
                assigned_count += 1

    save_state()

    return {
        "ok": True,
        "week": week_key,
        "assigned": assigned_count,
        "message": f"Populated {assigned_count} demo assignments in {week_key} ({num_blocks} blocks).",
    }


def set_week_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Manually set week status (admin only, for demo/testing purposes)."""
    week_key = payload.get("week")
    new_status = payload.get("status")

    if not week_key or not new_status:
        return {"error": "Missing 'week' or 'status' parameter."}

    if new_status not in ["executing", "open", "final", "future"]:
        return {"error": f"Invalid status: {new_status}"}

    if week_key not in state.get("days", {}):
        return {"error": f"Week not found: {week_key}"}

    old_status = state.get("days", {})[week_key]["status"]
    state.get("days", {})[week_key]["status"] = new_status

    save_state()

    return {
        "ok": True,
        "week": week_key,
        "old_status": old_status,
        "new_status": new_status,
        "message": f"Changed {week_key} from '{old_status}' to '{new_status}'.",
    }


def clear_week_bids(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Clear all bids from a specific week (admin only)."""
    week_key = payload.get("week")

    if not week_key:
        return {"error": "Missing 'week' parameter."}

    if week_key not in state.get("days", {}):
        return {"error": f"Week not found: {week_key}"}

    week_data = state.get("days", {})[week_key]

    # Clear all winner/price data
    cleared_count = 0
    for slot_data in week_data["slots"].values():
        for gpu_entry in slot_data["gpu_prices"]:
            if gpu_entry["winner"] or gpu_entry["price"] > 0:
                gpu_entry["winner"] = None
                gpu_entry["price"] = 0
                gpu_entry["bids"] = []
                cleared_count += 1

    save_state()

    return {
        "ok": True,
        "week": week_key,
        "cleared": cleared_count,
        "message": f"Cleared {cleared_count} bids from {week_key}.",
    }
# ==============================================================================
# VIEW BUILDERS
# ==============================================================================

def has_outbid_notifications_for_day(day_key: str, username: str) -> bool:
    """Check if user has any outbid notifications for a specific day."""
    user = state.get("users", {}).get(username)
    if not user:
        return False

    day_data = state.get("days", {}).get(day_key)
    if not day_data:
        return False

    # Only check open days for notifications
    if day_data.get("status") != OPEN_DAY_STATUS:
        return False

    # Check if any slots in the notification queue belong to this day
    queue = user.get("outbid_notification_queue", [])
    for slot_id in queue:
        if slot_id.startswith(f"{day_key}|"):
            return True

    return False


def build_overview(user: Dict[str, Any]) -> Dict[str, Any]:
    """Build overview data for user - returns current day + next 6 open days."""
    now = now_et()
    username = user.get("username", "")
    overview = {
        "now": now.isoformat(),
        "time_zone": "America/New_York",
        "transition_hour": get_day_transition_hour(),
        "weeks": [],  # Keep name for API compatibility, but contains days
        "user": user_summary(user),
        "policy": state.get("policy", {}),
    }

    # Get current and open days only (not history)
    current_entry = find_day_by_status(CURRENT_DAY_STATUS)
    open_days = find_days_by_status(OPEN_DAY_STATUS)

    # Add current day
    if current_entry:
        day_key, day_data = current_entry
        day_start = parse_day(day_key)
        open_at = day_start
        close_at = day_close_time(day_start)
        overview["weeks"].append(
            {
                "week_start": day_key,  # Keep field name for API compatibility
                "status": day_data["status"],
                "open_at": open_at.isoformat(),
                "close_at": close_at.isoformat(),
                "day": day_key,
                "has_notifications": has_outbid_notifications_for_day(day_key, username),
            }
        )

    # Add open days
    for day_key, day_data in open_days:
        day_start = parse_day(day_key)
        open_at = day_start
        close_at = day_close_time(day_start)
        overview["weeks"].append(
            {
                "week_start": day_key,
                "status": day_data["status"],
                "open_at": open_at.isoformat(),
                "close_at": close_at.isoformat(),
                "day": day_key,
                "has_notifications": has_outbid_notifications_for_day(day_key, username),
            }
        )

    return overview


def week_day_view(
    week_key: str, day: Optional[str], user: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Build day view for specific week."""
    week = state.get("days", {}).get(week_key)
    if not week:
        return None

    now = now_et()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)

    slots = week["slots"]
    day = day or format_week(parse_week(week_key))
    day_slots = {sid: data for sid, data in slots.items() if sid.startswith(day)}

    if not day_slots:
        return None

    policy = state.get("policy", {})
    reserved = policy.get("reserved_slots", {}).get(week_key, [])
    entries = []

    # Get live usage data if available
    with live_usage_lock:
        live_usage_snapshot = dict(live_gpu_usage)
        live_timestamp = live_gpu_timestamp

    # Get historical usage tracking data
    with gpu_tracking_lock:
        week_usage_data = gpu_usage_tracking.get(week_key, {})

    for slot_key, slot_data in sorted(day_slots.items()):
        slot_day, time_part = slot_key.split("T")
        hour = int(time_part[:2])
        try:
            slot_datetime = datetime.strptime(
                f"{slot_day}T{time_part}", "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=TZ)
        except ValueError:
            slot_datetime = None

        # Check if this is the current hour slot
        is_current_hour = slot_datetime == current_hour_start if slot_datetime else False

        row = {"slot": slot_key, "hour": hour, "entries": []}

        for entry in slot_data["gpu_prices"]:
            status = "open"
            is_reserved = any(r == f"{slot_key}_gpu{entry['gpu']}" for r in reserved)

            if week["status"] != OPEN_DAY_STATUS:
                status = "locked"
            if is_reserved:
                status = "reserved"

            can_release = (
                slot_datetime is not None
                and week["status"] == CURRENT_DAY_STATUS
                and entry["winner"] == user["username"]
                and slot_datetime >= next_hour
            )

            # Get live users for this GPU (current hour only)
            live_users = []
            if is_current_hour and entry["gpu"] in live_usage_snapshot:
                live_users = live_usage_snapshot[entry["gpu"]]

            # Get most frequent historical users for this slot
            most_frequent_user = None
            most_frequent_non_owner = None
            
            slot_usage = week_usage_data.get(slot_key, {})
            gpu_usage = slot_usage.get(entry["gpu"], {})
            
            if gpu_usage:
                # Find most frequent user overall
                sorted_users = sorted(gpu_usage.items(), key=lambda x: x[1], reverse=True)
                if sorted_users:
                    most_frequent_user = sorted_users[0][0]
                
                # Find most frequent user who is NOT the slot owner
                owner = entry.get("winner")
                non_owner_users = [(u, c) for u, c in sorted_users if u != owner]
                if non_owner_users:
                    most_frequent_non_owner = non_owner_users[0][0]

            row["entries"].append(
                {
                    "gpu": entry["gpu"],
                    "price": entry["price"],
                    "winner": entry["winner"],
                    "actual_user": entry.get("actual_user"),
                    "status": status,
                    "isMine": entry["winner"] == user["username"],
                    "hasBid": any(
                        b["username"] == user["username"] for b in entry["bids"]
                    ),
                    "canRelease": can_release,
                    "live_users": live_users,
                    "most_frequent_user": most_frequent_user,
                    "most_frequent_non_owner": most_frequent_non_owner,
                    "is_current_hour": is_current_hour,
                }
            )

        entries.append(row)

    week_start = parse_week(week_key)
    close_at = week_close_time(week_start)
    open_at = week_start

    return {
        "week_start": week_key,
        "day": day,
        "status": week["status"],
        "open_at": open_at.isoformat(),
        "close_at": close_at.isoformat(),
        "rows": entries,
        "live_timestamp": live_timestamp.isoformat() if live_timestamp else None,
        "outbid_notification_queue": user.get("outbid_notification_queue", []),
    }


def build_my_week(user: Dict[str, Any]) -> Dict[str, Any]:
    """Build user's own slots summary for current and open days."""
    now = now_et()
    summaries = []

    # Get current day and open days
    current_entry = find_day_by_status(CURRENT_DAY_STATUS)
    open_days = find_days_by_status(OPEN_DAY_STATUS)

    all_days = []
    if current_entry:
        all_days.append(current_entry)
    all_days.extend(open_days)

    for day_key, day_data in all_days:
        slots = []
        for slot_key, slot_data in day_data["slots"].items():
            for entry in slot_data["gpu_prices"]:
                if entry["winner"] == user["username"]:
                    slots.append(
                        {
                            "slot": slot_key,
                            "gpu": entry["gpu"],
                            "price": entry["price"],
                        }
                    )

        summaries.append(
            {
                "week_start": day_key,  # Keep field name for API compatibility
                "status": day_data["status"],
                "slots": sorted(slots, key=lambda x: (x["slot"], x["gpu"])),
            }
        )

    return {"weeks": summaries}


# ==============================================================================
# HTTP HANDLER
# ==============================================================================

class SchedulerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for GPU Scheduler."""

    server_version = "GPUScheduler/1.0"

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        if self.path.startswith("/api/"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_cors_headers()
            self.end_headers()
        else:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path.startswith("/api/"):
            with state_lock:
                update_system_state()
                self.handle_api_get()
        else:
            self.serve_static()

    def do_POST(self) -> None:
        """Handle POST requests."""
        if not self.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(content_length) if content_length else b""

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json(
                {"error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST
            )
            return

        with state_lock:
            update_system_state()
            self.handle_api_post(payload)

    def serve_static(self) -> None:
        """Serve static files."""
        path = self.path
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        else:
            # Remove leading slash and /static/ prefix if present
            safe_path = path.lstrip("/")
            if safe_path.startswith("static/"):
                safe_path = safe_path[7:]  # Remove "static/" prefix

            try:
                file_path = (STATIC_DIR / safe_path).resolve()
                file_path.relative_to(STATIC_DIR.resolve())
            except (OSError, ValueError):
                self.send_error(HTTPStatus.FORBIDDEN)
                return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            with file_path.open("rb") as f:
                content = f.read()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_response(HTTPStatus.OK)
        content_type = guess_mime_type(file_path)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_api_get(self) -> None:
        """Route GET API requests."""
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        current_user = get_session_user(self)

        if route == "/api/session":
            if not current_user:
                self.send_json({"authenticated": False})
                return
            summary = user_summary(current_user)
            self.send_json({"authenticated": True, "user": summary})
            return

        if route == "/api/gpu-live-status":
            # Public endpoint - current GPU usage for display (no auth required)
            with live_usage_lock:
                usage_snapshot = {str(gpu): users for gpu, users in live_gpu_usage.items()}
                timestamp = live_gpu_timestamp.isoformat() if live_gpu_timestamp else None
            
            self.send_json({
                "ok": True,
                "usage": usage_snapshot,
                "timestamp": timestamp,
                "gpu_count": NUM_GPUS,
            })
            return

        if route == "/api/overview":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            overview = build_overview(current_user)
            self.send_json(overview)
            return

        if route == "/api/week":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            week_key = params.get("week", [None])[0]
            if not week_key:
                self.send_json(
                    {"error": "Missing week parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            day = params.get("day", [None])[0]
            data = week_day_view(week_key, day, current_user)
            if data is None:
                self.send_json(
                    {"error": "Week or day not found."}, status=HTTPStatus.NOT_FOUND
                )
                return
            self.send_json(data)
            return

        if route == "/api/my/summary":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            data = build_my_week(current_user)
            self.send_json(data)
            return

        if route == "/api/my/bids":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            limit = int(params.get("limit", ["50"])[0])
            bids_raw = [
                item
                for item in reversed(state["bid_log"])
                if item["username"] == current_user["username"]
            ][:limit]

            bids = []
            for item in bids_raw:
                status = "open"
                week = state.get("days", {}).get(item["week"])
                if week:
                    slot = week["slots"].get(item["slot"])
                    if slot:
                        entry = next(
                            (
                                gpu_entry
                                for gpu_entry in slot["gpu_prices"]
                                if gpu_entry["gpu"] == item["gpu"]
                            ),
                            None,
                        )
                        if entry:
                            if entry["winner"] == current_user["username"]:
                                status = "leading"
                            elif entry["winner"]:
                                status = "lost"
                            else:
                                status = "open"
                enriched = dict(item)
                enriched["status"] = status
                bids.append(enriched)

            self.send_json({"bids": bids})
            return

        if route == "/api/history/days":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            # Get all final (historical) days
            final_days = find_days_by_status(FINAL_DAY_STATUS)
            days_list = []
            for day_key, day_data in sorted(final_days, reverse=True):  # Most recent first
                days_list.append({
                    "day": day_key,
                    "finalized_at": day_data.get("finalized_at"),
                })
            self.send_json({"days": days_list})
            return

        if route == "/api/history/day":
            if not current_user:
                self.send_json(
                    {"error": "Authentication required."},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            day_key = params.get("date", [None])[0]
            if not day_key:
                self.send_json(
                    {"error": "Missing date parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            day_data = state.get("days", {}).get(day_key)
            if not day_data or day_data["status"] != FINAL_DAY_STATUS:
                self.send_json(
                    {"error": "Historical day not found."},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            # Use the same view builder but for historical day
            data = week_day_view(day_key, day_key, current_user)
            if data is None:
                self.send_json(
                    {"error": "Day data not found."}, status=HTTPStatus.NOT_FOUND
                )
                return
            self.send_json(data)
            return

        if route == "/api/admin/users":
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            data = list_admin_users()
            self.send_json({"users": data})
            return

        if route == "/api/admin/weeks":
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            weeks = list_weeks()
            self.send_json({"weeks": weeks})
            return

        if route == "/api/admin/transition-hour":
            # Get current day transition hour
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            transition_hour = get_day_transition_hour()
            self.send_json({
                "transition_hour": transition_hour,
                "current_time": now_et().isoformat()
            })
            return

        if route == "/api/admin/export":
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            week_key = params.get("week", [None])[0]
            if not week_key:
                self.send_json(
                    {"error": "Missing week parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            csv_text = export_week_csv(week_key)
            if csv_text is None:
                self.send_json(
                    {"error": "Week not ready for export."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            filename = f"schedule_{week_key}.csv"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(csv_text.encode("utf-8"))
            return

        if route == "/api/admin/export-usage":
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            week_key = params.get("week", [None])[0]
            if not week_key:
                self.send_json(
                    {"error": "Missing week parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            csv_text = export_usage_tracking_csv(week_key)
            if csv_text is None:
                self.send_json(
                    {"error": "Week not ready for export."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            filename = f"usage_tracking_{week_key}.csv"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(csv_text.encode("utf-8"))
            return

        if route == "/api/admin/export-all":
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            timestamp = now_et().strftime("%Y%m%d_%H%M%S")
            filename = f"gpu_scheduler_full_backup_{timestamp}.json"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            data = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def handle_api_post(self, payload: Dict[str, Any]) -> None:
        """Route POST API requests."""
        parsed = urlparse(self.path)
        route = parsed.path
        current_user = get_session_user(self)

        # GPU monitoring endpoint - uses bearer token auth
        if route == "/api/gpu-status":
            response = process_gpu_status(self, payload)
            if "error" in response and "token" in response["error"].lower():
                status = HTTPStatus.UNAUTHORIZED
            elif response.get("ok"):
                status = HTTPStatus.OK
            else:
                status = HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/login":
            username = payload.get("username", "").strip()
            password = payload.get("password", "")
            user = state["users"].get(username)

            if not user or not user.get("enabled", True):
                self.send_json(
                    {"error": "Invalid credentials."}, status=HTTPStatus.UNAUTHORIZED
                )
                return

            if not verify_password(password, user):
                self.send_json(
                    {"error": "Invalid credentials."}, status=HTTPStatus.UNAUTHORIZED
                )
                return

            session_id = create_session(username)
            cookie = SimpleCookie()
            cookie[SESSION_COOKIE] = session_id
            cookie[SESSION_COOKIE]["path"] = "/"
            cookie[SESSION_COOKIE]["httponly"] = True

            self.send_response(HTTPStatus.OK)
            for morsel in cookie.values():
                self.send_header("Set-Cookie", morsel.OutputString())

            user["last_login"] = now_et().isoformat()
            save_state()

            body = json.dumps({"ok": True, "user": user_summary(user)}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/api/logout":
            destroy_session(self)
            cookie = SimpleCookie()
            cookie[SESSION_COOKIE] = ""
            cookie[SESSION_COOKIE]["path"] = "/"
            cookie[SESSION_COOKIE]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"

            self.send_response(HTTPStatus.OK)
            for morsel in cookie.values():
                self.send_header("Set-Cookie", morsel.OutputString())

            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not current_user:
            self.send_json(
                {"error": "Authentication required."}, status=HTTPStatus.UNAUTHORIZED
            )
            return

        # User operations
        if route == "/api/bid":
            response = place_bid(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/bid/bulk":
            response = place_bulk_bids(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/bid/undo":
            response = undo_bid(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/dismiss-outbid":
            # Dismiss outbid notifications for a specific day
            day_key = payload.get("day_key")
            if not day_key:
                self.send_json({"error": "day_key required"}, status=HTTPStatus.BAD_REQUEST)
                return

            username = current_user["username"]
            user = state["users"].get(username)
            if not user:
                self.send_json({"error": "User not found"}, status=HTTPStatus.NOT_FOUND)
                return

            # Remove all notifications for this day from the queue
            if "outbid_notification_queue" not in user:
                user["outbid_notification_queue"] = []

            original_len = len(user["outbid_notification_queue"])
            user["outbid_notification_queue"] = [
                slot_id for slot_id in user["outbid_notification_queue"]
                if not slot_id.startswith(f"{day_key}|")
            ]
            removed_count = original_len - len(user["outbid_notification_queue"])

            if removed_count > 0:
                save_state()
                print(f"REMOVED FROM QUEUE: {removed_count} slots for {username} on day {day_key}")

            self.send_json({"ok": True, "message": f"Dismissed {removed_count} notifications for {day_key}"})
            return

        if route == "/api/slot/release":
            response = release_slot(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/slot/release-bulk":
            response = release_slots_bulk(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/users/change-password":
            response = change_password(current_user, payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        # Admin operations
        if current_user["role"] != "admin":
            self.send_json(
                {"error": "Admin privileges required."}, status=HTTPStatus.FORBIDDEN
            )
            return

        if route == "/api/admin/users/update":
            response = update_user(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/users/bulk-update":
            response = bulk_update_users(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/users/create":
            response = create_user(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/users/password":
            response = reset_user_password(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/weeks/cleanup":
            response = cleanup_old_weeks(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/policy":
            response = update_policy(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/transition-hour":
            # Set day transition hour
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            hour = payload.get("transition_hour")
            if hour is None:
                self.send_json({"error": "transition_hour required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                set_day_transition_hour(int(hour))
                self.send_json({
                    "ok": True,
                    "transition_hour": get_day_transition_hour(),
                    "message": f"Day transition hour set to {hour}:00. Days now start at this hour."
                })
            except (TypeError, ValueError) as e:
                self.send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

        if route == "/api/admin/reset-all-days":
            # Wipe all day data and reinitialize fresh
            if not current_user or current_user["role"] != "admin":
                self.send_json(
                    {"error": "Admin privileges required."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            try:
                with state_lock:
                    state["days"] = {}
                    save_state()
                initialize_days()
                self.send_json({
                    "ok": True,
                    "message": "All day data wiped and reinitialized with current day + 6 future days"
                })
            except Exception as e:
                self.send_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if route == "/api/admin/clear-demo-data":
            response = clear_demo_data()
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/populate-demo-data":
            response = populate_demo_data()
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/set-week-status":
            response = set_week_status(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        if route == "/api/admin/clear-week-bids":
            response = clear_week_bids(payload)
            status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(response, status=status)
            return

        self.send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def send_json(
        self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        """Send JSON response."""
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_cors_headers(self) -> None:
        """Send CORS headers."""
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Credentials", "true")


# ==============================================================================
# MAIN
# ==============================================================================

def run_server(port: int = 5000, host: str = "127.0.0.1") -> None:
    """Start HTTP server."""
    load_state()

    # Check for force reset flag
    force_reset = os.environ.get("FORCE_RESET", "").lower() in ("1", "true", "yes")

    if force_reset:
        print("FORCE_RESET detected - wiping all day data and reinitializing...")
        state["days"] = {}
        save_state()

    # Only initialize on first run or after reset
    days_dict = state.get("days", {})
    if len(days_dict) == 0:
        print("First run detected - initializing days...")
        initialize_days()
    else:
        print(f"Loaded existing state with {len(days_dict)} days")

    update_system_state()

    server = HTTPServer((host, port), SchedulerHandler)
    print(f"GPU Scheduler running on http://{host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0"
    run_server(port=port, host=host)

