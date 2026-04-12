# Check-in API Implications

**Status:** Approved for implementation  
**Scope:** Changes required to support the redesigned check-in page

---

## 1. Modified Endpoints

### 1.1 `POST /checkin/{task_id}` — Enhanced Response `[MVP]`

**Current response (all actions):**
```json
{"ok": true, "message": "..."}
```

**New response schema:**
```json
{
  "ok": true,
  "message": "Logged — 40/60m",
  "action": "continue",
  "block": {
    "slice_minutes": 20,
    "block_minutes": 40,
    "target_minutes": 60,
    "remaining_minutes": 20,
    "reached_target": false,
    "exceeded_max": false
  },
  "redirect_to": null,
  "next_task": null,
  "partial_failure": false
}
```

**Field details:**

| Field | Type | When Present | Description |
|-------|------|-------------|-------------|
| `ok` | bool | Always | Whether the primary action succeeded |
| `message` | string | Always | Human-readable status for the status line |
| `action` | string | Always | Echo of the action performed |
| `block` | object/null | `continue` action | Block progress after increment |
| `redirect_to` | string/null | `complete`, `switch`, `blocked` | URL for auto-redirect. Format: `/checkin/{new_task_id}` or null if no task promoted |
| `next_task` | object/null | `complete`, `switch`, `blocked` | `{id, name}` of the promoted task, for transition UI |
| `partial_failure` | bool | Always | `true` if action succeeded but some field updates failed |

**Error response (non-2xx):**
```json
{
  "ok": false,
  "error_code": "clickup_unavailable",
  "message": "ClickUp returned an error. Your check-in wasn't saved.",
  "retry_safe": true
}
```

| Error Code | HTTP Status | `retry_safe` | Description |
|-----------|-------------|-------------|-------------|
| `clickup_unavailable` | 503 | true | ClickUp API error |
| `clickup_timeout` | 504 | true | ClickUp API timeout |
| `invalid_input` | 422 | false | Validation failure |
| `session_expired` | 401 | false | Session cookie invalid |
| `bad_payload` | 400 | false | Malformed JSON body |

**Implementation notes:**
- For `complete`, `switch`, `blocked`: after running the scheduler, fetch the newly promoted task's ID and construct `redirect_to` as `/checkin/{new_task_id}`. If no task was promoted, set `redirect_to` to `/active/checkin`.
- `partial_failure` is set to `true` when `handle_continue` (or similar) succeeds on the primary action (status update) but a secondary field update (e.g., pulse write, today_minutes) throws `ClickUpError`. Wrap secondary writes in try/except.
- `action` is echoed back so the client knows which state machine transition to execute without maintaining local state about which button was tapped.

---

### 1.2 `GET /checkin/{task_id}` — Embedded Data `[MVP]`

**Current behavior:** Returns HTML with no embedded task data beyond the task name.

**New behavior:** Embed a `<script>` tag with initial data so the client can render the block progress bar and task context immediately without an extra fetch:

```html
<script>
  window.__CHECKIN_DATA__ = {
    task_id: "abc123",
    task_name: "Formalize logistic regression in Lean",
    task_url: "https://app.clickup.com/t/abc123",
    block: {
      slice_minutes: 20,
      block_minutes: 40,
      target_minutes: 60,
      remaining_minutes: 20,
      reached_target: false,
      exceeded_max: false
    },
    queue_count: 5,
    settings: {
      short_break_minutes: 10,
      long_break_minutes: 20
    }
  };
</script>
```

**Implementation notes:**
- Serialize the data as JSON and embed it safely using `json.dumps()` with HTML-safe escaping.
- `queue_count` is the number of active queue tasks (cheap to compute since `get_list_tasks` is already called in the scheduler — can be derived at render time or estimated from the runtime store).
- Do NOT embed sensitive data (tokens, secrets, field IDs).

---

## 2. New Endpoints

### 2.1 `GET /api/queue` `[Phase 2]`

**Purpose:** Return the scored task queue for the drawer UI.

**Authentication:** Session cookie required (same as other endpoints).

**Response:**
```json
{
  "tasks": [
    {
      "id": "abc123",
      "name": "Formalize logistic regression in Lean",
      "task_type": "deep",
      "score": 72.5,
      "status": "To do",
      "url": "https://app.clickup.com/t/abc123"
    }
  ],
  "count": 5
}
```

