from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.clickup import (
    ClickUpClient,
    ClickUpError,
    ClickUpField,
    ClickUpStatusOption,
    dropdown_options,
    field_by_name,
    field_value,
    is_blocked_status,
    is_closed_status,
    list_statuses,
    normalize_name,
    option_by_label,
    parse_clickup_datetime,
)
from app.conformance import evaluate_field_conformance
from app.config import Settings
from app.store import RuntimeSessionStore


def field_names(settings: Settings) -> dict[str, str]:
    return {
        "scheduler_state": settings.field_scheduler_state_name,
        "task_type": settings.field_task_type_name,
        "progress_pulse": settings.field_progress_pulse_name,
        "energy_pulse": settings.field_energy_pulse_name,
        "friction_pulse": settings.field_friction_pulse_name,
        "block_count_today": settings.field_block_count_today_name,
        "last_worked_at": settings.field_last_worked_at_name,
        "next_eligible_at": settings.field_next_eligible_at_name,
        "today_minutes": settings.field_today_minutes_name,
        "rotation_score": settings.field_rotation_score_name,
    }


@dataclass
class SchedulerDecision:
    current_task: dict[str, Any] | None
    scores: dict[str, float]


@dataclass
class HygieneReport:
    current_count: int
    queue_count: int
    missing_fields: list[str]
    duplicate_title_groups: list[list[dict[str, Any]]]
    missing_resume_pack: list[dict[str, Any]]
    resume_pack_issues: list[dict[str, Any]]
    stale_queue_tasks: list[dict[str, Any]]
    warnings: list[str]


@dataclass(frozen=True)
class RuntimeStatusMap:
    active_status: str
    completed_status: str
    available_status: str | None
    blocked_status: str | None
    warnings: tuple[str, ...] = ()


def _find_status_option(options: list[ClickUpStatusOption], name: str) -> ClickUpStatusOption | None:
    target = str(name or "").strip().casefold()
    if not target:
        return None
    return next((option for option in options if option.status.strip().casefold() == target), None)


def _status_warnings_with_prefix(status_map: RuntimeStatusMap, prefix: str) -> list[str]:
    return [warning for warning in status_map.warnings if warning.startswith(prefix)]


def _pick_first(options: list[ClickUpStatusOption], *, exclude: set[str] | None = None, type_name: str | None = None) -> ClickUpStatusOption | None:
    exclude = exclude or set()
    for option in options:
        if option.status.strip().casefold() in exclude:
            continue
        if type_name and option.type != type_name:
            continue
        return option
    return None


def _resolve_active_status(options: list[ClickUpStatusOption], settings: Settings) -> tuple[ClickUpStatusOption | None, list[str]]:
    warnings: list[str] = []
    configured = _find_status_option(options, settings.clickup_current_status)
    if configured:
        return configured, warnings
    if str(settings.clickup_current_status or "").strip():
        warnings.append("task_status_active_config_invalid")

    preferred_names = {"in progress", "active", "current"}
    fallback = next((option for option in options if option.status.strip().casefold() in preferred_names), None)
    if fallback:
        warnings.append("task_status_active_fallback")
        return fallback, warnings

    fallback = _pick_first([option for option in options if option.type != "closed"])
    if fallback:
        warnings.append("task_status_active_fallback")
        return fallback, warnings
    return None, warnings


def _resolve_completed_status(options: list[ClickUpStatusOption], settings: Settings) -> tuple[ClickUpStatusOption | None, list[str]]:
    warnings: list[str] = []
    configured = _find_status_option(options, settings.clickup_completed_status)
    if configured:
        return configured, warnings
    if str(settings.clickup_completed_status or "").strip():
        warnings.append("task_status_completed_config_invalid")

    preferred_names = {"complete", "completed", "done", "closed"}
    fallback = next((option for option in options if option.status.strip().casefold() in preferred_names), None)
    if fallback:
        warnings.append("task_status_completed_fallback")
        return fallback, warnings

    fallback = _pick_first([option for option in options if option.type == "closed"], type_name="closed")
    if fallback:
        warnings.append("task_status_completed_fallback")
        return fallback, warnings
    return None, warnings


def _resolve_available_status(
    options: list[ClickUpStatusOption],
    settings: Settings,
    *,
    active_status: str,
    blocked_status: str | None,
) -> tuple[ClickUpStatusOption | None, list[str]]:
    warnings: list[str] = []
    configured = _find_status_option(options, settings.clickup_open_status)
    if configured:
        return configured, warnings
    if str(settings.clickup_open_status or "").strip():
        warnings.append("task_status_available_config_invalid")

    exclude = {active_status.strip().casefold()}
    if blocked_status:
        exclude.add(blocked_status.strip().casefold())

    preferred_names = {"open", "not started", "backlog", "queued", "ready"}
    fallback = next(
        (
            option for option in options
            if option.type != "closed"
            and option.status.strip().casefold() not in exclude
            and option.status.strip().casefold() in preferred_names
        ),
        None,
    )
    if fallback:
        warnings.append("task_status_available_fallback")
        return fallback, warnings

    fallback = _pick_first([option for option in options if option.type != "closed"], exclude=exclude)
    if fallback:
        warnings.append("task_status_available_fallback")
        return fallback, warnings
    return None, warnings


