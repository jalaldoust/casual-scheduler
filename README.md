# CausalAI HPC Scheduler

A simple web app for scheduling shared GPU resources using a credit-based bidding system.

Visit [casualai.net](https://casualai.net)

## What is this?

When multiple people share GPUs, someone needs to decide who gets what and when. This system lets users bid credits for GPU time slots. Highest bid wins. Simple.

## How it works

1. Each user gets a budget of credits
2. Users bid on specific GPU hours they need
3. At transition time, highest bids win
4. Winners get their GPU time, losers keep their credits
5. Unused credits roll over to next period

## Why bidding?

- Users prioritize what matters most to them
- No manual coordination needed
- Fair: everyone gets the same budget
- Transparent: you see what others are bidding

## Features

- Day-based schedule with 24-hour slots
- Bulk bidding for long jobs
- Real-time notifications when outbid
- Historical view of past allocations
- Admin tools for managing users and credits

## Tech

- Python 3.11+ (no dependencies)
- Vanilla JavaScript
- JSON storage
- Runs on port 5000

## Deploy

```bash
python app.py
```

That's it.

## Configuration

Edit via admin panel or directly in `data/state.json`:
- Credit budgets
- Rollover rates
- Transition times
- Number of GPUs

---

Developed by CausalAI research group.
