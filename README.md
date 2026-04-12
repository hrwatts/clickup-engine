# ClickUp Execution Engine Scheduler

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-hrwatts%2Fclickup--engine-blue?logo=github)](https://github.com/hrwatts/clickup-engine)

**A lightweight orchestration layer that bridges ClickUp and your daily work rhythm.**

This app adds the missing automation between task management and actually getting work done. It enforces the one-current-task rule, selects your next best task, handles breaks intelligently, provides a fast mobile check-in surface, and generates work hygiene reports—all while keeping ClickUp as your single source of truth.

Not meant to replace ClickUp. Meant to fill the gaps ClickUp can't automate well.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/hrwatts/clickup-engine.git
cd clickup-engine

# 2. Setup
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your ClickUp API token and space/folder names

# 4. Run
python app/main.py
# Visit http://localhost:8000/login
```

**Full setup guide**: [GETTING_STARTED.md](GETTING_STARTED.md)

## Documentation

| Guide | Purpose |
|-------|---------|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Step-by-step setup and first run |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Host on Railway, Render, Fly.io, or self-hosted |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system works internally |
| [EXECUTION_ENGINE_GUIDE.md](EXECUTION_ENGINE_GUIDE.md) | Operating philosophy and daily rhythm |
| [IPHONE_SHORTCUT_SETUP.md](IPHONE_SHORTCUT_SETUP.md) | Mobile workflow on iPhone |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development and contributing guidelines |

## What It Does

**The Problem**: ClickUp is great for storing tasks, but it doesn't help you pick the right task *right now* or enforce work discipline. You end up with notification fatigue, task pile anxiety, or spending 10 minutes choosing what to do.

**The Solution**: This app watches your Execution Engine list and:

1. **Enforces the one-current-task rule**: Exactly one task is marked `In Progress`. Everything else is in `To Do`.
2. **Selects your next task automatically**: Uses scoring rules to pick the best next task based on priority, urgency, what you've worked on recently, and your energy.
3. **Handles breaks intelligently**: Tracks when you take breaks and prevents context-switching fatigue.
4. **Provides a fast mobile check-in**: Answer three quick questions (Progress, Energy, Friction) with one tap.
5. **Logs actions automatically**: Your button presses (Continue, Switch, Complete, Take Break, Blocked) are written back to ClickUp.
6. **Generates reports**: See what you actually worked on, how your energy varied, and patterns in your work.

## Use ClickUp for:

- Spaces, folders, lists, and views
- Source-of-truth task storage
- Simple statuses (`To do`, `In progress`, `Done`)
- Custom fields (if your plan supports them)
- Filtering and searching
- Team collaboration

## Use this app for:

- Choosing the single best current task
- Preserving the one-current-task invariant
- Handling breaks and re-eligibility
- Fast mobile check-in without entering ClickUp
- Pulse logging (progress, energy, friction)
- Automatic state management
- Hygiene and summary reports

## Core Concepts

### The One-Current-Task Rule

At any given time:
- **Exactly one task** is marked as both `Status = In progress` AND `Scheduler State = current`
- **All other active tasks** are `Status = To do` AND `Scheduler State = queued`

This prevents the "which task am I on?" confusion and creates a clear work frontier.

### The Pulse Log

Every check-in, you answer three questions in 10 seconds:

| Question | Purpose |
|----------|---------|
| **Progress** | How far into this task? (red=stuck, yellow=moving, green=flowing) |
| **Energy** | How's your brain? (red=depleted, yellow=okay, green=sharp) |
| **Friction** | What's blocking you? (red=stuck, yellow=minor, green=smooth) |

This data feeds back into your next task selection and helps identify patterns.

### The Action Buttons

After checking in, you choose one:

- **Continue Slice** - Keep this task, log another 20-minute block
- **Switch Task** - Mark this queued, promote the next best task
- **Complete Task** - Mark done, promote next task
- **Take Break** - No task is current until you return
- **Blocked** - Mark task blocked, skip to next available

## System Architecture

The app is built in layers:

```
┌──────────────────────────┐
│   Your Work (Phone/Web)  │
└────────────┬─────────────┘
             │
┌────────────▼──────────────────────────────────┐
│  Execution Engine Scheduler (FastAPI app)    │
│  - Task scoring & selection                  │
│  - One-current rule enforcement              │
│  - Mobile check-in UX                        │
│  - Webhook processing                        │
└────────────┬──────────────────────────────────┘
             │
┌────────────▼──────────────────────────────────┐
│  ClickUp (Source of Truth)                   │
│  - Tasks, status, custom fields              │
│  - Webhooks for real-time sync               │
└───────────────────────────────────────────────┘
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed system design.

## Requirements

- **ClickUp**: Paid plan supporting Custom Fields (Pro or higher recommended)
- **ClickUp API Token**: [Generate in your settings](https://app.clickup.com/settings/apps)
- **Python 3.12+**: [Download here](https://www.python.org/downloads/)
- **Hosting**: Railway, Render, Fly.io, or your own server

Optional but recommended:
- **Telegram**: For push notifications
- **iPhone or Android**: For mobile check-ins (works in any modern browser)

## Getting Started

### 1. Quick Local Test

See [GETTING_STARTED.md](GETTING_STARTED.md) for detailed instructions.

### 2. Deploy to Production

See [DEPLOYMENT.md](DEPLOYMENT.md) for:
- Railway (recommended, free tier available)
- Render
- Fly.io
- Self-hosted

Estimated setup time: 10-15 minutes.

### 3. Set Up Mobile Access

See [IPHONE_SHORTCUT_SETUP.md](IPHONE_SHORTCUT_SETUP.md) to add a one-tap home screen shortcut.

## Why This Architecture

### Why not just use ClickUp native features?

ClickUp can do a lot natively (custom fields, automations, buttons), but it can't:
- **Compare multiple tasks and pick the best one** based on complex rules
- **Enforce invariants** (only one current task) without manual checking
- **Send interactive push notifications** with action buttons
- **Handle context-switching** with smart break management
- **Generate aggregated reports** across time periods

This external layer provides those capabilities while ClickUp remains the single source of truth.

### Why FastAPI + Python?

- **Simple to understand**: Small codebase, easy to modify
- **Fast to run**: Even on free hosting tiers
- **Good ClickUp support**: Official SDK + REST API are well-documented
- **Easy to extend**: Add custom scoring, new reports, new notification channels

## Philosophy

This is **not a second task manager**. 

It does not:
- Store tasks (ClickUp does)
- Own project structure (ClickUp does)
- Provide team collaboration (ClickUp does)
- Replace your workflow (ClickUp is your workflow)

It **only** fills the gap between having tasks and choosing the right one *right now*.

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Development setup
- Testing
- Code style
- Pull request process

## License

MIT License - See [LICENSE](LICENSE) for details.

## Support & Questions

- **Issues**: [GitHub Issues](https://github.com/hrwatts/clickup-engine/issues)
- **Discussions**: [GitHub Discussions](https://github.com/hrwatts/clickup-engine/discussions)
- **Documentation**: See guides above
- It does not own canonical task state.
- It does not create a parallel task database.
- It does not replace ClickUp views, notes, or list management.

Any durable task state should live in ClickUp.

The only runtime state kept by the app is ephemeral harmonic-block session data such as current block minutes. That state is safe to lose on restart.

## Field discovery and mapping

The app does not rely on hardcoded ClickUp field IDs.

Instead it:

- fetches the list's accessible custom fields at runtime
- maps fields by semantic name
- writes values only when the expected field is present
- surfaces missing field names in `/reports/hygiene`

If field discovery is ambiguous or the execution list cannot be resolved safely, the app fails closed with a clear configuration error instead of guessing.

If your ClickUp field labels differ, you can override semantic field names with env vars such as:

- `FIELD_SCHEDULER_STATE_NAME`
- `FIELD_TASK_TYPE_NAME`
- `FIELD_PROGRESS_PULSE_NAME`
- `FIELD_ENERGY_PULSE_NAME`
- `FIELD_FRICTION_PULSE_NAME`
- `FIELD_BLOCK_COUNT_TODAY_NAME`
- `FIELD_LAST_WORKED_AT_NAME`
- `FIELD_NEXT_ELIGIBLE_AT_NAME`
- `FIELD_TODAY_MINUTES_NAME`
- `FIELD_ROTATION_SCORE_NAME`

Relevant docs:

- ClickUp webhooks: https://developer.clickup.com/docs/webhooks
- ClickUp webhook signatures: https://developer.clickup.com/docs/webhooksignature
- ClickUp custom fields: https://developer.clickup.com/docs/customfields
- ClickUp get list fields: https://developer.clickup.com/reference/getaccessiblecustomfields
- ClickUp get tasks in a list: https://developer.clickup.com/reference/gettasks
- ClickUp set custom field value: https://developer.clickup.com/reference/setcustomfieldvalue
- ClickUp update task: https://developer.clickup.com/reference/updatetask
- ClickUp button custom fields: https://help.clickup.com/hc/en-us/articles/30117356611735-Use-Button-Custom-Fields
- ClickUp custom-field automations: https://help.clickup.com/hc/en-us/articles/35446142759575-Use-Custom-Fields-in-Automations
- Telegram inline buttons: https://core.telegram.org/bots/api

## Status model

Use the lightest statuses your ClickUp setup already has.

The recommended defaults are:

- `To do`
- `In progress`
- `Complete`

with execution behavior driven by `Scheduler State`, not by a large status taxonomy.

If you want the app to map to different statuses, configure:

- `CLICKUP_OPEN_STATUS`
- `CLICKUP_CURRENT_STATUS`
- `CLICKUP_COMPLETED_STATUS`
- `CLICKUP_BLOCKED_STATUS` (optional)

The scheduler enforces:

- only one task may be `current`
- the `current` task maps to your configured current status
- all other active tasks map to your configured open status
- blocked can live in `Scheduler State` alone unless you explicitly configure a blocked status
- completed tasks map to your configured completed status

## Required custom fields

Create these fields on the `Execution Engine` list in ClickUp:

- `Scheduler State` dropdown
- `Task Type` dropdown
- `Progress Pulse` dropdown
- `Energy Pulse` dropdown
- `Friction Pulse` dropdown
- `Block Count Today` number
- `Last Worked At` date/time
- `Next Eligible At` date/time
- `Today Minutes` number
- `Rotation Score` number

Pin at least these fields:

- `Scheduler State`
- `Task Type`
- `Progress Pulse`
- `Energy Pulse`
- `Friction Pulse`
- `Last Worked At`
- `Next Eligible At`
- `Today Minutes`
- `Rotation Score`

## Suggested Button Custom Fields in ClickUp

Create these Button Custom Fields on the same list:

- `Continue 20m`
- `Complete Task`
- `Switch Task`
- `Blocked`

Use them for in-app manual recovery actions if they feel lighter than opening the mobile check-in page.

## Prerequisites

- **Python 3.12+**
- **ClickUp account** with a paid plan that supports Custom Fields (Business plan or higher recommended)
- **ClickUp API token** — generate one at [ClickUp Settings > Apps](https://app.clickup.com/settings/apps)
- A ClickUp list with the required custom fields (see below)

## Setup

1. Create the custom fields and simple statuses in ClickUp.
2. Copy `.env.example` to `.env` and fill in your values.
   - Set `APP_SHARED_SECRET` and `SESSION_SECRET`.
   - Telegram is optional. Leave `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` empty if you want a simple Safari/Home Screen workflow.
3. Install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. Run the app:

```powershell
uvicorn app.main:app --reload
```

Before deploying, run the same env file through a live predeploy simulation:

```powershell
python scripts/predeploy_check.py --env-file .env.railway
```

This checks the configured ClickUp token, workspace, list, fields, and task access, boots the FastAPI app through startup using that env file, and runs the test suite so a bad list ID or permission issue is caught locally instead of after Railway deploy.

5. Expose it with HTTPS, for example behind a reverse proxy or tunnel.
6. Register a ClickUp webhook pointing to:

- `POST {PUBLIC_BASE_URL}/clickup/webhook`

7. Optional: register your Telegram webhook if you want richer push:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=<PUBLIC_BASE_URL>/telegram/webhook
```

8. Trigger a scheduler pass (manual):

- `POST {PUBLIC_BASE_URL}/scheduler/run`

Built-in scheduler loop:

- By default, the app self-runs scheduler reconciliation during configured work hours.
- External cron is optional now and mainly useful for advanced deployment policies.
- Run one instance of the app for a given ClickUp list. The built-in loop is intended for single-instance use so current-task reconciliation stays deterministic.

Minimum viable environment variables:

- `CLICKUP_API_TOKEN` or `CLICKUP_TOKEN`
- `PUBLIC_BASE_URL`
- either `CLICKUP_LIST_ID` or (`CLICKUP_LIST_NAME` and `CLICKUP_WORKSPACE_ID`)
- `APP_SHARED_SECRET`
- `SESSION_SECRET`

Strongly recommended:

- `CLICKUP_WEBHOOK_SECRET`

Optional:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- summary and scoring-tuning variables from `.env.example`

## Managed hosting

The easiest low-ops path is a single-instance web service on a managed platform.

Recommended order:

1. Railway
2. Render
3. Fly.io

Why single-instance:

- the built-in scheduler loop should run in one place
- one instance keeps current-task reconciliation deterministic
- it avoids duplicated prompts or competing scheduler passes

### Railway

Use the included [railway.json](./railway.json).

Deploy flow:

1. Create a new Railway project from this repo.
2. Set the service start command from `railway.json` or let Railway pick it up automatically.
3. Add environment variables from `.env.example`.
4. Set `PUBLIC_BASE_URL` to your Railway service URL after first deploy.
5. Keep the service as a single web service for this ClickUp list.

### Render

Use the included [render.yaml](./render.yaml).

Deploy flow:

1. Create a new Blueprint or Web Service from this repo.
2. Use the `render.yaml` defaults or equivalent dashboard settings.
3. Add the required environment variables.
4. Set `PUBLIC_BASE_URL` to the Render service URL.
5. Run a single instance only.

### Fly.io

Fly.io is the third choice here because it is still simple but a little more operational.

Use the included [Dockerfile](./Dockerfile) as the container base, then set:

- one app
- one machine
- the same required environment variables
- `PUBLIC_BASE_URL` to your Fly app URL

For this project, prefer Railway or Render unless you already use Fly.io.

Useful endpoints:

- `GET {PUBLIC_BASE_URL}/active` current task summary
- `GET {PUBLIC_BASE_URL}/active/checkin` redirect to the live task check-in page
- `GET {PUBLIC_BASE_URL}/checkin/{task_id}` mobile pulse page
- `GET {PUBLIC_BASE_URL}/reports/daily` simple JSON daily report
- `GET {PUBLIC_BASE_URL}/reports/weekly` weekly reset prep snapshot
- `GET {PUBLIC_BASE_URL}/reports/hygiene` duplicate, queue, and Resume Pack diagnostics
- `GET {PUBLIC_BASE_URL}/reports/startup` configuration and discovery diagnostics

Endpoint privacy:

- The app uses a shared-secret login flow at `/login`.
- After login, protected endpoints are unlocked by a signed session cookie.
- Protected endpoints include:
   - `GET /active`
   - `GET /active/checkin`
   - `POST /scheduler/run`
   - `GET /checkin/{task_id}`
   - `POST /checkin/{task_id}`
   - `GET /reports/daily`
   - `GET /reports/weekly`
   - `GET /reports/hygiene`
   - `GET /reports/startup`
- ClickUp and Telegram webhooks remain independently authenticated by webhook signature and webhook secret.

## Security model

Secure defaults:

- secrets come from environment variables only
- interactive endpoints require login and a signed session cookie
- login attempts are rate limited in-process
- ClickUp webhook authenticity is verified with `CLICKUP_WEBHOOK_SECRET`
- Telegram webhook authenticity can be verified with `TELEGRAM_WEBHOOK_SECRET`
- malformed JSON and invalid user input return safe client errors
- scheduler failures return generic service errors instead of leaking task or account details
- the built-in scheduler is intended for a single running instance per ClickUp list

Threats explicitly handled:

- accidental public check-in exposure
- malformed or replayed webhook requests without a valid secret
- duplicate scheduler passes inside one process
- missing or renamed fields
- inconsistent ClickUp state such as zero or multiple current tasks
- notifier outages

## Local validation and smoke tests

Run these checks before exposing your tunnel URL publicly:

```powershell
python -m compileall app
python -m unittest discover -s tests -p "test_*.py"
```

What the smoke tests cover:

- health endpoint response
- login and session-cookie protection
- current-task check-in URL generation
- HTML escaping on task names for check-in page safety
- invalid pulse input rejection
- malformed webhook and check-in payload rejection
- friendly no-active-task response
- duplicate/hygiene diagnostics
- ClickUp failure handling
- weekly summary scheduling helper
- startup diagnostics endpoint

## Interaction flow

1. A task changes in ClickUp or the built-in scheduler loop ticks during work hours.
2. The service fetches tasks from the Execution Engine list.
3. It calculates a score for each eligible task.
4. It marks the best task as `current` and maps task status to your configured ClickUp status model.
5. If Telegram is configured, it sends a Telegram message with inline buttons.
6. The system exposes a mobile check-in URL either way.
7. When you press a button or submit the check-in page:
   - `Continue Slice`: updates time fields and harmonic block progress while keeping the task current
   - `Switch Task`: rotates away without marking the task blocked
   - `Break 10m` or `Long Break 20m`: pauses the task and delays re-eligibility
   - `Complete Task`: marks it completed and picks the next task
   - `Blocked`: marks it blocked, increments block count, delays re-eligibility, and picks the next task

## Harmonic block model

The runtime defaults align with a 40 to 80-minute block model:

- `CHECKIN_SLICE_MINUTES=20`
- `BLOCK_TARGET_MINUTES=60`
- `BLOCK_MIN_MINUTES=40`
- `BLOCK_MAX_MINUTES=80`

On each Continue action, the app tracks cumulative minutes in the current block and returns guidance when target or max is reached.

Queue hygiene defaults:

- `QUEUE_TARGET_MIN=3`
- `QUEUE_TARGET_MAX=7`
- `STALE_QUEUE_HOURS=72`

Use `/reports/hygiene` to detect duplicate tasks, queue-size drift, stale queue items, missing Resume Pack required markers, and invalid current-task counts.

Built-in scheduler and summary settings:

- `ENABLE_BUILTIN_SCHEDULER`
- `SCHEDULER_TICK_SECONDS`
- `SCHEDULER_MIN_INTERVAL_MINUTES`
- `WORKDAY_START_HOUR`
- `WORKDAY_END_HOUR`
- `WORKDAY_WEEKDAYS`
- `ENABLE_DAILY_SUMMARY`
- `DAILY_SUMMARY_HOUR`
- `ENABLE_WEEKLY_SUMMARY`
- `WEEKLY_SUMMARY_WEEKDAY`
- `WEEKLY_SUMMARY_HOUR`
- `MORNING_START_HOUR`
- `MORNING_END_HOUR`
- `MIDDAY_END_HOUR`
- `EVENING_END_HOUR`
- `MORNING_DEEP_BONUS`
- `MORNING_PAPER_BONUS`
- `MIDDAY_MEDIUM_BONUS`
- `MIDDAY_READING_BONUS`
- `EVENING_LIGHT_BONUS`
- `EVENING_ADMIN_BONUS`
- `CURRENT_MOMENTUM_BONUS`
- `MEDIUM_MOMENTUM_BONUS`
- `FATIGUE_TARGET_PENALTY`
- `FATIGUE_MAX_PENALTY`
- `SYSTEM_TASK_PENALTY`

If Telegram is configured and daily summaries are enabled, the app sends a daily summary automatically at `DAILY_SUMMARY_HOUR` on configured workdays.

If Telegram is configured and weekly summaries are enabled, the app sends a weekly summary at `WEEKLY_SUMMARY_HOUR` on `WEEKLY_SUMMARY_WEEKDAY`.

## Scoring model

The included scheduler is intentionally simple and editable:

- favors tasks that are eligible now
- penalizes blocked and completed tasks
- biases deep and paper work toward the morning
- biases reading and lighter work later in the day
- favors continuation when the current task has high progress and friction is not high
- pushes harder toward a break or lighter switch after 60 to 80 minutes
- favors lower `Today Minutes`
- boosts tasks not worked recently
- uses `Task Type`, `Energy Pulse`, and `Friction Pulse`
- writes the resulting score into `Rotation Score`

That gives you a practical starting point without burying the logic in fragile automations.

To tune your rhythm without editing code:

1. adjust the time-window boundaries
2. adjust the matching bonus values
3. increase or decrease momentum/fatigue penalties
4. restart the app

Use `/reports/startup` and `/reports/hygiene` after changes to confirm the app still maps your ClickUp setup correctly.

## Native ClickUp automations to add

Inside ClickUp, add these automations:

1. When task created in `Execution Engine`
   - set `Scheduler State = queued`
   - set `Progress Pulse = none`
   - set `Energy Pulse = medium`
   - set `Friction Pulse = none`
   - set `Block Count Today = 0`
   - set `Today Minutes = 0`

2. When status changes to your completed status
   - set `Scheduler State = done today`

3. When `Scheduler State` changes to `Blocked`
   - optionally set `Friction Pulse = High`

Avoid automations that try to decide which task should be current. That is exactly the gap this repo is meant to fill.

These are safety rails. The service should remain the only place that chooses the next current task.

## Notes

- ClickUp webhook requests should be acknowledged quickly. This app processes lightly and returns fast.
- ClickUp marks unhealthy webhooks as failing if requests take too long or error repeatedly.
- Button Custom Fields cannot dynamically pick the next task by themselves, so use them only if they feel lighter than the mobile check-in page.
- The easiest strong daily loop is: phone prompt -> mobile check-in page -> back to deep work.
- If a field is missing in ClickUp, the app degrades gracefully and skips that write rather than inventing a second source of truth.
- Resume Pack hygiene checks use required marker matching (`RESUME_PACK_REQUIRED_MARKERS`) for stronger consistency.
- Good enough Resume Pack means the task description includes these markers with one short useful line under each:
  - `Resume Pack`
  - `Outcome:`
  - `Next Step:`
  - `Re-entry Cue:`
  - `Context:`
- Hygiene reports surface missing markers explicitly so Weekly Reset can fix only the tasks that need it.

## iPhone quick entry

If you want a one-button phone entry point, use:

- `GET /active/checkin`

That endpoint redirects to the current task's check-in page, so it works well with an iPhone home-screen shortcut.

Minimum viable phone setup:

1. Host the app on Railway or Render.
2. Open `{PUBLIC_BASE_URL}/login` in Safari and log in once.
3. After login, open `{PUBLIC_BASE_URL}/active/checkin`.
4. Add that page to Home Screen or make one Shortcut that opens `/active/checkin`.
5. Tap that single icon whenever a work block or check-in starts.

See [IPHONE_SHORTCUT_SETUP.md](./IPHONE_SHORTCUT_SETUP.md).

## ClickUp-native first

If your ClickUp workspace already has:

- pulse fields
- a `Now` view
- a recurring 20-minute reminder

then the next best step is not to replace that. It is to stabilize it.

Recommended rollout:

1. Run the ClickUp-native loop first.
2. Add this app for dynamic task prompts and mobile check-ins.
3. Keep the external logic focused on what ClickUp cannot do well by itself.

See [CLICKUP_NATIVE_ROLLOUT.md](./CLICKUP_NATIVE_ROLLOUT.md) for the full phased rollout guide.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for setup, testing, and pull request guidelines.

## License

[MIT](./LICENSE)
