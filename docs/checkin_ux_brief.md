# Check-in Page UX Brief

**Author:** UX/Product Design  
**Date:** 2026-04-12  
**Status:** Approved for implementation

---

## 1. Diagnosis of Current UX Failures

### F1: Indeterminate Save State

`submitCheckin()` sets `result.textContent = "Saving..."` with no timeout, no `try/catch`, and no error UI. If the fetch fails, throws a network error, or the server takes >5 seconds (common with ClickUp API under load), "Saving..." remains permanently. The user cannot distinguish pending, success, slow, timeout, or failure.

**Evidence:** Jakob Nielsen's response time research (1993, revalidated 2014) identifies three thresholds: 0.1s (instantaneous), 1.0s (flow preserved), 10s (attention lost). The current UI provides no feedback past the initial "Saving..." at 0ms, violating the 1s and 10s thresholds.

### F2: Dead-End After Task-Changing Actions

After Complete, Switch, or Blocked, the page still displays the OLD task. The user must: (1) mentally register the action succeeded, (2) navigate back to home, (3) tap the iPhone Shortcut, (4) land on the new task. That is 4 manual steps where there should be 0. This breaks momentum and is the #1 friction source in the current flow.

### F3: Single-Task Tunnel Vision

The page shows only one task with no additional context:
- No block-session timer (user cannot see 40/60m without reading the success message)
- No queue visibility ("Switch Task" is a blind leap — the scheduler auto-picks)
- No way to add a forgotten task without leaving the app
- No way to pick a specific next task

### F4: Undifferentiated Feedback

All feedback — success, error, and loading — renders as the same muted gray `<p id="result" class="muted">`. Success looks identical to loading. There is no visual distinction for warnings (block target reached) vs. errors (ClickUp down) vs. success (logged).

### F5: No Error Recovery

- Session expiry (401) causes a silent JavaScript exception — `response.json()` throws on a redirect response
- ClickUp downtime (502/503) returns JSON that renders as raw error text with no retry option
- Network failures (offline, DNS) throw an unhandled fetch exception
- There is no retry mechanism for any error class

### F6: Defaults Mask Actual Input

Progress defaults to "medium" and friction to "none" via pre-selected chips. Users who tap "Continue Slice" without reviewing pulse inputs log false data, inflating progress and deflating friction measurements. There is no visual cue distinguishing "user-selected medium" from "default medium."

---

## 2. UX Principles

