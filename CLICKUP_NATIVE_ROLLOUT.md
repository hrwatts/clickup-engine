# ClickUp-Native Rollout

This is the next-step implementation plan that matches the ClickUp Brain reply while staying realistic.

The guiding idea is:

- start with the strongest low-friction ClickUp-native loop
- layer in the external scheduler only where ClickUp becomes too static
- keep your phone prompts short and decisive

## Phase 1: ClickUp-native baseline

Use this even if you never turn on the external scheduler.

### Step 1: Keep Execution Engine small

Only keep 3 to 7 runnable tasks in `Execution Engine`.

That means:

- no someday tasks
- no giant backlog review during focus time
- no browsing other lists when a work block starts

### Step 2: Use one active task rule

At any point:

- exactly one task should be in your configured current status, usually `In progress`
- all other active tasks should be in your configured open status, usually `To do`

This turns the list into a real execution queue instead of a pile.

### Step 3: Add the pulse fields if available

Minimum fields:

- `Task Type`
- `Progress Pulse`
- `Energy Pulse`
- `Friction Pulse`

If ClickUp allows more fields, add:

- `Scheduler State`
- `Block Count Today`
- `Last Worked At`
- `Next Eligible At`
- `Today Minutes`
- `Rotation Score`

### Step 4: Create these core views

1. `Now`
   - only non-completed tasks
   - show pulse columns
   - sorted with your current status at the top

2. `Queue`
   - open-status tasks only
   - grouped by `Task Type`

3. `Blocked`
   - `Blocked` tasks only

4. `Done Today`
   - completed tasks filtered to today if possible

### Step 5: Avoid reminder clutter

Prefer one iPhone shortcut entry point (`/active/checkin`) and optional external prompts over piling recurring ClickUp reminders.

If you do use reminders, keep one conservative reminder at block-end cadence only. Do not create many recurring reminder objects.

## Phase 2: Notification decision loop

Each block-end prompt should force one of five decisions.

### Option 1: Continue Slice

Use this when:

- progress is happening
- the task is still the best use of the next 20 minutes
- friction is manageable

Do this:

- keep task in your current status
- log `Progress Pulse`
- log `Energy Pulse`
- log `Friction Pulse`
- continue another slice

### Option 2: Complete Task

Use this when:

- the task is done
- the task has a clean stopping point and should leave the queue

Do this:

- mark it complete
- let the next best task become current

### Option 3: Short Break 10m

Use this when:

- you are still cognitively good
- the same task should probably continue
- you need a real reset before the next slice

Do this:

- keep the task effectively current
- step away
- resume after 10 minutes

### Option 4: Long Break 20m

Use this when:

- the block is ending
- you have completed 2 to 4 slices
- you need food, walking, or a stronger reset

Do this:

- put the task back into queue if needed
- re-evaluate after the break

### Option 5: Switch Task

Use this when:

- momentum is broken
- the next 20 minutes are no longer obvious
- the task is not truly blocked, but it is no longer the best fit

Do this:

- return the task to queue
- give it a short cooldown
- choose the next best task

### Option 6: Blocked
Use this when:

- friction is high
- the task became unclear
- you are missing a dependency
- the task cannot move without an external unblock

Do this:

- mark it `Blocked` or temporarily ineligible
- choose the next best task

## Phase 3: Pomodoro patterns that fit your actual day

You do not need one rigid timer pattern. Use three modes.

### Mode A: Standard focus block

Best for most work.

- 20m focus
- 20m focus
- 10m break

This is a 40-minute work block plus reset.

### Mode B: Deep block

Best for math, logic, Lean, proofs, and paper reasoning.

- 20m focus
- 20m focus
- 20m focus
- 20m focus
- 20m break

This gives you an 80-minute deep block with boundaries every 20 minutes but without forcing a context change.

### Mode C: Recovery block

Best for low-energy afternoons or heavy switching days.

- 20m focus
- 10m break
- 20m focus
- 10m break

Use this when you need more frequent resets to avoid collapse.

## How to choose the mode

Use this quick rule:

- `deep`, `paper`, `proof`, `theory` -> Mode B in morning
- `reading`, `medium`, `implementation` -> Mode A
- `admin`, `low energy`, `high friction`, `decision fatigue` -> Mode C

## Phase 4: External scheduler layer

Add the app in this repo when you want:

- the phone prompt to include the actual current task
- a dedicated mobile check-in page
- break logic
- automatic rotation
- reporting

The app should remain a thin orchestration layer, not a second planner or second task database.

That is where the external layer becomes worth it.

Use these endpoints:

- `/active/checkin`
- `/scheduler/run`
- `/reports/daily`

## Phase 5: Logging and notes

Your best logging structure is:

- task-level pulse fields for fast taps
- list-level execution dashboard for metrics
- one recurring `Weekly Reset` task for review notes

Do not add long daily journaling requirements to the execution loop.

Instead:

- use comments only when something unusual happened
- use weekly notes for reflection
- keep block logging to the 3 pulse questions
