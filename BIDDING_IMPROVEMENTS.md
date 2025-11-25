# Bidding System Improvements

## Overview
The bidding system has been enhanced with fine-grained locking and optimized user experience for both single and bulk bids.

## Key Changes

### 1. Per-Slot Locking (Backend)
**Previous:** Global `state_lock` for all operations - caused unnecessary blocking
**New:** Per GPU/day/hour slot locks + global state lock

#### Implementation:
- Each slot has its own lock identified by `{week_key}|{slot_key}|{gpu_index}`
- Lock acquisition is sorted to prevent deadlocks
- Users can bid on different slots simultaneously without blocking each other
- Only bids on the same exact slot will block each other

#### Benefits:
- **Parallelism:** Multiple users can bid on different slots simultaneously
- **Speed:** Single bids are fast - only lock the specific slot being bid on
- **Deadlock Prevention:** Sorted lock acquisition prevents circular waiting

### 2. Single Bid Behavior
**Previous:** Had delays and potential prompts
**New:** Instant execution if affordable, no prompts

#### User Experience:
- Click a cell → bid placed immediately (if affordable)
- No confirmation dialog
- Fast UI feedback
- Only locks that specific GPU/hour slot

### 3. Bulk Bid Behavior
**Previous:** Sequential execution with potential partial failures
**New:** Atomic all-or-nothing execution

#### User Experience:
- Select multiple cells by dragging
- **If 1 cell selected:** Executes immediately as single bid (no prompt)
- **If 2+ cells selected:** Shows confirmation prompt
- Atomic operation: ALL bids succeed or ALL bids fail
- No partial states

#### Implementation:
- New `/api/bid/bulk` endpoint
- Acquires all necessary locks in sorted order
- Validates all bids before executing any
- Executes all bids within a single transaction
- Releases all locks at the end

### 4. Lock Timing
Both single and bulk bids follow the same pattern:
1. Acquire slot lock(s) - in sorted order for bulk
2. Briefly acquire global state lock to read/write
3. Execute bid(s)
4. Release global state lock
5. Release slot lock(s)

**Result:** Lock hold time is minimized - just the time to validate and execute, not including network latency.

## Technical Details

### Backend Changes (`app.py`)

#### New Functions:
```python
def get_slot_lock_key(week_key: str, slot_key: str, gpu_index: int) -> str
def get_slot_lock(lock_key: str) -> threading.Lock
def place_bulk_bids(user: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]
```

#### Modified Functions:
- `place_bid()`: Now uses per-slot locking
- Added new `/api/bid/bulk` route

### Frontend Changes (`app.js`)

#### Modified Functions:
- `onBulkSelectEnd()`: Differentiates between single (no prompt) and bulk (with prompt)
- `executeBulkBids()`: Now calls the atomic bulk endpoint
- Added `executeSingleBid()`: Fast path for single cell selection

## Performance Characteristics

### Single Bid
- **Lock Scope:** One slot only
- **Lock Duration:** ~1-5ms (just the bid execution time)
- **Network Latency:** User perceives this, but doesn't hold lock during it
- **Parallelism:** High - different slots can be bid on simultaneously

### Bulk Bid
- **Lock Scope:** N slots (where N = number of cells selected)
- **Lock Duration:** ~N×2ms (validation + execution for all bids)
- **Atomicity:** Guaranteed - all succeed or all fail
- **Parallelism:** Other users can still bid on non-overlapping slots

## Example Scenarios

### Scenario 1: Two users bidding on different slots
- User A bids on GPU 0, Hour 10
- User B bids on GPU 1, Hour 10
- **Result:** Both succeed simultaneously (no blocking)

### Scenario 2: Two users bidding on same slot
- User A bids on GPU 0, Hour 10
- User B bids on GPU 0, Hour 10
- **Result:** One gets the lock first and wins, the other sees the new price

### Scenario 3: Bulk bid with 10 slots
- User A selects 10 cells and confirms
- Backend acquires all 10 locks, validates all, executes all
- **Result:** Either all 10 bids succeed or none do (atomic)

### Scenario 4: Concurrent bulk bids
- User A bulk bids on GPUs 0-3, Hour 10
- User B bulk bids on GPUs 4-7, Hour 10
- **Result:** Both succeed simultaneously (no overlapping locks)

## Error Handling

### Single Bid Errors:
- Insufficient credits → Alert shown, bid not placed
- Slot already taken → Alert shown with error message
- Network error → Alert shown, can retry

### Bulk Bid Errors:
- Insufficient credits for all → Alert shown, NO bids placed
- Any slot validation fails → Alert shown, NO bids placed
- Any bid execution fails → Alert shown, NO bids placed (rollback)
- All validations pass → ALL bids executed atomically

## Migration Notes

- No database migration required (state.json handles this automatically)
- Existing single bids work the same way (just faster)
- Bulk selection feature enhanced to use atomic endpoint
- Backward compatible with existing state files
