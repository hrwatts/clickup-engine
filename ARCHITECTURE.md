# Architecture

This document describes the system architecture, components, and data flow.

## System Overview

The Execution Engine Scheduler is a lightweight orchestration layer between ClickUp and your work blocks. It does not replace ClickUp—it extends it.

```
┌─────────────────────────────────────────────────────────────┐
│                    ClickUp (Source of Truth)               │
│  - Task list, status, custom fields, webhooks              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     │ API calls / webhooks
                     ▼
┌─────────────────────────────────────────────────────────────┐
│         Execution Engine Scheduler (This App)              │
│  - Task scoring & selection                                │
│  - Invariant enforcement (one-current rule)               │
│  - Mobile check-in page                                    │
│  - Pulse logging                                           │
│  - Action handling                                         │
└────────────────────┬────────────────────────────────────────┘
                     │
                     │ HTTP responses
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              User (Web & Mobile)                           │
│  - Check-in page                                           │
│  - Action buttons                                          │
│  - Pulse questions                                         │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. `app/main.py` - Main Application

The FastAPI application that handles:
- Web routes (login, check-in, operations, reports)
- ClickUp webhook reception
- Task orchestration
- Session management

**Key functions:**
- `get_current_task()` - Fetch current task from ClickUp
- `build_pipeline_drift_payload()` - Detect tasks that drifted between lists
- `handle_action()` - Process user button presses

### 2. `app/clickup.py` - ClickUp API Client

Wrapper around the ClickUp REST API with methods for:
- Reading tasks and custom fields
- Updating task status and custom fields
- Managing task relationships
- Webhook verification

**Key methods:**
- `get_task(task_id)` - Fetch single task details
- `list_tasks(list_id)` - Fetch all tasks in a list
- `update_custom_field()` - Write pulse data back to ClickUp
- `update_task_status()` - Change task status

### 3. `app/conformance.py` - Task Scoring & Rules

Defines the scheduler logic for:
- Task eligibility (which tasks can be current?)
- Task scoring (which task is best next?)
- Scheduler state transitions (queued → current → done)

**Key concepts:**
- `TOPOLOGY_DECISION` - Defines which custom fields and statuses to use
- Scoring weights for task prioritization
- Break and block count rules

### 4. `app/scheduler.py` - State Machine

Manages the scheduler state transitions and rules:
- One-current-task invariant enforcement
- Break handling
- Task rotation logic
- Cooldown tracking

### 5. `app/operational_state.py` - Session & State

Manages per-user session state:
- Current task tracking
- Break state
- Pulse history
- Action responses

### 6. `app/store.py` - Data Persistence

Simple file-based storage for:
- User sessions (JWT)
- Operational state
- Scheduler snapshots

**Note:** For production multi-user scenarios, this should be replaced with a database.

### 7. `app/config.py` - Configuration

Loads and validates environment variables:
- ClickUp credentials
- Server settings
- Notification settings
- Custom field mappings

## Data Flow

### Typical User Interaction

```
1. User taps "Execution Engine" shortcut
   ↓
2. Browser navigates to /active/checkin
   ↓
3. App checks session (redirects to /login if needed)
   ↓
4. App calls get_current_task() from ClickUp API
   ↓
5. App renders check-in page with:
   - Current task name/description
   - Three pulse dropdowns (Progress, Energy, Friction)
   - Action buttons (Continue, Switch, Break, etc.)
   ↓
6. User selects pulse values and taps action
   ↓
7. App calls handle_action():
   - Writes pulse values to custom fields in ClickUp
   - Updates scheduler state
   - Selects next best task (if switching)
   - Updates task statuses
   ↓
8. App redirects to /active/checkin
   (cycles back to step 4 with new current task)
```

### Webhook Flow (Optional)

When ClickUp tasks change externally:

```
1. User changes task status in ClickUp
   ↓
2. ClickUp sends webhook to app
   ↓
3. App verifies webhook signature
   ↓
4. App calls conformance rules to check invariants
   ↓
5. If rule violated (e.g., two tasks marked "In progress"):
   - App demotes the extra task
   - Notifies user (optional, via Telegram)
   ↓
