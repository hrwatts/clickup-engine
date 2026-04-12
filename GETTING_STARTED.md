# Getting Started with ClickUp Execution Engine Scheduler

This guide walks you through the minimum viable setup to run the Execution Engine Scheduler.

## Prerequisites

- **ClickUp**: A paid plan with Custom Fields support (Pro or higher)
- **ClickUp API token**: [Generate one in your ClickUp settings](https://app.clickup.com/settings/apps)
- **Python 3.12+**: [Download here](https://www.python.org/downloads/)
- **Optional**: Telegram Bot Token for push notifications (skip if you want to use only web check-ins)
- **Optional**: Railway or Render account for hosting

## Step 1: Clone and Setup

```bash
git clone https://github.com/hrwatts/clickup-engine.git
cd clickup-engine

# Create virtual environment
python -m venv .venv

# Activate it
# On macOS/Linux:
source .venv/bin/activate

# On Windows PowerShell:
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

## Step 2: Configure Environment Variables

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

### Required

```
CLICKUP_API_TOKEN=<your-token-from-clickup>
PIPELINE_SPACE_NAME=<your-clickup-space-name>
PIPELINE_FOLDER_NAME=<your-clickup-folder-name>
SECRET_KEY=<a-random-secret-for-session-encryption>
```

### Optional (for mobile notifications)

```
TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
TELEGRAM_CHAT_ID=<your-telegram-chat-id>
```

### Optional (for production hosting)

```
DEPLOYED_URL=https://your-domain.example
CLICKUP_WEBHOOK_SECRET=<webhook-secret-from-clickup>
```

## Step 3: Set Up Your ClickUp Workspace

### Create the Execution Engine List

1. In ClickUp, create a new List called **Execution Engine** in your chosen Space/Folder
2. Keep it small: only 3-7 active tasks at a time
3. Use consistent statuses:
   - `To do` - active and available
   - `In progress` - current task
   - `Done` - completed

### Add Custom Fields (if available on your plan)

These fields are optional but recommended for full functionality:

- **Scheduler State** (select): `queued`, `current`, `break`, `blocked`, `done today`
- **Progress Pulse** (select): `red`, `yellow`, `green`
- **Energy Pulse** (select): `red`, `yellow`, `green`
- **Friction Pulse** (select): `red`, `yellow`, `green`
- **Today Minutes** (number): minutes worked today
- **Block Count** (number): number of work blocks completed
- **Last Worked At** (text): timestamp of last activity

## Step 4: Run Locally

```bash
# Make sure .venv is activated
python app/main.py

# You should see:
# INFO:     Uvicorn running on http://127.0.0.1:8000
```

Visit: `http://localhost:8000/login`

## Step 5: Test the Mobile Check-in

1. Visit `http://localhost:8000/active/checkin`
2. You should see your current task (or a prompt to create one)
3. Log a quick pulse: answer Progress, Energy, and Friction (red/yellow/green)
4. Try an action button: Continue, Switch, Break, Complete, or Blocked

## Step 6: Deploy to Production

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete hosting instructions on:
- Railway (recommended, free tier available)
- Render
- Fly.io
- Self-hosted

## Step 7: Add the Mobile Shortcut (Optional)

### iPhone Shortcut Setup

Create a Shortcut named `Execution Engine`:

1. Open the Shortcuts app
2. Create New Shortcut
3. Add action: `URL`
   - Value: `https://your-domain.example/active/checkin`
4. Add action: `Open URLs`
5. Add to Home Screen

Now tap the home screen icon to open your check-in page directly.

### Browser Bookmark Alternative

Or simply bookmark `https://your-domain.example/active/checkin` in Safari for quick access.

## Understanding the System

### The One-Current-Task Rule

This system enforces:
- **Exactly one task** is marked as both `Status = In progress` AND `Scheduler State = current`
- **All other active tasks** are in `Status = To do` with `Scheduler State = queued`

This creates a clear execution frontier instead of task pile anxiety.

### The Pulse Log

When you check in, you're answering three quick questions:

- **Progress**: How far into this task are you? (red = stuck, yellow = moving, green = flowing)
- **Energy**: How's your cognitive state? (red = depleted, yellow = okay, green = sharp)
- **Friction**: What's blocking you? (red = stuck, yellow = minor friction, green = smooth)

This takes 10 seconds and helps the scheduler learn your patterns.

### The Action Buttons

After logging, choose one action:

- **Continue Slice**: Keep this task, log another 20-min block
- **Switch Task**: Mark this one queued, promote the next task to current
- **Complete Task**: Mark done, promote next task
- **Take Break**: Mark yourself on break (no task is current)
- **Blocked**: Mark task blocked, promote next available

## Troubleshooting

### "ClickUp API token invalid"

- Verify your `CLICKUP_API_TOKEN` in `.env`
- Tokens expire after 90 days of inactivity; regenerate at [ClickUp Settings](https://app.clickup.com/settings/apps)

### "Cannot find space or folder"

- Make sure `PIPELINE_SPACE_NAME` and `PIPELINE_FOLDER_NAME` exactly match your ClickUp workspace
- They are case-sensitive

### "Login page shows but won't let me in"

- Verify `SECRET_KEY` is set in `.env` (should be a random string)
- Clear browser cookies and try again

### "No current task appears"

- Create at least one task in your Execution Engine list
- Mark it with status `In progress`

## Next Steps

- **Configure scheduler rules**: Customize task scoring in `app/conformance.py`
- **Set up webhooks**: Enable real-time sync with ClickUp (see [ARCHITECTURE.md](ARCHITECTURE.md))
- **Add notifications**: Set up Telegram for push prompts
- **Review the guides**: See [EXECUTION_ENGINE_GUIDE.md](EXECUTION_ENGINE_GUIDE.md) for operating philosophy

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/hrwatts/clickup-engine/issues)
- **Contributing**: See [CONTRIBUTING.md](CONTRIBUTING.md)
- **Architecture**: See [ARCHITECTURE.md](ARCHITECTURE.md)