**Implementation:**
- Reuse `choose_current_task()` logic, but return the full sorted scores list instead of just the winner.
- Add a `score_queue_tasks()` function in `scheduler.py`:
  ```python
  async def score_queue_tasks(
      tasks: list[dict], fields: list[ClickUpField], settings: Settings, *, limit: int = 5
  ) -> list[dict]:
      fields_by_name = field_by_name(fields)
      now = datetime.now(timezone.utc)
      scored = []
      for task in tasks:
          score = task_score(task, fields_by_name, now, settings)
          if score > -999:  # exclude ineligible
              scored.append({"task": task, "score": score})
      scored.sort(key=lambda x: x["score"], reverse=True)
      return scored[:limit]
  ```
- Exclude the current task from the queue response.
- Sort by score descending, return top 5.

---

### 2.2 `POST /api/tasks/quick-add` `[Phase 3]`

**Purpose:** Create a new task in the ClickUp Execution Engine list with default fields.

**Authentication:** Session cookie required.

**Request:**
```json
{
  "name": "Review portfolio allocations",
  "task_type": "admin"
}
```

**Validation:**
- `name`: required, 1-200 characters, stripped of leading/trailing whitespace
- `task_type`: optional, must be one of: `deep`, `medium`, `light`, `reading`, `paper`, `admin`. Defaults to `medium` if omitted.

**Response (success):**
```json
{
  "ok": true,
  "task_id": "abc123",
  "name": "Review portfolio allocations"
}
```

**Response (error):**
```json
{
  "ok": false,
  "message": "Couldn't create task in ClickUp."
}
```

**Implementation:**
- Add `create_task()` method to `ClickUpClient` in `clickup.py`:
  ```python
  async def create_task(self, list_id: str, name: str, **kwargs) -> dict:
      response = await self._request("POST", f"/list/{list_id}/task", json={"name": name, **kwargs})
      return response.json()
  ```
- After creating, set default custom fields (Scheduler State = "Queued", Task Type = provided value, pulse defaults).
- This is a Phase 3 endpoint. Do NOT implement in MVP.

---

## 3. Unchanged Endpoints

The following endpoints require no changes:

- `GET /login`, `POST /login`, `POST /logout` — auth flow unchanged
- `GET /healthz`, `GET /readyz` — health checks unchanged
- `GET /active` — JSON API unchanged
- `GET /active/checkin` — redirect logic unchanged (but the no-task HTML will be enhanced in the template)
- `POST /scheduler/run` — unchanged (already returns `current_task_id`)
- `GET /reports/*` — all report endpoints unchanged
- `POST /clickup/webhook` — unchanged
- `POST /telegram/webhook` — unchanged

---

## 4. Error Handling Strategy

### Current Problem
The current `POST /checkin/{task_id}` raises `HTTPException` on ClickUp errors, which FastAPI serializes as `{"detail": "..."}`. The client-side JS doesn't check `response.ok` and tries to parse the error response as the success format, causing silent failures.

### Solution
1. **All error responses** from check-in endpoints use the structured format:
   ```json
   {"ok": false, "error_code": "...", "message": "...", "retry_safe": true}
   ```
2. The backend catches ClickUp errors at the handler level and returns structured JSON instead of raising `HTTPException` for check-in actions.
3. For task-fetch errors (can't load the task at all), continue to raise `HTTPException` — the HTML page won't render.
4. For action errors (task loaded but action failed), return structured JSON with appropriate status code.

### Partial Failure Handling
When the primary action (status change) succeeds but secondary fields fail:
```python
partial = False
try:
    await _set_optional_pulses(...)
except ClickUpError:
    partial = True
    logger.warning("Pulse update failed for task %s", task_id)
```
Return `{"ok": true, "partial_failure": true, ...}`.

---

## 5. Implementation Order

### MVP (this PR)
1. Enhance `POST /checkin/{task_id}` response with `redirect_to`, `next_task`, `action`, `partial_failure`
2. Enhance `GET /checkin/{task_id}` to embed `__CHECKIN_DATA__`
3. Rewrite the check-in HTML template with the new state machine, progress bar, and status line
4. Add structured error responses for check-in actions

### Phase 2 (next PR)
5. Add `GET /api/queue` endpoint
6. Add `score_queue_tasks()` to `scheduler.py`
7. Add queue drawer to the check-in HTML template

### Phase 3 (future PR)
8. Add `POST /api/tasks/quick-add` endpoint
9. Add `create_task()` to `ClickUpClient`
10. Add quick-add form to queue drawer
