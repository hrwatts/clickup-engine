from __future__ import annotations

import os
from dataclasses import dataclass

from app.clickup import normalize_clickup_list_id


@dataclass(frozen=True)
class Settings:
    clickup_token: str
    clickup_workspace_id: str
    clickup_list_id: str
    clickup_list_name: str
    clickup_space_id: str
    clickup_webhook_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_webhook_secret: str
    public_base_url: str
    app_shared_secret: str
    session_secret: str
    session_cookie_name: str
    session_max_age_seconds: int
    session_cookie_secure: bool
    session_cookie_samesite: str
    login_rate_limit_attempts: int
    login_rate_limit_window_seconds: int
    clickup_open_status: str
    clickup_current_status: str
    clickup_completed_status: str
    clickup_blocked_status: str
    checkin_slice_minutes: int
    block_target_minutes: int
    block_min_minutes: int
    block_max_minutes: int
    queue_target_min: int
    queue_target_max: int
    stale_queue_hours: int
    resume_pack_required_markers: tuple[str, ...]
    field_scheduler_state_name: str
    field_task_type_name: str
    field_progress_pulse_name: str
    field_energy_pulse_name: str
    field_friction_pulse_name: str
    field_block_count_today_name: str
    field_last_worked_at_name: str
    field_next_eligible_at_name: str
    field_today_minutes_name: str
    field_rotation_score_name: str
    enable_builtin_scheduler: bool
    scheduler_tick_seconds: int
    scheduler_min_interval_minutes: int
    workday_start_hour: int
    workday_end_hour: int
    workday_weekdays: tuple[int, ...]
    enable_daily_summary: bool
    daily_summary_hour: int
    enable_weekly_summary: bool
    weekly_summary_weekday: int
    weekly_summary_hour: int
    morning_start_hour: int
    morning_end_hour: int
    midday_end_hour: int
    evening_end_hour: int
    morning_deep_bonus: int
    morning_paper_bonus: int
    midday_medium_bonus: int
    midday_reading_bonus: int
    evening_light_bonus: int
    evening_admin_bonus: int
    current_momentum_bonus: int
    medium_momentum_bonus: int
    fatigue_target_penalty: int
    fatigue_max_penalty: int
    system_task_penalty: int
    pipeline_space_name: str
    pipeline_folder_name: str
    default_continue_minutes: int = 20
    short_break_minutes: int = 10
    long_break_minutes: int = 20
    blocked_cooldown_minutes: int = 90


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _require_any(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    joined = ", ".join(names)
    raise RuntimeError(f"Missing required environment variable: one of {joined}")


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_weekdays(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if 0 <= value <= 6:
            values.append(value)
    return tuple(sorted(set(values))) or default


def _as_markers(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    markers = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(markers) if markers else default


def load_settings() -> Settings:
    checkin_slice = max(5, int(os.getenv("CHECKIN_SLICE_MINUTES", "20")))
    block_min = max(20, int(os.getenv("BLOCK_MIN_MINUTES", "40")))
    block_max = max(block_min, int(os.getenv("BLOCK_MAX_MINUTES", "80")))
    block_target = int(os.getenv("BLOCK_TARGET_MINUTES", "60"))
    if block_target < block_min:
        block_target = block_min
    if block_target > block_max:
        block_target = block_max
    queue_min = max(1, int(os.getenv("QUEUE_TARGET_MIN", "3")))
    queue_max = max(queue_min, int(os.getenv("QUEUE_TARGET_MAX", "7")))
    stale_queue_hours = max(1, int(os.getenv("STALE_QUEUE_HOURS", "72")))
    scheduler_tick_seconds = max(15, int(os.getenv("SCHEDULER_TICK_SECONDS", "60")))
    scheduler_min_interval_minutes = max(1, int(os.getenv("SCHEDULER_MIN_INTERVAL_MINUTES", "5")))
    workday_start_hour = max(0, min(23, int(os.getenv("WORKDAY_START_HOUR", "7"))))
    workday_end_hour = max(0, min(23, int(os.getenv("WORKDAY_END_HOUR", "22"))))
    daily_summary_hour = max(0, min(23, int(os.getenv("DAILY_SUMMARY_HOUR", "21"))))
    weekly_summary_hour = max(0, min(23, int(os.getenv("WEEKLY_SUMMARY_HOUR", "18"))))
    weekly_summary_weekday = max(0, min(6, int(os.getenv("WEEKLY_SUMMARY_WEEKDAY", "6"))))
    morning_start_hour = max(0, min(23, int(os.getenv("MORNING_START_HOUR", "6"))))
    morning_end_hour = max(0, min(23, int(os.getenv("MORNING_END_HOUR", "12"))))
    midday_end_hour = max(0, min(23, int(os.getenv("MIDDAY_END_HOUR", "17"))))
    evening_end_hour = max(0, min(23, int(os.getenv("EVENING_END_HOUR", "22"))))

    clickup_workspace_id = os.getenv("CLICKUP_WORKSPACE_ID", "").strip()
    clickup_list_id = normalize_clickup_list_id(os.getenv("CLICKUP_LIST_ID", "").strip())
    clickup_list_name = os.getenv("CLICKUP_LIST_NAME", "").strip()
    if not clickup_list_id and not clickup_list_name:
        raise RuntimeError("Set CLICKUP_LIST_ID or CLICKUP_LIST_NAME.")
    if clickup_list_name and not clickup_workspace_id:
        raise RuntimeError("Set CLICKUP_WORKSPACE_ID when using CLICKUP_LIST_NAME.")

    session_cookie_secure = _as_bool("SESSION_COOKIE_SECURE", _require("PUBLIC_BASE_URL").startswith("https://"))
    session_cookie_samesite = os.getenv("SESSION_COOKIE_SAMESITE", "lax").strip().lower() or "lax"
    if session_cookie_samesite not in {"lax", "strict"}:
        session_cookie_samesite = "lax"

    return Settings(
        clickup_token=_require_any("CLICKUP_API_TOKEN", "CLICKUP_TOKEN"),
        clickup_workspace_id=clickup_workspace_id,
        clickup_list_id=clickup_list_id,
        clickup_list_name=clickup_list_name,
        clickup_space_id=os.getenv("CLICKUP_SPACE_ID", "").strip(),
        clickup_webhook_secret=os.getenv("CLICKUP_WEBHOOK_SECRET", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip(),
        public_base_url=_require("PUBLIC_BASE_URL").rstrip("/"),
        app_shared_secret=_require("APP_SHARED_SECRET"),
        session_secret=_require("SESSION_SECRET"),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "execution_engine_session").strip() or "execution_engine_session",
        session_max_age_seconds=max(300, int(os.getenv("SESSION_MAX_AGE_SECONDS", "2592000"))),
        session_cookie_secure=session_cookie_secure,
        session_cookie_samesite=session_cookie_samesite,
        login_rate_limit_attempts=max(1, int(os.getenv("LOGIN_RATE_LIMIT_ATTEMPTS", "5"))),
        login_rate_limit_window_seconds=max(60, int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "900"))),
        clickup_open_status=os.getenv("CLICKUP_OPEN_STATUS", "To do").strip() or "To do",
        clickup_current_status=os.getenv("CLICKUP_CURRENT_STATUS", "In progress").strip() or "In progress",
        clickup_completed_status=os.getenv("CLICKUP_COMPLETED_STATUS", "Complete").strip() or "Complete",
        clickup_blocked_status=os.getenv("CLICKUP_BLOCKED_STATUS", "").strip(),
        checkin_slice_minutes=checkin_slice,
        block_target_minutes=block_target,
        block_min_minutes=block_min,
        block_max_minutes=block_max,
        queue_target_min=queue_min,
        queue_target_max=queue_max,
        stale_queue_hours=stale_queue_hours,
        resume_pack_required_markers=_as_markers(
            "RESUME_PACK_REQUIRED_MARKERS",
            ("Resume Pack", "Outcome:", "Next Step:", "Re-entry Cue:", "Context:"),
        ),
        field_scheduler_state_name=os.getenv("FIELD_SCHEDULER_STATE_NAME", "Scheduler State").strip() or "Scheduler State",
        field_task_type_name=os.getenv("FIELD_TASK_TYPE_NAME", "Task Type").strip() or "Task Type",
        field_progress_pulse_name=os.getenv("FIELD_PROGRESS_PULSE_NAME", "Progress Pulse").strip() or "Progress Pulse",
        field_energy_pulse_name=os.getenv("FIELD_ENERGY_PULSE_NAME", "Energy Pulse").strip() or "Energy Pulse",
        field_friction_pulse_name=os.getenv("FIELD_FRICTION_PULSE_NAME", "Friction Pulse").strip() or "Friction Pulse",
        field_block_count_today_name=os.getenv("FIELD_BLOCK_COUNT_TODAY_NAME", "Block Count Today").strip() or "Block Count Today",
        field_last_worked_at_name=os.getenv("FIELD_LAST_WORKED_AT_NAME", "Last Worked At").strip() or "Last Worked At",
        field_next_eligible_at_name=os.getenv("FIELD_NEXT_ELIGIBLE_AT_NAME", "Next Eligible At").strip() or "Next Eligible At",
        field_today_minutes_name=os.getenv("FIELD_TODAY_MINUTES_NAME", "Today Minutes").strip() or "Today Minutes",
        field_rotation_score_name=os.getenv("FIELD_ROTATION_SCORE_NAME", "Rotation Score").strip() or "Rotation Score",
        enable_builtin_scheduler=_as_bool("ENABLE_BUILTIN_SCHEDULER", True),
        scheduler_tick_seconds=scheduler_tick_seconds,
        scheduler_min_interval_minutes=scheduler_min_interval_minutes,
        workday_start_hour=workday_start_hour,
        workday_end_hour=workday_end_hour,
        workday_weekdays=_as_weekdays("WORKDAY_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6)),
        enable_daily_summary=_as_bool("ENABLE_DAILY_SUMMARY", True),
        daily_summary_hour=daily_summary_hour,
        enable_weekly_summary=_as_bool("ENABLE_WEEKLY_SUMMARY", False),
        weekly_summary_weekday=weekly_summary_weekday,
        weekly_summary_hour=weekly_summary_hour,
        morning_start_hour=morning_start_hour,
        morning_end_hour=morning_end_hour,
        midday_end_hour=midday_end_hour,
        evening_end_hour=evening_end_hour,
        morning_deep_bonus=int(os.getenv("MORNING_DEEP_BONUS", "30")),
        morning_paper_bonus=int(os.getenv("MORNING_PAPER_BONUS", "34")),
        midday_medium_bonus=int(os.getenv("MIDDAY_MEDIUM_BONUS", "18")),
        midday_reading_bonus=int(os.getenv("MIDDAY_READING_BONUS", "20")),
        evening_light_bonus=int(os.getenv("EVENING_LIGHT_BONUS", "16")),
        evening_admin_bonus=int(os.getenv("EVENING_ADMIN_BONUS", "18")),
        current_momentum_bonus=int(os.getenv("CURRENT_MOMENTUM_BONUS", "22")),
        medium_momentum_bonus=int(os.getenv("MEDIUM_MOMENTUM_BONUS", "10")),
        fatigue_target_penalty=int(os.getenv("FATIGUE_TARGET_PENALTY", "18")),
        fatigue_max_penalty=int(os.getenv("FATIGUE_MAX_PENALTY", "42")),
        system_task_penalty=int(os.getenv("SYSTEM_TASK_PENALTY", "60")),
        pipeline_space_name=os.getenv("PIPELINE_SPACE_NAME", "").strip(),
        pipeline_folder_name=os.getenv("PIPELINE_FOLDER_NAME", "").strip(),
        default_continue_minutes=int(os.getenv("DEFAULT_CONTINUE_MINUTES", "20")),
        short_break_minutes=int(os.getenv("SHORT_BREAK_MINUTES", "10")),
        long_break_minutes=int(os.getenv("LONG_BREAK_MINUTES", "20")),
        blocked_cooldown_minutes=int(os.getenv("BLOCKED_COOLDOWN_MINUTES", "90")),
    )