async def resolve_runtime_status_map(
    clickup: ClickUpClient,
    list_id: str,
    settings: Settings,
    *,
    require_active: bool = True,
    require_completed: bool = False,
    require_available: bool = False,
) -> RuntimeStatusMap:
    list_info = await clickup.validate_access(list_id)
    options = list_statuses(list_info)
    if not options:
        raise ClickUpError(
            "Execution list statuses could not be loaded from ClickUp.",
            status_code=503,
            error_code="STATUS_MAP_UNAVAILABLE",
        )

    warnings: list[str] = []
    blocked_option = _find_status_option(options, settings.clickup_blocked_status) if settings.clickup_blocked_status else None
    active_option, active_warnings = _resolve_active_status(options, settings)
    warnings.extend(active_warnings)
    if require_active and not active_option:
        raise ClickUpError(
            f"No valid active status could be resolved on the execution list. Configured current status: '{settings.clickup_current_status}'.",
            status_code=503,
            error_code="STATUS_CONFIG_INVALID",
        )

    completed_option, completed_warnings = _resolve_completed_status(options, settings)
    warnings.extend(completed_warnings)
    if require_completed and not completed_option:
        raise ClickUpError(
            f"No valid completed status could be resolved on the execution list. Configured completed status: '{settings.clickup_completed_status}'.",
            status_code=503,
            error_code="STATUS_CONFIG_INVALID",
        )

    available_option, available_warnings = _resolve_available_status(
        options,
        settings,
        active_status=str(active_option.status if active_option else ""),
        blocked_status=blocked_option.status if blocked_option else None,
    )
    warnings.extend(available_warnings)
    if require_available and not available_option:
        raise ClickUpError(
            f"No valid non-complete runtime status could be resolved on the execution list. Configured open status: '{settings.clickup_open_status}'.",
            status_code=503,
            error_code="STATUS_CONFIG_INVALID",
        )

    return RuntimeStatusMap(
        active_status=active_option.status if active_option else "",
        available_status=available_option.status if available_option else None,
        completed_status=completed_option.status if completed_option else "",
        blocked_status=blocked_option.status if blocked_option else None,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _dropdown_name(task: dict[str, Any], fields_by_name: dict[str, ClickUpField], field_name: str) -> str | None:
    raw_value = field_value(task, field_name)
    if raw_value is None:
        return None
    field = fields_by_name.get(field_name)
    if not field:
        return None
    for name, option in dropdown_options(field).items():
        if option.id == raw_value:
            return name
    return None


def _number(task: dict[str, Any], field_name: str) -> float:
    value = field_value(task, field_name)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalized_title(name: str) -> str:
    # Normalize for duplicate detection while preserving semantic title meaning.
    cleaned = re.sub(r"\s+", " ", (name or "").strip().lower())
    cleaned = re.sub(r"^[\[\(].*?[\]\)]\s*", "", cleaned)
    return cleaned


def _is_system_control_task(task: dict[str, Any]) -> bool:
    name = str(task.get("name") or "").strip().lower()
    return name.startswith("[system]") or name.startswith("(system)")


def _read_scheduler_state_name(
    task: dict[str, Any],
    fields_by_name: dict[str, ClickUpField],
    settings: Settings,
) -> str:
    """Return the lowercase name of the Scheduler State field option, or empty string."""
    raw_value = field_value(task, settings.field_scheduler_state_name)
    if raw_value is None:
        return ""
    field = fields_by_name.get(settings.field_scheduler_state_name)
    if not field:
        return ""
    for option in field.type_config.get("options", []):
        if option.get("id") == raw_value:
            return str(option.get("name") or "").strip().casefold()
    return ""


def _is_current_task(
    task: dict[str, Any],
    settings: Settings,
    fields_by_name: dict[str, ClickUpField] | None = None,
) -> bool:
    """Return True if the task is the active current task.

    Uses both native status AND Scheduler State field as signals so that
    manual ClickUp edits that only touch the custom field are respected.
    """
    status = task.get("status", {}).get("status", "")
    if status.strip().casefold() == settings.clickup_current_status.strip().casefold():
        return True
    if fields_by_name is not None:
        sched_state = _read_scheduler_state_name(task, fields_by_name, settings)
        if sched_state == "current":
            return True
    return False


def _runtime_switch_cooldown_active(task: dict[str, Any], now: datetime) -> bool:
    cooldown_until_ms = task.get("_runtime", {}).get("switch_cooldown_until")
    if not cooldown_until_ms:
        return False
    cooldown_until = parse_clickup_datetime(cooldown_until_ms)
    return bool(cooldown_until and cooldown_until > now)


def _is_active_queue_task(
    task: dict[str, Any],
    settings: Settings,
    fields_by_name: dict[str, ClickUpField] | None = None,
) -> bool:
    status = task.get("status", {}).get("status", "")
    if is_closed_status(status):
        return False
    if is_blocked_status(status, settings.clickup_blocked_status):
        return False
    if fields_by_name is not None:
        sched_state = _read_scheduler_state_name(task, fields_by_name, settings)
        if sched_state in {"blocked", "break", "done today"}:
            return False
    if _is_current_task(task, settings, fields_by_name):
        return False
    if _is_system_control_task(task):
        return False
    return True


def _has_resume_pack(task: dict[str, Any]) -> bool:
    candidates = [
        str(task.get("description") or ""),
        str(task.get("text_content") or ""),
        str(task.get("markdown_description") or ""),
    ]
    blob = "\n".join(candidates).lower()
    return "resume pack" in blob


def _missing_resume_pack_markers(task: dict[str, Any], settings: Settings) -> list[str]:
    candidates = [
        str(task.get("description") or ""),
        str(task.get("text_content") or ""),
        str(task.get("markdown_description") or ""),
    ]
    blob = "\n".join(candidates).lower()
    missing: list[str] = []
    for marker in settings.resume_pack_required_markers:
        if marker.lower() not in blob:
            missing.append(marker)
    return missing


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def block_progress(task: dict[str, Any], settings: Settings) -> dict[str, Any]:
    block_minutes = int(float(task.get("_runtime", {}).get("block_minutes") or 0))
    target = max(settings.block_min_minutes, min(settings.block_target_minutes, settings.block_max_minutes))
    remaining = max(target - block_minutes, 0)
    reached_target = block_minutes >= target
    exceeded_max = block_minutes >= settings.block_max_minutes
    return {
        "slice_minutes": settings.checkin_slice_minutes,
        "block_minutes": block_minutes,
        "target_minutes": target,
        "remaining_minutes": remaining,
        "reached_target": reached_target,
        "exceeded_max": exceeded_max,
    }


def analyze_hygiene(tasks: list[dict[str, Any]], settings: Settings) -> HygieneReport:
    duplicate_map: dict[str, list[dict[str, Any]]] = {}
    current_tasks: list[dict[str, Any]] = []
    queue_tasks: list[dict[str, Any]] = []
    missing_resume_pack: list[dict[str, Any]] = []
    resume_pack_issues: list[dict[str, Any]] = []
    stale_queue_tasks: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        title_key = _normalized_title(task.get("name", ""))
        if title_key:
            duplicate_map.setdefault(title_key, []).append(task)

        if _is_current_task(task, settings):
            current_tasks.append(task)

        if _is_active_queue_task(task, settings):
            queue_tasks.append(task)
            missing_markers = _missing_resume_pack_markers(task, settings)
            if missing_markers:
                missing_resume_pack.append(task)
                resume_pack_issues.append(
                    {
                        "id": task.get("id"),
                        "name": task.get("name"),
                        "missing_markers": missing_markers,
                    }
                )

            last_worked_value = field_value(task, settings.field_last_worked_at_name)
            last_worked_at = parse_clickup_datetime(last_worked_value)
            if last_worked_at and (now - last_worked_at).total_seconds() >= settings.stale_queue_hours * 3600:
                stale_queue_tasks.append(task)

    duplicate_groups = [
        sorted(group, key=lambda item: item.get("id", ""))
        for group in duplicate_map.values()
        if len(group) > 1 and any(not is_closed_status(item.get("status", {}).get("status", "")) for item in group)
    ]
    duplicate_groups.sort(key=lambda group: _normalized_title(group[0].get("name", "")))

    warnings: list[str] = []
    if len(current_tasks) == 0:
        warnings.append("No current task is set. Run scheduler to promote one task.")
    if len(current_tasks) > 1:
        warnings.append("Multiple current tasks detected. Scheduler should collapse to one.")
    if len(queue_tasks) < settings.queue_target_min:
        warnings.append(
            f"Active queue is below target ({len(queue_tasks)} < {settings.queue_target_min}). Add candidates from domain lists."
        )
    if len(queue_tasks) > settings.queue_target_max:
        warnings.append(
            f"Active queue exceeds target ({len(queue_tasks)} > {settings.queue_target_max}). Reduce to improve focus."
        )
    if duplicate_groups:
        warnings.append("Potential duplicate tasks detected in Execution Engine.")
    if missing_resume_pack:
        warnings.append("Some active tasks are missing required Resume Pack markers.")

    return HygieneReport(
        current_count=len(current_tasks),
        queue_count=len(queue_tasks),
        missing_fields=[],
        duplicate_title_groups=duplicate_groups,
        missing_resume_pack=missing_resume_pack,
        resume_pack_issues=resume_pack_issues,
        stale_queue_tasks=stale_queue_tasks,
        warnings=warnings,
    )


def detect_missing_fields(fields: list[ClickUpField], settings: Settings) -> list[str]:
    field_names = [field.name for field in fields]
    return evaluate_field_conformance(field_names).missing_required


def task_score(
    task: dict[str, Any],
    fields_by_name: dict[str, ClickUpField],
    now: datetime,
    settings: Settings,
    *,
    current_task_friction: str | None = None,
    current_task_type: str | None = None,
) -> float:
    status = task["status"]["status"]
    if is_closed_status(status):
        return -10000.0
    if is_blocked_status(status, settings.clickup_blocked_status):
        return -5000.0
    if _runtime_switch_cooldown_active(task, now):
        return -1500.0
    scheduler_state = (_read_scheduler_state_name(task, fields_by_name, settings) or "").strip().casefold()
    if scheduler_state == "inbox":
        return -2500.0
    # Hard-exclude blocked tasks regardless of which signal set the state.
    if scheduler_state == "blocked":
        return -5000.0
    if _is_system_control_task(task) and scheduler_state != "current":
        return -4000.0

    next_eligible_value = field_value(task, settings.field_next_eligible_at_name)
    next_eligible = parse_clickup_datetime(next_eligible_value)
    if next_eligible and next_eligible > now:
        return -1000.0

    score = 0.0

    task_type = _dropdown_name(task, fields_by_name, settings.field_task_type_name)
    energy = _dropdown_name(task, fields_by_name, settings.field_energy_pulse_name)
    friction = _dropdown_name(task, fields_by_name, settings.field_friction_pulse_name)
    progress = _dropdown_name(task, fields_by_name, settings.field_progress_pulse_name)

    type_weights = {
        "deep": 34,
        "medium": 20,
        "light": 8,
        "reading": 14,
        "paper": 30,
        "admin": 6,
    }
    energy_weights = {"high": 10, "medium": 5, "low": 0}
    friction_weights = {"none": 10, "some": 0, "high": -10}
    progress_weights = {"high": 8, "medium": 4, "low": 1, "none": 0}
    # ClickUp native priority: 1=Urgent, 2=High, 3=Normal, 4=Low.
    priority_weights = {"1": 30, "2": 20, "3": 0, "4": -10}

    score += type_weights.get(task_type or "", 0)
    score += energy_weights.get(energy or "", 0)
    score += friction_weights.get(friction or "", 0)
    score += progress_weights.get(progress or "", 0)

    raw_priority = str((task.get("priority") or {}).get("priority") or "").strip()
    score += priority_weights.get(raw_priority, 0)

    # Cross-task friction avoidance: if the current task has high friction,
    # penalise candidates of the same type to encourage a domain switch.
    if current_task_friction == "high" and task_type and task_type == current_task_type:
        score -= 20

    local_hour = now.astimezone().hour
    if settings.morning_start_hour <= local_hour < settings.morning_end_hour:
        if task_type == "deep":
            score += settings.morning_deep_bonus
        if task_type == "paper":
            score += settings.morning_paper_bonus
        if task_type in {"medium", "reading"}:
            score -= 4
        if task_type in {"admin", "light"}:
            score -= 14
    elif settings.morning_end_hour <= local_hour < settings.midday_end_hour:
        if task_type == "medium":
            score += settings.midday_medium_bonus
        if task_type == "reading":
            score += settings.midday_reading_bonus
        if task_type in {"deep", "paper"}:
            score += 6
    elif settings.midday_end_hour <= local_hour < settings.evening_end_hour:
        if task_type == "light":
            score += settings.evening_light_bonus
        if task_type == "admin":
            score += settings.evening_admin_bonus
        if task_type == "reading":
            score += 10
        if task_type in {"deep", "paper"}:
            score -= 14
    else:
        if task_type in {"light", "admin"}:
            score += 8
        if task_type in {"deep", "paper"}:
            score -= 20

    if energy == "low":
        if task_type in {"deep", "paper"}:
            score -= 18
        elif task_type == "medium":
            score -= 6
        elif task_type in {"reading", "light", "admin"}:
            score += 8
    elif energy == "high" and task_type in {"deep", "paper"}:
        score += 8

    block_minutes = float(task.get("_runtime", {}).get("block_minutes") or 0)
    if block_minutes >= settings.block_max_minutes:
        score -= settings.fatigue_max_penalty
    elif block_minutes >= settings.block_target_minutes:
        score -= settings.fatigue_target_penalty

    today_minutes = _number(task, settings.field_today_minutes_name)
    block_count = _number(task, settings.field_block_count_today_name)
    score -= today_minutes * 0.35
    score -= block_count * 15

    last_worked_value = field_value(task, settings.field_last_worked_at_name)
    last_worked = parse_clickup_datetime(last_worked_value)
    if last_worked:
        minutes_ago = max((now - last_worked).total_seconds() / 60, 0)
        score += min(minutes_ago / 15, 25)
    else:
        score += 15

    # scheduler_state was already read at the top of the function via
    # _read_scheduler_state_name; reuse it here for the score adjustments.
    if scheduler_state == "queued":
        score += 8
    if scheduler_state == "current":
        score += 5
    if scheduler_state == "done today":
        score -= 20
    if scheduler_state == "break":
        score -= 5
    if scheduler_state == "blocked":
        score -= 30

    if _is_system_control_task(task):
        score -= settings.system_task_penalty

    if scheduler_state == "current":
        if progress == "high" and friction != "high":
            score += settings.current_momentum_bonus
        elif progress == "medium" and friction == "none":
            score += settings.medium_momentum_bonus
        if friction == "high":
            score -= 24
        if block_minutes >= settings.block_max_minutes:
            score -= 22

    return round(score, 2)


async def choose_current_task(tasks: list[dict[str, Any]], fields: list[ClickUpField], settings: Settings) -> SchedulerDecision:
    fields_by_name = field_by_name(fields)
    now = datetime.now(timezone.utc)

    # Identify current task to provide cross-task context for friction avoidance.
    current_task_obj = next(
        (t for t in tasks if _is_current_task(t, settings, fields_by_name)),
        None,
    )
    current_task_friction: str | None = None
    current_task_type: str | None = None
    if current_task_obj is not None:
        current_task_friction = _dropdown_name(current_task_obj, fields_by_name, settings.field_friction_pulse_name)
        current_task_type = _dropdown_name(current_task_obj, fields_by_name, settings.field_task_type_name)

    scores = {
        task["id"]: task_score(
            task,
            fields_by_name,
            now,
            settings,
            current_task_friction=current_task_friction,
            current_task_type=current_task_type,
        )
        for task in tasks
    }
    candidates = [task for task in tasks if scores[task["id"]] > -999]
    current = max(candidates, key=lambda task: scores[task["id"]], default=None)
    return SchedulerDecision(current_task=current, scores=scores)


def score_queue_tasks(
    tasks: list[dict[str, Any]],
    fields: list[ClickUpField],
    settings: Settings,
    *,
    exclude_task_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the top eligible queue tasks with scores, for the switch drawer."""
    fields_by_name = field_by_name(fields)
    now = datetime.now(timezone.utc)
    # Pass the current task's friction/type context so the switch drawer also
    # respects cross-task friction avoidance.
    current_task_obj = next(
        (t for t in tasks if _is_current_task(t, settings, fields_by_name)),
        None,
    )
    current_task_friction: str | None = None
    current_task_type: str | None = None
    if current_task_obj is not None:
        current_task_friction = _dropdown_name(current_task_obj, fields_by_name, settings.field_friction_pulse_name)
        current_task_type = _dropdown_name(current_task_obj, fields_by_name, settings.field_task_type_name)
    scored: list[dict[str, Any]] = []
    for task in tasks:
        if exclude_task_id and task["id"] == exclude_task_id:
            continue
        score = task_score(
            task,
            fields_by_name,
            now,
            settings,
            current_task_friction=current_task_friction,
            current_task_type=current_task_type,
        )
        if score <= -999:
            continue
        task_type = _dropdown_name(task, fields_by_name, settings.field_task_type_name) or ""
        energy = _dropdown_name(task, fields_by_name, settings.field_energy_pulse_name) or ""
        friction = _dropdown_name(task, fields_by_name, settings.field_friction_pulse_name) or ""
        progress = _dropdown_name(task, fields_by_name, settings.field_progress_pulse_name) or ""
        reasons: list[str] = []
        if task_type:
            reasons.append(task_type)
        if progress == "high":
            reasons.append("momentum")
        if friction in {"none", "some"}:
            reasons.append("low friction")
        if energy == "high":
            reasons.append("high energy")
        if not reasons:
            reasons.append("ready")
        scored.append({
            "id": task["id"],
            "name": task.get("name", ""),
            "task_type": task_type,
            "score": round(score, 1),
            "reasons": reasons[:3],
            "status": task.get("status", {}).get("status", ""),
            "url": task.get("url", ""),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


async def sync_scheduler_state(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    tasks: list[dict[str, Any]],
    fields: list[ClickUpField],
    decision: SchedulerDecision,
    status_map: RuntimeStatusMap,
) -> dict[str, Any] | None:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)

    # Rotation score is computed locally and never needs to be read back from
    # ClickUp — writing it on every sync only burns API quota. Skip entirely.

    if not decision.current_task:
        return None

    if not status_map.active_status:
        raise ClickUpError(
            "No valid active status could be resolved for scheduler promotion.",
            status_code=503,
            error_code="STATUS_CONFIG_INVALID",
        )

    current_id = decision.current_task["id"]
    # Round 1 — promote the chosen task; status update + scheduler field in parallel
    current_option = option_by_label(scheduler_field, "Current")
    current_coros: list[Any] = [clickup.update_task(current_id, status=status_map.active_status)]
    if scheduler_field and current_option:
        current_coros.append(clickup.set_custom_field(current_id, scheduler_field.id, current_option.id))
    promotion_results = await asyncio.gather(*current_coros, return_exceptions=True)
    # The status update (index 0) is required — surface any error
    if isinstance(promotion_results[0], BaseException):
        raise promotion_results[0]

    # Round 2 — reset non-current tasks in the background so we can return immediately
    queued_option = option_by_label(scheduler_field, "Queued")
    eligible: list[dict[str, Any]] = []
    for task in tasks:
        if task["id"] == current_id:
            continue
        status = task["status"]["status"]
        if is_closed_status(status) or is_blocked_status(status, settings.clickup_blocked_status):
            continue
        # Preserve Break and Blocked states — do not overwrite back to Queued on sync
        current_sched_state = _read_scheduler_state_name(task, fields_by_name, settings)
        if current_sched_state in {"break", "blocked"}:
            continue
        # Skip no-op writes: task is already in the target state
        already_available = (
            not status_map.available_status
            or task["status"]["status"].strip().casefold() == status_map.available_status.strip().casefold()
        )
        already_queued = current_sched_state == "queued"
        if already_available and already_queued:
            store.clear(task["id"])
            continue
        eligible.append(task)

    async def _reset_eligible_bg() -> None:
        if not eligible:
            return
        reset_coros: list[Any] = []
        for task in eligible:
            if status_map.available_status:
                reset_coros.append(clickup.update_task(task["id"], status=status_map.available_status))
            if scheduler_field and queued_option:
                reset_coros.append(clickup.set_custom_field(task["id"], scheduler_field.id, queued_option.id))
        if reset_coros:
            await asyncio.gather(*reset_coros, return_exceptions=True)
        for task in eligible:
            store.clear(task["id"])

    asyncio.create_task(_reset_eligible_bg())
    return decision.current_task


async def handle_continue(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    task: dict[str, Any],
    fields: list[ClickUpField],
    status_map: RuntimeStatusMap,
    continue_minutes: int,
    *,
    progress: str | None = None,
    energy: str | None = None,
    friction: str | None = None,
) -> dict[str, Any]:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    last_worked_field = fields_by_name.get(settings.field_last_worked_at_name)
    today_minutes_field = fields_by_name.get(settings.field_today_minutes_name)
    current_minutes = _number(task, settings.field_today_minutes_name)
    current_block_minutes = float(task.get("_runtime", {}).get("block_minutes") or 0)
    now_ms = _utc_now_ms()
    increment = settings.checkin_slice_minutes if settings.checkin_slice_minutes > 0 else continue_minutes
    block_started_at = task.get("_runtime", {}).get("block_started_at") or now_ms

    failures: list[str] = []
    field_writes: list[dict[str, Any]] = []
    try:
        await clickup.update_task(task["id"], status=status_map.active_status)
    except ClickUpError as exc:
        raise ClickUpError(
            f"Task status update failed while continuing task: {exc}",
            status_code=exc.status_code,
            error_code=exc.error_code,
            body_preview=exc.body_preview,
            path=exc.path,
        ) from exc
    store.set_many(task["id"], {"block_started_at": block_started_at, "block_minutes": current_block_minutes + increment})
    scheduler_option = option_by_label(scheduler_field, "Current")
    _field_coros: list[Any] = []
    if scheduler_field and scheduler_option:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], scheduler_field.id, scheduler_option.id, failures, "scheduler_state", details=field_writes,
        ))
    if last_worked_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], last_worked_field.id, now_ms, failures, "last_worked_at", time=True, details=field_writes,
        ))
    if today_minutes_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], today_minutes_field.id, current_minutes + increment, failures, "today_minutes", details=field_writes,
        ))
    _field_coros.append(_set_optional_pulses(
        clickup, task, fields_by_name, settings, progress=progress, energy=energy, friction=friction, failures=failures, details=field_writes,
    ))
    await asyncio.gather(*_field_coros)
    return {
        "partial_failure": bool(failures),
        "failures": failures,
        "status_write": {"ok": True, "applied_status": status_map.active_status, "required": True},
        "field_writes": field_writes,
        "primary_write_labels": ["task_status", "scheduler_state"],
    }


async def handle_complete(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    task: dict[str, Any],
    fields: list[ClickUpField],
    status_map: RuntimeStatusMap,
) -> dict[str, Any]:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    last_worked_field = fields_by_name.get(settings.field_last_worked_at_name)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    try:
        await clickup.update_task(task["id"], status=status_map.completed_status)
        applied_status = status_map.completed_status
    except ClickUpError as exc:
        raise ClickUpError(
            f"Completion task status update failed: {exc}",
            status_code=exc.status_code,
            error_code=exc.error_code,
            body_preview=exc.body_preview,
            path=exc.path,
        ) from exc

    store.clear(task["id"])
    failures: list[str] = []
    field_writes: list[dict[str, Any]] = []
    scheduler_option = option_by_label(scheduler_field, "Done today")
    _field_coros: list[Any] = []
    if scheduler_field and scheduler_option:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], scheduler_field.id, scheduler_option.id, failures, "scheduler_state", details=field_writes,
        ))
    if last_worked_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], last_worked_field.id, now_ms, failures, "last_worked_at", time=True, details=field_writes,
        ))
    if _field_coros:
        await asyncio.gather(*_field_coros)
    return {
        "partial_failure": bool(failures),
        "failures": failures,
        "applied_status": applied_status,
        "status_write": {"ok": True, "applied_status": applied_status, "required": True},
        "field_writes": field_writes,
        "primary_write_labels": ["task_status"],
    }


async def handle_break(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    task: dict[str, Any],
    fields: list[ClickUpField],
    status_map: RuntimeStatusMap,
    break_minutes: int,
    *,
    progress: str | None = None,
    energy: str | None = None,
    friction: str | None = None,
) -> dict[str, Any]:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    next_eligible_field = fields_by_name.get(settings.field_next_eligible_at_name)
    last_worked_field = fields_by_name.get(settings.field_last_worked_at_name)
    now = datetime.now(timezone.utc)
    next_eligible = now + timedelta(minutes=break_minutes)

    store.clear(task["id"])
    failures: list[str] = []
    field_writes: list[dict[str, Any]] = []
    scheduler_option = option_by_label(scheduler_field, "Break")
    _field_coros: list[Any] = []
    if scheduler_field and scheduler_option:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], scheduler_field.id, scheduler_option.id, failures, "scheduler_state", details=field_writes,
        ))
    if last_worked_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], last_worked_field.id, int(now.timestamp() * 1000), failures, "last_worked_at", time=True, details=field_writes,
        ))
    if next_eligible_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], next_eligible_field.id, int(next_eligible.timestamp() * 1000), failures, "next_eligible_at", time=True, details=field_writes,
        ))
    _field_coros.append(_set_optional_pulses(
        clickup, task, fields_by_name, settings, progress=progress, energy=energy, friction=friction, failures=failures, details=field_writes,
    ))
    await asyncio.gather(*_field_coros)
    status_write = {"attempted": False, "ok": False, "applied_status": "", "required": False}
    if status_map.available_status:
        try:
            await clickup.update_task(task["id"], status=status_map.available_status)
            status_write = {"attempted": True, "ok": True, "applied_status": status_map.available_status, "required": False}
        except ClickUpError:
            failures.append("task_status_write_failed")
            status_write = {"attempted": True, "ok": False, "applied_status": "", "required": False}
    else:
        failures.extend(_status_warnings_with_prefix(status_map, "task_status_available_"))
        failures.append("task_status_unresolved")
    return {
        "partial_failure": bool(failures),
        "failures": failures,
        "status_write": status_write,
        "field_writes": field_writes,
        "primary_write_labels": ["scheduler_state"],
    }


async def handle_switch(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    task: dict[str, Any],
    fields: list[ClickUpField],
    status_map: RuntimeStatusMap,
    *,
    progress: str | None = None,
    energy: str | None = None,
    friction: str | None = None,
) -> dict[str, Any]:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    last_worked_field = fields_by_name.get(settings.field_last_worked_at_name)
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    cooldown_until_ms = int((now + timedelta(minutes=settings.short_break_minutes)).timestamp() * 1000)

    store.clear(task["id"])
    store.set_many(task["id"], {"switch_cooldown_until": cooldown_until_ms})
    failures: list[str] = []
    field_writes: list[dict[str, Any]] = []
    scheduler_option = option_by_label(scheduler_field, "Queued")
    _field_coros: list[Any] = []
    if scheduler_field and scheduler_option:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], scheduler_field.id, scheduler_option.id, failures, "scheduler_state", details=field_writes,
        ))
    if last_worked_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], last_worked_field.id, now_ms, failures, "last_worked_at", time=True, details=field_writes,
        ))
    _field_coros.append(_set_optional_pulses(
        clickup, task, fields_by_name, settings, progress=progress, energy=energy, friction=friction, failures=failures, details=field_writes,
    ))
    await asyncio.gather(*_field_coros)
    status_write = {"attempted": False, "ok": False, "applied_status": "", "required": False}
    if status_map.available_status:
        try:
            await clickup.update_task(task["id"], status=status_map.available_status)
            status_write = {"attempted": True, "ok": True, "applied_status": status_map.available_status, "required": False}
        except ClickUpError:
            failures.append("task_status_write_failed")
            status_write = {"attempted": True, "ok": False, "applied_status": "", "required": False}
    else:
        failures.extend(_status_warnings_with_prefix(status_map, "task_status_available_"))
        failures.append("task_status_unresolved")
    return {
        "partial_failure": bool(failures),
        "failures": failures,
        "status_write": status_write,
        "field_writes": field_writes,
        "primary_write_labels": ["scheduler_state"],
    }


async def handle_blocked(
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    settings: Settings,
    task: dict[str, Any],
    fields: list[ClickUpField],
    status_map: RuntimeStatusMap,
    cooldown_minutes: int,
    *,
    progress: str | None = None,
    energy: str | None = None,
    friction: str | None = None,
) -> dict[str, Any]:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    next_eligible_field = fields_by_name.get(settings.field_next_eligible_at_name)
    block_count_field = fields_by_name.get(settings.field_block_count_today_name)
    block_count = _number(task, settings.field_block_count_today_name)
    next_eligible = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
    next_eligible_ms = int(next_eligible.timestamp() * 1000)

    store.clear(task["id"])
    failures: list[str] = []
    field_writes: list[dict[str, Any]] = []
    scheduler_option = option_by_label(scheduler_field, "Blocked")
    _field_coros: list[Any] = []
    if scheduler_field and scheduler_option:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], scheduler_field.id, scheduler_option.id, failures, "scheduler_state", details=field_writes,
        ))
    if block_count_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], block_count_field.id, block_count + 1, failures, "block_count_today", details=field_writes,
        ))
    if next_eligible_field:
        _field_coros.append(_set_custom_field_best_effort(
            clickup, task["id"], next_eligible_field.id, next_eligible_ms, failures, "next_eligible_at", time=True, details=field_writes,
        ))
    _field_coros.append(_set_optional_pulses(
        clickup, task, fields_by_name, settings, progress=progress, energy=energy, friction=friction, failures=failures, details=field_writes,
    ))
    await asyncio.gather(*_field_coros)
    blocked_status = status_map.blocked_status or status_map.available_status
    status_write = {"attempted": False, "ok": False, "applied_status": "", "required": False}
    if blocked_status:
        try:
            await clickup.update_task(task["id"], status=blocked_status)
            status_write = {"attempted": True, "ok": True, "applied_status": blocked_status, "required": False}
        except ClickUpError:
            failures.append("task_status_write_failed")
            status_write = {"attempted": True, "ok": False, "applied_status": "", "required": False}
    else:
        failures.extend(_status_warnings_with_prefix(status_map, "task_status_available_"))
        failures.append("task_status_unresolved")
    return {
        "partial_failure": bool(failures),
        "failures": failures,
        "status_write": status_write,
        "field_writes": field_writes,
        "primary_write_labels": ["scheduler_state"],
    }


async def _set_optional_pulses(
    clickup: ClickUpClient,
    task: dict[str, Any],
    fields_by_name: dict[str, ClickUpField],
    settings: Settings,
    *,
    progress: str | None,
    energy: str | None,
    friction: str | None,
    failures: list[str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> None:
    await asyncio.gather(
        _set_optional_dropdown(
            clickup,
            task["id"],
            fields_by_name,
            settings.field_progress_pulse_name,
            progress,
            failures=failures,
            failure_label="progress_pulse",
            details=details,
        ),
        _set_optional_dropdown(
            clickup,
            task["id"],
            fields_by_name,
            settings.field_energy_pulse_name,
            energy,
            failures=failures,
            failure_label="energy_pulse",
            details=details,
        ),
        _set_optional_dropdown(
            clickup,
            task["id"],
            fields_by_name,
            settings.field_friction_pulse_name,
            friction,
            failures=failures,
            failure_label="friction_pulse",
            details=details,
        ),
    )


async def _set_optional_dropdown(
    clickup: ClickUpClient,
    task_id: str,
    fields_by_name: dict[str, ClickUpField],
    field_name: str,
    label: str | None,
    *,
    failures: list[str] | None = None,
    failure_label: str | None = None,
    details: list[dict[str, Any]] | None = None,
) -> None:
    if not label:
        return
    field = fields_by_name.get(field_name)
    if not field:
        return
    option = dropdown_options(field).get(label)
    if not option:
        return
    try:
        await clickup.set_custom_field(task_id, field.id, option.id)
        if details is not None:
            details.append({"label": failure_label or field_name, "ok": True, "field_id": field.id})
    except Exception:
        if failures is not None:
            failures.append(failure_label or field_name)
        if details is not None:
            details.append({"label": failure_label or field_name, "ok": False, "field_id": field.id})


async def _set_custom_field_best_effort(
    clickup: ClickUpClient,
    task_id: str,
    field_id: str,
    value: Any,
    failures: list[str],
    failure_label: str,
    *,
    time: bool | None = None,
    details: list[dict[str, Any]] | None = None,
) -> None:
    try:
        await clickup.set_custom_field(task_id, field_id, value, time=time)
        if details is not None:
            details.append({"label": failure_label, "ok": True, "field_id": field_id})
    except Exception:
        failures.append(failure_label)
        if details is not None:
            details.append({"label": failure_label, "ok": False, "field_id": field_id})
