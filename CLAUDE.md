# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A production-grade GPU scheduling system with credit-based bidding for shared GPU resources. Built with zero external dependencies using only Python 3.11+ standard library and vanilla JavaScript.

**Core concept:** Users bid credits to reserve GPU time slots in a weekly auction system. Weeks operate on Saturday-Friday cycles with automatic advancement at Friday midnight.

## Running & Development

**Local development:**
```bash
python app.py
# Server runs on http://localhost:5000 (or PORT env var on port 8000)
```

**No build step required** - Pure Python backend with vanilla JS frontend.

**Default credentials:** All users have password = username. Admin user is `eb` / `eb`.

**Data storage:** All state persists to `data/state.json` (JSON file). No database.

## Architecture

### Backend (app.py)

Single-file Python HTTP server (~2500 lines) handling all backend logic:

**State Management:**
- Global `state` dict loaded from/saved to `data/state.json`
- Thread-safe with `state_lock` (RLock) for all mutations
- GPU-specific slot locks prevent race conditions during bidding
- In-memory session store (12-hour TTL)

**Week Lifecycle:**
- Three week statuses: "executing" (current), "open" (bidding), "final" (archived)
- Auto-advance at Friday 23:59:59 ET via `maybe_auto_advance()` called on every request
- Week advancement: deducts credits, applies 50% rollover, refills budgets, promotes weeks
- Weeks run Saturday-Friday on Eastern Time (America/New_York)

**Credit System:**
- Each user gets weekly credit budget (default 100)
- Bids increment price by 1 credit per bid
- Credits committed on week finalization (not on bid)
- 50% of unused credits roll over to next week
- Release slots early for 0.34 credit refund (RELEASE_REFUND_CREDITS constant)

**GPU Monitoring:**
- Bearer token authenticated endpoint `/api/gpu-status` for external daemon
- Tracks actual GPU usage vs assigned users
- Live usage: `live_gpu_usage` dict updated in real-time for current hour
- Historical: `gpu_usage_tracking` dict stores samples per slot for later finalization
- Server time is authoritative (daemon timestamp only for validation)

**Concurrency:**
- Per-slot locks via `get_slot_lock()` prevent concurrent bid conflicts
- Bulk operations acquire multiple locks in sorted order (deadlock prevention)
- All state mutations protected by `state_lock`

### Frontend (static/)

**Files:**
- `index.html` - Minimal shell (356 bytes)
- `app.js` - All frontend logic (~1800 lines, vanilla JS)
- `styles.css` - All styling

**Key features:**
- Live countdown timer to week close
- Bulk bid/release via drag-select
- Outbid notifications with localStorage persistence
- Real-time GPU usage display with live polling
- Week view caching to reduce API calls
- Admin panel for user/week management

**State management:** Single global `state` object with nested structure. No framework.

**API pattern:** All endpoints prefixed `/api/`. Session cookie for auth (except GPU monitoring uses Bearer token).

### Data Model

**State structure:**
```json
{
  "users": {
    "username": {
      "password_hash": "...",
      "salt": "...",
      "role": "user|admin",
      "weekly_budget": 100,
      "balance": 100.0,
      "rollover_applied": 0
    }
  },
  "weeks": {
    "2025-11-15": {
      "status": "executing|open|final",
      "finalized_at": "ISO timestamp",
      "slots": {
        "2025-11-15T14:00": {
          "gpu_prices": [
            {
              "gpu": 0,
              "price": 3,
              "winner": "username",
              "actual_user": "username",
              "bids": [...]
            }
          ]
        }
      }
    }
  },
  "gpu_usage_tracking": {
    "week_key": {
      "slot_key": {
        "gpu_index": {
          "username": sample_count
        }
      }
    }
  }
}
```

**Week keys:** Saturday date in format `YYYY-MM-DD` (e.g., "2025-11-15")

**Slot keys:** ISO format `YYYY-MM-DDTHH:MM` (e.g., "2025-11-15T14:00")

## API Endpoints

**Authentication:**
- `POST /api/login` - Create session
- `POST /api/logout` - Destroy session
- `GET /api/session` - Check auth status

**Bidding:**
- `POST /api/bid` - Place single bid (auto-increments price)
- `POST /api/bid/bulk` - Atomic multi-bid (all succeed or all fail)
- `POST /api/bid/undo` - Undo last bid if you owned or slot was empty

**Releasing:**
- `POST /api/slot/release` - Release single future slot for 0.34 credit refund
- `POST /api/slot/release-bulk` - Release multiple slots

**Views:**
- `GET /api/overview` - Week list, user summary, policy
- `GET /api/week?week=YYYY-MM-DD&day=YYYY-MM-DD` - Day grid view
- `GET /api/my/summary` - User's won slots
- `GET /api/my/bids` - User's bid history

**GPU Monitoring:**
- `POST /api/gpu-status` - Submit usage data (Bearer token auth via GPU_MONITOR_TOKEN env var)
- `GET /api/gpu-live-status` - Public endpoint for current GPU usage