| # | Principle | Rationale |
|---|-----------|-----------|
| P1 | **Visible system status** | Every action has a beginning, progress indicator, and definitive end state (Nielsen #1) |
| P2 | **Momentum over completeness** | Auto-navigate after task-changing actions; never leave the user on a stale page |
| P3 | **Optimistic but honest** | Show success UI immediately; revert with clear explanation on failure |
| P4 | **Progressive disclosure** | Pulse check-in is the primary surface; queue and add-task are secondary (appear on demand) |
| P5 | **Trust through differentiation** | Success (green ✓), warning (amber ⚠), error (red ✗), loading (neutral spinner) are visually distinct |
| P6 | **Context preservation** | Pulse selections survive errors, retries, and slow saves — never reset user input on failure |
| P7 | **Mobile-first, thumb-friendly** | Primary action in easy-reach zone, 44px+ touch targets per Apple HIG, no hover-dependent interactions |

---

## 3. Three UX Concepts

### Concept A: "Control Surface" — Single Page + Bottom Drawer

**Information Architecture:**
```
┌─────────────────────────────────┐
│ ● Current                       │
│ Task Name                       │
│ ━━━━━━━━━━━━░░░░░ 40/60m       │  ← block progress bar
│                                 │
│ Progress  [none][low][med][high]│
│ Energy    [low][med][high]      │
│ Friction  [none][some][high]    │
│                                 │
│ [Continue Slice]  [Switch ▾]    │  ← Switch opens drawer
│ [Break 10m]  [Long Break 20m]  │
│ [     Complete Task            ]│
│ [     Blocked                  ]│
│                                 │
│ ✓ Logged — 40/60m              │  ← status line
└─────────────────────────────────┘
```

**Drawer (opened by "Switch ▾"):**
```
┌─────────────────────────────────┐
│ ━━━━━ (drag handle)             │
│ Next Priorities                 │
│                                 │
│  📋 Formalize logistic regr...  │  ← tap to switch
│  📋 Portfolio review            │
│  📋 Lean tactic proof           │
│                                 │
│ [Let scheduler choose]          │
│                                 │
│ ── Quick Add ──                 │
│ [Task name...        ] [Add]    │
│                                 │
│ [Close]                         │
└─────────────────────────────────┘
```

**Interaction Model:**
- Pulse chips → tap to select (same as current)
- Action buttons → tap fires save, button shows spinner, all buttons disabled during save
- "Switch ▾" → opens bottom drawer instead of immediately switching
- Drawer task items → tap to switch to that specific task
- "Let scheduler choose" → same as current switch behavior (blind pick)
- Quick add → inline form, creates task in ClickUp with default fields

**Loading/Saving/Error Model:**
- **Saving:** Tapped button → inline spinner, opacity 0.6; all buttons disabled; status line: "Saving..."
- **Slow save (3s):** Status line: "Still saving — ClickUp is slow."
- **Success:** Status line → green ✓ + message. Buttons re-enable after 300ms.
- **Partial failure:** Status line → amber ⚠ + "Action saved, some fields didn't update."
- **Hard failure:** Status line → red ✗ + message + [Retry] link. Pulse preserved.
- **Timeout (10s):** Status line → amber ⚠ + "Timed out. May have gone through." + [Retry] [Reload]
- **Session expired (401):** Overlay → "Session expired. [Log in]"

**Auto-Redirect Model (task-changing actions):**
- Complete/Switch/Blocked: success message for 1.5s → `window.location = redirect_to`
- Break: success message remains (no redirect — user is stepping away)
- Continue: status line updates in place (no redirect — user continues same task)

**Switching-Task Model:**
- "Switch ▾" opens drawer with top 5 scored queue tasks
- Tap a specific task → POST to new `/api/switch-to/{task_id}` endpoint (or existing switch handler with target param)
- "Let scheduler choose" → POST existing switch action

**Add Forgotten Task Model:**
- Inline form in drawer: task name + optional type dropdown
- POST to `/api/tasks/quick-add` → creates in ClickUp with default EE fields
- Added task appears in drawer list immediately (optimistic update)

**Pros:** Minimal migration risk, mobile-first, preserves single-task focus, momentum via auto-redirect.  
**Cons:** Drawer adds moderate JS complexity, queue needs new API endpoint.

---

### Concept B: "Dashboard Split" — Two-Panel Layout

**Information Architecture:** Left panel: check-in card. Right panel: task queue with scores. On mobile, these become two tabs.

**Interaction Model:** Tab bar at top for mobile: "Check-in" | "Queue". Queue panel shows all scored tasks with type badges and score indicators.

**Pros:** Full queue visibility at all times, power-user friendly.  
**Cons:** Tab-switching adds friction on mobile (the primary device), violates single-focus philosophy of the Execution Engine Guide, heavier layout with more visual noise.

---

### Concept C: "Card Stack" — Gesture-Driven Flow

**Information Architecture:** Current task as a card that can be swiped. Swipe right = continue, swipe left = switch, swipe up = complete. Queue cards stacked behind.

**Pros:** Playful, gesture-native for mobile.  
**Cons:** 6 actions don't map cleanly to gestures, poor discoverability (no visible affordances), accessibility issues for motor-impaired users, non-standard pattern that requires learning.

---

## 4. Recommended Concept: A — "Control Surface"

**Justification:**

1. **Mobile-first fit.** The iPhone Shortcut is the primary entry point. A single scrollable page with a bottom drawer is the native iOS pattern (Maps, Uber, Apple Music).
2. **Lowest migration risk.** The current page is already a single card. We add a progress bar, a state machine in JS, and a drawer — no new pages, no routing changes, no tabs.
3. **Progressive disclosure.** 80% of check-ins are "Continue Slice." The queue drawer is secondary — users who just continue never see it. This preserves the "one tiny decision" philosophy from the Execution Engine Guide.
4. **Auto-redirect solves the #1 pain (F2).** After Complete/Switch/Blocked, the server responds with `redirect_to` and the client auto-navigates in 1.5s. Zero manual steps.
5. **Concept B** overserves: the queue is visible at all times (distraction) and the tab pattern requires an extra tap on every check-in (friction). **Concept C** underserves: gesture discovery is terrible and 6 actions don't map to directional swipes.

---

## 5. Page State Model

### State Machine

```
                    ┌──────────────────┐
                    │     loading      │
                    └────────┬─────────┘
                   ┌─────────┼──────────┐
                   ▼         ▼          ▼
            ┌──────────┐ ┌───────┐ ┌─────────┐
            │ no-task  │ │ ready │ │  error   │
            └──────────┘ └───┬───┘ └─────────┘
                             │
                 ┌───────────┼───────────┐
                 ▼                       ▼
          ┌──────────┐           ┌──────────────┐
          │  saving  │           │ drawer-open  │
          └────┬─────┘           └──────────────┘
     ┌─────┬──┼──┬──────┐
     ▼     ▼  ▼  ▼      ▼
  success partial error timeout session-expired
```

### State Definitions

| State | What User Sees | Entry Condition | Exit Transitions |
|-------|---------------|-----------------|------------------|
| `loading` | Skeleton card: shimmer placeholders for task name, progress bar, chips, action buttons | Page load / redirect arrival | → `ready` (data loaded) · → `no-task` (no current task) · → `error` (fetch failed) |
| `no-task` | "Nothing queued right now." + [Run Scheduler] + [Open ClickUp] buttons | Server returns no current task | → `loading` (after Run Scheduler succeeds) |
| `ready` | Full check-in: task header + progress bar + pulse chips + action buttons + empty status line | Task data available | → `saving` (user taps action) · → `drawer-open` (user taps "Switch ▾") |
| `saving` | Tapped button shows spinner; all buttons disabled; status line: "Saving..." At 3s: "Still saving — ClickUp is slow." | User taps any action button | → `save-success` · → `save-partial` · → `save-error` · → `save-timeout` (10s) |
| `save-success` | Green ✓ + message. **Continue:** buttons re-enable, progress bar updates. **Complete/Switch/Blocked:** "Loading next task..." + auto-redirect at 1.5s. **Break:** "Break for Nm. Come back when ready." | Server returns `{ok: true}` | → `ready` (continue/break) · → `loading` (redirect for complete/switch/blocked) |
| `save-partial` | Amber ⚠ "Action saved, but some fields didn't update. Your progress is safe." | Server returns `{ok: true, partial_failure: true}` | → `ready` (auto-dismiss after 4s) |
| `save-error` | Red ✗ + error message + [Retry] link. Pulse selections preserved. | Server returns error or non-2xx status | → `saving` (retry) · → `ready` (dismiss) |
| `save-timeout` | Amber ⚠ "Timed out. It may have gone through." + [Retry] [Reload] | 10s elapsed with no response | → `saving` (retry) · → `loading` (reload) |
| `drawer-open` | Bottom sheet overlay: queue tasks + "Let scheduler choose" + Quick Add form | User taps "Switch ▾" | → `ready` (close/tap outside) · → `saving` (tap a task to switch) |
| `modal-add-task` | Inline form within drawer: name input + type dropdown + [Add] button | User taps Quick Add section | → `drawer-open` (after add or cancel) |
| `offline` | Top banner: "You're offline." Buttons show "Can't save while offline" on tap. | `navigator.onLine === false` | → `ready` (back online) |
| `session-expired` | Full-screen overlay: "Session expired." + [Log in] button | Server returns 401 | → redirect to `/login` |

---

## 6. Microcopy Recommendations

### Action Buttons
| Button | Label | Style |
|--------|-------|-------|
| Primary action | Continue Slice | Teal filled |
| Switch | Switch ▾ | Neutral with caret |
| Short break | Break {N}m | Neutral |
| Long break | Long Break {N}m | Amber/warning |
| Complete | Complete Task | Full-width, neutral |
| Blocked | Blocked | Full-width, red/danger |

### Status Line Messages

**Continue — success:**
- Default: `✓ Logged — {block_minutes}/{target_minutes}m`
- At target: `✓ Block target reached ({block_minutes}/{target_minutes}m). Consider a break.`
- Exceeded max: `✓ {block_minutes}m in this block. Take a break.`

**Complete — success:** `✓ Done! Loading next task...`

**Switch — success:** `✓ Switching...`

**Break — success:** `✓ Break for {N}m. Come back when ready.`

**Blocked — success:** `✓ Marked blocked. Loading next task...`

### Error Messages

| Condition | Message |
|-----------|---------|
| Network failure | Couldn't reach the server. Check your connection and retry. |
| ClickUp error (502/503) | ClickUp returned an error. Your check-in wasn't saved. [Retry] |
| Partial failure | Action saved, but some fields didn't update. Your progress is safe. |
| Session expired (401) | Session expired. Log in to continue. |
| Timeout (10s) | Request timed out. It may have gone through. [Retry] [Reload] |
| Invalid input (422) | Invalid input. Please try again. |
| Slow save (3s) | Still saving — ClickUp is slow. |

### Empty States

| State | Heading | Body |
|-------|---------|------|
| No current task | Nothing queued right now | Add tasks in ClickUp or run the scheduler. |
| Empty queue drawer | No other tasks in queue | All tasks are completed or blocked. |
| Quick-add success | (toast) | Added to queue. |
| Quick-add failure | (toast) | Couldn't add task. Try again. |

---

## 7. Front-End Component List

All components are implemented as semantic HTML sections with a vanilla JS state machine. No framework required for MVP.

| # | Component | Responsibility |
|---|-----------|---------------|
| 1 | `CheckinPage` | Top-level container. Owns the state machine. Renders correct sub-components per state. |
| 2 | `TaskHeader` | Task name (h1) + "Current" badge + "Open in ClickUp ↗" link |
| 3 | `BlockProgress` | Animated progress bar (`<div>` with width transition) + "{current}/{target}m" label. Color: teal < target, amber ≥ target, red ≥ max. |
| 4 | `PulseChips` | Reusable chip group for each pulse field. Handles tap → toggle `.selected`. |
| 5 | `ActionBar` | Grid of action buttons. Handles `disabled` state during saves. |
| 6 | `ActionButton` | Individual button with spinner overlay when its action is in-flight. |
| 7 | `StatusLine` | Feedback line below actions. Renders icon + message + optional [Retry] link. Distinct styles per status class. |
| 8 | `QueueDrawer` | Bottom sheet with backdrop. Slides up on "Switch ▾", slides down on close/backdrop tap. |
| 9 | `QueueTaskItem` | Row in drawer: task name + type badge. Tap fires switch-to action. |
| 10 | `QuickAddForm` | Inline form in drawer: text input + type `<select>` + Add button. |
| 11 | `OfflineBanner` | Sticky top bar. Shown/hidden by `navigator.onLine` listener. |
| 12 | `SessionExpiredOverlay` | Full-screen overlay with login CTA. Triggered by 401 response. |
| 13 | `SkeletonCard` | CSS shimmer placeholders matching the layout of the real card. |

---

## 8. Backend / API Implications

See [checkin_api_implications.md](checkin_api_implications.md) for full details.

**Summary of changes:**

| Type | Endpoint | Phase |
|------|----------|-------|
| Modify | `POST /checkin/{task_id}` | MVP |
| Modify | `GET /checkin/{task_id}` | MVP |
| New | `GET /api/queue` | Phase 2 |
| New | `POST /api/tasks/quick-add` | Phase 3 |

---

## 9. Prioritization

### MVP (ship first)

1. **Save state machine** — saving → spinner + disabled → success/error with distinct visuals. Timeout at 3s (slow message) and 10s (abort).
2. **Error display with retry** — red error status line + [Retry] link. Pulse preserved across retries.
3. **Auto-redirect** — after Complete/Switch/Blocked, server returns `redirect_to` URL; client auto-navigates after 1.5s.
4. **Block progress bar** — animated bar in task header showing current/target minutes.
5. **Session-expired detection** — 401 response → overlay with login button (not a silent JS crash).
6. **Current task clarity** — "Current" badge, prominent task name, clickable ClickUp link.
7. **Backend response enhancements** — `redirect_to`, `next_task`, `partial_failure`, structured errors with `error_code` and `retry_safe`.

### Phase 2

8. **Queue drawer** — bottom sheet with top 5 scored tasks from `GET /api/queue`.
9. **Direct task switching** — tap a specific task in drawer to switch to it.
10. **Skeleton loading** — shimmer placeholders while task data loads.
11. **"Let scheduler choose"** — button in drawer for blind switch (current behavior).

### Phase 3

12. **Quick-add task** — inline form in drawer, creates task via `POST /api/tasks/quick-add`.
13. **Offline detection banner** — `navigator.onLine` listener + "You're offline" banner.
14. **Offline action queuing** — hold actions in IndexedDB, replay on reconnect.
