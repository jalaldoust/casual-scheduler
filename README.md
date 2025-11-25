# CausalAI HPC Scheduler

A web-based resource allocation system for managing shared high-performance computing infrastructure. Designed for research teams that need fair and transparent access to GPU resources.

Learn more at [casualai.net](https://casualai.net)

## Overview

The CausalAI HPC Scheduler implements a market-based approach to computational resource allocation. Rather than traditional first-come-first-served or manual assignment methods, this system allows researchers to express the relative value of their computational needs through a credit-based bidding mechanism.

## How It Works

### Credit-Based Bidding System

Each user receives a periodic budget of credits that can be allocated across available GPU time slots. Users bid credits on specific hour-long slots for individual GPUs, with higher bids winning access to the resource. This creates a self-regulating system where users naturally prioritize their most important workloads.

### Resource Allocation Process

1. **Planning Phase**: Users view available time slots in a day-based schedule and place bids using their credit budget. The system supports bulk bidding for efficient allocation of extended computational runs.

2. **Auction Resolution**: At the transition time, the system resolves all bids. For each GPU hour slot, the highest bidder wins access. Credits are only deducted for winning bids.

3. **Execution Phase**: During execution, users can see their allocated resources and monitor real-time GPU utilization. Administrators receive notifications when jobs are not utilizing their allocated resources efficiently.

4. **Credit Management**: Unused credits roll over between periods (at a configurable rate), incentivizing strategic resource planning rather than use-it-or-lose-it behavior.

### Fair Access Mechanisms

- **Outbid Notifications**: Users are alerted when outbid on their planned slots, allowing them to adjust their strategy during the planning phase.
- **Budget Constraints**: Periodic credit allocation prevents any single user from monopolizing resources long-term.
- **Price Discovery**: The bidding system reveals the actual demand for resources, helping both users and administrators make informed decisions.
- **Historical Tracking**: Complete bid and allocation history enables analysis of resource usage patterns and needs.

## Features

### For Users
- Interactive day-based scheduling interface with 24-hour visibility
- Real-time bid status updates and outbid notifications
- Bulk bidding for multi-hour GPU reservations
- Personal credit balance and allocation tracking
- Historical view of past bids and resource usage
- Undo functionality for quick bid corrections

### For Administrators
- User and credit management interface
- Real-time GPU monitoring with utilization alerts
- System-wide resource allocation overview
- Configurable budget parameters and rollover rates
- Audit trail of all bidding activity

## System Architecture

The scheduler is built as a lightweight, self-contained web application:

- **Backend**: Python 3.11+ with no external dependencies beyond the standard library
- **Frontend**: Vanilla JavaScript with responsive CSS
- **Storage**: JSON-based persistence for simplicity and transparency
- **Authentication**: Session-based with bcrypt password hashing

This architecture prioritizes reliability, maintainability, and ease of deployment in research computing environments.

## Deployment

The application can be deployed on any platform supporting Python web services:

```bash
python app.py
```

The server runs on port 5000 by default and serves both the web interface and REST API.

## Configuration

Key system parameters are configurable through the admin interface or directly in the state file:

- Credit budget per period
- Credit rollover percentage
- Day transition time
- Number of available GPUs
- User accounts and roles

## About

This system is developed and maintained by the CausalAI research group to support fair allocation of computational resources across research projects. For more information about our research and infrastructure, visit [casualai.net](https://casualai.net).
