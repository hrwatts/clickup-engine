# Execution Engine Guide

This is the practical routine the conversation points toward:

- keep the `Execution Engine` list small
- let the system choose the next task
- work in 20-minute slices inside 40 to 80 minute blocks
- use short pulse logging so your brain stays on the real work

## The operating model

Think of the list as a live frontier, not a giant backlog.

Use the lightest status model your ClickUp setup already prefers.

In the current recommended setup, that usually means:

- `To do` means active and available
- `In progress` means the single task currently in front of you
- `Complete` means done

If you also use a dedicated blocked status, keep it simple and optional.

The matching scheduler states are:

- `queued`
- `current`
- `break`
- `blocked`
- `done today`

Only one task should ever be both:

- `Status = In progress`
- `Scheduler State = current`

## Your daily rhythm

Use the day in three bands.

### Morning

Reserve this for the highest-cognition work:

- Lean
- logic
- ML theory
- proofs
- hard pencil-and-paper thinking

### Midday / afternoon

Use this for medium-demand work:

- structured reading
- implementation
- problem sets
- writing passes

### Late day

Use this for lighter work:

- admin
- portfolio checks
- reviews
- follow-up reading

That pattern is baked into the default scheduler scoring in the app.

## What happens every 20 minutes

Every prompt should answer one question:

What should I do for the next 20 minutes?

You respond with one action:

- `Continue 20m`
- `Switch Task`
- `Break 10m`
- `Long Break 20m`
- `Complete Task`
- `Blocked`

And three tiny pulse answers:

- `Progress`: none / low / medium / high
- `Energy`: low / medium / high
- `Friction`: none / some / high

This produces useful metrics without turning your day into journaling.

## Best-practice setup

### Keep ClickUp as source of truth

All tasks live there. All state changes land there. Dashboards read from there.

If a desired field does not exist in ClickUp, prefer skipping that feature or simplifying the workflow rather than creating a second canonical task database outside ClickUp.

### Use phone prompts as the front door

The app pushes a task prompt to your phone and includes a one-tap check-in page.

That is better than requiring you to navigate inside ClickUp every 20 minutes.

For the fastest recovery path, use the `/active/checkin` endpoint from an iPhone shortcut so one tap always lands on the live current task.

### Keep the loop closed

Every important event should end with one of these:

- task stays current
- task goes on break
- task is completed
- task is blocked and replaced

No ambiguous middle state.

## Reports to watch

The most important metrics are:

- total `Today Minutes`
- tasks completed
- blocks logged
- average friction by task type
- average energy by task type
- minutes spent before completion

If a task type repeatedly shows low progress and high friction, it should move to a different time of day or be redefined into a smaller task.

## Resume Pack standard

For this system, a Resume Pack is good enough when the task description contains all of these exact markers:

- `Resume Pack`
- `Outcome:`
- `Next Step:`
- `Re-entry Cue:`
- `Context:`

Each marker only needs one short useful line under it.

That is enough for:

- fast restart after a break
- easier switching without anxiety
- clear Weekly Reset hygiene checks

## Notification-by-notification playbook

Every 20-minute notification should lead to one tiny decision, not a planning session.

At each notification:

1. Look only at the current task.
2. Ask:
   - am I making progress?
   - does this still match my current energy?
   - is the next 20 minutes obvious?
3. Choose one:
   - `Continue 20m`
   - `Switch Task`
   - `Complete Task`
   - `Break 10m`
   - `Long Break 20m`
   - `Blocked`

Use this rule of thumb:

- progress medium or high + friction none or some -> continue
- progress low or momentum broken but task is not truly blocked -> switch
- clean stopping point reached -> complete
- energy dipping but task still good -> short break
- block already long or concentration fading hard -> long break
- friction high or task truly unclear -> blocked

The goal is to return to work quickly, not to produce a perfect diagnosis.

## Pomodoro styles for your day

Do not force a single rigid Pomodoro pattern. Use the one that matches the work.

### Short pattern

- 20 focus
- 10 break
- 20 focus

Use for admin, lighter reading, or mentally noisy days.

### Standard pattern

- 20 focus
- 20 focus
- 10 break

Use for most medium work.

### Deep pattern

- 20 focus
- 20 focus
- 20 focus
- 20 focus
- 20 break

Use for Lean, formal reasoning, proofs, math, and concentrated paper work.

When in doubt:

- morning -> deep pattern
- midday -> standard pattern
- low-energy afternoon -> short pattern

## Other app integrations worth adding

If you want the easiest strong stack on iPhone, this is the order:

1. This scheduler service + ClickUp
2. iPhone home-screen shortcut to the active task check-in page
3. Calendar blocks synced to your day
4. Optional Pushcut or Pushover for an even stronger notification surface

The key best practice is to avoid building the whole system from brittle no-code chains if the scheduler logic is central. Keep the scoring and task rotation in one place.

## Graceful degradation

If a field is unavailable in ClickUp:

1. Keep the list statuses and task structure in ClickUp.
2. Let the scheduler continue using the fields that do exist.
3. Use the mobile check-in page and reports without inventing a second task database.
4. Prefer simpler reports over parallel durable state outside ClickUp.

## Weekly reset hygiene loop

During Weekly Reset, check three things in this order:

1. `/reports/hygiene`
2. `/reports/weekly`
3. the `Execution Engine` views in ClickUp

Use that review to:

- keep the queue between 3 and 7
- restore exactly one current task if needed
- fix missing Resume Packs only where the report flags them
- clear duplicates and stale queue items
- keep `[SYSTEM] Weekly Reset` in Inbox unless you are actively doing reset work