**Admin:**
- `GET /api/admin/users` - List all users
- `GET /api/admin/weeks` - List all weeks
- `POST /api/admin/users/create` - Create user
- `POST /api/admin/users/update` - Update user (balance, budget, enabled)
- `POST /api/admin/users/bulk-update` - Apply delta to all users
- `POST /api/admin/weeks/advance` - Manually advance week
- `POST /api/admin/weeks/cleanup` - Delete old weeks
- `GET /api/admin/export?week=YYYY-MM-DD` - CSV export of schedule
- `GET /api/admin/export-usage?week=YYYY-MM-DD` - CSV export with actual usage tracking
- `GET /api/admin/export-all` - Full JSON backup

## Utility Scripts

**State management:**
- `reset_state.py` - Wipe state and create fresh demo data
- `populate_bids.py` - Add sample bids to current week
- `populate_current_week.py` - Populate realistic demo data in executing week
- `force_populate_current_week.py` - Force populate even if data exists

**Deployment:**
- `fix_railway.py` - Railway-specific state fixes
- `deploy-to-server.sh` - Generic deployment script
- `test_api.sh` - API integration tests

## Configuration Constants

**In app.py:**
- `NUM_GPUS = 8` - Number of GPUs to schedule
- `HOURS_PER_DAY = 24` - Hours per day (full day coverage)
- `TZ = ZoneInfo("America/New_York")` - Eastern Time
- `SESSION_TTL_SECONDS = 12 * 60 * 60` - 12-hour sessions
- `RELEASE_REFUND_CREDITS = 0.34` - Refund for releasing slots
- `ROLLOVER_PERCENTAGE = 0.5` - 50% credit rollover

**Week status constants:**
- `CURRENT_WEEK_STATUS = "executing"` - Currently running week
- `NEXT_WEEK_STATUS = "open"` - Open for bidding
- `FINAL_WEEK_STATUS = "final"` - Archived/completed

## Deployment

**Railway.app (recommended):** Uses `railway.json` config. Set `PORT` env var.

**Render.com (free tier):** Uses `Procfile` (web: python app.py).

**Environment variables:**
- `PORT` - Server port (default: 8000)
- `GPU_MONITOR_TOKEN` - Bearer token for GPU monitoring endpoint (optional)

**Requirements:** Python 3.11+ only. No pip dependencies.

## Important Implementation Notes

**Bidding logic:**
- All bids increment current price by 1 (no custom bid amounts)
- Bidding checks available balance MINUS already-committed credits
- Bulk bids are atomic: validate all, then execute all (or fail all)
- Slot locks acquired in sorted order to prevent deadlocks

**Week advancement:**
- Triggered automatically by `maybe_auto_advance()` on every API request
- Can catch up multiple weeks if server was down
- Handles partial weeks safely (max 10 iterations)
- Credits deducted at week transition, not on bid placement

**Time handling:**
- Server time (`now_et()`) is always authoritative
- All datetime operations use Eastern Time (ZoneInfo)
- Week boundaries: Saturday 00:00:00 to Friday 23:59:59
- Slot boundaries: Hour start to hour end (e.g., 14:00:00 to 14:59:59)

**GPU tracking:**
- Monitoring daemon POSTs to `/api/gpu-status` with Bearer auth
- Records samples throughout the hour: `{gpu_index: [usernames]}`
- At hour end, `finalize_past_gpu_slots()` determines `actual_user` (most frequent)
- Tracking data auto-cleaned (keeps current + previous week only)

**Security:**
- PBKDF2-SHA256 password hashing (150k iterations)
- Constant-time password comparison
- Session-based auth with secure random tokens
- Path traversal protection on static file serving

**Concurrency safety:**
- All state mutations use `state_lock`
- Per-GPU-slot locks for bidding operations
- Atomic file writes via temp file + replace
- Thread-safe session cleanup

## Common Tasks

**Reset to fresh state:**
```bash
python reset_state.py
```

**Add demo bids:**
```bash
python populate_bids.py
```

**Manually advance week (as admin via API):**
```bash
curl -X POST http://localhost:5000/api/admin/weeks/advance \
  -H "Cookie: gpu_sched_session=YOUR_SESSION" \
  -H "Content-Type: application/json"
```

**Export week schedule:**
Visit `/api/admin/export?week=YYYY-MM-DD` (must be admin)

**Change configuration:**
Edit constants at top of `app.py` (lines 23-37)

## Testing

**Manual testing:**
```bash
./test_api.sh  # Requires running server
```

**Testing GPU monitoring:**
```bash
export GPU_MONITOR_TOKEN="your-secret-token"
python app.py &

curl -X POST http://localhost:8000/api/gpu-status \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"timestamp": "2025-11-24T14:30:00-05:00", "usage": {"0": ["user1"], "1": ["user2", "user3"]}}'
```

## Code Style Notes

- Type hints used throughout (Python 3.11+ style)
- HTTP status codes via `http.HTTPStatus` enum
- JSON serialization for all API responses
- Thread locks always acquired/released via context managers
- Docstrings on all major functions
- Constants in UPPER_CASE
- No external dependencies (standard library only)
