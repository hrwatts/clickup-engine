# Check-in Page Acceptance Criteria

**Status:** Approved for implementation  
**Phase tags:** `[MVP]` `[Phase 2]` `[Phase 3]`

---

## AC-1: Save State Feedback `[MVP]`

**Given** the user is on the check-in page with a current task loaded  
**When** the user taps any action button (Continue, Break, Complete, Switch, Blocked)  
**Then:**
- The tapped button shows an inline spinner (CSS animation)
- All action buttons become `disabled` (pointer-events: none, opacity: 0.6)
- The status line shows "Saving..."
- Pulse chip selections are visually preserved (not reset)

**And if** the server has not responded within 3 seconds  
**Then** the status line updates to "Still saving — ClickUp is slow."

**And if** the server has not responded within 10 seconds  
**Then** the request is aborted (`AbortController`), buttons are re-enabled, and the status line shows the timeout message with Retry and Reload options.

---

## AC-2: Save Success — Continue `[MVP]`

**Given** the user tapped "Continue Slice" and the server returns `{ok: true}`  
**Then:**
- The status line shows green ✓ text: "Logged — {block_minutes}/{target_minutes}m"
- If `reached_target` is true: "Block target reached. Consider a break."
- If `exceeded_max` is true: "{block_minutes}m in this block. Take a break."
- The block progress bar updates to the new `block_minutes` value
- The progress bar color reflects: teal (below target), amber (at target), red (at/above max)
- All buttons are re-enabled after 300ms

---

## AC-3: Save Success — Task-Changing Actions `[MVP]`

**Given** the user tapped Complete, Switch (with auto-pick), or Blocked, and the server returns `{ok: true}`  
**Then:**
- The status line shows the appropriate success message
- After 1.5 seconds, the browser navigates to the `redirect_to` URL from the response
- During the 1.5s wait, the status line includes "Loading next task..."
- If `redirect_to` is null (no next task available), the page navigates to `/active/checkin` which will show the no-task empty state

---

## AC-4: Save Hard Failure `[MVP]`

**Given** the user tapped an action button and the server returns a non-2xx status (502, 503, 504) OR a network error occurs  
**Then:**
- The status line shows red ✗ text with the error message
- A "Retry" link appears in the status line
- All buttons are re-enabled
- Pulse chip selections remain intact (not reset to defaults)
- Tapping "Retry" re-submits the same action with the current pulse selections

---

## AC-5: Save Partial Failure `[MVP]`

**Given** the user tapped an action and the server returns `{ok: true, partial_failure: true}`  
**Then:**
- The status line shows amber ⚠ text: "Action saved, but some fields didn't update. Your progress is safe."
- The block progress bar updates if block data is present
- The message auto-dismisses after 4 seconds, reverting to the normal ready state

---

## AC-6: Block Progress Visibility `[MVP]`

**Given** the check-in page is loaded with a current task  
**Then:**
- A horizontal progress bar is visible below the task name
- The bar shows "{block_minutes}/{target_minutes}m" as a text label
- The bar fill width = `min(block_minutes / max_minutes, 1.0) * 100%`
- Bar color: teal (`--accent`) when below target, amber (`--accent-2`) when at/above target, red (`#991b1b`) when at/above max
- The bar has a smooth CSS transition on width changes (300ms ease)

---

## AC-7: Auto-Redirect Mechanics `[MVP]`

**Given** a task-changing action (Complete, Switch, Blocked) succeeded  
**When** the server response includes a `redirect_to` field  
**Then:**
- The page auto-navigates to `redirect_to` after 1.5 seconds
- If `redirect_to` is absent or null, the page navigates to `/active/checkin`
- During the 1.5s transition, the user sees the success message and "Loading next task..."
- If the user taps the "Cancel" link during the 1.5s window, the redirect is cancelled and the page remains on the current (stale) task

**Backend requirement:** `POST /checkin/{task_id}` must include `redirect_to` in the response for complete, switch, and blocked actions. The value is `/checkin/{new_task_id}` or null if no task was promoted.

---

## AC-8: No-Task Empty State `[MVP]`

**Given** the user navigates to `/active/checkin` and no current task exists  
**Then:**
- The page displays: heading "Nothing queued right now", body "Add tasks in ClickUp or run the scheduler."
- Two action buttons are shown: [Run Scheduler] and [Open ClickUp ↗]
- [Run Scheduler] sends POST to `/scheduler/run`; on success, the page reloads via `window.location = '/active/checkin'`
- [Open ClickUp ↗] links to the ClickUp list URL (opened in new tab)

---

## AC-9: Session Expired `[MVP]`

**Given** the user taps an action button and the server responds with HTTP 401  
**Then:**
- A full-screen overlay appears with the message "Session expired. Log in to continue."
- A [Log in] button navigates to `/login`
- The overlay is not dismissible (the session is invalid)
- The underlying page content remains visible but non-interactive (backdrop blur)

---

## AC-10: Queue Drawer `[Phase 2]`

**Given** the user taps "Switch ▾" on the check-in page  
**Then:**
- A bottom sheet slides up with a dim backdrop
- The drawer shows the top 5 tasks from `GET /api/queue`, each with name and task type badge
- A "Let scheduler choose" button is shown above the task list
- Tapping a task row fires the switch action targeting that specific task
- Tapping the backdrop or a close handle dismisses the drawer
- The drawer does not reset pulse chip selections in the main card

**Given** the queue is empty  
**Then** the drawer shows "No other tasks in queue" and the Quick Add form (Phase 3).

---

## AC-11: Quick Add Task `[Phase 3]`

**Given** the queue drawer is open  
**Then:**
- Below the task list, a "Quick Add" section shows a text input and an [Add] button
- The user types a task name (1-200 characters) and optionally selects a task type
- Tapping [Add] sends POST to `/api/tasks/quick-add`
- On success: the new task appears at the bottom of the drawer list and a "Added to queue" toast appears
- On failure: an inline error appears below the form: "Couldn't add task. Try again."
- The drawer remains open after add

---

## AC-12: Current Task Clarity `[MVP]`

**Given** the check-in page is loaded with a current task  
**Then:**
- The task name is displayed in a prominent `<h1>` heading
- A "Current" badge (pill-shaped, small, teal background) appears above or beside the task name
- An "Open in ClickUp ↗" link appears near the task name, linking to the task's ClickUp URL
- The task name is HTML-escaped (existing XSS protection preserved)

---

## AC-13: Mobile Responsiveness `[MVP]`

**Given** the page is viewed on a mobile device (viewport width ≤ 480px)  
**Then:**
- All touch targets are at least 44×44px (Apple HIG minimum)
- No horizontal scroll occurs
- The primary action button ("Continue Slice") is within easy thumb reach (bottom half of screen)
- Text is legible without zooming (minimum 16px body font)
- The block progress bar spans the full card width