6. App writes corrected state back to ClickUp
```

## Deployment Architecture

### Local Development

```
Your Machine
├── Python 3.12+ + FastAPI
├── .env with credentials
└── Browser at http://localhost:8000
```

### Production (Railway/Render)

```
┌─────────────────────────────────────────┐
│         Cloud Platform                  │
│  (Railway, Render, or Fly.io)          │
├─────────────────────────────────────────┤
│  ├─ Container (Python + FastAPI)       │
│  ├─ Environment Variables               │
│  └─ Public HTTPS URL                   │
└─────────────────────────────────────────┘
        │
        ├─ Outbound: ClickUp API
        ├─ Inbound: ClickUp Webhooks
        ├─ Inbound: User browser
        └─ Outbound: Telegram (optional)
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for hosting details.

## Key Algorithms

### Task Scoring

Tasks are scored using a combination of factors:

```python
score = (
    priority_weight * normalized_priority +
    urgency_weight * normalized_urgency +
    rotation_weight * rotation_score +
    freshness_weight * time_since_worked +
    ...other factors...
)
```

The task with the highest score becomes the next current task.

See `app/conformance.py` for exact implementation.

### One-Current-Task Invariant

The system enforces:

1. **At most one task** has `Status = In progress` AND `Scheduler State = current`
2. **All other active tasks** have `Status = To do` AND `Scheduler State = queued`

This is checked:
- After user actions (Continue, Switch, Complete)
- When webhooks arrive from ClickUp
- Periodically via remediation endpoints (for drift detection)

## Custom Fields

The system uses these optional custom fields to track state:

| Field | Type | Values | Purpose |
|-------|------|--------|---------|
| `Scheduler State` | Select | queued, current, break, blocked, done today | Tracks task position in work cycle |
| `Progress Pulse` | Select | red, yellow, green | User's progress on task |
| `Energy Pulse` | Select | red, yellow, green | User's cognitive state |
| `Friction Pulse` | Select | red, yellow, green | Blockers/friction level |
| `Today Minutes` | Number | 0+ | Minutes worked on task today |
| `Block Count` | Number | 0+ | Work blocks completed today |
| `Last Worked At` | Text | ISO timestamp | When task was last touched |

**Note:** The system works with minimal custom fields. If your ClickUp plan doesn't support many custom fields, you can still use the app with just status-based tracking.

## Database & Persistence

Currently, the app uses file-based storage in `.venv/.local/` for:
- Session tokens (JWT)
- Operational state snapshots
- Temporary data

**For production**, consider:
- PostgreSQL + SQLAlchemy
- MongoDB + motor
- Redis for session cache

See `app/store.py` to customize storage.

## Security

### Authentication

- Simple username/password login (not production-grade)
- JWT session tokens stored in cookies
- Webhook signature verification (HMAC-SHA256)

**For production**, consider:
- OAuth2 / OpenID Connect
- LDAP integration
- Multi-factor authentication

### ClickUp API Security

- Tokens stored in environment variables (never in code)
- All API calls use HTTPS
- Webhook signatures verified before processing

### Session Security

- Tokens expire after 24 hours
- Secure cookies (HttpOnly, SameSite)
- CSRF protection on state-changing endpoints

## Extension Points

### Customizing Task Scoring

Edit `app/conformance.py` to change how tasks are ranked:

```python
def score_task(task: Task) -> float:
    # Add your own scoring logic here
    score = task.priority * 10 + task.urgency * 5
    return score
```

### Adding Notifications

Extend `app/notifications.py` to add channels:
- Slack
- Discord
- Email
- PagerDuty

### Customizing the Mobile UI

Edit the templates in `app/main.py` to style the check-in page differently.

## Performance Considerations

- **ClickUp API calls are cached** in memory for 30 seconds
- **Task lists are paginated** (50 items per page by default)
- **Webhooks are processed asynchronously** where possible
- **Session tokens use JWT** (no database lookup needed)

For typical usage (1 person, 5-50 tasks), the system performs well on free hosting tiers.

## Troubleshooting

### Slow Response Times

- Check ClickUp API rate limits (50 req/sec limit)
- Reduce custom field count if possible
- Add database caching for production

### Invariant Violations

If the system detects the one-current-task rule is broken:
1. Visit `/ops/remediate/runtime-current` to auto-repair
2. Or manually fix statuses in ClickUp
3. Check webhook processing logs

### Webhook Failures

- Verify `CLICKUP_WEBHOOK_SECRET` matches ClickUp
- Check app logs for signature verification failures
- Manually sync by visiting `/reports/diagnostics`

