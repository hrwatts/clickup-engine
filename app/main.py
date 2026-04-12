from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import hmac
import json
import logging
from json import JSONDecodeError
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import time
from typing import Any, Optional

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.clickup import ClickUpClient, ClickUpConfigError, ClickUpError, field_by_name, field_value, option_by_label
from app.conformance import TOPOLOGY_DECISION, build_minimum_viable_guidance, evaluate_field_conformance
from app.config import Settings, load_settings
from app.notifications import NoopNotifier, NotificationError, TelegramNotifier
from app.operational_state import OperationalState, build_operational_state, classify_source_failure, operational_state_from_dict
from app.scheduler import (
    analyze_hygiene,
    block_progress,
    choose_current_task,
    detect_missing_fields,
    handle_break,
    handle_blocked,
    handle_complete,
    handle_continue,
    handle_switch,
    resolve_runtime_status_map,
    score_queue_tasks,
    task_score,
    sync_scheduler_state,
)
from app.store import RuntimeSessionStore


ALLOWED_PROGRESS = {"none", "low", "medium", "high"}
ALLOWED_ENERGY = {"low", "medium", "high"}
ALLOWED_FRICTION = {"none", "some", "high"}
ALLOWED_ACTIONS = {"continue", "complete", "break", "blocked", "switch"}
ALLOWED_QUICK_ADD_TYPES = {"deep", "medium", "light", "reading", "paper", "admin"}
SESSION_VERSION = "v1"
logger = logging.getLogger(__name__)


def minify_html(html_str: str) -> str:
    """Minify HTML/CSS/JS by removing unnecessary whitespace and comments.
    
    Reduces payload size by ~12-20% without adding external dependencies.
    Preserves functionality and text content while improving network performance.
    """
    import re
    # Remove HTML comments
    html_str = re.sub(r'<!--.*?-->', '', html_str, flags=re.DOTALL)
    # Remove whitespace between tags (but keep single space where needed)
    html_str = re.sub(r'>\s+<', '><', html_str)
    # Remove leading/trailing whitespace from lines
    html_str = re.sub(r'\n\s+', '\n', html_str)
    # Remove multiple newlines
    html_str = re.sub(r'\n\n+', '\n', html_str)
    # Minify inline CSS - only in style="..." attributes
    # Match style=" followed by CSS content
    def minify_css_attr(match):
        css = match.group(1)
        # Remove spaces around CSS operators
        css = re.sub(r':\s+', ':', css)
        css = re.sub(r';\s+', ';', css)
        css = re.sub(r'\s+([{};,])\s+', r'\1', css)
        return f'style="{css}"'
    html_str = re.sub(r'style="([^"]*)"', minify_css_attr, html_str)
    # Minify <style> blocks
    def minify_style_block(match):
        css = match.group(1)
        # Remove CSS comments
        css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
        # Minify: remove spaces around operators
        css = re.sub(r':\s+', ':', css)
        css = re.sub(r';\s+', ';', css)
        css = re.sub(r'\s+([{};,])\s+', r'\1', css)
        css = re.sub(r'\n', '', css)
        return f'<style>{css}</style>'
    html_str = re.sub(r'<style>(.*?)</style>', minify_style_block, html_str, flags=re.DOTALL)
    return html_str.strip()


def _build_current_task_resolution_state(current_invariant: dict[str, Any], eligible_count: int) -> tuple[str, str]:
    status = str(current_invariant.get("status") or "")
    if status == "one_current":
        return ("current_present", "Open Check-in.")
    if status == "multi_current":
        return ("multi_current_violation", "Run runtime remediation to keep exactly one current task.")
    if eligible_count > 0:
        return (
            "zero_current_candidates_available",
            "Open Check-in to trigger deterministic auto-selection, or run scheduler now.",
        )
    return (
        "zero_current_no_eligible_candidates",
        "No eligible task exists yet; adjust task states and retry scheduler.",
    )


def _annotate_operational_snapshot(
    payload: dict[str, Any],
    operational_state: OperationalState,
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["data_freshness"] = operational_state.data_freshness
    enriched["snapshot_timestamp"] = operational_state.snapshot_timestamp
    enriched["retry_recommended"] = operational_state.retry_recommended
    enriched["retryable_failure"] = operational_state.retryable_failure
    enriched["usable_despite_failure"] = operational_state.usable_despite_failure
    enriched["source_failure"] = operational_state.source_failure
    enriched["current_task_resolution_state"] = operational_state.current_task_resolution_state
    enriched["current_task_resolution_next_action"] = operational_state.current_task_resolution_next_action
    enriched["promotion_attempted"] = operational_state.promotion_attempted
    enriched["promotion_verified"] = operational_state.promotion_verified
    enriched["operational_state"] = operational_state.as_dict()
    return enriched


def _save_last_known_operational_snapshot(app: FastAPI, payload: dict[str, Any]) -> None:
    app.state.last_known_operational_snapshot = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "payload": payload,
    }


ACTION_VERIFY_TIMEOUT_SECONDS = 2.5
ACTION_FOLLOWUP_TIMEOUT_SECONDS = 2.5


def _scheduler_state_label(task: dict[str, Any], fields: list[Any], settings: Settings) -> str:
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    if not scheduler_field:
        return ""
    raw_value = field_value(task, settings.field_scheduler_state_name)
    for option in scheduler_field.type_config.get("options", []):
        if option.get("id") == raw_value:
            return str(option.get("name") or "")
    return ""


async def _read_task_for_verification(clickup: ClickUpClient, task_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        task = await asyncio.wait_for(clickup.get_task(task_id), timeout=ACTION_VERIFY_TIMEOUT_SECONDS)
        return task, None
    except asyncio.TimeoutError:
        return None, "verification_timeout"
    except ClickUpError:
        return None, "verification_read_failed"


def _classify_failure_groups(failures: list[str]) -> dict[str, list[str]]:
    groups = {
        "task_status": [],
        "scheduler_state": [],
        "pulse_metrics": [],
        "verification": [],
        "followup": [],
        "other": [],
    }
    for item in failures:
        if item in {"task_status_write_failed", "task_status_unresolved"} or item.startswith("task_status_"):
            groups["task_status"].append(item)
        elif item == "scheduler_state" or item.endswith("scheduler_state"):
            groups["scheduler_state"].append(item)
        elif item in {"last_worked_at", "today_minutes", "next_eligible_at", "block_count_today", "progress_pulse", "energy_pulse", "friction_pulse"}:
            groups["pulse_metrics"].append(item)
        elif item.startswith("verification_") or item == "refresh_after_continue":
            groups["verification"].append(item)
        elif item.startswith("scheduler_followup"):
            groups["followup"].append(item)
        else:
            groups["other"].append(item)
    return groups


def _field_write_succeeded(action_result: dict[str, Any], label: str) -> bool:
    field_writes = list(action_result.get("field_writes") or [])
    matching = [item for item in field_writes if str(item.get("label") or "") == label]
    return bool(matching) and any(item.get("ok") is True for item in matching)


def _primary_write_succeeded(action_result: dict[str, Any]) -> bool:
    primary_labels = list(action_result.get("primary_write_labels") or [])
    status_write = dict(action_result.get("status_write") or {})
    for label in primary_labels:
        if label == "task_status":
            if status_write.get("ok") is not True:
                return False
            continue
        if not _field_write_succeeded(action_result, label):
            return False
    return True


def _task_ref(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not task:
        return None
    return {
        "id": str(task.get("id") or ""),
        "name": str(task.get("name") or ""),
        "status": str(task.get("status", {}).get("status", "")),
        "url": str(task.get("url") or ""),
    }


async def _read_current_slot_state(
    clickup: ClickUpClient,
    list_id: str,
    store: RuntimeSessionStore,
    settings: Settings,
) -> dict[str, Any]:
    try:
        tasks = store.attach_many(
            await asyncio.wait_for(clickup.get_list_tasks(list_id), timeout=ACTION_VERIFY_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        return {
            "followup_read_state": "timeout",
            "invariant": None,
            "current_task": None,
            "current_task_ref": None,
            "tasks": [],
        }
    except ClickUpError:
        return {
            "followup_read_state": "failed",
            "invariant": None,
            "current_task": None,
            "current_task_ref": None,
            "tasks": [],
        }
    invariant = detect_current_task_invariant(tasks, settings)
    current_task = None
    if invariant.get("status") == "one_current":
        current_id = str((invariant.get("task_ids") or [""])[0] or "")
        current_task = next((item for item in tasks if str(item.get("id") or "") == current_id), None)
    return {
        "followup_read_state": "succeeded",
        "invariant": invariant,
        "current_task": current_task,
        "current_task_ref": _task_ref(current_task),
        "tasks": tasks,
    }


def _derive_next_task_resolution_state(
    *,
    action: str,
    current_task_before: dict[str, Any] | None,
    current_state: dict[str, Any],
    scheduler_resolution_state: str,
) -> str:
    if action == "continue":
        return "not_applicable"
    if scheduler_resolution_state == "deferred":
        return "deferred"
    if current_state.get("followup_read_state") != "succeeded":
        return "unverified"
    invariant = current_state.get("invariant") or {}
    before_id = str((current_task_before or {}).get("id") or "")
    after_id = str((current_state.get("current_task_ref") or {}).get("id") or "")
    if invariant.get("status") == "multi_current":
        return "multi_current"
    if invariant.get("status") == "zero_current":
        return "zero_current"
    if after_id and after_id != before_id:
        return "resolved"
    if after_id == before_id:
        return "unchanged"
    return "unverified"


def _derive_action_semantics(
    *,
    action: str,
    action_result: dict[str, Any],
    verification_status: str,
    current_task_before: dict[str, Any] | None,
    current_task_after: dict[str, Any] | None,
    next_task_resolution_state: str,
) -> tuple[bool, bool, str]:
    primary_write_succeeded = _primary_write_succeeded(action_result)
    status_write = dict(action_result.get("status_write") or {})
    before_id = str((current_task_before or {}).get("id") or "")
    after_id = str((current_task_after or {}).get("id") or "")
    transition_verified = False
    if action == "continue":
        primary_write_succeeded = status_write.get("ok") is True
        transition_verified = bool(after_id and after_id == before_id and verification_status == "failed")
    elif action in {"break", "blocked", "switch", "complete"}:
        transition_verified = next_task_resolution_state in {"resolved", "zero_current"}
    direct_verified = verification_status == "verified"
    post_write_verified = direct_verified or transition_verified
    probable_success = verification_status == "unverified" or next_task_resolution_state in {"deferred", "unverified"}
    action_semantically_succeeded = primary_write_succeeded and (post_write_verified or probable_success)
    if not action_semantically_succeeded:
        return False, False, "verified_failure"
    if post_write_verified:
        if bool(action_result.get("failures")) or next_task_resolution_state == "deferred":
            return True, True, "verified_partial_success"
        return True, True, "verified_success"
    return True, False, "unverified_but_probable_success"


def _compose_ui_message(
    *,
    action: str,
    action_semantically_succeeded: bool,
    semantic_outcome: str,
    post_write_verified: bool,
    followup_read_succeeded: bool,
    next_task_resolution_succeeded: bool,
    next_task_resolution_state: str,
    current_task_closed: bool,
    partial_failure: bool,
    has_warnings: bool,
) -> tuple[str, str]:
    action_titles = {
        "continue": "Continue slice saved.",
        "break": "Break started.",
        "switch": "Switched away from the current task.",
        "blocked": "Task marked blocked.",
        "complete": "Task completed.",
    }
    base = action_titles.get(action, "Action processed.")
    if not action_semantically_succeeded:
        return ("The action could not be verified.", "error")
    if semantic_outcome == "unverified_but_probable_success":
        probable_messages = {
            "continue": "Continue saved. Secondary verification is still pending.",
            "break": "Break started. Next task verification is still pending.",
            "switch": "Switch applied. Next task verification is still pending.",
            "blocked": "Blocked applied. A follow-up check is still pending.",
            "complete": "Task completed. Follow-up verification is still pending.",
        }
        return (probable_messages.get(action, base + " Verification is still pending."), "warn")
    if not post_write_verified and not followup_read_succeeded:
        return (base + " Follow-up read could not be completed yet. Reload to confirm.", "warn")
    if current_task_closed and not next_task_resolution_succeeded:
        return (base + " Reloading check-in to resolve the next task.", "warn")
    if partial_failure:
        return (base + " Some secondary updates still need attention.", "warn")
    if has_warnings:
        return (base + " Some optional updates could not be confirmed.", "warn")
    if action in {"break", "blocked", "switch", "complete"} and next_task_resolution_state == "zero_current":
        return (base + " No next task was eligible yet.", "success")
    if action == "complete" and next_task_resolution_succeeded:
        return ("Task completed. Loading next task.", "success")
    if action == "blocked" and next_task_resolution_succeeded:
        return ("Task marked blocked. Loading next task.", "success")
    if action == "switch" and next_task_resolution_succeeded:
        return ("Switching to the next task.", "success")
    if not post_write_verified:
        return (base + " Verification is still pending.", "warn")
    return (base, "success")


def _build_action_result(
    *,
    action: str,
    message: str,
    block: dict[str, Any] | None,
    redirect_to: str | None,
    next_task: dict[str, Any] | None,
    action_result: dict[str, Any],
    verification_status: str,
    verification_details: list[str] | None = None,
    extra_failures: list[str] | None = None,
    current_task_before: dict[str, Any] | None = None,
    current_task_after: dict[str, Any] | None = None,
    followup_read_state: str = "not_needed",
    current_task_closed: bool = False,
    next_task_resolution_state: str = "not_applicable",
) -> dict[str, Any]:
    failures = list(action_result.get("failures") or [])
    if extra_failures:
        failures.extend(extra_failures)
    failure_groups = _classify_failure_groups(failures)
    action_semantically_succeeded, post_write_verified, semantic_outcome = _derive_action_semantics(
        action=action,
        action_result=action_result,
        verification_status=verification_status,
        current_task_before=current_task_before,
        current_task_after=current_task_after,
        next_task_resolution_state=next_task_resolution_state,
    )
    followup_read_succeeded = followup_read_state in {"succeeded", "not_needed"}
    next_task_resolution_succeeded = next_task_resolution_state in {"resolved", "zero_current", "not_applicable"}
    partial_failure = semantic_outcome == "verified_partial_success"
    has_warnings = bool(failures)
    ui_message, ui_severity = _compose_ui_message(
        action=action,
        action_semantically_succeeded=action_semantically_succeeded,
        semantic_outcome=semantic_outcome,
        post_write_verified=post_write_verified,
        followup_read_succeeded=followup_read_succeeded,
        next_task_resolution_succeeded=next_task_resolution_succeeded,
        next_task_resolution_state=next_task_resolution_state,
        current_task_closed=current_task_closed,
        partial_failure=partial_failure,
        has_warnings=has_warnings,
    )
    return {
        "ok": action_semantically_succeeded,
        "message": message,
        "action": action,
        "block": block,
        "redirect_to": redirect_to,
        "next_task": next_task,
        "partial_failure": partial_failure,
        "warnings": failures,
        "semantic_outcome": semantic_outcome,
        "verification_state": semantic_outcome,
        "primary_write_succeeded": _primary_write_succeeded(action_result),
        "secondary_write_failures": failures,
        "current_task_before": current_task_before,
        "current_task_after": current_task_after,
        "action_semantically_succeeded": action_semantically_succeeded,
        "post_write_verified": post_write_verified,
        "followup_read_succeeded": followup_read_succeeded,
        "followup_read_state": followup_read_state,
        "next_task_resolution_succeeded": next_task_resolution_succeeded,
        "next_task_resolution_state": next_task_resolution_state,
        "current_task_closed": current_task_closed,
        "ui_message": ui_message,
        "ui_severity": ui_severity,
        "redirect_mode": "redirect" if redirect_to else "stay",
        "retry_recommended": semantic_outcome == "unverified_but_probable_success",
        "reload_recommended": semantic_outcome == "unverified_but_probable_success" or next_task_resolution_state == "deferred",
        "verification_status": verification_status,
        "verification_details": list(verification_details or []),
        "write_details": {
            "status_write": dict(action_result.get("status_write") or {}),
            "field_writes": list(action_result.get("field_writes") or []),
            "primary_write_labels": list(action_result.get("primary_write_labels") or []),
            "failure_groups": failure_groups,
        },
    }


async def _verify_continue_result(
    clickup: ClickUpClient,
    task_id: str,
    fields: list[Any],
    settings: Settings,
    status_map: Any,
) -> tuple[str, list[str], dict[str, Any] | None]:
    task, error = await _read_task_for_verification(clickup, task_id)
    if not task:
        return "unverified", [error or "verification_unavailable"], None
    scheduler_state = _scheduler_state_label(task, fields, settings).strip().casefold()
    native_status = str(task.get("status", {}).get("status", "")).strip().casefold()
    if scheduler_state == "current" or (status_map.active_status and native_status == status_map.active_status.strip().casefold()):
        return "verified", [], task
    return "failed", ["verification_mismatch"], task


async def _verify_scheduler_state_result(
    clickup: ClickUpClient,
    task_id: str,
    fields: list[Any],
    settings: Settings,
    expected_state: str,
) -> tuple[str, list[str], dict[str, Any] | None]:
    task, error = await _read_task_for_verification(clickup, task_id)
    if not task:
        return "unverified", [error or "verification_unavailable"], None
    scheduler_state = _scheduler_state_label(task, fields, settings).strip().casefold()
    if scheduler_state == expected_state.strip().casefold():
        return "verified", [], task
    return "failed", ["verification_mismatch"], task


async def _verify_complete_result(
    clickup: ClickUpClient,
    task_id: str,
    fields: list[Any],
    settings: Settings,
    status_map: Any,
) -> tuple[str, list[str], dict[str, Any] | None]:
    task, error = await _read_task_for_verification(clickup, task_id)
    if not task:
        return "unverified", [error or "verification_unavailable"], None
    scheduler_state = _scheduler_state_label(task, fields, settings).strip().casefold()
    native_status = str(task.get("status", {}).get("status", "")).strip().casefold()
    completed_matches = status_map.completed_status and native_status == status_map.completed_status.strip().casefold()
    if completed_matches or scheduler_state == "done today":
        return "verified", [], task
    return "failed", ["verification_mismatch"], task


def verify_clickup_signature(secret: str, body: bytes, signature: Optional[str]) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_shared_secret(expected: str, provided: Optional[str]) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def _cookie_signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_session_cookie(settings: Settings) -> str:
    issued_at = int(time.time())
    payload = f"{SESSION_VERSION}:{issued_at}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = _cookie_signature(settings.session_secret, payload)
    return f"{encoded}.{signature}"


def parse_session_cookie(settings: Settings, cookie_value: str | None) -> bool:
    if not cookie_value:
        return False
    try:
        encoded, provided_sig = cookie_value.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expected_sig = _cookie_signature(settings.session_secret, payload)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return False
        version, issued_at_text = payload.decode("utf-8").split(":", 1)
        if version != SESSION_VERSION:
            return False
        issued_at = int(issued_at_text)
    except (ValueError, UnicodeDecodeError):
        return False
    return time.time() - issued_at <= settings.session_max_age_seconds


def is_authenticated(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(settings.session_cookie_name)
    return parse_session_cookie(settings, cookie)


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def login_allowed(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    now = time.time()
    attempts: dict[str, list[float]] = request.app.state.login_attempts
    key = get_client_ip(request)
    window_start = now - settings.login_rate_limit_window_seconds
    history = [ts for ts in attempts.get(key, []) if ts >= window_start]
    attempts[key] = history
    return len(history) < settings.login_rate_limit_attempts


def record_login_attempt(request: Request) -> None:
    attempts: dict[str, list[float]] = request.app.state.login_attempts
    key = get_client_ip(request)
    attempts.setdefault(key, []).append(time.time())


def clear_login_attempts(request: Request) -> None:
    attempts: dict[str, list[float]] = request.app.state.login_attempts
    attempts.pop(get_client_ip(request), None)


def set_session_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=build_session_cookie(settings),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def redirect_to_login() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def require_session(request: Request, *, redirect: bool = False) -> Response | None:
    if is_authenticated(request):
        return None
    if redirect:
        return redirect_to_login()
    raise HTTPException(status_code=401, detail="Unauthorized")


def service_ready(request: Request) -> bool:
    return not bool(getattr(request.app.state, "startup_error", ""))


def require_ready(request: Request) -> None:
    if service_ready(request):
        return
    detail = getattr(request.app.state, "startup_error_detail", "") or "Service is not fully configured yet."
    raise HTTPException(status_code=503, detail=detail)


def describe_clickup_error(exc: ClickUpError) -> str:
    if exc.status_code == 401:
        return "ClickUp rejected the API token."
    if exc.status_code == 403:
        return "ClickUp token does not have permission to access the configured workspace or list."
    if exc.status_code == 404:
        return "ClickUp could not find the configured workspace or list."
    if exc.status_code == 400 and exc.error_code == "INPUT_003":
        return "CLICKUP_LIST_ID is invalid. Use the raw API list ID or rely on CLICKUP_LIST_NAME resolution."
    if exc.status_code:
        return f"ClickUp returned status {exc.status_code}."
    return str(exc) or "ClickUp is temporarily unavailable."


def clickup_http_exception(exc: ClickUpError) -> HTTPException:
    detail = describe_clickup_error(exc)
    if exc.error_code == "STATUS_CONFIG_INVALID":
        return HTTPException(status_code=503, detail=str(exc))
    if exc.status_code in {400, 401, 403, 404}:
        logger.warning("ClickUp configuration or access failure: %s", exc.as_dict())
        return HTTPException(status_code=502, detail=detail)
    if "timed out" in str(exc).lower():
        logger.warning("ClickUp timeout: %s", exc.as_dict())
        return HTTPException(status_code=504, detail=detail)
    logger.exception("ClickUp request failed: %s", exc.as_dict())
    return HTTPException(status_code=503, detail=detail)


def checkin_error_response(exc: ClickUpError) -> JSONResponse:
    if exc.error_code == "STATUS_CONFIG_INVALID":
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_code": "invalid_execution_config",
                "message": str(exc),
                "retry_safe": False,
            },
        )
    if exc.status_code in {502, 503}:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_code": "clickup_unavailable",
                "message": "ClickUp returned an error. Your check-in wasn't saved.",
                "retry_safe": True,
            },
        )
    if "timed out" in str(exc).lower():
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "error_code": "clickup_timeout",
                "message": "ClickUp timed out. It may still have gone through.",
                "retry_safe": True,
            },
        )
    if exc.status_code in {400, 422}:
        msg = str(exc).strip() or "Invalid input. Please try again."
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_code": "invalid_input",
                "message": msg,
                "retry_safe": False,
            },
        )
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error_code": "clickup_unavailable",
            "message": "Execution service is temporarily unavailable.",
            "retry_safe": True,
        },
    )


def record_degradation(app: FastAPI, source: str, detail: str) -> None:
    event = {
        "source": source,
        "detail": detail,
        "at": datetime.now().astimezone().isoformat(),
    }
    events: list[dict[str, Any]] = getattr(app.state, "degradation_events", [])
    events.append(event)
    # Keep a bounded tail to avoid unbounded growth in long-lived processes.
    app.state.degradation_events = events[-50:]
    logger.warning("degraded flow [%s]: %s", source, detail)


class RuntimeStateError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 503, retry_safe: bool = False, next_step: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_safe = retry_safe
        self.next_step = next_step


def runtime_state_payload(err: RuntimeStateError) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": err.code,
        "message": err.message,
        "retry_safe": err.retry_safe,
        "next_step": err.next_step,
    }


def runtime_state_response(err: RuntimeStateError) -> JSONResponse:
    return JSONResponse(status_code=err.status_code, content=runtime_state_payload(err))


def detect_current_task_invariant(
    tasks: list[dict[str, Any]],
    settings: Settings,
    fields: list[Any] | None = None,
) -> dict[str, Any]:
    """Detect the current-task invariant using dual-signal detection.

    A task is treated as "current" if EITHER its native ClickUp status matches
    clickup_current_status OR its Scheduler State custom field value is
    "Current".  Manual ClickUp edits that only change the custom field (without
    touching the native status) are therefore captured correctly.
    """
    from app.scheduler import _read_scheduler_state_name  # local import to avoid circular
    fields_by_name = field_by_name(fields) if fields else {}

    current = []
    dual_signal_drift: list[str] = []
    for task in tasks:
        native_status = str(task.get("status", {}).get("status", "")).strip().casefold()
        native_is_current = native_status == settings.clickup_current_status.strip().casefold()
        sched_state = _read_scheduler_state_name(task, fields_by_name, settings) if fields_by_name else ""
        field_is_current = sched_state == "current"

        if native_is_current or field_is_current:
            current.append(task)
            if native_is_current != field_is_current:
                task_id = str(task.get("id") or "")
                dual_signal_drift.append(task_id)

    count = len(current)
    if count == 0:
        return {
            "status": "zero_current",
            "count": 0,
            "task_ids": [],
            "violation": False,
            "message": "No current task is set in runtime list.",
            "dual_signal_drift": dual_signal_drift,
        }
    if count == 1:
        return {
            "status": "one_current",
            "count": 1,
            "task_ids": [str(current[0].get("id") or "")],
            "violation": False,
            "message": "Exactly one current task is set.",
            "dual_signal_drift": dual_signal_drift,
        }
    return {
        "status": "multi_current",
        "count": count,
        "task_ids": [str(task.get("id") or "") for task in current],
        "violation": True,
        "message": "Multiple tasks are marked current in runtime list.",
        "dual_signal_drift": dual_signal_drift,
    }


def select_deterministic_current_task(
    tasks: list[dict[str, Any]],
    fields: list[Any],
    settings: Settings,
) -> dict[str, Any] | None:
    fields_by_name = field_by_name(fields)
    now = datetime.now().astimezone()
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for task in tasks:
        score = float(task_score(task, fields_by_name, now, settings))
        scored.append((score, str(task.get("id") or ""), task))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2]


def build_operator_actions_summary(
    current_invariant: dict[str, Any],
    conformance: Any,
    pipeline_drift: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if current_invariant.get("status") == "multi_current":
        actions.append("Leave exactly one task as current in \u2699\ufe0f Execution Engine.")
        actions.append("Change the other current tasks to queued, break, or blocked.")
    if conformance.missing_recommended:
        actions.append("Add missing recommended fields to reach full intended mode.")
    if pipeline_drift.get("has_drift") is True:
        actions.append("Keep pipeline lists for intake/review only; move runtime-ready work into \u2699\ufe0f Execution Engine.")
    if not actions:
        actions.append("Runtime is healthy. Continue daily execution from the authoritative runtime list.")
    return actions


def build_field_conformance_payload(conformance: Any) -> dict[str, Any]:
    guidance = build_minimum_viable_guidance(conformance)
    return {
        "mode": conformance.mode,
        "present_fields": conformance.present,
        "missing_required_fields": conformance.missing_required,
        "missing_recommended_fields": conformance.missing_recommended,
        "missing_optional_fields": conformance.missing_optional,
        "unexpected_fields": conformance.unexpected,
        "capabilities": conformance.capabilities,
        "limitations": conformance.limitations,
        "operator_actions_required": conformance.operator_actions_required,
        "minimum_viable_guidance": guidance,
    }


async def build_pipeline_drift_payload(clickup: ClickUpClient, settings: Settings) -> dict[str, Any]:
    pipeline_drift: dict[str, Any] = {
        "status": "unavailable",
        "reason": "pipeline_lookup_not_supported",
        "lists": {},
        "has_drift": None,
        "drifted_tasks": [],
        "guidance": "Move tasks to your Execution Engine list when ready.",
    }
    if not settings.pipeline_space_name or not settings.pipeline_folder_name:
        return pipeline_drift
    try:
        spaces = await clickup.get_spaces(settings.clickup_workspace_id)
        space = next((s for s in spaces if str(s.get("name") or "").strip() == settings.pipeline_space_name), None)
        if not space:
            return pipeline_drift
        folders = await clickup.get_space_folders(str(space.get("id") or ""))
        folder = next((f for f in folders if str(f.get("name") or "").strip() == settings.pipeline_folder_name), None)
        if not folder:
            return pipeline_drift
        folder_lists = await clickup.get_folder_lists(str(folder.get("id") or ""))
        target_pipeline = {
            "Inbox",
            "Clarify",
            "Ready",
            "Agent Running",
            "Human Refinement",
            "Review",
            "Validation Failed",
            "Done",
        }
        list_counts: dict[str, int] = {}
        drifted_tasks: list[dict[str, Any]] = []
        for item in folder_lists:
            name = str(item.get("name") or "")
            if name not in target_pipeline:
                continue
            list_id = str(item.get("id") or "")
            if not list_id:
                continue
            list_tasks = await clickup.get_list_tasks(list_id)
            list_counts[name] = len(list_tasks)
            if list_tasks:
                label = "intake" if name in {"Inbox", "Clarify", "Ready"} else "should be promoted"
                for task in list_tasks[:10]:
                    drifted_tasks.append(
                        {
                            "id": str(task.get("id") or ""),
                            "name": str(task.get("name") or ""),
                            "url": str(task.get("url") or ""),
                            "status": str(task.get("status", {}).get("status") or ""),
                            "pipeline_list": name,
                            "label": label,
                        }
                    )
        non_zero = {k: v for k, v in list_counts.items() if v > 0}
        return {
            "status": "ok",
            "lists": list_counts,
            "non_zero_lists": non_zero,
            "has_drift": bool(non_zero),
            "note": "Pipeline lists are upstream only; runtime remains the single authoritative execution list.",
            "drifted_tasks": drifted_tasks,
            "guidance": "Move tasks to your Execution Engine list when ready.",
        }
    except Exception as exc:
        pipeline_drift["reason"] = str(exc) or "pipeline_lookup_failed"
        return pipeline_drift


def classify_clickup_runtime_error(exc: ClickUpError) -> RuntimeStateError:
    if exc.error_code == "STATUS_CONFIG_INVALID":
        return RuntimeStateError(
            "runtime_list_misconfigured",
            str(exc),
            status_code=503,
            retry_safe=False,
            next_step="Fix the configured ClickUp status names so they match the authoritative runtime list.",
        )
    if exc.status_code in {401, 403}:
        return RuntimeStateError(
            "clickup_auth_error",
            "ClickUp authorization failed for the execution list.",
            status_code=502,
            retry_safe=False,
            next_step="Verify CLICKUP_API_TOKEN permissions for list access.",
        )
    if exc.status_code == 404:
        return RuntimeStateError(
            "runtime_list_not_found",
            "Execution list cannot be found in ClickUp.",
            status_code=503,
            retry_safe=False,
            next_step="Confirm the authoritative runtime list id/name in configuration.",
        )
    if exc.status_code == 400 and exc.error_code == "INPUT_003":
        return RuntimeStateError(
            "runtime_list_misconfigured",
            "Execution list id is misconfigured for ClickUp.",
            status_code=503,
            retry_safe=False,
            next_step="Use a valid ClickUp list id or matching list-name resolution.",
        )
    if "timed out" in str(exc).lower():
        return RuntimeStateError(
            "clickup_connectivity_error",
            "ClickUp request timed out while reading execution state.",
            status_code=504,
            retry_safe=True,
            next_step="Retry, then check ClickUp API availability if this persists.",
        )
    return RuntimeStateError(
        "clickup_connectivity_error",
        "ClickUp request failed while reading execution state.",
        status_code=503,
        retry_safe=True,
        next_step="Retry, then inspect diagnostics for upstream failure details.",
    )


def classify_runtime_config_error(exc: ClickUpConfigError) -> RuntimeStateError:
    return RuntimeStateError(
        "runtime_list_misconfigured",
        str(exc) or "Execution list configuration is invalid.",
        status_code=503,
        retry_safe=False,
        next_step="Correct runtime list environment configuration.",
    )


def normalize_choice(value: Any, allowed: set[str]) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in allowed:
        raise HTTPException(status_code=422, detail=f"Invalid value: {value}")
    return normalized


def in_work_hours(now: datetime, settings: Settings) -> bool:
    if now.weekday() not in settings.workday_weekdays:
        return False
    hour = now.hour
    start = settings.workday_start_hour
    end = settings.workday_end_hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def should_run_scheduler(now: datetime, last_run: datetime | None, settings: Settings) -> bool:
    if not in_work_hours(now, settings):
        return False
    if last_run is None:
        return True
    return now - last_run >= timedelta(minutes=settings.scheduler_min_interval_minutes)


def should_send_daily_summary(now: datetime, last_summary_date: str | None, settings: Settings) -> bool:
    if not settings.enable_daily_summary:
        return False
    if now.weekday() not in settings.workday_weekdays:
        return False
    if now.hour < settings.daily_summary_hour:
        return False
    return last_summary_date != now.date().isoformat()


def should_send_weekly_summary(now: datetime, last_summary_key: str | None, settings: Settings) -> bool:
    if not settings.enable_weekly_summary:
        return False
    if now.weekday() != settings.weekly_summary_weekday:
        return False
    if now.hour < settings.weekly_summary_hour:
        return False
    week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    return last_summary_key != week_key


def format_daily_summary_message(report: dict[str, Any], report_date: str) -> str:
    totals = report.get("totals", {})
    return (
        f"Execution Engine Daily Summary ({report_date})\n"
        f"Current: {totals.get('in_progress', 0)}\n"
        f"Open Queue: {totals.get('planned', 0)}\n"
        f"Blocked: {totals.get('blocked', 0)}\n"
        f"Complete: {totals.get('completed', 0)}\n"
        f"Today Minutes: {totals.get('today_minutes', 0)}"
    )


def format_weekly_summary_message(report: dict[str, Any]) -> str:
    totals = report.get("totals", {})
    lines = [
        f"Execution Engine Weekly Summary ({datetime.now().date().isoformat()})",
        f"Current: {totals.get('in_progress', 0)}",
        f"Open Queue: {totals.get('planned', 0)}",
        f"Blocked: {totals.get('blocked', 0)}",
        f"Complete: {totals.get('completed', 0)}",
        f"Today Minutes Snapshot: {totals.get('today_minutes', 0)}",
        f"High-Friction Tasks: {totals.get('high_friction_tasks', 0)}",
    ]
    friction_heavy = report.get("high_friction_tasks", [])
    if friction_heavy:
        names = ", ".join(item["name"] for item in friction_heavy[:3])
        lines.append(f"Watchlist: {names}")
    return "\n".join(lines)


async def compute_daily_report_payload(
    settings: Settings,
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    list_id: str,
) -> dict[str, Any]:
    tasks = store.attach_many(await clickup.get_list_tasks(list_id))
    totals = {
        "completed": 0,
        "blocked": 0,
        "in_progress": 0,
        "planned": 0,
        "today_minutes": 0,
    }
    task_summaries: list[dict[str, Any]] = []
    for task in tasks:
        status = task["status"]["status"]
        normalized_status = status.strip().casefold()
        if normalized_status == settings.clickup_completed_status.strip().casefold() or normalized_status in {"completed", "complete", "closed"}:
            totals["completed"] += 1
        elif settings.clickup_blocked_status and normalized_status == settings.clickup_blocked_status.strip().casefold():
            totals["blocked"] += 1
        elif normalized_status == settings.clickup_current_status.strip().casefold():
            totals["in_progress"] += 1
        else:
            totals["planned"] += 1
        minutes = 0
        for field in task.get("custom_fields", []):
            if field.get("name") == "Today Minutes":
                minutes = int(field.get("value") or 0)
                break
        totals["today_minutes"] += minutes
        task_summaries.append({"id": task["id"], "name": task["name"], "status": status, "today_minutes": minutes})
    task_summaries.sort(key=lambda item: item["today_minutes"], reverse=True)
    return {"totals": totals, "tasks": task_summaries}


async def compute_weekly_prep_payload(
    settings: Settings,
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    list_id: str,
) -> dict[str, Any]:
    tasks = store.attach_many(await clickup.get_list_tasks(list_id))
    totals = {
        "completed": 0,
        "blocked": 0,
        "in_progress": 0,
        "planned": 0,
        "today_minutes": 0,
        "high_friction_tasks": 0,
    }
    friction_heavy: list[dict[str, Any]] = []
    for task in tasks:
        status = task["status"]["status"]
        normalized_status = status.strip().casefold()
        if normalized_status == settings.clickup_completed_status.strip().casefold() or normalized_status in {"completed", "complete", "closed"}:
            totals["completed"] += 1
        elif settings.clickup_blocked_status and normalized_status == settings.clickup_blocked_status.strip().casefold():
            totals["blocked"] += 1
        elif normalized_status == settings.clickup_current_status.strip().casefold():
            totals["in_progress"] += 1
        else:
            totals["planned"] += 1

        minutes = 0
        friction = ""
        for field in task.get("custom_fields", []):
            if field.get("name") == "Today Minutes":
                minutes = int(field.get("value") or 0)
            elif field.get("name") == "Friction Pulse":
                friction = str(field.get("value") or "")
        totals["today_minutes"] += minutes

        if str(friction).strip().casefold() == "high":
            totals["high_friction_tasks"] += 1
            friction_heavy.append({"id": task["id"], "name": task["name"], "status": status})

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "totals": totals,
        "high_friction_tasks": friction_heavy,
        "note": "Weekly prep uses current ClickUp snapshot only and does not create a parallel historical store.",
    }


async def compute_startup_diagnostics(
    settings: Settings,
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    list_id: str,
) -> dict[str, Any]:
    fields = await clickup.get_list_fields(list_id)
    tasks = store.attach_many(await clickup.get_list_tasks(list_id))
    hygiene = analyze_hygiene(tasks, settings)
    missing_fields = detect_missing_fields(fields, settings)
    return {
        "config": {
            "list_resolution": "id" if settings.clickup_list_id else "name",
            "list_id_configured": bool(settings.clickup_list_id),
            "list_name_configured": bool(settings.clickup_list_name),
            "space_id_configured": bool(settings.clickup_space_id),
            "session_cookie_secure": settings.session_cookie_secure,
            "session_cookie_samesite": settings.session_cookie_samesite,
            "builtin_scheduler_enabled": settings.enable_builtin_scheduler,
            "notifier_enabled": bool(settings.telegram_bot_token and settings.telegram_chat_id),
            "clickup_webhook_secret_configured": bool(settings.clickup_webhook_secret),
            "telegram_webhook_secret_configured": bool(settings.telegram_webhook_secret),
        },
        "clickup": {
            "connectivity": "ok",
            "resolved_list_id": list_id,
            "field_count": len(fields),
            "task_count": len(tasks),
            "missing_fields": missing_fields,
        },
        "scheduler": {
            "current_count": hygiene.current_count,
            "queue_count": hygiene.queue_count,
            "queue_target_min": settings.queue_target_min,
            "queue_target_max": settings.queue_target_max,
        },
        "hygiene": {
            "warnings": hygiene.warnings + (["Some expected ClickUp fields are missing or renamed."] if missing_fields else []),
            "duplicate_group_count": len(hygiene.duplicate_title_groups),
            "missing_resume_pack_count": len(hygiene.missing_resume_pack),
            "stale_queue_count": len(hygiene.stale_queue_tasks),
            "resume_pack_issues": hygiene.resume_pack_issues,
        },
    }


async def compute_operational_diagnostics(
    settings: Settings,
    clickup: ClickUpClient,
    store: RuntimeSessionStore,
    resolved_list_id: str,
    resolved_list_name: str,
    degradation_events: list[dict[str, Any]],
) -> dict[str, Any]:
    fields = await clickup.get_list_fields(resolved_list_id)
    tasks = store.attach_many(await clickup.get_list_tasks(resolved_list_id))
    field_names = [field.name for field in fields]
    conformance = evaluate_field_conformance(field_names)
    conformance_payload = build_field_conformance_payload(conformance)
    configured_list_id = settings.clickup_list_id
    configured_name = settings.clickup_list_name

    configured_normalized = configured_list_id or ""
    if configured_normalized.startswith("6-") and configured_normalized.endswith("-1"):
        configured_normalized = configured_normalized[2:-2]

    config_mismatch = {
        "configured_vs_resolved_list_id": bool(configured_normalized and configured_normalized != resolved_list_id),
        "configured_name_vs_resolved_name": bool(configured_name and configured_name != resolved_list_name),
    }

    current_count = sum(
        1
        for task in tasks
        if task.get("status", {}).get("status", "").strip().casefold()
        == settings.clickup_current_status.strip().casefold()
    )
    current_invariant = detect_current_task_invariant(tasks, settings, fields)
    current_tasks = [
        {
            "id": str(task.get("id") or ""),
            "name": str(task.get("name") or ""),
            "url": str(task.get("url") or ""),
            "status": str(task.get("status", {}).get("status") or ""),
        }
        for task in tasks
        if str(task.get("status", {}).get("status", "")).strip().casefold()
        == settings.clickup_current_status.strip().casefold()
    ]
    top_candidates = score_queue_tasks(tasks, fields, settings, exclude_task_id=None, limit=5)
    current_resolution_state, current_resolution_next_action = _build_current_task_resolution_state(
        current_invariant,
        len(top_candidates),
    )
    blocked_or_closed_count = sum(
        1
        for task in tasks
        if str(task.get("status", {}).get("status", "")).strip().casefold()
        in {
            settings.clickup_completed_status.strip().casefold(),
            "completed",
            "complete",
            "closed",
            (settings.clickup_blocked_status or "").strip().casefold(),
            "blocked",
        }
    )
    pipeline_drift = await build_pipeline_drift_payload(clickup, settings)
    operational_state = build_operational_state(
        current_task_resolution_state=current_resolution_state,
        current_task_resolution_next_action=current_resolution_next_action,
        conformance=conformance_payload,
        pipeline_drift=pipeline_drift,
        data_freshness="live",
        snapshot_timestamp=datetime.now().astimezone().isoformat(),
        retry_recommended=False,
        retryable_failure=False,
        usable_despite_failure=True,
        source_failure=None,
        promotion_attempted=False,
        promotion_verified=None,
        promotion_reason="Read-only diagnostics do not attempt promotion.",
        top_candidates=top_candidates,
        selection_attempted=False,
        selection_not_attempted_reason="Diagnostics is read-only and never attempts promotion.",
        config_mismatch=config_mismatch,
        current_invariant=current_invariant,
    )
    operator_actions = build_operator_actions_summary(current_invariant, conformance, pipeline_drift)
    if current_resolution_state == "zero_current_candidates_available":
        operator_actions.append("Review the top candidates below, then open Check-in for deterministic auto-selection or set one manually in ClickUp.")
    if pipeline_drift.get("has_drift") is True:
        operator_actions.append("Drifted pipeline tasks are visible below; move them to your Execution Engine list when ready.")

    return {
        "topology": TOPOLOGY_DECISION,
        "runtime_list": {
            "configured": {
                "list_id": settings.clickup_list_id,
                "list_name": settings.clickup_list_name,
            },
            "resolved": {
                "list_id": resolved_list_id,
                "list_name": resolved_list_name,
            },
            "config_mismatch": config_mismatch,
            "task_count": len(tasks),
            "current_task_count": current_count,
            "current_task_invariant": current_invariant,
            "current_tasks": current_tasks,
        },
        "field_conformance": conformance_payload,
        "selection_visibility": {
            "top_candidates": top_candidates,
            "eligibility_summary": {
                "total_tasks": len(tasks),
                "eligible_candidate_count": len(top_candidates),
                "blocked_or_closed_count": blocked_or_closed_count,
                "explanation": "Candidates are derived from runtime scoring and eligibility rules; empty candidates means no task is currently eligible.",
            },
        },
        "operational_state": operational_state.as_dict(),
        "current_task_resolution_state": operational_state.current_task_resolution_state,
        "current_task_resolution_next_action": operational_state.current_task_resolution_next_action,
        "promotion_attempted": operational_state.promotion_attempted,
        "promotion_verified": operational_state.promotion_verified,
        "operator_actions_summary": operator_actions,
        "routing_assumptions": {
            "authoritative_runtime_list_only": True,
            "scheduler_reads_runtime_list": True,
            "queue_reads_runtime_list": True,
            "quick_add_writes_runtime_list": True,
            "pipeline_used_for_runtime": False,
        },
        "pipeline_drift": pipeline_drift,
        "degradation_events": degradation_events[-20:],
    }


async def scheduler_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    clickup: ClickUpClient = app.state.clickup
    notifier = app.state.notifier
    store: RuntimeSessionStore = app.state.store
    last_run: datetime | None = None
    last_summary_date: str | None = None
    last_weekly_summary_key: str | None = None
    while True:
        now = datetime.now().astimezone()
        try:
            if should_run_scheduler(now, last_run, settings):
                async with app.state.scheduler_lock:
                    result = await run_scheduler(
                        settings,
                        clickup,
                        notifier,
                        store,
                        app.state.execution_list_id,
                        suppress_errors=True,
                    )
                if result.get("ok"):
                    last_run = now
                else:
                    record_degradation(app, "scheduler_loop", str(result.get("error") or "scheduler_run_failed"))

            if (
                should_send_daily_summary(now, last_summary_date, settings)
                and settings.telegram_bot_token
                and settings.telegram_chat_id
            ):
                report = await compute_daily_report_payload(settings, clickup, store, app.state.execution_list_id)
                message = format_daily_summary_message(report, now.date().isoformat())
                try:
                    await notifier.send_message(message)
                    last_summary_date = now.date().isoformat()
                except NotificationError:
                    pass

            if (
                should_send_weekly_summary(now, last_weekly_summary_key, settings)
                and settings.telegram_bot_token
                and settings.telegram_chat_id
            ):
                report = await compute_weekly_prep_payload(settings, clickup, store, app.state.execution_list_id)
                message = format_weekly_summary_message(report)
                try:
                    await notifier.send_message(message)
                    last_weekly_summary_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
                except NotificationError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the loop resilient; failures should not crash the API process.
            record_degradation(app, "scheduler_loop_exception", str(exc))
        await asyncio.sleep(settings.scheduler_tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_error = ""
    app.state.startup_error_detail = ""
    app.state.settings = None
    app.state.clickup = None
    app.state.notifier = NoopNotifier()
    app.state.store = RuntimeSessionStore()
    app.state.scheduler_lock = asyncio.Lock()
    app.state.login_attempts = {}
    app.state.execution_list_id = ""
    app.state.loop_task = None
    app.state.degradation_events = []
    app.state.last_known_operational_snapshot = None

    try:
        settings = load_settings()
        clickup = ClickUpClient(settings.clickup_token)
        if settings.telegram_bot_token and settings.telegram_chat_id:
            notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        else:
            notifier = NoopNotifier()
        execution_list_id = settings.clickup_list_id
        if not execution_list_id:
            execution_list_id = await clickup.resolve_list_id(
                settings.clickup_workspace_id,
                settings.clickup_list_name,
                space_id=settings.clickup_space_id,
            )
        list_info = await clickup.validate_access(execution_list_id)
        app.state.settings = settings
        app.state.clickup = clickup
        app.state.notifier = notifier
        app.state.execution_list_id = list_info["id"]
        if settings.enable_builtin_scheduler:
            app.state.loop_task = asyncio.create_task(scheduler_loop(app))
    except ClickUpError as exc:
        logger.exception("Startup ClickUp validation failed: %s", exc.as_dict())
        app.state.startup_error = "startup_incomplete"
        app.state.startup_error_detail = describe_clickup_error(exc)
    except ClickUpConfigError as exc:
        logger.exception("Startup ClickUp configuration failed")
        app.state.startup_error = "startup_incomplete"
        app.state.startup_error_detail = str(exc)
    except Exception:
        logger.exception("Startup failed unexpectedly")
        app.state.startup_error = "startup_incomplete"
        app.state.startup_error_detail = "Service is not fully configured yet."

    try:
        yield
    finally:
        if app.state.loop_task:
            app.state.loop_task.cancel()
            try:
                await app.state.loop_task
            except asyncio.CancelledError:
                pass
        if app.state.clickup:
            await app.state.clickup.aclose()
        if app.state.notifier:
            await app.state.notifier.aclose()


app = FastAPI(title="ClickUp Execution Engine Scheduler", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/login"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Shared CSS design-system used by all HTML pages
# ---------------------------------------------------------------------------
_BASE_CSS = """
    :root {
      --bg: #f5f0e8;
      --card: #fffdf8;
      --ink: #1a2230;
      --ink-secondary: #374151;
      --accent: #0f766e;
      --accent-hover: #0d9488;
      --accent-2: #b45309;
      --danger: #991b1b;
      --border: #e3d9cc;
      --border-subtle: rgba(227,217,204,0.55);
      --success: #0f766e;
      --warn: #b45309;
      --error: #991b1b;
      --muted: #6b7280;
      --surface-inner: rgba(255,253,250,0.82);
      --surface-inner-border: rgba(229,220,205,0.55);
      --shadow-card: 0 2px 6px rgba(30,40,55,0.06), 0 14px 44px rgba(30,40,55,0.10);
      --shadow-elevated: 0 4px 16px rgba(30,40,55,0.10), 0 24px 64px rgba(30,40,55,0.14);
      --shadow-sm: 0 1px 4px rgba(30,40,55,0.08);
      --shadow-btn: 0 1px 3px rgba(30,40,55,0.10), 0 4px 12px rgba(30,40,55,0.08);
      --shadow-btn-colored: 0 2px 6px rgba(15,118,110,0.30), 0 6px 20px rgba(15,118,110,0.18);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 18px;
      --radius-pill: 999px;
      --transition-fast: 0.12s ease;
      --transition-normal: 0.20s ease;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .card {
      background: linear-gradient(160deg, #fffdf9 0%, #fffaf2 100%);
      border: 1px solid var(--border);
      border-top: 1px solid rgba(255,255,255,0.90);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-card);
    }
    /* Shared button / link-button */
    .btn, a.btn, button.btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 40px;
      padding: 9px 16px;
      border-radius: var(--radius-md);
      border: 1.5px solid transparent;
      font-family: inherit;
      font-size: 0.88rem;
      font-weight: 600;
      letter-spacing: 0.01em;
      cursor: pointer;
      text-decoration: none;
      transition: box-shadow var(--transition-fast), transform var(--transition-fast),
                  background var(--transition-fast);
    }
    .btn:focus-visible, a.btn:focus-visible, button.btn:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 3px;
    }
    .btn.primary, a.btn.primary, button.btn.primary {
      background: linear-gradient(135deg, #0f766e, #0d9488);
      color: #fff;
      border-color: transparent;
      box-shadow: var(--shadow-btn-colored);
    }
    .btn.primary:hover, a.btn.primary:hover, button.btn.primary:hover {
      box-shadow: 0 3px 10px rgba(15,118,110,0.38), 0 8px 26px rgba(15,118,110,0.20);
      transform: translateY(-1px);
    }
    .btn.secondary, a.btn.secondary, button.btn.secondary,
    .btn:not([class*="primary"]):not([class*="danger"]):not([class*="warn"]) {
      background: rgba(255,253,250,0.90);
      color: var(--ink-secondary);
      border-color: var(--border);
    }
    .btn.secondary:hover, a.btn.secondary:hover, button.btn.secondary:hover {
      background: #faf6f0;
      box-shadow: var(--shadow-btn);
      transform: translateY(-1px);
    }
    /* Shared pill */
    .pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: var(--radius-pill);
      background: rgba(227,217,204,0.60);
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
    }
    .pill.healthy  { background: rgba(15,118,110,0.12); color: #065f51; }
    .pill.degraded { background: rgba(180,83,9,0.12);  color: #7c3610; }
    .pill.blocked  { background: rgba(153,27,27,0.12); color: #7f1d1d; }
    /* Shared muted text */
    .muted { color: var(--muted); font-size: 0.85rem; }
    .strong { font-weight: 700; }
"""


def login_page_html(error: str = "") -> str:
    safe_error = html.escape(error)
    error_block = (
        f"<div class='error-callout'>{safe_error}</div>"
        if safe_error else ""
    )
    html_content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Execution Engine Login</title>
  <style>
    {_BASE_CSS}
    body {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px 16px;
      background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%);
    }}
    .login-card {{
      width: min(92vw, 420px);
      padding: 32px 28px;
    }}
    .login-logo {{
      font-size: 0.7rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      margin: 0 0 20px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 1.4rem;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      letter-spacing: -0.01em;
    }}
    .login-desc {{
      margin: 0 0 20px;
      font-size: 0.875rem;
      color: var(--muted);
      line-height: 1.55;
    }}
    .error-callout {{
      margin: 0 0 16px;
      padding: 9px 13px;
      border-radius: var(--radius-sm);
      border-left: 3px solid var(--error);
      background: rgba(153,27,27,0.07);
      color: var(--error);
      font-size: 0.875rem;
      font-weight: 500;
    }}
    input[type="password"] {{
      display: block;
      width: 100%;
      padding: 13px 14px;
      border-radius: var(--radius-md);
      border: 1.5px solid var(--border);
      font-family: inherit;
      font-size: 0.95rem;
      color: var(--ink);
      background: rgba(255,253,250,0.90);
      transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
    }}
    input[type="password"]:focus {{
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(15,118,110,0.12);
    }}
    .submit-btn {{
      display: block;
      width: 100%;
      margin-top: 12px;
      padding: 14px;
      border-radius: var(--radius-md);
      border: none;
      background: linear-gradient(135deg, #0f766e, #0d9488);
      color: white;
      font-family: inherit;
      font-size: 0.95rem;
      font-weight: 700;
      cursor: pointer;
      box-shadow: var(--shadow-btn-colored);
      transition: box-shadow var(--transition-fast), transform var(--transition-fast);
    }}
    .submit-btn:hover {{
      box-shadow: 0 3px 10px rgba(15,118,110,0.38), 0 8px 26px rgba(15,118,110,0.20);
      transform: translateY(-1px);
    }}
    .submit-btn:active {{ transform: translateY(0); }}
    .submit-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
  </style>
</head>
<body>
  <main class="card login-card">
    <p class="login-logo">Execution Engine</p>
    <h1>Sign in</h1>
    <p class="login-desc">Enter your shared secret to open your private check-in flow.</p>
    {error_block}
    <form method="post" action="/login">
      <input type="password" name="password" autocomplete="current-password" autofocus required placeholder="Shared secret">
      <button type="submit" class="submit-btn">Log In</button>
    </form>
  </main>
</body>
</html>"""
    return minify_html(html_content)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if not service_ready(request):
        return HTMLResponse(login_page_html("Service setup is incomplete."), status_code=503)
    if is_authenticated(request):
        return RedirectResponse(url="/active/checkin", status_code=303)
    return HTMLResponse(login_page_html())


@app.post("/login")
async def login_submit(request: Request, password: str = Form(default="")) -> Response:
    if not service_ready(request):
        return HTMLResponse(login_page_html("Service setup is incomplete."), status_code=503)
    settings: Settings = request.app.state.settings
    if not login_allowed(request):
        record_login_attempt(request)
        return HTMLResponse(login_page_html("Login failed."), status_code=429)
    if not verify_shared_secret(settings.app_shared_secret, password):
        record_login_attempt(request)
        return HTMLResponse(login_page_html("Login failed."), status_code=401)
    clear_login_attempts(request)
    response = RedirectResponse(url="/active/checkin", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    set_session_cookie(response, settings)
    return response


@app.post("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response, request.app.state.settings)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def root(request: Request) -> Any:
    if is_authenticated(request) and service_ready(request):
        report = await diagnostics_report(request)
        if isinstance(report, JSONResponse):
            report_payload = json.loads(report.body.decode("utf-8"))
        else:
            report_payload = report
        op_state = dict(report_payload.get("operational_state") or {})
        current_tasks = list(report_payload.get("runtime_list", {}).get("current_tasks") or [])
        current_label = "No current task verified."
        if current_tasks:
            current_label = str(current_tasks[0].get("name") or current_tasks[0].get("id") or "Current task")
        next_action = html.escape(str(op_state.get("next_action") or report_payload.get("current_task_resolution_next_action") or "Open Check-in."))
        freshness = html.escape(str(report_payload.get("data_freshness") or "unknown"))
        snapshot = html.escape(str(report_payload.get("snapshot_timestamp") or ""))
        failure = dict(report_payload.get("source_failure") or {})
        failure_text = ""
        if freshness == "stale":
            failure_text = f"<div class='stale-callout'><strong>Stale snapshot.</strong> {html.escape(str(failure.get('class') or 'unknown'))}: {html.escape(str(failure.get('message') or 'Live ClickUp read failed.'))}</div>"
        health_val = html.escape(str(op_state.get("health") or op_state.get("status") or "unknown"))
        health_cls = "healthy" if health_val.lower() in ("healthy", "ok", "running") else ("degraded" if health_val.lower() in ("degraded", "warn", "warning") else "")
        return HTMLResponse(
            f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Execution Control Center</title>
  <style>
    {_BASE_CSS}
    body {{
      padding: 24px 16px 48px;
      background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%);
      min-height: 100vh;
    }}
    .dash-card {{
      max-width: 820px;
      margin: 0 auto;
      padding: 28px 28px 24px;
    }}
    .dash-logo {{
      font-size: 0.65rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      margin: 0 0 16px;
    }}
    h1 {{
      margin: 0 0 22px;
      font-size: 1.55rem;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      letter-spacing: -0.01em;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin: 0 0 20px;
    }}
    .stat-block {{
      padding: 14px 16px;
      border: 1px solid var(--surface-inner-border);
      border-radius: var(--radius-md);
      background: var(--surface-inner);
    }}
    .stat-label {{
      font-size: 0.68rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      color: var(--muted);
      margin: 0 0 5px;
    }}
    .stat-value {{
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--ink);
      overflow-wrap: anywhere;
    }}
    .stale-callout {{
      margin: 0 0 18px;
      padding: 9px 13px;
      border-radius: var(--radius-sm);
      border-left: 3px solid var(--warn);
      background: rgba(180,83,9,0.07);
      color: var(--warn);
      font-size: 0.875rem;
    }}
    .btn-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 20px;
    }}
  </style>
</head>
<body>
  <main class='card dash-card'>
    <p class='dash-logo'>Execution Engine</p>
    <h1>Control Center</h1>
    <div class='stat-grid'>
      <div class='stat-block'>
        <div class='stat-label'>Operational state:</div>
        <div class='stat-value'><span class='pill {health_cls}'>{health_val}</span></div>
      </div>
      <div class='stat-block'>
        <div class='stat-label'>Current task:</div>
        <div class='stat-value'>{html.escape(current_label)}</div>
      </div>
      <div class='stat-block'>
        <div class='stat-label'>Next action:</div>
        <div class='stat-value'>{next_action}</div>
      </div>
      <div class='stat-block'>
        <div class='stat-label'>Data freshness</div>
        <div class='stat-value muted'>{freshness}{f" &middot; {snapshot}" if snapshot else ""}</div>
      </div>
    </div>
    {failure_text}
    <div class='btn-row'>
      <a class='btn primary' href='/active/checkin'>Open Check-in</a>
      <a class='btn secondary' href='/diagnostics'>Open Operations</a>
    </div>
  </main>
</body>
</html>"""
        )
    return {
        "service": "clickup-execution-engine",
        "status": "running" if service_ready(request) else "degraded",
        "login": "/login",
        "health": "/healthz",
        "ready": "/readyz",
        "diagnostics": "/diagnostics",
        "docs": "/docs",
    }


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


async def run_scheduler(
    settings: Settings,
    clickup: ClickUpClient,
    notifier: TelegramNotifier,
    store: RuntimeSessionStore,
    list_id: str,
    *,
    suppress_errors: bool = False,
) -> dict[str, Any]:
    try:
        fields = await clickup.get_list_fields(list_id)
        field_conformance = evaluate_field_conformance([field.name for field in fields])
        if field_conformance.missing_required:
            missing = ", ".join(field_conformance.missing_required)
            raise RuntimeStateError(
                "insufficient_field_configuration",
                f"Execution list is missing required fields: {missing}",
                status_code=503,
                retry_safe=False,
                next_step="Add required fields to the authoritative runtime list.",
            )

        tasks = store.attach_many(await clickup.get_list_tasks(list_id))
        if not tasks:
            return {
                "ok": True,
                "current_task_id": None,
                "score_count": 0,
                "state": {
                    "code": "empty_runtime_list",
                    "message": "Execution list is empty.",
                    "next_step": "Add 3-7 active tasks to the authoritative runtime list.",
                },
            }

        decision = await choose_current_task(tasks, fields, settings)
        if not decision.current_task:
            return {
                "ok": True,
                "current_task_id": None,
                "score_count": len(decision.scores),
                "state": {
                    "code": "no_eligible_task",
                    "message": "Execution list has tasks but none are currently eligible.",
                    "next_step": "Review task statuses/pulses and blocked cooldowns, then retry scheduler.",
                },
            }

        status_map = await resolve_runtime_status_map(clickup, list_id, settings)
        current_task = await sync_scheduler_state(clickup, store, settings, tasks, fields, decision, status_map)
        if current_task:
            _notify_task_id = current_task["id"]
            _notify_url = f"{settings.public_base_url}/checkin/{_notify_task_id}"

            async def _notify_bg() -> None:
                try:
                    _refreshed = await clickup.get_task(_notify_task_id)
                    _refreshed["_runtime"] = store.get(_notify_task_id)
                    await notifier.send_task_prompt(_refreshed, _notify_url)
                except Exception:  # noqa: BLE001
                    pass

            asyncio.create_task(_notify_bg())
        return {
            "ok": True,
            "current_task_id": current_task["id"] if current_task else None,
            "score_count": len(decision.scores),
        }
    except RuntimeStateError as exc:
        if suppress_errors:
            return {"ok": False, "current_task_id": None, "score_count": 0, "error_code": exc.code, "error": exc.message}
        raise
    except ClickUpConfigError as exc:
        runtime_error = classify_runtime_config_error(exc)
        if suppress_errors:
            return {
                "ok": False,
                "current_task_id": None,
                "score_count": 0,
                "error_code": runtime_error.code,
                "error": runtime_error.message,
            }
        raise runtime_error
    except ClickUpError as exc:
        runtime_error = classify_clickup_runtime_error(exc)
        if suppress_errors:
            return {
                "ok": False,
                "current_task_id": None,
                "score_count": 0,
                "error_code": runtime_error.code,
                "error": runtime_error.message,
            }
        raise runtime_error
    except Exception as exc:
        runtime_error = RuntimeStateError(
            "scheduler_internal_error",
            "Scheduler hit an internal error.",
            status_code=500,
            retry_safe=False,
            next_step="Inspect diagnostics and server logs for exception details.",
        )
        if suppress_errors:
            return {
                "ok": False,
                "current_task_id": None,
                "score_count": 0,
                "error_code": runtime_error.code,
                "error": str(exc) or runtime_error.message,
            }
        raise runtime_error


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    if not service_ready(request):
        return {
            "status": "degraded",
            "reason": "startup_incomplete",
            "detail": getattr(request.app.state, "startup_error_detail", "") or "startup_incomplete",
        }
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request) -> Any:
    if not service_ready(request):
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "reason": "startup_incomplete",
                "detail": getattr(request.app.state, "startup_error_detail", "") or "startup_incomplete",
            },
        )
    return {"status": "ready"}


@app.get("/active")
async def active_task(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
        tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
    except ClickUpError as exc:
        return runtime_state_response(classify_clickup_runtime_error(exc))
    invariant = detect_current_task_invariant(tasks, settings, fields)
    top_candidates = score_queue_tasks(tasks, fields, settings, exclude_task_id=None, limit=5)
    if invariant["status"] == "multi_current":
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error_code": "multi_current_violation",
                "message": "Multiple tasks are marked current in the authoritative runtime list.",
                "retry_safe": False,
                "next_step": "Use runtime remediation to keep one current task and demote others.",
                "current_task_count": invariant["count"],
                "current_task_ids": invariant["task_ids"],
                "remediation": "/ops/remediate/runtime-current",
                "operational_state": build_operational_state(
                    current_task_resolution_state="multi_current_violation",
                    current_task_resolution_next_action="Run runtime remediation to keep exactly one current task.",
                    conformance={},
                    pipeline_drift={},
                    data_freshness="live",
                    snapshot_timestamp="",
                    retry_recommended=False,
                    retryable_failure=False,
                    usable_despite_failure=True,
                    source_failure=None,
                    promotion_attempted=False,
                    promotion_verified=None,
                    promotion_reason="No promotion attempted while multi-current violation exists.",
                    top_candidates=top_candidates,
                    selection_attempted=False,
                    selection_not_attempted_reason="Selection is blocked until exactly one current task remains.",
                    current_invariant=invariant,
                ).as_dict(),
            },
        )
    current_candidates = [
        task
        for task in tasks
        if task["status"]["status"].strip().casefold() == settings.clickup_current_status.strip().casefold()
    ]
    current = current_candidates[0] if len(current_candidates) == 1 else None
    if not current:
        try:
            status_map = await resolve_runtime_status_map(
                clickup,
                request.app.state.execution_list_id,
                settings,
                require_active=True,
                require_available=False,
            )
            async with request.app.state.scheduler_lock:
                result = await run_scheduler(
                    settings,
                    clickup,
                    request.app.state.notifier,
                    store,
                    request.app.state.execution_list_id,
                )
        except RuntimeStateError as exc:
            return runtime_state_response(exc)
        current_id = result.get("current_task_id")
        if not current_id:
            base_state = result.get("state") or {
                "code": "no_eligible_task",
                "message": "Execution list has tasks but none are currently eligible.",
            }
            state = dict(base_state)
            code = str(state.get("code") or "no_eligible_task")
            if code == "no_eligible_task":
                resolution = "zero_current_no_eligible_candidates"
            else:
                resolution = "resolution_blocked_by_source_failure"
            op_state = build_operational_state(
                current_task_resolution_state=resolution,
                current_task_resolution_next_action=str(state.get("next_step") or "Review Operations before retrying."),
                conformance={},
                pipeline_drift={},
                data_freshness="live",
                snapshot_timestamp="",
                retry_recommended=bool(state.get("retry_safe")),
                retryable_failure=bool(state.get("retry_safe")),
                usable_despite_failure=True,
                source_failure=None,
                promotion_attempted=False,
                promotion_verified=None,
                promotion_reason="No promotion attempt occurred because no eligible current task was established.",
                top_candidates=top_candidates,
                selection_attempted=False,
                selection_not_attempted_reason="Scheduler did not establish a current task.",
                current_invariant=invariant,
            )
            state.update(op_state.as_dict())
            return {
                "current_task_id": None,
                "checkin_url": None,
                "state": state,
            }

        try:
            verification_tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
            verification_invariant = detect_current_task_invariant(verification_tasks, settings)
        except ClickUpError as exc:
            runtime_err = classify_clickup_runtime_error(exc)
            op_state = build_operational_state(
                current_task_resolution_state="resolution_blocked_by_source_failure",
                current_task_resolution_next_action=runtime_err.next_step,
                conformance={},
                pipeline_drift={},
                data_freshness="live",
                snapshot_timestamp="",
                retry_recommended=bool(runtime_err.retry_safe),
                retryable_failure=bool(runtime_err.retry_safe),
                usable_despite_failure=False,
                source_failure=classify_source_failure(runtime_err),
                promotion_attempted=True,
                promotion_verified=False,
                promotion_reason="Promotion write was attempted, but post-write verification read failed.",
                top_candidates=top_candidates,
                selection_attempted=True,
                selection_not_attempted_reason=None,
                current_invariant=invariant,
            )
            return {
                "current_task_id": None,
                "checkin_url": None,
                "state": {
                    "code": "promotion_verification_failed",
                    "message": "Promotion attempted but post-write verification read failed.",
                    "next_step": runtime_err.next_step,
                    "retry_safe": runtime_err.retry_safe,
                    **op_state.as_dict(),
                },
            }

        promotion_verified = (
            verification_invariant.get("status") == "one_current"
            and str(current_id) in set(verification_invariant.get("task_ids") or [])
        )
        if not promotion_verified:
            op_state = build_operational_state(
                current_task_resolution_state="promotion_failed",
                current_task_resolution_next_action="Open Operations and inspect runtime invariant before retrying.",
                conformance={},
                pipeline_drift={},
                data_freshness="live",
                snapshot_timestamp="",
                retry_recommended=False,
                retryable_failure=False,
                usable_despite_failure=True,
                source_failure=None,
                promotion_attempted=True,
                promotion_verified=False,
                promotion_reason="ClickUp write did not produce a single verified current task.",
                top_candidates=top_candidates,
                selection_attempted=True,
                selection_not_attempted_reason=None,
                current_invariant=verification_invariant,
            )
            return {
                "current_task_id": None,
                "checkin_url": None,
                "state": {
                    "code": "promotion_not_verified",
                    "message": "Promotion attempted but no single verified current task was established.",
                    "next_step": "Open Operations and inspect runtime invariant before retrying.",
                    "post_check_invariant": verification_invariant,
                    **op_state.as_dict(),
                },
            }
        try:
            current = await clickup.get_task(current_id)
        except ClickUpError as exc:
            return runtime_state_response(classify_clickup_runtime_error(exc))
        current["_runtime"] = store.get(current_id)
        op_state = build_operational_state(
            current_task_resolution_state="promotion_succeeded",
            current_task_resolution_next_action="Open Check-in.",
            conformance={},
            pipeline_drift={},
            data_freshness="live",
            snapshot_timestamp="",
            retry_recommended=False,
            retryable_failure=False,
            usable_despite_failure=True,
            source_failure=None,
            promotion_attempted=True,
            promotion_verified=True,
            promotion_reason="Promotion was verified by a post-write read.",
            top_candidates=top_candidates,
            selection_attempted=True,
            selection_not_attempted_reason=None,
            current_invariant=verification_invariant,
        )
    else:
        op_state = build_operational_state(
            current_task_resolution_state="current_present",
            current_task_resolution_next_action="Open Check-in.",
            conformance={},
            pipeline_drift={},
            data_freshness="live",
            snapshot_timestamp="",
            retry_recommended=False,
            retryable_failure=False,
            usable_despite_failure=True,
            source_failure=None,
            promotion_attempted=False,
            promotion_verified=None,
            promotion_reason="No promotion needed because one current task already exists.",
            top_candidates=top_candidates,
            selection_attempted=False,
            selection_not_attempted_reason="A current task is already established.",
            current_invariant=invariant,
        )
    return {
        "current_task_id": current["id"],
        "task_name": current["name"],
        "status": current["status"]["status"],
        "clickup_url": current["url"],
        "checkin_url": f"{settings.public_base_url}/checkin/{current['id']}",
        "block": block_progress(current, settings),
        "data_freshness": op_state.data_freshness,
        "snapshot_timestamp": op_state.snapshot_timestamp,
        "retry_recommended": op_state.retry_recommended,
        "retryable_failure": op_state.retryable_failure,
        "usable_despite_failure": op_state.usable_despite_failure,
        "source_failure": op_state.source_failure,
        "current_task_resolution_state": op_state.current_task_resolution_state,
        "promotion_attempted": op_state.promotion_attempted,
        "promotion_verified": op_state.promotion_verified,
        "operational_state": op_state.as_dict(),
    }


@app.get("/active/checkin")
async def active_checkin(request: Request):
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    payload = await active_task(request)
    if isinstance(payload, Response):
        if isinstance(payload, JSONResponse):
            try:
                details = json.loads(payload.body.decode("utf-8"))
            except Exception:
                return payload
            code = str(details.get("error_code") or "scheduler_internal_error")
            message = html.escape(str(details.get("message") or "Execution state is unavailable."))
            title_map = {
                "runtime_list_not_found": "Execution List Not Found",
                "runtime_list_misconfigured": "Execution List Misconfigured",
                "clickup_auth_error": "ClickUp Authorization Failed",
                "clickup_connectivity_error": "ClickUp Request Failed",
                "insufficient_field_configuration": "Execution List Missing Required Fields",
                "scheduler_internal_error": "Scheduler Internal Error",
                "multi_current_violation": "Multiple Current Tasks Detected",
            }
            title = title_map.get(code, "Execution State Unavailable")
            return HTMLResponse(
                f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    {_BASE_CSS}
    body {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px 16px;
      background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%);
    }}
    .error-card {{
      width: min(92vw, 540px);
      padding: 36px 28px;
      text-align: center;
    }}
    .error-icon {{
      font-size: 1.8rem;
      margin: 0 0 12px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.2rem;
      font-weight: 700;
      color: var(--ink);
    }}
    .error-msg {{
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.9rem;
      margin: 0 0 22px;
    }}
    .btns {{
      display: flex;
      gap: 10px;
      justify-content: center;
      flex-wrap: wrap;
    }}
  </style>
</head>
<body>
  <div class="card error-card">
    <div class="error-icon">⚠</div>
    <h1>{title}</h1>
    <p class="error-msg">{message}</p>
    <div class="btns">
      <a class="btn primary" href="/diagnostics">Diagnostics</a>
      <a class="btn secondary" href="https://app.clickup.com" target="_blank" rel="noopener">Open ClickUp ↗</a>
    </div>
  </div>
</body>
</html>""",
                status_code=payload.status_code,
            )
        return payload
    checkin_url = payload.get("checkin_url")
    if not checkin_url:
        state = payload.get("state") or {}
        code = str(state.get("code") or "no_eligible_task")
        if code == "empty_runtime_list":
            title = "Execution list is empty"
            description = "Add active tasks to the authoritative runtime list (⚙️ Execution Engine)."
        elif code == "promotion_not_verified":
            title = "Promotion could not be verified"
            description = "Scheduler attempted a promotion, but post-write verification did not establish exactly one current task."
        elif code == "promotion_verification_failed":
            title = "Verification read failed"
            description = "Scheduler attempted a promotion, but ClickUp could not be re-read to verify the live result."
        else:
            title = "Nothing queued right now"
            description = "Execution list has tasks, but none are currently eligible."
        if state.get("next_step"):
            description = f"{description} {str(state.get('next_step'))}".strip()
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>No Active Task</title>
  <style>
    {_BASE_CSS}
    body {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px 16px;
      background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%);
    }}
    .empty-card {{
      width: min(92vw, 480px);
      padding: 36px 28px;
      text-align: center;
    }}
    .empty-icon {{ font-size: 1.8rem; margin: 0 0 12px; }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.2rem;
      font-weight: 700;
      color: var(--ink);
    }}
    .empty-desc {{
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.9rem;
      margin: 0 0 22px;
    }}
    .btns {{
      display: flex;
      gap: 10px;
      justify-content: center;
      flex-wrap: wrap;
    }}
    #msg {{
      margin-top: 14px;
      min-height: 22px;
      font-size: 0.875rem;
      color: var(--muted);
      padding: 0 4px;
      transition: color var(--transition-normal);
    }}
    #msg.running {{ color: var(--accent); }}
    #msg.done {{ color: var(--muted); }}
    #msg.error-state {{ color: var(--error); }}
  </style>
</head>
<body>
  <div class="card empty-card">
    <div class="empty-icon">📭</div>
    <h1>{html.escape(title)}</h1>
    <p class="empty-desc">{html.escape(description)}</p>
    <div class="btns">
      <button class="btn primary" onclick="runScheduler()">Run Scheduler</button>
      <a class="btn secondary" href="https://app.clickup.com" target="_blank" rel="noopener">Open ClickUp ↗</a>
      <a class="btn secondary" href="/diagnostics">Diagnostics</a>
    </div>
    <p id="msg"></p>
  </div>
  <script>
    async function runScheduler() {{
      const m = document.getElementById('msg');
      m.className = 'running';
      m.textContent = 'Running\u2026';
      try {{
        const r = await fetch('/scheduler/run', {{ method: 'POST' }});
        if (r.status === 401) {{ window.location = '/login'; return; }}
        const d = await r.json();
        if (d.current_task_id) {{ window.location = '/checkin/' + d.current_task_id; }}
        else {{ m.className = 'done'; m.textContent = 'No eligible task found.'; }}
      }} catch (e) {{
        m.className = 'error-state';
        m.textContent = 'Error. Try again.';
      }}
    }}
  </script>
</body>
</html>""",
            status_code=404,
        )
    return RedirectResponse(url=checkin_url, status_code=307)


@app.post("/scheduler/run")
async def scheduler_run(request: Request) -> Any:
    require_ready(request)
    require_session(request)
    try:
        async with request.app.state.scheduler_lock:
            return await run_scheduler(
                request.app.state.settings,
                request.app.state.clickup,
                request.app.state.notifier,
                request.app.state.store,
                request.app.state.execution_list_id,
            )
    except RuntimeStateError as exc:
        return runtime_state_response(exc)


@app.get("/reports/hygiene")
async def hygiene_report(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
        tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc
    report = analyze_hygiene(tasks, settings)
    missing_fields = detect_missing_fields(fields, settings)
    warnings = list(report.warnings)
    if missing_fields:
        warnings.append("Some expected ClickUp fields are missing or renamed.")
    return {
        "current_count": report.current_count,
        "queue_count": report.queue_count,
        "warnings": warnings,
        "missing_fields": missing_fields,
        "duplicate_title_groups": [
            [{"id": item["id"], "name": item["name"], "status": item["status"]["status"]} for item in group]
            for group in report.duplicate_title_groups
        ],
        "missing_resume_pack": [
            {"id": task["id"], "name": task["name"], "status": task["status"]["status"]}
            for task in report.missing_resume_pack
        ],
        "resume_pack_issues": report.resume_pack_issues,
        "stale_queue_tasks": [
            {"id": task["id"], "name": task["name"], "status": task["status"]["status"]}
            for task in report.stale_queue_tasks
        ],
    }


@app.get("/reports/daily")
async def daily_report(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        return await compute_daily_report_payload(settings, clickup, store, request.app.state.execution_list_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc


@app.get("/reports/weekly")
async def weekly_report(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        return await compute_weekly_prep_payload(settings, clickup, store, request.app.state.execution_list_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc


@app.get("/reports/startup")
async def startup_report(request: Request) -> Any:
    if not service_ready(request):
        return {
            "status": "degraded",
            "reason": getattr(request.app.state, "startup_error", "startup_incomplete") or "startup_incomplete",
            "detail": getattr(request.app.state, "startup_error_detail", "") or "startup_incomplete",
            "degradation_events": getattr(request.app.state, "degradation_events", []),
        }
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        return await compute_startup_diagnostics(settings, clickup, store, request.app.state.execution_list_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc


@app.get("/reports/diagnostics")
async def diagnostics_report(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        list_info = await clickup.validate_access(request.app.state.execution_list_id)
        live_payload = await compute_operational_diagnostics(
            settings,
            clickup,
            store,
            str(list_info.get("id") or request.app.state.execution_list_id),
            str(list_info.get("name") or ""),
            getattr(request.app.state, "degradation_events", []),
        )
        enriched_live = _annotate_operational_snapshot(live_payload, operational_state_from_dict(live_payload["operational_state"]))
        _save_last_known_operational_snapshot(request.app, enriched_live)
        return enriched_live
    except ClickUpError as exc:
        runtime_err = classify_clickup_runtime_error(exc)
        failure = classify_source_failure(runtime_err)
        last_snapshot = getattr(request.app.state, "last_known_operational_snapshot", None)
        if isinstance(last_snapshot, dict) and isinstance(last_snapshot.get("payload"), dict):
            stale_payload = dict(last_snapshot["payload"])
            stale_state = build_operational_state(
                current_task_resolution_state="resolution_blocked_by_source_failure",
                current_task_resolution_next_action=runtime_err.next_step,
                conformance=dict(stale_payload.get("field_conformance") or {}),
                pipeline_drift=dict(stale_payload.get("pipeline_drift") or {}),
                data_freshness="stale",
                snapshot_timestamp=str(last_snapshot.get("captured_at") or ""),
                retry_recommended=bool(runtime_err.retry_safe),
                retryable_failure=bool(runtime_err.retry_safe),
                usable_despite_failure=True,
                source_failure=failure,
                promotion_attempted=False,
                promotion_verified=None,
                promotion_reason="No promotion attempted while serving stale snapshot data.",
                top_candidates=list(stale_payload.get("selection_visibility", {}).get("top_candidates") or []),
                selection_attempted=False,
                selection_not_attempted_reason="Live ClickUp data is unavailable; stale snapshot is read-only.",
                config_mismatch=dict(stale_payload.get("runtime_list", {}).get("config_mismatch") or {}),
                current_invariant=dict(stale_payload.get("runtime_list", {}).get("current_task_invariant") or {}),
            )
            stale_payload["operator_actions_summary"] = list(
                dict.fromkeys(
                    list(stale_payload.get("operator_actions_summary") or [])
                    + [
                        f"Stale snapshot captured at {stale_state.snapshot_timestamp} is being shown instead of live ClickUp data.",
                        f"Source failure: {failure['class']} - {failure['message']}",
                        "Retry live diagnostics before relying on this state for write-sensitive decisions.",
                    ]
                )
            )
            return _annotate_operational_snapshot(stale_payload, stale_state)

        empty_field_conformance = {
            "mode": "unknown",
            "present_fields": [],
            "missing_required_fields": [],
            "missing_recommended_fields": [],
            "missing_optional_fields": [],
            "unexpected_fields": [],
            "capabilities": {},
            "limitations": [],
            "operator_actions_required": [],
            "minimum_viable_guidance": {
                "mode_explanation": "Current execution works, but some scoring, traceability, and cooldown features are degraded.",
                "next_best_fields": [],
                "priority_groups": [],
            },
        }
        empty_pipeline_drift = {
            "status": "unavailable",
            "lists": {},
            "has_drift": None,
            "drifted_tasks": [],
            "guidance": "Move tasks to your Execution Engine list when ready.",
        }

        empty_payload = {
            "topology": TOPOLOGY_DECISION,
            "runtime_list": {
                "configured": {
                    "list_id": settings.clickup_list_id,
                    "list_name": settings.clickup_list_name,
                },
                "resolved": {
                    "list_id": request.app.state.execution_list_id,
                    "list_name": "",
                },
                "task_count": 0,
                "current_task_count": 0,
                "current_task_invariant": {
                    "status": "zero_current",
                    "count": 0,
                    "task_ids": [],
                    "violation": False,
                    "message": "No current task is set in runtime list.",
                },
                "current_tasks": [],
            },
            "field_conformance": empty_field_conformance,
            "selection_visibility": {
                "top_candidates": [],
                "eligibility_summary": {
                    "total_tasks": 0,
                    "eligible_candidate_count": 0,
                    "blocked_or_closed_count": 0,
                    "explanation": "No live data available.",
                },
            },
            "operator_actions_summary": ["Retry live diagnostics. If failure persists, inspect ClickUp access and configuration."],
            "routing_assumptions": {
                "authoritative_runtime_list_only": True,
                "scheduler_reads_runtime_list": True,
                "queue_reads_runtime_list": True,
                "quick_add_writes_runtime_list": True,
                "pipeline_used_for_runtime": False,
            },
            "pipeline_drift": empty_pipeline_drift,
            "degradation_events": getattr(request.app.state, "degradation_events", [])[-20:],
        }
        empty_state = build_operational_state(
            current_task_resolution_state="resolution_blocked_by_source_failure",
            current_task_resolution_next_action=runtime_err.next_step,
            conformance=empty_field_conformance,
            pipeline_drift=empty_pipeline_drift,
            data_freshness="stale",
            snapshot_timestamp="",
            retry_recommended=bool(runtime_err.retry_safe),
            retryable_failure=bool(runtime_err.retry_safe),
            usable_despite_failure=False,
            source_failure=failure,
            promotion_attempted=False,
            promotion_verified=None,
            promotion_reason="No promotion attempted because live ClickUp access failed.",
            top_candidates=[],
            selection_attempted=False,
            selection_not_attempted_reason="No snapshot or live data is available.",
        )
        return JSONResponse(
            status_code=runtime_err.status_code,
            content=_annotate_operational_snapshot(empty_payload, empty_state),
        )


@app.get("/ops/runtime")
async def ops_runtime(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    return await diagnostics_report(request)


@app.post("/ops/remediate/runtime-current")
async def remediate_runtime_current(request: Request) -> Any:
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store

    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
        status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings)
        tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
    except ClickUpError as exc:
        return runtime_state_response(classify_clickup_runtime_error(exc))

    invariant = detect_current_task_invariant(tasks, settings, fields)
    if invariant["status"] != "multi_current":
        return {
            "ok": True,
            "changed": False,
            "message": "No multi-current violation detected.",
            "invariant": invariant,
        }

    current_tasks = [
        task for task in tasks
        if str(task.get("status", {}).get("status", "")).strip().casefold()
        == settings.clickup_current_status.strip().casefold()
    ]
    keep = select_deterministic_current_task(current_tasks, fields, settings)
    if not keep:
        return runtime_state_response(
            RuntimeStateError(
                "scheduler_internal_error",
                "Unable to select a deterministic current task.",
                status_code=500,
            )
        )

    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    queued_option = option_by_label(scheduler_field, "Queued") if scheduler_field else None
    current_option = option_by_label(scheduler_field, "Current") if scheduler_field else None

    def _error_details(exc: ClickUpError) -> dict[str, Any]:
        code = "clickup_write_error"
        if exc.status_code in {401, 403}:
            code = "permission_denied"
        elif exc.status_code == 404:
            code = "resource_not_found"
        elif exc.status_code == 400 and exc.error_code == "INPUT_003":
            code = "misconfigured_input"
        elif exc.status_code == 400:
            code = "invalid_status_or_payload"
        elif exc.status_code and exc.status_code >= 500:
            code = "clickup_server_error"
        return {
            "class": code,
            "status_code": exc.status_code,
            "error_code": exc.error_code,
            "path": exc.path,
            "message": str(exc),
            "body_preview": exc.body_preview,
        }

    demotion_status_candidates: list[str] = []

    def _push_status_candidate(value: str) -> None:
        candidate = str(value or "").strip()
        if not candidate:
            return
        if candidate.casefold() == status_map.active_status.strip().casefold():
            return
        if any(existing.casefold() == candidate.casefold() for existing in demotion_status_candidates):
            return
        demotion_status_candidates.append(candidate)

    _push_status_candidate(status_map.blocked_status or "")
    _push_status_candidate(status_map.available_status)
    warnings: list[str] = []
    demoted: list[str] = []
    demotion_results: list[dict[str, Any]] = []
    deterministic_rule = "highest_task_score_then_lowest_task_id"

    for task in current_tasks:
        task_id = str(task.get("id") or "")
        if task_id == str(keep.get("id") or ""):
            continue
        demoted_status = ""
        status_error: dict[str, Any] | None = None
        for status_candidate in demotion_status_candidates:
            try:
                await clickup.update_task(task_id, status=status_candidate)
                demoted_status = status_candidate
                break
            except ClickUpError as exc:
                status_error = _error_details(exc)
                continue

        scheduler_attempted = bool(scheduler_field)
        scheduler_ok = not scheduler_attempted
        scheduler_error: dict[str, Any] | None = None
        if scheduler_field:
            if queued_option:
                try:
                    await clickup.set_custom_field(task_id, scheduler_field.id, queued_option.id)
                    scheduler_ok = True
                except ClickUpError as exc:
                    warnings.append(f"demote_scheduler_state:{task_id}")
                    scheduler_ok = False
                    scheduler_error = _error_details(exc)
            else:
                scheduler_ok = False
                scheduler_error = {
                    "class": "scheduler_state_option_mismatch",
                    "message": "Queued option is not available on Scheduler State field.",
                }

        if demoted_status:
            demoted.append(task_id)
        else:
            warnings.append(f"demote_status:{task_id}")
            if status_error is None:
                status_error = {
                    "class": "native_status_unresolved",
                    "message": "No safe non-complete native status could be resolved for remediation.",
                }

        demotion_results.append(
            {
                "task_id": task_id,
                "status_write": {
                    "ok": bool(demoted_status),
                    "attempted_statuses": demotion_status_candidates,
                    "applied_status": demoted_status,
                    "error": None if demoted_status else status_error,
                },
                "scheduler_state_write": {
                    "attempted": scheduler_attempted,
                    "ok": scheduler_ok,
                    "field_id": str(scheduler_field.id) if scheduler_field else "",
                    "option_id": str(queued_option.id) if queued_option else "",
                    "error": scheduler_error,
                },
                "remained_current_after_refresh": None,
            }
        )

    keep_write = {
        "attempted": bool(scheduler_field),
        "ok": not bool(scheduler_field),
        "task_id": str(keep.get("id") or ""),
        "field_id": str(scheduler_field.id) if scheduler_field else "",
        "option_id": str(current_option.id) if current_option else "",
        "error": None,
    }
    if scheduler_field:
        if current_option:
            try:
                await clickup.set_custom_field(str(keep.get("id") or ""), scheduler_field.id, current_option.id)
                keep_write["ok"] = True
            except ClickUpError as exc:
                warnings.append(f"keep_scheduler_state:{str(keep.get('id') or '')}")
                keep_write["ok"] = False
                keep_write["error"] = _error_details(exc)
        else:
            keep_write["ok"] = False
            keep_write["error"] = {
                "class": "scheduler_state_option_mismatch",
                "message": "Current option is not available on Scheduler State field.",
            }

    final_invariant = invariant
    refreshed_tasks: list[dict[str, Any]] = []
    try:
        refreshed_tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
        final_invariant = detect_current_task_invariant(refreshed_tasks, settings)
    except ClickUpError:
        warnings.append("post_verify_failed")

    current_status_folded = settings.clickup_current_status.strip().casefold()
    status_by_id = {
        str(task.get("id") or ""): str(task.get("status", {}).get("status", ""))
        for task in refreshed_tasks
    }
    remaining_current_ids = [
        task_id
        for task_id, status in status_by_id.items()
        if status.strip().casefold() == current_status_folded
    ]
    for result in demotion_results:
        task_id = str(result.get("task_id") or "")
        if task_id in status_by_id:
            remained = status_by_id[task_id].strip().casefold() == current_status_folded
            result["remained_current_after_refresh"] = remained

    attempted_demotions = len(demotion_results)
    successful_demotions = sum(1 for item in demotion_results if item.get("status_write", {}).get("ok") is True)
    failed_demotions = attempted_demotions - successful_demotions

    resolved = final_invariant.get("status") != "multi_current"
    changed = successful_demotions > 0
    if resolved and failed_demotions == 0:
        remediation_state = "fully_repaired"
        message = "Multi-current violation repaired."
        next_step = "Reload /active/checkin and continue execution."
    elif resolved:
        remediation_state = "partially_repaired"
        message = (
            f"Invariant repaired, but ClickUp rejected {failed_demotions} demotion write"
            f"{'s' if failed_demotions != 1 else ''}."
        )
        next_step = "Review failed demotions in the remediation result before continuing."
    elif changed:
        remediation_state = "partially_repaired"
        message = (
            "Attempted repair, but invariant remains multi-current after refresh. "
            f"ClickUp rejected {failed_demotions} demotion write{'s' if failed_demotions != 1 else ''}."
        )
        next_step = "Manually move remaining current tasks to queued, break, or blocked in ClickUp, then reload /active/checkin."
    else:
        remediation_state = "attempted_no_live_change"
        message = (
            "Attempted repair, but no live demotion was applied. "
            f"ClickUp rejected {failed_demotions} demotion write{'s' if failed_demotions != 1 else ''}."
        )
        next_step = "Manual fix required in ClickUp: keep one task current and move others to queued, break, or blocked."

    if resolved:
        request.app.state.last_known_operational_snapshot = None
    record_degradation(
        request.app,
        "runtime_remediation",
        (
            "multi_current remediation "
            f"keep={str(keep.get('id') or '')} "
            f"attempted={attempted_demotions} success={successful_demotions} failed={failed_demotions} "
            f"warnings={len(warnings)} resolved={resolved}"
        ),
    )

    return {
        "ok": bool(resolved),
        "changed": bool(changed),
        "invariant_resolved": bool(resolved),
        "remediation_state": remediation_state,
        "message": message,
        "kept_current_task_id": str(keep.get("id") or ""),
        "attempted_demotions": attempted_demotions,
        "successful_demotions": successful_demotions,
        "failed_demotions": failed_demotions,
        "demotion_results": demotion_results,
        "demoted_task_ids": demoted,
        "remaining_current_task_ids": remaining_current_ids,
        "post_check_current_count": int(len(remaining_current_ids)),
        "deterministic_rule": deterministic_rule,
        "partial_failure": bool(warnings) or not resolved,
        "warnings": warnings,
        "invariant": final_invariant,
        "scheduler_state_write": {
            "field_configured_name": settings.field_scheduler_state_name,
            "field_found": bool(scheduler_field),
            "field_id": str(scheduler_field.id) if scheduler_field else "",
            "queued_option_found": bool(queued_option),
            "current_option_found": bool(current_option),
            "keep_current_write": keep_write,
        },
        "status_write_candidates": demotion_status_candidates,
        "next_step": next_step,
    }


@app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    report = await diagnostics_report(request)
    if isinstance(report, Response):
        return report

    field_mode = html.escape(str(report.get("field_conformance", {}).get("mode") or "unknown"))
    op_state = html.escape(str(report.get("operational_state", {}).get("status") or "unknown"))
    invariant = report.get("runtime_list", {}).get("current_task_invariant", {})
    invariant_status = html.escape(str(invariant.get("status") or "unknown"))
    current_count = int(report.get("runtime_list", {}).get("current_task_count") or 0)
    missing_recommended = list(report.get("field_conformance", {}).get("missing_recommended_fields") or [])
    operator_actions = list(report.get("operator_actions_summary") or [])
    pipeline = report.get("pipeline_drift", {})
    non_zero_lists = dict(pipeline.get("non_zero_lists") or {})
    drifted_tasks = list(pipeline.get("drifted_tasks") or [])
    current_tasks = list(report.get("runtime_list", {}).get("current_tasks") or [])
    freshness = html.escape(str(report.get("data_freshness") or "unknown"))
    snapshot_timestamp = html.escape(str(report.get("snapshot_timestamp") or ""))
    retry_note = "Retry recommended." if bool(report.get("retry_recommended")) else ""
    source_failure = dict(report.get("source_failure") or {})
    source_failure_text = (
        f"{html.escape(str(source_failure.get('class') or 'unknown'))}: {html.escape(str(source_failure.get('message') or 'Live ClickUp read failed.'))}"
        if source_failure
        else "None"
    )
    guidance = report.get("field_conformance", {}).get("minimum_viable_guidance", {})
    next_best_fields = list(guidance.get("next_best_fields") or [])
    priority_groups = list(guidance.get("priority_groups") or [])
    top_candidates = list(report.get("selection_visibility", {}).get("top_candidates") or [])
    selection_guidance = dict(report.get("operational_state", {}).get("selection_guidance") or {})
    next_action = html.escape(str(report.get("operational_state", {}).get("next_action") or report.get("current_task_resolution_next_action") or "Open Check-in."))

    def _priority_items(label: str) -> str:
        target = next((group for group in priority_groups if str(group.get("label") or "") == label), {})
        names = list(target.get("fields") or [])
        capability = html.escape(str(target.get("capability") or ""))
        degraded = html.escape(str(target.get("currently_degraded") or ""))
        improve = html.escape(str(target.get("why") or ""))
        if not names:
            return f"<li>No missing fields. Capability: {capability or 'n/a'}.</li>"
        return (
            f"<li>Capability: {capability or 'n/a'}. Improves: {improve} Currently degraded: {degraded} Fields: {', '.join(html.escape(str(name)) for name in names)}</li>"
        )

    current_task_items = "".join(
        f"<li><a href=\"{html.escape(str(item.get('url') or '#'))}\" target=\"_blank\" rel=\"noopener\">{html.escape(str(item.get('name') or item.get('id') or 'task'))}</a>"
        f" ({html.escape(str(item.get('status') or ''))})</li>"
        for item in current_tasks
    ) or "<li>None</li>"
    action_items = "".join(f"<li>{html.escape(str(action))}</li>" for action in operator_actions) or "<li>No operator actions required.</li>"
    missing_items = "".join(f"<li>{html.escape(str(name))}</li>" for name in missing_recommended) or "<li>None</li>"
    drift_items = "".join(f"<li>{html.escape(str(name))}: {int(count)}</li>" for name, count in non_zero_lists.items()) or "<li>No pipeline drift detected.</li>"
    drift_task_items = "".join(
        f"<li>[{html.escape(str(item.get('label') or 'drift'))}] {html.escape(str(item.get('pipeline_list') or 'pipeline'))}: "
        f"<a href=\"{html.escape(str(item.get('url') or '#'))}\" target=\"_blank\" rel=\"noopener\">{html.escape(str(item.get('name') or item.get('id') or 'task'))}</a></li>"
        for item in drifted_tasks
    ) or "<li>No drifted tasks listed.</li>"
    candidate_items = "".join(
        f"<li>{html.escape(str(item.get('name') or item.get('id') or 'task'))}</li>"
        for item in top_candidates
    ) or "<li>No eligible candidates.</li>"
    next_best_items = "".join(f"<li>{html.escape(str(name))}</li>" for name in next_best_fields) or "<li>None</li>"

    def _pill_cls(val: str) -> str:
        v = val.lower()
        if v in ("healthy", "ok", "standard", "normal"): return "healthy"
        if v in ("degraded", "warn", "warning"): return "degraded"
        if v in ("blocked", "error", "failed", "critical"): return "blocked"
        return ""

    page = """<!doctype html>
<html lang='en'>
<head>
    <meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>Execution Operations</title>
    <style>
        __BASE_CSS__
        body { padding: 24px 16px; background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%); }
        .ops-card { max-width: 900px; margin: 0 auto; padding: 28px 24px; }
        h1 { margin: 0 0 4px; font-size: 1.4rem; font-weight: 800; }
        .lead { color: var(--muted); font-size: 0.875rem; margin: 0 0 18px; }
        .row { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
        .section-panel {
            margin-top: 16px;
            padding: 16px;
            background: var(--surface-inner);
            border: 1px solid var(--surface-inner-border);
            border-radius: var(--radius-md);
            box-shadow: var(--shadow-sm);
        }
        .section-label {
            text-transform: uppercase;
            letter-spacing: .05em;
            font-size: 0.7rem;
            font-weight: 700;
            color: var(--ink-secondary);
            margin: 0 0 10px;
        }
        .kv { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 6px; }
        .kv-label { font-weight: 600; font-size: 0.875rem; }
        ul { margin: 6px 0 0; padding-left: 18px; }
        li { font-size: 0.875rem; line-height: 1.6; }
        pre {
            white-space: pre-wrap;
            word-break: break-word;
            background: #0f172a;
            color: #e2e8f0;
            border-radius: var(--radius-md);
            padding: 14px 16px;
            font-family: "SF Mono", "Fira Code", "Consolas", monospace;
            font-size: 0.8rem;
            max-height: 60vh;
            overflow: auto;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <main class='card ops-card'>
        <h1>Execution Operations</h1>
        <p class='lead'>Authoritative runtime is the execution list. Use this page to inspect and fix daily operational state.</p>
        <div class='row'>
            <a class='btn primary' href='/active/checkin'>← Check-in</a>
            <a class='btn secondary' href='/reports/diagnostics' target='_blank' rel='noopener'>Open JSON ↗</a>
            <button class='btn secondary' id='repair-btn'>Repair Multi-Current</button>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Runtime Status</p>
            <div class='kv'>
                <span class='kv-label'>Mode:</span> <span class='pill __FIELD_MODE_CLS__'>__FIELD_MODE__</span>
                <span class='kv-label' style='margin-left:8px;'>Operational State:</span> <span class='pill __OP_STATE_CLS__'>__OP_STATE__</span>
            </div>
            <div class='muted' style='font-size:.875rem;'>Data freshness: __FRESHNESS__ __SNAPSHOT_NOTE__ __RETRY_NOTE__</div>
            <div class='muted' style='font-size:.875rem;'>Source failure: __SOURCE_FAILURE__</div>
            <div class='muted' style='font-size:.875rem;margin-top:6px;'><strong>Next action:</strong> __NEXT_ACTION__</div>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Task Invariant</p>
            <div class='kv'>
                <span class='kv-label'>Current-task invariant:</span> <span class='pill __INVARIANT_CLS__'>__INVARIANT_STATUS__</span>
            </div>
            <div class='muted' style='font-size:.875rem;'>Exact current task count: __CURRENT_COUNT__</div>
            <ul>__CURRENT_TASK_ITEMS__</ul>
            <p class='muted' style='font-size:.875rem;margin-top:8px;'>Zero-current candidates:</p>
            <ul>__CANDIDATE_ITEMS__</ul>
            <div class='muted' style='font-size:.875rem;'>Selection note: __SELECTION_NOTE__</div>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Field Conformance</p>
            <div class='muted' style='font-size:.875rem;'>Missing recommended fields:</div>
            <ul>__MISSING_ITEMS__</ul>
            <div class='muted' style='font-size:.875rem;margin-top:8px;'>Next best fields to add:</div>
            <ul>__NEXT_BEST_ITEMS__</ul>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Pipeline Drift</p>
            <ul>__DRIFT_ITEMS__</ul>
            <p class='muted' style='font-size:.875rem;'>Drifted tasks (never auto-moved):</p>
            <ul>__DRIFT_TASK_ITEMS__</ul>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Operator Actions Required</p>
            <ul>__ACTION_ITEMS__</ul>
            <p class='muted' style='font-size:.875rem;'>Minimum viable guidance — priority groups:</p>
            <div class='muted' style='font-size:.8rem;font-weight:600;margin-top:6px;'>Scheduling correctness</div>
            <ul>__SCHED_PRIORITY_ITEMS__</ul>
            <div class='muted' style='font-size:.8rem;font-weight:600;margin-top:6px;'>Decision quality</div>
            <ul>__DECISION_PRIORITY_ITEMS__</ul>
            <div class='muted' style='font-size:.8rem;font-weight:600;margin-top:6px;'>Execution traceability</div>
            <ul>__TRACE_PRIORITY_ITEMS__</ul>
            <div class='muted' style='font-size:.8rem;font-weight:600;margin-top:6px;'>Operator convenience</div>
            <ul>__CONVENIENCE_PRIORITY_ITEMS__</ul>
        </div>
        <div class='section-panel'>
            <p class='section-label'>Remediation Output</p>
            <pre id='out'>No remediation run in this session.</pre>
        </div>
    </main>
    <script>
        document.getElementById('repair-btn').addEventListener('click', async () => {
            const out = document.getElementById('out');
            out.textContent = 'Running remediation...';
            try {
                const r = await fetch('/ops/remediate/runtime-current', { method: 'POST' });
                if (r.status === 401) { location.href = '/login'; return; }
                const d = await r.json();
                const lines = [];
                if (d.message) lines.push(String(d.message));
                if (typeof d.attempted_demotions === 'number') {
                    lines.push(
                        `Attempted demotions: ${d.attempted_demotions}, successful: ${d.successful_demotions ?? 0}, failed: ${d.failed_demotions ?? 0}`
                    );
                }
                if (Array.isArray(d.remaining_current_task_ids)) {
                    lines.push(`Remaining current tasks after refresh: ${d.remaining_current_task_ids.length}`);
                }
                const failed = Array.isArray(d.demotion_results)
                    ? d.demotion_results.filter(item => item?.status_write?.ok === false).map(item => item.task_id)
                    : [];
                if (failed.length) {
                    lines.push(`ClickUp rejected demotions for tasks: ${failed.join(', ')}`);
                    lines.push('Manual fix required in ClickUp for failed tasks.');
                }
                out.textContent = `${lines.join('\n')}\n\n${JSON.stringify(d, null, 2)}`.trim();
                if (d.ok || d.invariant_resolved) { setTimeout(() => location.reload(), 1200); }
            } catch (e) {
                out.textContent = 'Remediation request failed.';
            }
        });
    </script>
</body>
</html>"""
    page = page.replace("__BASE_CSS__", _BASE_CSS)
    page = page.replace("__FIELD_MODE_CLS__", _pill_cls(field_mode))
    page = page.replace("__OP_STATE_CLS__", _pill_cls(op_state))
    page = page.replace("__INVARIANT_CLS__", _pill_cls(invariant_status))
    page = page.replace("__FIELD_MODE__", field_mode)
    page = page.replace("__OP_STATE__", op_state)
    page = page.replace("__INVARIANT_STATUS__", invariant_status)
    page = page.replace("__CURRENT_COUNT__", str(current_count))
    page = page.replace("__CURRENT_TASK_ITEMS__", current_task_items)
    page = page.replace("__FRESHNESS__", freshness)
    page = page.replace("__SNAPSHOT_NOTE__", f"(snapshot: {snapshot_timestamp})" if snapshot_timestamp else "")
    page = page.replace("__RETRY_NOTE__", html.escape(retry_note))
    page = page.replace("__SOURCE_FAILURE__", source_failure_text)
    page = page.replace("__NEXT_ACTION__", next_action)
    page = page.replace("__CANDIDATE_ITEMS__", candidate_items)
    page = page.replace("__SELECTION_NOTE__", html.escape(str(selection_guidance.get("selection_not_attempted_reason") or "Open Check-in for deterministic auto-selection or set one manually in ClickUp.")))
    page = page.replace("__MISSING_ITEMS__", missing_items)
    page = page.replace("__NEXT_BEST_ITEMS__", next_best_items)
    page = page.replace("__DRIFT_ITEMS__", drift_items)
    page = page.replace("__DRIFT_TASK_ITEMS__", drift_task_items)
    page = page.replace("__ACTION_ITEMS__", action_items)
    page = page.replace("__SCHED_PRIORITY_ITEMS__", _priority_items("Scheduling correctness"))
    page = page.replace("__DECISION_PRIORITY_ITEMS__", _priority_items("Decision quality"))
    page = page.replace("__TRACE_PRIORITY_ITEMS__", _priority_items("Execution traceability"))
    page = page.replace("__CONVENIENCE_PRIORITY_ITEMS__", _priority_items("Operator convenience"))
    return HTMLResponse(minify_html(page))


@app.post("/clickup/webhook")
async def clickup_webhook(request: Request, x_signature: Optional[str] = Header(default=None)) -> dict[str, bool]:
    require_ready(request)
    body = await request.body()
    settings: Settings = request.app.state.settings
    if not settings.clickup_webhook_secret:
        raise HTTPException(status_code=503, detail="ClickUp webhook secret is not configured")
    if not verify_clickup_signature(settings.clickup_webhook_secret, body, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    event = payload.get("event")
    if event and event.startswith("task"):
        async with request.app.state.scheduler_lock:
            result = await run_scheduler(
                settings,
                request.app.state.clickup,
                request.app.state.notifier,
                request.app.state.store,
                request.app.state.execution_list_id,
                suppress_errors=True,
            )
        if not result.get("ok"):
            record_degradation(request.app, "clickup_webhook", str(result.get("error") or "scheduler_run_failed"))
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    require_ready(request)
    settings: Settings = request.app.state.settings
    if settings.telegram_webhook_secret and not verify_shared_secret(
        settings.telegram_webhook_secret,
        x_telegram_bot_api_secret_token,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    callback = payload.get("callback_query")
    if not callback:
        return {"ok": True}

    callback_id = callback["id"]
    data = callback.get("data", "")
    try:
        action, task_id = data.split(":", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid callback payload") from exc

    clickup: ClickUpClient = request.app.state.clickup
    notifier: TelegramNotifier = request.app.state.notifier
    settings: Settings = request.app.state.settings
    store: RuntimeSessionStore = request.app.state.store

    try:
        task = await clickup.get_task(task_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc
    task["_runtime"] = store.get(task_id)
    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc

    if action == "continue":
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=True)
        except ClickUpError as exc:
            raise clickup_http_exception(exc) from exc
        await handle_continue(clickup, store, settings, task, fields, status_map, settings.default_continue_minutes)
        try:
            await notifier.answer_callback(callback_id, "Logged another focus block.")
        except NotificationError:
            pass
    elif action == "complete":
        try:
            status_map = await resolve_runtime_status_map(
                clickup, request.app.state.execution_list_id, settings, require_active=False, require_completed=True
            )
        except ClickUpError as exc:
            raise clickup_http_exception(exc) from exc
        await handle_complete(clickup, store, settings, task, fields, status_map)
        try:
            await notifier.answer_callback(callback_id, "Marked complete. Picking the next task.")
        except NotificationError:
            pass
        async with request.app.state.scheduler_lock:
            result = await run_scheduler(settings, clickup, notifier, store, request.app.state.execution_list_id, suppress_errors=True)
        if not result.get("ok"):
            record_degradation(request.app, "telegram_webhook_complete", str(result.get("error") or "scheduler_run_failed"))
    elif action == "break":
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
        except ClickUpError as exc:
            raise clickup_http_exception(exc) from exc
        await handle_break(clickup, store, settings, task, fields, status_map, settings.short_break_minutes)
        try:
            await notifier.answer_callback(callback_id, "Break started. I will bring work back after the pause.")
            await notifier.send_message(f"Take a {settings.short_break_minutes}-minute break, then reopen the check-in link.")
        except NotificationError:
            pass
    elif action == "switch":
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
        except ClickUpError as exc:
            raise clickup_http_exception(exc) from exc
        await handle_switch(clickup, store, settings, task, fields, status_map)
        try:
            await notifier.answer_callback(callback_id, "Switching tasks without marking blocked.")
        except NotificationError:
            pass
        async with request.app.state.scheduler_lock:
            result = await run_scheduler(settings, clickup, notifier, store, request.app.state.execution_list_id, suppress_errors=True)
        if not result.get("ok"):
            record_degradation(request.app, "telegram_webhook_switch", str(result.get("error") or "scheduler_run_failed"))
    elif action == "blocked":
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
        except ClickUpError as exc:
            raise clickup_http_exception(exc) from exc
        await handle_blocked(clickup, store, settings, task, fields, status_map, settings.blocked_cooldown_minutes)
        try:
            await notifier.answer_callback(callback_id, "Marked blocked. Switching.")
        except NotificationError:
            pass
        async with request.app.state.scheduler_lock:
            result = await run_scheduler(settings, clickup, notifier, store, request.app.state.execution_list_id, suppress_errors=True)
        if not result.get("ok"):
            record_degradation(request.app, "telegram_webhook_blocked", str(result.get("error") or "scheduler_run_failed"))
    else:
        raise HTTPException(status_code=400, detail="Unknown action")

    return {"ok": True}


@app.get("/checkin/{task_id}", response_class=HTMLResponse)
async def checkin_page(task_id: str, request: Request) -> Any:
    require_ready(request)
    session_response = require_session(request, redirect=True)
    if session_response:
        return session_response
    clickup: ClickUpClient = request.app.state.clickup
    settings: Settings = request.app.state.settings
    try:
        task = await clickup.get_task(task_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc
    task["_runtime"] = request.app.state.store.get(task_id)
    safe_task_name = html.escape(task["name"])
    task_url = html.escape(task.get("url", ""))
    block = block_progress(task, settings)
    task_parent_name = ""
    # Count active queue tasks and precompute top priorities for inline context.
    queue_preview: list[dict[str, Any]] = []
    try:
        fields, _raw_tasks = await asyncio.gather(
            clickup.get_list_fields(request.app.state.execution_list_id),
            clickup.get_list_tasks(request.app.state.execution_list_id),
        )
        all_tasks = request.app.state.store.attach_many(_raw_tasks)
        _parent_lookup = {t["id"]: t.get("name", "") for t in all_tasks}
        _parent_id = str(task.get("parent") or "")
        task_parent_name = _parent_lookup.get(_parent_id, "")
        if not task_parent_name and _parent_id:
            try:
                _parent_task = await clickup.get_task(_parent_id)
                task_parent_name = _parent_task.get("name", "") or ""
                _parent_lookup[_parent_id] = task_parent_name
            except Exception:
                pass
        queue_preview = score_queue_tasks(all_tasks, fields, settings, exclude_task_id=task_id, limit=5)
        _all_tasks_by_id = {t["id"]: t for t in all_tasks}
        _unknown_parent_ids = list({
            (_all_tasks_by_id.get(qi["id"], {}).get("parent") or "")
            for qi in queue_preview
            if (_all_tasks_by_id.get(qi["id"], {}).get("parent") or "") not in _parent_lookup
        } - {""})
        if _unknown_parent_ids:
            _fetched = await asyncio.gather(
                *[clickup.get_task(pid) for pid in _unknown_parent_ids],
                return_exceptions=True,
            )
            for _pid, _res in zip(_unknown_parent_ids, _fetched):
                if isinstance(_res, dict):
                    _parent_lookup[_pid] = _res.get("name", "") or ""
        for _qitem in queue_preview:
            _qt = _all_tasks_by_id.get(_qitem["id"], {})
            _qitem["parent_name"] = _parent_lookup.get(_qt.get("parent") or "", "")
        queue_count = sum(
            1 for t in all_tasks
            if t["id"] != task_id
            and not t["status"]["status"].strip().casefold() in {
                settings.clickup_completed_status.strip().casefold(),
                "complete", "completed", "closed",
            }
            and t["status"]["status"].strip().casefold() != settings.clickup_current_status.strip().casefold()
            and (not settings.clickup_blocked_status or t["status"]["status"].strip().casefold() != settings.clickup_blocked_status.strip().casefold())
        )
    except ClickUpError:
        queue_count = 0
    import json as _json
    embedded_data = (
        _json.dumps({
            "task_id": task_id,
            "task_name": task["name"],
            "task_parent_name": task_parent_name,
            "task_url": task.get("url", ""),
            "block": block,
            "queue_count": queue_count,
            "settings": {
                "short_break_minutes": settings.short_break_minutes,
                "long_break_minutes": settings.long_break_minutes,
            },
            "queue_preview": queue_preview,
        })
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Check-in</title>
  <style>
    {_BASE_CSS}
    /* Check-in page extras */
    body {{
      padding: 20px 16px 40px;
      background: linear-gradient(160deg, #ede8dc 0%, #f5f0e8 55%, #f0ece2 100%);
      min-height: 100vh;
    }}
    .card {{
      max-width: 700px;
      margin: 0 auto;
      padding: 28px 26px 24px;
    }}
    /* Task header */
    .task-header {{
      margin-bottom: 20px;
      padding-left: 14px;
      border-left: 3px solid var(--accent);
    }}
    .badge {{
      display: inline-block;
      font-size: 0.65rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      background: linear-gradient(135deg, #0f766e, #0d9488);
      color: white;
      padding: 3px 10px;
      border-radius: var(--radius-pill);
      border: 1px solid rgba(15,118,110,0.30);
      box-shadow: 0 1px 3px rgba(15,118,110,0.25);
      vertical-align: middle;
    }}
    h1 {{
      margin: 6px 0 4px;
      font-size: 1.5rem;
      font-weight: 700;
      line-height: 1.25;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      letter-spacing: -0.01em;
    }}
    .task-link {{
      display: inline-flex;
      align-items: center;
      gap: 3px;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--accent);
      text-decoration: none;
      padding: 3px 9px;
      border-radius: var(--radius-pill);
      border: 1px solid rgba(15,118,110,0.25);
      background: rgba(15,118,110,0.05);
      transition: background var(--transition-fast), border-color var(--transition-fast);
    }}
    .task-link:hover {{
      background: rgba(15,118,110,0.11);
      border-color: rgba(15,118,110,0.40);
      text-decoration: none;
    }}
    .task-link:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    /* Block progress */
    .block-bar-wrap {{
      margin: 16px 0 20px;
    }}
    .block-bar-label {{
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--muted);
      margin-bottom: 6px;
      display: flex;
      justify-content: space-between;
      font-variant-numeric: tabular-nums;
      letter-spacing: 0.01em;
    }}
    .block-bar {{
      height: 10px;
      background: rgba(227,217,204,0.55);
      border-radius: var(--radius-pill);
      overflow: hidden;
      border: 1px solid rgba(227,217,204,0.70);
    }}
    .block-bar-fill {{
      height: 100%;
      border-radius: var(--radius-pill);
      background: linear-gradient(90deg, #0d9488, #0f766e);
      transition: width 0.4s ease, background 0.3s ease;
    }}
    .block-bar-fill.at-target {{ background: linear-gradient(90deg, #d97706, #b45309); }}
    .block-bar-fill.at-max {{ background: linear-gradient(90deg, #dc2626, #991b1b); }}
    /* Parent context */
    .task-parent {{
        font-size: 0.72rem;
        font-weight: 600;
        color: var(--muted);
        letter-spacing: 0.02em;
        margin-bottom: 5px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        opacity: 0.85;
    }}
    .task-parent::before {{ content: "\u203a "; opacity: 0.60; }}
    .up-next-item .item-parent {{
        font-size: 0.67rem;
        font-weight: 500;
        color: var(--muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        opacity: 0.80;
    }}
    .up-next-item .item-parent::before {{ content: "\u203a "; }}
    .queue-item .q-main {{
        flex: 1;
        min-width: 0;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        gap: 1px;
    }}
    .queue-item .q-parent {{
        font-size: 0.65rem;
        font-weight: 500;
        color: var(--muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        opacity: 0.80;
    }}
    .queue-item .q-parent::before {{ content: "\u203a "; }}
        .up-next {{
            margin: 16px 0 20px;
            border: 1px solid var(--surface-inner-border);
            border-radius: var(--radius-md);
            background: var(--surface-inner);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
            padding: 14px 14px 12px;
            box-shadow: var(--shadow-sm);
        }}
        .up-next h2 {{
            margin: 0 0 10px;
            font-size: 0.7rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.09em;
            color: var(--muted);
        }}
        .up-next-list {{ display: grid; gap: 6px; }}
        .up-next-item {{
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            gap: 4px;
            padding: 9px 12px;
            border: 1px solid rgba(227,217,204,0.60);
            border-radius: var(--radius-sm);
            background: rgba(255,253,250,0.70);
            cursor: pointer;
            width: 100%;
            min-width: 0;
            overflow: hidden;
            text-align: left;
            transition: background var(--transition-fast), box-shadow var(--transition-fast), transform var(--transition-fast);
        }}
        .up-next-item:hover {{
            background: rgba(255,253,248,0.95);
            box-shadow: var(--shadow-sm);
            transform: translateY(-1px);
        }}
        .up-next-item:active {{ transform: translateY(0); box-shadow: none; }}
        .up-next-item:focus-visible {{
            outline: 2px solid var(--accent);
            outline-offset: 2px;
        }}
        .up-next-item .name {{
            min-width: 0;
            overflow-wrap: anywhere;
            word-break: break-word;
            white-space: normal;
            font-size: 0.88rem;
            font-weight: 500;
            color: var(--ink);
        }}
        .up-next-item .meta {{
            font-size: 0.72rem;
            color: var(--muted);
            min-width: 0;
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            align-items: center;
        }}
        .reason-chip {{
            display: inline-block;
            padding: 2px 7px;
            border-radius: var(--radius-pill);
            background: #fde8c0;
            color: #92400e;
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 0.03em;
            margin-left: 3px;
            white-space: normal;
            overflow-wrap: anywhere;
        }}
        .up-next-empty {{ color: var(--muted); font-size: 0.85rem; font-style: italic; }}
    /* Pulse section */
    .pulse-section {{
      margin: 4px 0 0;
      padding: 18px 16px 16px;
      border: 1px solid var(--surface-inner-border);
      border-radius: var(--radius-md);
      background: var(--surface-inner);
      box-shadow: var(--shadow-sm);
    }}
    /* Instruction */
    .instruction {{
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin: 0 0 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--surface-inner-border);
    }}
    /* Pulse chips */
    .row {{ margin: 12px 0; }}
    .label {{
      font-weight: 700;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: var(--muted);
      margin-bottom: 7px;
    }}
    .chips {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 7px;
    }}
    .chips.three {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .chip {{
      width: 100%;
      min-height: 42px;
      border: 1.5px solid var(--border);
      background: rgba(255,253,250,0.80);
      border-radius: var(--radius-md);
      padding: 8px 6px;
      font-size: 0.88rem;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
      color: var(--ink-secondary);
      transition: background var(--transition-fast), border-color var(--transition-fast),
                  box-shadow var(--transition-fast), transform var(--transition-fast);
    }}
    .chip:hover {{
      background: rgba(255,250,245,0.95);
      border-color: rgba(15,118,110,0.30);
      box-shadow: var(--shadow-sm);
      transform: translateY(-1px);
    }}
    .chip:active {{ transform: translateY(0); box-shadow: none; }}
    .chip.selected {{
      border-color: var(--accent);
      background: linear-gradient(135deg, rgba(15,118,110,0.08), rgba(13,148,136,0.06));
      color: var(--accent);
      font-weight: 700;
      box-shadow: 0 0 0 3px rgba(15,118,110,0.12);
    }}
    .chip:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    /* Actions section */
    .actions-section {{
      margin-top: 22px;
    }}
    .actions {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    .actions .wide {{ grid-column: 1 / -1; }}
    .act-btn {{
      width: 100%;
      min-height: 48px;
      border: 1.5px solid var(--border);
      background: rgba(255,253,250,0.90);
      border-radius: var(--radius-md);
      padding: 12px 14px;
      font-size: 0.95rem;
      font-weight: 600;
      letter-spacing: 0.01em;
      cursor: pointer;
      font-family: inherit;
      color: var(--ink-secondary);
      position: relative;
      transition: background var(--transition-fast), box-shadow var(--transition-fast),
                  transform var(--transition-fast), border-color var(--transition-fast);
    }}
    .act-btn:hover {{
      background: #faf6f0;
      border-color: rgba(180,160,130,0.60);
      box-shadow: var(--shadow-btn);
      transform: translateY(-1px);
    }}
    .act-btn:active {{ transform: translateY(0); box-shadow: var(--shadow-sm); }}
    .act-btn:disabled {{ opacity: 0.40; cursor: not-allowed; transform: none; box-shadow: none; }}
    .act-btn:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 3px;
    }}
    .act-btn.primary {{
      background: linear-gradient(135deg, #0f766e 0%, #0d9488 100%);
      background-size: 200% 100%;
      background-position: 0% 0%;
      color: white;
      border-color: transparent;
      box-shadow: var(--shadow-btn-colored);
    }}
    .act-btn.primary:hover {{
      background-position: 100% 0%;
      box-shadow: 0 3px 10px rgba(15,118,110,0.38), 0 8px 28px rgba(15,118,110,0.22);
      transform: translateY(-1px);
    }}
    .act-btn.primary:active {{
      transform: translateY(0);
      box-shadow: var(--shadow-btn-colored);
    }}
    @keyframes btn-shimmer {{
      0%   {{ background-position: -200% center; }}
      100% {{ background-position: 200% center; }}
    }}
    .act-btn.warn {{
      background: linear-gradient(135deg, #b45309 0%, #ca6519 100%);
      color: white;
      border-color: transparent;
      box-shadow: 0 2px 6px rgba(180,83,9,0.28), 0 5px 18px rgba(180,83,9,0.16);
    }}
    .act-btn.warn:hover {{
      box-shadow: 0 3px 10px rgba(180,83,9,0.38), 0 8px 26px rgba(180,83,9,0.20);
      transform: translateY(-1px);
    }}
    .act-btn.danger {{
      background: linear-gradient(135deg, #991b1b 0%, #b22222 100%);
      color: white;
      border-color: transparent;
      box-shadow: 0 2px 6px rgba(153,27,27,0.28), 0 5px 18px rgba(153,27,27,0.16);
    }}
    .act-btn.danger:hover {{
      box-shadow: 0 3px 10px rgba(153,27,27,0.38), 0 8px 26px rgba(153,27,27,0.20);
      transform: translateY(-1px);
    }}
    .act-btn .spinner {{
      display: none;
      width: 16px; height: 16px;
      border: 2px solid rgba(255,255,255,0.25);
      border-top-color: rgba(255,255,255,0.90);
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
      margin: 0 auto;
    }}
    .act-btn.loading .btn-label {{ visibility: hidden; }}
    .act-btn.loading .spinner {{ display: inline-block; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    /* Status callout */
    .status-line {{
      margin-top: 14px;
      min-height: 24px;
      font-size: 0.875rem;
      font-weight: 500;
      line-height: 1.5;
      border-radius: var(--radius-sm);
      padding: 0;
      transition: color var(--transition-normal), background var(--transition-normal),
                  border-color var(--transition-normal), padding var(--transition-normal);
    }}
    .status-line:not(:empty) {{
      padding: 9px 13px 9px 13px;
      border-left: 3px solid currentColor;
    }}
    .status-line.success {{
      color: var(--success);
      background: rgba(15,118,110,0.07);
      border-color: var(--success);
    }}
    .status-line.warn {{
      color: var(--warn);
      background: rgba(180,83,9,0.07);
      border-color: var(--warn);
    }}
    .status-line.error {{
      color: var(--error);
      background: rgba(153,27,27,0.07);
      border-color: var(--error);
    }}
    .status-line.loading {{
      color: var(--muted);
      background: rgba(107,114,128,0.06);
      border-color: var(--muted);
    }}
    .status-line a {{
      color: inherit;
      text-decoration: underline;
      cursor: pointer;
      font-weight: 700;
    }}
    /* Queue drawer */
    .drawer-backdrop {{
      display: none;
      position: fixed; inset: 0;
      background: rgba(15,20,30,0.28);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      z-index: 100;
    }}
    .drawer-backdrop.open {{ display: block; }}
    .drawer {{
      position: fixed;
      left: 0; right: 0; bottom: 0;
      background: linear-gradient(160deg, #fffdf9, #fffaf2);
      border-top: 1px solid var(--border);
      border-radius: var(--radius-lg) var(--radius-lg) 0 0;
      padding: 16px 20px 28px;
      max-height: 62vh;
      overflow-y: auto;
      z-index: 101;
      transform: translateY(100%);
      transition: transform 0.28s cubic-bezier(0.22, 1, 0.36, 1);
      box-shadow: 0 -6px 32px rgba(30,40,55,0.12);
    }}
    .drawer-backdrop.open .drawer {{ transform: translateY(0); }}
    .drawer-handle {{
      width: 48px; height: 5px;
      background: var(--border);
      border-radius: var(--radius-pill);
      margin: 0 auto 16px;
    }}
    .drawer h2 {{ margin: 0 0 14px; font-size: 1rem; font-weight: 700; color: var(--ink); }}
    .queue-item {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 11px 13px;
      border: 1px solid rgba(227,217,204,0.70);
      border-radius: var(--radius-sm);
      margin-bottom: 7px;
      cursor: pointer;
      background: rgba(255,253,250,0.80);
      transition: background var(--transition-fast), box-shadow var(--transition-fast), transform var(--transition-fast);
    }}
    .queue-item:hover {{
      background: rgba(255,252,246,0.95);
      box-shadow: var(--shadow-sm);
      transform: translateY(-1px);
    }}
    .queue-item:active {{ transform: translateY(0); box-shadow: none; }}
    .queue-item:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .queue-item .q-name {{
      font-size: 0.9rem;
      font-weight: 500;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--ink);
    }}
    .queue-item .q-badge {{
      font-size: 0.63rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      background: rgba(227,217,204,0.80);
      color: var(--muted);
      padding: 3px 7px;
      border-radius: var(--radius-pill);
      flex-shrink: 0;
    }}
    .drawer-actions {{
      margin-top: 14px;
      display: flex;
      gap: 10px;
    }}
    .drawer-actions button {{
      flex: 1;
      min-height: 44px;
      border: 1.5px solid var(--border);
      background: rgba(255,253,250,0.90);
      border-radius: var(--radius-md);
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
      color: var(--ink-secondary);
      transition: background var(--transition-fast), box-shadow var(--transition-fast), transform var(--transition-fast);
    }}
    .drawer-actions button:hover {{
      background: #faf6f0;
      box-shadow: var(--shadow-sm);
      transform: translateY(-1px);
    }}
    .drawer-actions button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .drawer-actions button.primary {{
      background: linear-gradient(135deg, #0f766e, #0d9488);
      color: white;
      border-color: transparent;
      box-shadow: var(--shadow-btn-colored);
    }}
    .drawer-actions button.primary:hover {{
      box-shadow: 0 3px 10px rgba(15,118,110,0.35), 0 8px 24px rgba(15,118,110,0.20);
    }}
    .drawer-empty {{ color: var(--muted); font-style: italic; margin: 8px 0; font-size: 0.88rem; }}
    .queue-loading {{ color: var(--muted); margin: 8px 0; font-size: 0.88rem; }}
        .quick-add {{
            margin-top: 16px;
            padding-top: 14px;
            border-top: 1px solid var(--surface-inner-border);
        }}
        .quick-add h3 {{ margin: 0 0 9px; font-size: 0.85rem; font-weight: 700; color: var(--ink-secondary); }}
        .quick-add-list {{ display: flex; flex-direction: column; gap: 6px; }}
        .importable-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 11px;
            border-radius: var(--radius-md);
            border: 1.5px solid var(--border);
            background: rgba(255,253,250,0.90);
            cursor: pointer;
            font-size: 0.88rem;
            color: var(--ink);
            transition: background var(--transition-fast), border-color var(--transition-fast);
        }}
        .importable-item:hover {{ background: rgba(13,148,136,0.08); border-color: var(--accent); }}
        .importable-item:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
        .importable-name {{ flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .importable-list {{ font-size: 0.75rem; color: var(--muted); white-space: nowrap; }}
        .importable-priority {{
            font-size: 0.7rem;
            font-weight: 700;
            padding: 1px 5px;
            border-radius: 4px;
            background: rgba(15,118,110,0.10);
            color: var(--accent);
            white-space: nowrap;
        }}
        .importable-priority.urgent {{ background: rgba(220,38,38,0.12); color: #dc2626; }}
        .importable-priority.high {{ background: rgba(234,88,12,0.12); color: #ea580c; }}
        .importable-priority.none {{ display: none; }}
        .quick-add-switch {{
            margin-top: 9px;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.82rem;
            color: var(--muted);
        }}
        .quick-add-msg {{ margin-top: 8px; min-height: 20px; font-size: 0.82rem; color: var(--muted); }}
    /* Session overlay */
    .session-overlay {{
      display: none;
      position: fixed; inset: 0;
      background: rgba(10,15,25,0.45);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      z-index: 200;
      place-items: center;
    }}
    .session-overlay.open {{ display: grid; }}
    .session-box {{
      background: linear-gradient(160deg, #fffdf9, #fffaf2);
      border: 1px solid var(--border);
      border-top: 1px solid rgba(255,255,255,0.85);
      border-radius: var(--radius-lg);
      padding: 36px 28px;
      text-align: center;
      max-width: 340px;
      box-shadow: var(--shadow-elevated);
    }}
    .session-box h2 {{ margin: 0 0 8px; font-size: 1.2rem; font-weight: 700; color: var(--ink); }}
    .session-box p {{ color: var(--muted); margin: 0; font-size: 0.9rem; }}
    .session-box a {{
      display: inline-block;
      margin-top: 20px;
      padding: 12px 36px;
      background: linear-gradient(135deg, #0f766e, #0d9488);
      color: white;
      border-radius: var(--radius-md);
      text-decoration: none;
      font-weight: 700;
      font-size: 0.95rem;
      box-shadow: var(--shadow-btn-colored);
      transition: box-shadow var(--transition-fast), transform var(--transition-fast);
    }}
    .session-box a:hover {{
      box-shadow: 0 3px 10px rgba(15,118,110,0.38), 0 8px 26px rgba(15,118,110,0.20);
      transform: translateY(-1px);
    }}
    .session-box a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
    /* Responsive */
    @media (max-width: 480px) {{
      .card {{ padding: 22px 18px 20px; }}
      .chips {{ gap: 6px; }}
      .actions {{ gap: 8px; }}
      h1 {{ font-size: 1.25rem; }}
      .quick-add-list {{ gap: 4px; }}
    }}
    @media (max-width: 380px) {{
      body {{ padding: 12px 8px 32px; }}
      .card {{ padding: 18px 14px 16px; border-radius: var(--radius-md); }}
      .actions {{ grid-template-columns: 1fr; }}
      .actions .wide {{ grid-column: 1; }}
      .chip {{ min-height: 44px; font-size: 0.82rem; }}
      .act-btn {{ min-height: 50px; font-size: 0.9rem; }}
      .block-bar-label {{ font-size: 0.73rem; }}
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="task-header">
      {f'<div class="task-parent" title="{html.escape(task_parent_name)}">{html.escape(task_parent_name)}</div>' if task_parent_name else ''}
      <span class="badge">Current</span>
      <h1>{safe_task_name}</h1>
      <a href="{task_url}" target="_blank" rel="noopener" class="task-link">Open in ClickUp \u2197</a>
    </div>
    <div class="block-bar-wrap">
      <div class="block-bar-label">
        <span id="block-label">{block['block_minutes']}/{block['target_minutes']}m</span>
        <span id="block-remaining">{block['remaining_minutes']}m to target</span>
      </div>
      <div class="block-bar">
        <div id="block-fill" class="block-bar-fill" style="width:{min(block['block_minutes'] / max(block['block_minutes'], settings.block_max_minutes, 1) * 100, 100):.0f}%"></div>
      </div>
    </div>
        <section class="up-next" aria-label="Up next tasks">
            <h2>Up Next</h2>
            <div id="up-next-list" class="up-next-list"></div>
        </section>
    <div class="pulse-section">
    <p class="instruction">Pulse check &mdash; select answers then choose an action below.</p>
    <div class="row">
      <div class="label">Progress</div>
      <div class="chips" data-field="progress">
        <button class="chip" data-value="none">none</button>
        <button class="chip" data-value="low">low</button>
        <button class="chip" data-value="medium">medium</button>
        <button class="chip" data-value="high">high</button>
      </div>
    </div>
    <div class="row">
      <div class="label">Energy</div>
      <div class="chips three" data-field="energy">
        <button class="chip" data-value="low">low</button>
        <button class="chip" data-value="medium">medium</button>
        <button class="chip" data-value="high">high</button>
      </div>
    </div>
    <div class="row">
      <div class="label">Friction</div>
      <div class="chips three" data-field="friction">
        <button class="chip" data-value="none">none</button>
        <button class="chip" data-value="some">some</button>
        <button class="chip" data-value="high">high</button>
      </div>
    </div>
    </div><!-- /.pulse-section -->
    <div class="actions-section">
    <div class="actions">
      <button class="act-btn primary" data-action="continue"><span class="btn-label">Continue Slice</span><span class="spinner"></span></button>
      <button class="act-btn" id="btn-switch" data-action="drawer"><span class="btn-label">Switch \u25be</span><span class="spinner"></span></button>
      <button class="act-btn" data-action="break" data-break="{settings.short_break_minutes}"><span class="btn-label">Break {settings.short_break_minutes}m</span><span class="spinner"></span></button>
      <button class="act-btn warn" data-action="break" data-break="{settings.long_break_minutes}"><span class="btn-label">Long Break {settings.long_break_minutes}m</span><span class="spinner"></span></button>
      <button class="act-btn wide" data-action="complete"><span class="btn-label">Complete Task</span><span class="spinner"></span></button>
      <button class="act-btn danger wide" data-action="blocked"><span class="btn-label">Blocked</span><span class="spinner"></span></button>
    </div>
    <div id="status" class="status-line"></div>
    </div><!-- /.actions-section -->
  </div>

  <!-- Queue drawer -->
  <div id="drawer-backdrop" class="drawer-backdrop">
    <div class="drawer">
      <div class="drawer-handle"></div>
      <h2>Next Priorities</h2>
      <div id="queue-list"><div class="queue-loading">Loading queue\u2026</div></div>
      <div class="drawer-actions">
        <button class="primary" id="btn-auto-switch">Let scheduler choose</button>
        <button id="btn-close-drawer">Close</button>
      </div>
            <section class="quick-add" aria-label="Add task from elsewhere">
                <h3>Add from ClickUp</h3>
                <div id="quick-add-list" class="quick-add-list"><div class="queue-loading">Retrieving…</div></div>
                <label class="quick-add-switch">
                    <input id="quick-add-switch-now" type="checkbox" />
                    Start this task now
                </label>
                <div id="quick-add-msg" class="quick-add-msg"></div>
            </section>
    </div>
  </div>

  <!-- Session expired overlay -->
  <div id="session-overlay" class="session-overlay">
    <div class="session-box">
      <h2>Session expired</h2>
      <p>Log in to continue.</p>
      <a href="/login">Log in</a>
    </div>
  </div>

  <script>
  (function() {{
    "use strict";
    window.__CHECKIN_DATA__ = {embedded_data};
    const D = window.__CHECKIN_DATA__;
    const TASK_ID = D.task_id;
    const SLOW_MS = 3000;
    const TIMEOUT_MS = 10000;
    const REDIRECT_MS = 1500;
        const UI_STATE = Object.freeze({{
            INITIAL_LOAD: "initial_load",
            NO_TASK: "no_task",
            TASK_LOADED: "task_loaded",
            SAVING: "saving",
            SLOW_SAVING: "slow_saving",
            SUCCESS: "success",
            PARTIAL_FAILURE: "partial_failure",
            HARD_FAILURE: "hard_failure",
            SWITCHING: "switching",
            ADDING_TASK: "adding_task",
            SESSION_EXPIRED: "session_expired"
        }});
        let uiState = UI_STATE.INITIAL_LOAD;

    // --- Pulse state ---
    const pulse = {{ progress: "medium", energy: "medium", friction: "none" }};
    document.querySelectorAll("[data-field]").forEach(group => {{
      const f = group.dataset.field;
      const btns = [...group.querySelectorAll(".chip")];
      const set = v => {{ pulse[f] = v; btns.forEach(b => b.classList.toggle("selected", b.dataset.value === v)); }};
      btns.forEach(b => b.addEventListener("click", () => set(b.dataset.value)));
      set(pulse[f]);
    }});

        function setUiState(nextState) {{
            uiState = nextState;
            if (nextState === UI_STATE.SAVING || nextState === UI_STATE.SLOW_SAVING || nextState === UI_STATE.SWITCHING || nextState === UI_STATE.ADDING_TASK) {{
                disableAll();
            }} else if (nextState === UI_STATE.TASK_LOADED || nextState === UI_STATE.SUCCESS || nextState === UI_STATE.PARTIAL_FAILURE || nextState === UI_STATE.HARD_FAILURE) {{
                enableAll();
            }}
        }}

    // --- Status line ---
    const statusEl = document.getElementById("status");
    function setStatus(text, cls, extra) {{
      statusEl.className = "status-line " + cls;
      statusEl.innerHTML = text + (extra || "");
    }}
    function clearStatus() {{ statusEl.className = "status-line"; statusEl.innerHTML = ""; }}

    // --- Block progress bar ---
    const fillEl = document.getElementById("block-fill");
    const labelEl = document.getElementById("block-label");
    const remainEl = document.getElementById("block-remaining");
    function updateBlock(block) {{
      if (!block) return;
      const pct = Math.min(block.block_minutes / Math.max(block.block_minutes, {settings.block_max_minutes}, 1) * 100, 100);
      fillEl.style.width = pct + "%";
      fillEl.className = "block-bar-fill" + (block.exceeded_max ? " at-max" : block.reached_target ? " at-target" : "");
      labelEl.textContent = block.block_minutes + "/" + block.target_minutes + "m";
      remainEl.textContent = block.remaining_minutes + "m to target";
    }}

        function renderUpNext(tasks) {{
            const host = document.getElementById("up-next-list");
            if (!host) return;
            if (!tasks || tasks.length === 0) {{
                host.innerHTML = '<div class="up-next-empty">No other tasks in queue right now.</div>';
                return;
            }}
            const frag = document.createDocumentFragment();
            tasks.slice(0, 5).forEach(t => {{
                const el = document.createElement("button");
                el.type = "button";
                el.className = "up-next-item";
                const score = typeof t.score === "number" ? String(t.score) : "";
                const meta = [t.task_type || "", score ? ("score " + score) : ""].filter(Boolean).join(" · ");
                const reasons = (t.reasons || []).slice(0, 2).map(r => '<span class="reason-chip">' + escapeHtml(r) + '</span>').join("");
                const parentHtml = t.parent_name ? '<span class="item-parent">' + escapeHtml(t.parent_name) + '</span>' : '';
                el.innerHTML = parentHtml +
                    '<span class="name">' + escapeHtml(t.name || "") + '</span>' +
                    '<span class="meta">' + escapeHtml(meta) + reasons + '</span>';
                el.addEventListener("click", () => switchTo(t.id, t.name || "Next task"));
                frag.appendChild(el);
            }});
            host.replaceChildren(...frag.childNodes);
        }}

        renderUpNext(D.queue_preview || []);

    // --- Action buttons ---
    const allBtns = [...document.querySelectorAll(".act-btn")];
    function disableAll() {{ allBtns.forEach(b => b.disabled = true); }}
    function enableAll() {{ allBtns.forEach(b => {{ b.disabled = false; b.classList.remove("loading"); }}); }}
    function setLoading(btn) {{ btn.classList.add("loading"); disableAll(); }}

    // --- Session check ---
    function checkSession(status) {{
      if (status === 401) {{
                setUiState(UI_STATE.SESSION_EXPIRED);
        document.getElementById("session-overlay").classList.add("open");
        return true;
      }}
      return false;
    }}

    // --- Core submit ---
    let pendingAbort = null;
    async function submit(action, breakMin) {{
      clearStatus();
      const triggerBtn = document.querySelector('[data-action="' + action + '"]') || allBtns[0];
      setLoading(triggerBtn);
            setUiState(action === "switch" ? UI_STATE.SWITCHING : UI_STATE.SAVING);
      setStatus("Saving\u2026", "loading");

      const controller = new AbortController();
      pendingAbort = controller;
            const slowTimer = setTimeout(() => {{
                setUiState(UI_STATE.SLOW_SAVING);
                const slowMsg = navigator.onLine
                    ? "Still working\u2026 waiting on server response."
                    : "Still working\u2026 network connection looks unstable.";
                setStatus(slowMsg, "loading");
            }}, SLOW_MS);
      const timeoutTimer = setTimeout(() => controller.abort(), TIMEOUT_MS);

      let lastAction = action;
      let lastBreak = breakMin;

      try {{
        const resp = await fetch("/checkin/" + TASK_ID, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            action: action,
            break_minutes: breakMin,
            progress: pulse.progress,
            energy: pulse.energy,
            friction: pulse.friction
          }}),
          signal: controller.signal
        }});
        clearTimeout(slowTimer);
        clearTimeout(timeoutTimer);

        if (checkSession(resp.status)) return;

        const data = await resp.json().catch(() => null);

        if (!resp.ok) {{
                    const fallback = resp.status >= 500
                        ? "Server error while saving."
                        : (resp.status === 422 ? "Invalid input. Please review your selections." : "Something went wrong.");
          const msg = (data && data.message) || fallback;
          const retrySafe = data && data.retry_safe;
          const retryLink = retrySafe ? ' <a href="#" id="retry-link">Retry</a>' : '';
                    setUiState(UI_STATE.HARD_FAILURE);
          setStatus(msg, "error", retryLink);
          enableAll();
          if (retrySafe) {{
            document.getElementById("retry-link").addEventListener("click", e => {{
              e.preventDefault();
              submit(lastAction, lastBreak);
            }});
          }}
          return;
        }}

        if (data.ok === false) {{
                    setUiState(UI_STATE.HARD_FAILURE);
          const msg = (data && (data.ui_message || data.message)) || "Action could not be verified.";
          setStatus(msg, (data && data.ui_severity) || "error");
          enableAll();
          return;
        }}

        if (data.verification_status === "unverified") {{
                    setUiState(UI_STATE.PARTIAL_FAILURE);
          setStatus((data && data.ui_message) || "Action response returned before verification completed. Reload to confirm the latest state.", (data && data.ui_severity) || "warn");
          updateBlock(data.block);
          if (data.redirect_to) {{
            setTimeout(() => {{ window.location.href = data.redirect_to; }}, REDIRECT_MS);
          }}
                    setTimeout(() => {{ clearStatus(); setUiState(UI_STATE.TASK_LOADED); }}, 5000);
          return;
        }}

        if (data.partial_failure) {{
                    setUiState(UI_STATE.PARTIAL_FAILURE);
          setStatus((data && data.ui_message) || "Action saved, but some fields didn\u2019t update. Your progress is safe.", (data && data.ui_severity) || "warn");
          updateBlock(data.block);
          if (data.redirect_to) {{
            setTimeout(() => {{ window.location.href = data.redirect_to; }}, REDIRECT_MS);
          }}
                    setTimeout(() => {{ clearStatus(); setUiState(UI_STATE.TASK_LOADED); }}, 4000);
          return;
        }}

        // Success
                setUiState(UI_STATE.SUCCESS);
        setStatus((data && data.ui_message) || data.message, (data && data.ui_severity) || "success");
        updateBlock(data.block);

        if (data.redirect_to) {{
          setStatus((data && data.ui_message) || data.message, (data && data.ui_severity) || "success", ' <a href="#" id="cancel-redirect">Cancel</a>');
          let cancelled = false;
          const link = document.getElementById("cancel-redirect");
          if (link) link.addEventListener("click", e => {{ e.preventDefault(); cancelled = true; clearStatus(); enableAll(); }});
          setTimeout(() => {{
            if (!cancelled) window.location.href = data.redirect_to;
          }}, REDIRECT_MS);
        }} else {{
                    setTimeout(() => setUiState(UI_STATE.TASK_LOADED), 300);
        }}

      }} catch (err) {{
        clearTimeout(slowTimer);
        clearTimeout(timeoutTimer);
        if (err.name === "AbortError") {{
                    setUiState(UI_STATE.HARD_FAILURE);
          setStatus("Timed out before the server could confirm the result. " +
            '<a href="#" id="retry-link">Retry</a> \u00b7 ' +
            '<a href="#" id="reload-link">Reload</a>', "warn");
          enableAll();
          document.getElementById("retry-link").addEventListener("click", e => {{ e.preventDefault(); submit(lastAction, lastBreak); }});
          document.getElementById("reload-link").addEventListener("click", e => {{ e.preventDefault(); window.location.reload(); }});
        }} else {{
                    setUiState(UI_STATE.HARD_FAILURE);
          setStatus("\u2717 Couldn\u2019t reach the server. Check your connection. " +
            '<a href="#" id="retry-link">Retry</a>', "error");
          enableAll();
          document.getElementById("retry-link").addEventListener("click", e => {{ e.preventDefault(); submit(lastAction, lastBreak); }});
        }}
      }}
    }}

    // --- Button handlers ---
    allBtns.forEach(btn => {{
      btn.addEventListener("click", () => {{
        const action = btn.dataset.action;
        if (action === "drawer") {{
          openDrawer();
          return;
        }}
        const breakMin = btn.dataset.break ? parseInt(btn.dataset.break) : undefined;
        submit(action, breakMin);
      }});
    }});

    // --- Queue drawer ---
    const backdrop = document.getElementById("drawer-backdrop");
    const queueList = document.getElementById("queue-list");
    const quickAddSwitchNow = document.getElementById("quick-add-switch-now");
    const quickAddMsg = document.getElementById("quick-add-msg");

    function openDrawer() {{
      backdrop.classList.add("open");
      loadQueue();
      loadImportable();
    }}
    function closeDrawer() {{
      backdrop.classList.remove("open");
    }}
    backdrop.addEventListener("click", e => {{
      if (e.target === backdrop) closeDrawer();
    }});
    document.getElementById("btn-close-drawer").addEventListener("click", closeDrawer);
    document.getElementById("btn-auto-switch").addEventListener("click", () => {{
      closeDrawer();
      submit("switch");
    }});

        function setQuickAddMessage(text, cls) {{
            quickAddMsg.className = "quick-add-msg " + (cls || "");
            quickAddMsg.innerHTML = text || "";
        }}

    function renderQueue(data) {{
      if (!data.tasks || data.tasks.length === 0) {{
        queueList.innerHTML = '<div class="drawer-empty">No other tasks in queue.</div>';
        return;
      }}
      const frag = document.createDocumentFragment();
      data.tasks.forEach(t => {{
        const el = document.createElement("div");
        el.className = "queue-item";
        const reason = (t.reasons && t.reasons.length) ? ('<span class="reason-chip">' + escapeHtml(t.reasons[0]) + '</span>') : '';
        const qParent = t.parent_name ? '<span class="q-parent">' + escapeHtml(t.parent_name) + '</span>' : '';
        el.innerHTML = '<div class="q-main"><span class="q-name">' + escapeHtml(t.name) + '</span>' + qParent + '</div>' +
                      (t.task_type ? '<span class="q-badge">' + escapeHtml(t.task_type) + '</span>' : '') + reason;
        el.addEventListener("click", () => {{
          closeDrawer();
          switchTo(t.id, t.name || "Next task");
        }});
        frag.appendChild(el);
      }});
      queueList.replaceChildren(...frag.childNodes);
    }}

    function renderImportableData(data) {{
      const container = document.getElementById("quick-add-list");
      if (!container) return;
      if (!data.tasks || data.tasks.length === 0) {{
        container.innerHTML = '<div class="drawer-empty">No tasks from other lists to add.</div>';
        return;
      }}
      const frag = document.createDocumentFragment();
      data.tasks.forEach(t => {{
        const el = document.createElement("button");
        el.className = "importable-item";
        el.type = "button";
        const pClass = "importable-priority " + (t.priority_label || "none");
        const pLabel = (t.priority_label && t.priority_label !== "none") ? escapeHtml(t.priority_label) : "";
        el.innerHTML =
            '<span class="importable-name">' + escapeHtml(t.name) + '</span>' +
            (t.list_name ? '<span class="importable-list">' + escapeHtml(t.list_name) + '</span>' : '') +
            (pLabel ? '<span class="' + pClass + '">' + pLabel + '</span>' : '');
        el.setAttribute("aria-label", "Add " + t.name + " to Execution Engine");
        el.addEventListener("click", () => enrollTask(t.id, t.name));
        frag.appendChild(el);
      }});
      container.replaceChildren(...frag.childNodes);
    }}

    async function loadQueue() {{
      queueList.innerHTML = '<div class="queue-loading">Loading queue\u2026</div>';
      try {{
        const resp = await fetch("/api/queue");
        if (checkSession(resp.status)) return;
        if (!resp.ok) {{
          queueList.innerHTML = '<div class="drawer-empty">Couldn\u2019t load queue.</div>';
          return;
        }}
        const data = await resp.json();
        renderQueue(data);
      }} catch (err) {{
        queueList.innerHTML = '<div class="drawer-empty">Couldn\u2019t load queue.</div>';
      }}
    }}

        async function loadImportable() {{
            const container = document.getElementById("quick-add-list");
            if (!container) return;
            container.innerHTML = '<div class="queue-loading">Retrieving\u2026</div>';
            try {{
                const resp = await fetch("/api/tasks/importable");
                if (checkSession(resp.status)) return;
                if (!resp.ok) {{
                    container.innerHTML = '<div class="drawer-empty">Couldn\u2019t load candidates.</div>';
                    return;
                }}
                const data = await resp.json();
                renderImportableData(data);
            }} catch (err) {{
                container.innerHTML = '<div class="drawer-empty">Couldn\u2019t load candidates.</div>';
            }}
        }}

        async function enrollTask(taskId, taskName) {{
            if (!taskId) return;
            const switchNow = !!(quickAddSwitchNow && quickAddSwitchNow.checked);
            setUiState(UI_STATE.ADDING_TASK);
            setQuickAddMessage("Adding \u201c" + escapeHtml(taskName) + "\u201d\u2026", "loading");
            try {{
                const resp = await fetch("/api/tasks/quick-add", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ task_id: taskId, switch_to: switchNow }})
                }});
                if (checkSession(resp.status)) return;
                const data = await resp.json().catch(() => null);
                if (!resp.ok || !data || !data.ok) {{
                    const msg = (data && data.message) || "Couldn\u2019t add task. Try again.";
                    setUiState(UI_STATE.HARD_FAILURE);
                    setQuickAddMessage("\u2717 " + escapeHtml(msg), "error");
                    return;
                }}
                const inserted = {{
                    id: data.task_id,
                    name: data.name,
                    score: "new",
                    reasons: data.warnings && data.warnings.length ? ["needs sync"] : ["new"]
                }};
                D.queue_preview = [inserted].concat(D.queue_preview || []).slice(0, 5);
                renderUpNext(D.queue_preview);
                await Promise.all([loadQueue(), loadImportable()]);
                if (quickAddSwitchNow) quickAddSwitchNow.checked = false;
                setUiState(UI_STATE.SUCCESS);
                const warningText = data.partial_failure ? ' Field sync partial.' : '';
                if (data.redirect_to) {{
                    setQuickAddMessage('Added and switching now\u2026 <a href="#" id="cancel-quick-redirect">Cancel</a>' + warningText, "success");
                    let cancelled = false;
                    const cancel = document.getElementById("cancel-quick-redirect");
                    if (cancel) cancel.addEventListener("click", e => {{ e.preventDefault(); cancelled = true; }});
                    setTimeout(() => {{ if (!cancelled) window.location.href = data.redirect_to; }}, REDIRECT_MS);
                    return;
                }}
                setQuickAddMessage('\u2713 Added to queue. <a href="#" id="quick-switch-link">Switch now</a>' + warningText, "success");
                const switchLink = document.getElementById("quick-switch-link");
                if (switchLink) {{
                    switchLink.addEventListener("click", e => {{
                        e.preventDefault();
                        closeDrawer();
                        switchTo(data.task_id, data.name);
                    }});
                }}
            }} catch (err) {{
                setUiState(UI_STATE.HARD_FAILURE);
                setQuickAddMessage("\u2717 Couldn\u2019t add task. Check your connection.", "error");
            }} finally {{
                if (uiState !== UI_STATE.SESSION_EXPIRED) setUiState(UI_STATE.TASK_LOADED);
            }}
        }}

        async function switchTo(targetId, targetName) {{
      clearStatus();
      const btn = document.getElementById("btn-switch");
      setLoading(btn);
            setUiState(UI_STATE.SWITCHING);
            setStatus("Switching to " + escapeHtml(targetName || "next task") + "\u2026", "loading");
      try {{
        const resp = await fetch("/api/switch-to/" + targetId, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ progress: pulse.progress, energy: pulse.energy, friction: pulse.friction }})
        }});
        if (checkSession(resp.status)) return;
        const data = await resp.json().catch(() => null);
        if (!resp.ok) {{
                    setUiState(UI_STATE.HARD_FAILURE);
          setStatus("\u2717 " + ((data && data.message) || "Switch failed."), "error");
          enableAll();
          return;
        }}
                setUiState(UI_STATE.SUCCESS);
                setStatus("\u2713 Switching to " + escapeHtml(targetName || "next task") + "\u2026", "success");
        setTimeout(() => {{ window.location.href = data.redirect_to || "/active/checkin"; }}, REDIRECT_MS);
      }} catch (err) {{
                setUiState(UI_STATE.HARD_FAILURE);
        setStatus("\u2717 Couldn\u2019t reach the server.", "error");
        enableAll();
      }}
    }}

    function escapeHtml(s) {{
      const el = document.createElement("span");
      el.textContent = s;
      return el.innerHTML;
    }}

    // --- Offline banner ---
    window.addEventListener("offline", () => setStatus("You\u2019re offline. Actions won\u2019t save.", "warn"));
    window.addEventListener("online", () => {{ if (statusEl.textContent.includes("offline")) clearStatus(); }});
        setUiState(UI_STATE.TASK_LOADED);
  }})();
  </script>
</body>
</html>"""


_PRIORITY_ORDER: dict[str | None, int] = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


@app.get("/api/tasks/importable")
async def api_tasks_importable(request: Request) -> Any:
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup

    if not settings.clickup_workspace_id:
        return {"tasks": []}

    execution_list_id = request.app.state.execution_list_id

    try:
        all_tasks = await clickup.get_team_tasks(settings.clickup_workspace_id)
    except ClickUpError:
        return {"tasks": []}

    candidates = []
    for task in all_tasks:
        # Skip closed tasks
        status_type = str((task.get("status") or {}).get("type") or "").strip().lower()
        if status_type == "closed":
            continue
        # Skip tasks already in the engine list
        task_list_id = str((task.get("list") or {}).get("id") or "")
        if task_list_id == execution_list_id:
            continue
        # Only include leaf tasks (no subtasks/children) - skip parent/folder tasks
        subtask_ids = task.get("subtask_ids") or []
        if isinstance(subtask_ids, list) and len(subtask_ids) > 0:
            # This is a parent task with children - skip it
            continue
        priority_raw = str((task.get("priority") or {}).get("priority") or "").strip().lower() or None
        due = task.get("due_date")
        due_ts = int(due) if due else None
        candidates.append({
            "id": str(task.get("id") or ""),
            "name": str(task.get("name") or "").strip(),
            "list_name": str((task.get("list") or {}).get("name") or "").strip(),
            "priority_label": priority_raw or "none",
            "status": str((task.get("status") or {}).get("status") or "").strip(),
            "_priority_sort": _PRIORITY_ORDER.get(priority_raw, 4),
            "_due_sort": due_ts if due_ts is not None else 2**53,
        })

    candidates.sort(key=lambda t: (t["_priority_sort"], t["_due_sort"]))
    top = candidates[:5]
    for t in top:
        del t["_priority_sort"]
        del t["_due_sort"]

    return {"tasks": top}


@app.get("/api/queue")
async def api_queue(request: Request) -> Any:
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    store: RuntimeSessionStore = request.app.state.store
    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
        tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc
    # Find the current task ID to exclude from queue
    current_id = None
    for t in tasks:
        if t["status"]["status"].strip().casefold() == settings.clickup_current_status.strip().casefold():
            current_id = t["id"]
            break
    _parent_lookup = {t["id"]: t.get("name", "") for t in tasks}
    _tasks_by_id = {t["id"]: t for t in tasks}
    queue = score_queue_tasks(tasks, fields, settings, exclude_task_id=current_id, limit=5)
    for _qitem in queue:
        _qt = _tasks_by_id.get(_qitem["id"], {})
        _qitem["parent_name"] = _parent_lookup.get(_qt.get("parent") or "", "")
    return {"tasks": queue, "count": len(queue)}


@app.post("/api/switch-to/{target_task_id}")
async def api_switch_to(target_task_id: str, request: Request) -> Any:
    """Switch from the current task to a specific target task."""
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup
    notifier = request.app.state.notifier
    store: RuntimeSessionStore = request.app.state.store
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    progress = normalize_choice(payload.get("progress"), ALLOWED_PROGRESS)
    energy = normalize_choice(payload.get("energy"), ALLOWED_ENERGY)
    friction = normalize_choice(payload.get("friction"), ALLOWED_FRICTION)

    # Find current task and switch it away
    try:
        tasks = store.attach_many(await clickup.get_list_tasks(request.app.state.execution_list_id))
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
    except ClickUpError as exc:
        return checkin_error_response(exc)

    current_task = None
    for t in tasks:
        if t["status"]["status"].strip().casefold() == settings.clickup_current_status.strip().casefold():
            current_task = t
            break

    if current_task:
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
            switch_result = await handle_switch(
                clickup,
                store,
                settings,
                current_task,
                fields,
                status_map,
                progress=progress,
                energy=energy,
                friction=friction,
            )
        except ClickUpError as exc:
            return checkin_error_response(exc)
    else:
        switch_result = {"partial_failure": False, "failures": []}

    # Promote the target task to current.
    target_warnings: list[str] = []
    fields_by_name = field_by_name(fields)
    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    try:
        promotion_status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=True)
    except ClickUpError as exc:
        return checkin_error_response(exc)
    try:
        await clickup.update_task(target_task_id, status=promotion_status_map.active_status)
    except ClickUpError as exc:
        return checkin_error_response(
            ClickUpError(
                f"Switch target task status update failed: {exc}",
                status_code=exc.status_code,
                error_code=exc.error_code,
                body_preview=exc.body_preview,
                path=exc.path,
            )
        )
    if scheduler_field:
        scheduler_option = option_by_label(scheduler_field, "Current")
        if scheduler_option:
            try:
                await clickup.set_custom_field(target_task_id, scheduler_field.id, scheduler_option.id)
            except ClickUpError:
                target_warnings.append("target_scheduler_state")

    # Notify
    try:
        target = await clickup.get_task(target_task_id)
        checkin_url = f"{settings.public_base_url}/checkin/{target_task_id}"
        try:
            await notifier.send_task_prompt(target, checkin_url)
        except NotificationError:
            pass
    except ClickUpError:
        pass

    return {
        "ok": True,
        "message": "\u2713 Switching\u2026",
        "redirect_to": f"/checkin/{target_task_id}",
        "next_task": {"id": target_task_id},
        "partial_failure": bool(switch_result.get("partial_failure") or target_warnings),
        "warnings": list(switch_result.get("failures") or []) + target_warnings,
    }


@app.post("/api/tasks/quick-add")
async def api_quick_add_task(request: Request) -> Any:
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    clickup: ClickUpClient = request.app.state.clickup

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    task_id = str(payload.get("task_id") or "").strip()
    switch_to = bool(payload.get("switch_to", False))

    if not task_id:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": "task_id is required."},
        )

    # Fetch the task first so we have its name and can confirm it exists
    try:
        task = await clickup.get_task(task_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc

    name = str(task.get("name") or "").strip()
    task_url = str(task.get("url") or "")
    warnings: list[str] = []

    # Enroll the task into the engine list (multi-list — task stays in its original list)
    try:
        await clickup.add_task_to_list(request.app.state.execution_list_id, task_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc

    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
        fields_by_name = field_by_name(fields)
    except ClickUpError:
        warnings.append("field_lookup_failed")
        fields_by_name = {}

    scheduler_field = fields_by_name.get(settings.field_scheduler_state_name)
    progress_field = fields_by_name.get(settings.field_progress_pulse_name)
    energy_field = fields_by_name.get(settings.field_energy_pulse_name)
    friction_field = fields_by_name.get(settings.field_friction_pulse_name)

    async def _set_best_effort(field_id: str, value: Any, warning: str, *, time: bool | None = None) -> None:
        try:
            await clickup.set_custom_field(task_id, field_id, value, time=time)
        except ClickUpError:
            warnings.append(warning)

    status_map = None
    if switch_to:
        try:
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=True)
        except ClickUpError:
            warnings.append("switch_promotion_status_unresolved")
            switch_to = False

    if switch_to and status_map:
        try:
            await clickup.update_task(task_id, status=status_map.active_status)
        except ClickUpError:
            warnings.append("task_status_write_failed")
            switch_to = False
    elif switch_to and not status_map:
        warnings.append("switch_promotion_status_unresolved")
        switch_to = False

    scheduler_option = option_by_label(scheduler_field, "Current") if switch_to else option_by_label(scheduler_field, "Queued") if scheduler_field else None
    if scheduler_field and scheduler_option:
        await _set_best_effort(scheduler_field.id, scheduler_option.id, "scheduler_state")
    elif scheduler_field and not scheduler_option:
        warnings.append("scheduler_state_option_missing")

    progress_option = option_by_label(progress_field, "medium") if progress_field else None
    energy_option = option_by_label(energy_field, "medium") if energy_field else None
    friction_option = option_by_label(friction_field, "none") if friction_field else None
    if progress_field and progress_option:
        await _set_best_effort(progress_field.id, progress_option.id, "progress_pulse")
    elif progress_field and not progress_option:
        warnings.append("progress_pulse_option_missing")
    if energy_field and energy_option:
        await _set_best_effort(energy_field.id, energy_option.id, "energy_pulse")
    elif energy_field and not energy_option:
        warnings.append("energy_pulse_option_missing")
    if friction_field and friction_option:
        await _set_best_effort(friction_field.id, friction_option.id, "friction_pulse")
    elif friction_field and not friction_option:
        warnings.append("friction_pulse_option_missing")

    return {
        "ok": True,
        "task_id": task_id,
        "name": name,
        "url": task_url,
        "redirect_to": f"/checkin/{task_id}" if switch_to else None,
        "partial_failure": bool(warnings),
        "warnings": warnings,
    }


@app.post("/checkin/{task_id}")
async def submit_checkin(task_id: str, request: Request) -> Any:
    require_ready(request)
    require_session(request)
    settings: Settings = request.app.state.settings
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid check-in payload") from exc
    action = normalize_choice(payload.get("action"), ALLOWED_ACTIONS)
    progress = normalize_choice(payload.get("progress"), ALLOWED_PROGRESS)
    energy = normalize_choice(payload.get("energy"), ALLOWED_ENERGY)
    friction = normalize_choice(payload.get("friction"), ALLOWED_FRICTION)
    raw_break_minutes = payload.get("break_minutes")
    try:
        break_minutes = int(raw_break_minutes or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid break_minutes") from exc
    if break_minutes < 0 or break_minutes > 240:
        raise HTTPException(status_code=422, detail="break_minutes must be between 0 and 240")

    clickup: ClickUpClient = request.app.state.clickup
    notifier: TelegramNotifier = request.app.state.notifier
    store: RuntimeSessionStore = request.app.state.store
    try:
        task = await clickup.get_task(task_id)
    except ClickUpError as exc:
        raise clickup_http_exception(exc) from exc
    task["_runtime"] = store.get(task_id)
    try:
        fields = await clickup.get_list_fields(request.app.state.execution_list_id)
    except ClickUpError as exc:
        return checkin_error_response(exc)

    try:
        current_task_before = _task_ref(task)
        if action == "continue":
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=True)
            action_result = await handle_continue(
                clickup,
                store,
                settings,
                task,
                fields,
                status_map,
                settings.default_continue_minutes,
                progress=progress,
                energy=energy,
                friction=friction,
            )
            verification_status, verification_details, refreshed_task = await _verify_continue_result(
                clickup, task_id, fields, settings, status_map
            )
            if refreshed_task is None:
                refreshed_task = dict(task)
                refreshed_task["_runtime"] = store.get(task_id)
            else:
                refreshed_task["_runtime"] = store.get(task_id)
            block = block_progress(refreshed_task, settings)
            current_state = await _read_current_slot_state(
                clickup, request.app.state.execution_list_id, store, settings
            )
            current_task_after = current_state.get("current_task_ref")
            followup_read_state = str(current_state.get("followup_read_state") or "not_needed")
            if block["exceeded_max"]:
                message = f"\u2713 {block['block_minutes']}m in this block. Take a break."
            elif block["reached_target"]:
                message = f"\u2713 Block target reached ({block['block_minutes']}/{block['target_minutes']}m). Consider a break."
            else:
                message = f"\u2713 Logged \u2014 {block['block_minutes']}/{block['target_minutes']}m"
            return _build_action_result(
                action="continue",
                message=message,
                block=block,
                redirect_to=None,
                next_task=None,
                action_result=action_result,
                verification_status=verification_status,
                verification_details=verification_details,
                current_task_before=current_task_before,
                current_task_after=current_task_after,
                followup_read_state=followup_read_state,
                current_task_closed=False,
                next_task_resolution_state="not_applicable",
            )

        async def _run_scheduler_and_resolve() -> tuple[str | None, dict[str, Any] | None, str]:
            """Run scheduler after a task-changing action and resolve the next task."""
            try:
                async with request.app.state.scheduler_lock:
                    result = await asyncio.wait_for(
                        run_scheduler(
                            settings, clickup, notifier, store,
                            request.app.state.execution_list_id, suppress_errors=True,
                        ),
                        timeout=ACTION_FOLLOWUP_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                return "/active/checkin", None, "deferred"
            next_id = result.get("current_task_id")
            if next_id:
                return f"/checkin/{next_id}", {"id": next_id}, "resolved"
            return "/active/checkin", None, "zero_current"

        if action == "complete":
            status_map = await resolve_runtime_status_map(
                clickup, request.app.state.execution_list_id, settings, require_active=False, require_completed=True
            )
            action_result = await handle_complete(clickup, store, settings, task, fields, status_map)
            verification_status, verification_details, _ = await _verify_complete_result(
                clickup, task_id, fields, settings, status_map
            )
            extra_failures: list[str] = []
            redirect_to = "/active/checkin"
            next_task = None
            next_task_resolution_state = "not_applicable"
            if verification_status != "failed":
                redirect_to, next_task, next_task_resolution_state = await _run_scheduler_and_resolve()
                if next_task_resolution_state == "deferred":
                    extra_failures.append("scheduler_followup_deferred")
            current_state = await _read_current_slot_state(
                clickup, request.app.state.execution_list_id, store, settings
            )
            current_task_after = current_state.get("current_task_ref")
            followup_read_state = str(current_state.get("followup_read_state") or "not_needed")
            next_task_resolution_state = _derive_next_task_resolution_state(
                action="complete",
                current_task_before=current_task_before,
                current_state=current_state,
                scheduler_resolution_state=next_task_resolution_state,
            )
            if (
                current_state.get("followup_read_state") == "succeeded"
                and current_task_after
                and str(current_task_after.get("id") or "") != str((current_task_before or {}).get("id") or "")
            ):
                redirect_to = f"/checkin/{current_task_after['id']}"
                next_task = current_task_after
            return _build_action_result(
                action="complete",
                message="\u2713 Done! Loading next task\u2026",
                block=None,
                redirect_to=redirect_to,
                next_task=next_task,
                action_result=action_result,
                verification_status=verification_status,
                verification_details=verification_details,
                extra_failures=extra_failures,
                current_task_before=current_task_before,
                current_task_after=current_task_after,
                followup_read_state=followup_read_state,
                current_task_closed=True,
                next_task_resolution_state=next_task_resolution_state,
            )
        if action == "break":
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
            minutes = break_minutes or settings.short_break_minutes
            action_result = await handle_break(
                clickup,
                store,
                settings,
                task,
                fields,
                status_map,
                minutes,
                progress=progress,
                energy=energy,
                friction=friction,
            )
            verification_status, verification_details, _ = await _verify_scheduler_state_result(
                clickup, task_id, fields, settings, "break"
            )
            redirect_to, next_task, scheduler_resolution_state = await _run_scheduler_and_resolve()
            extra_failures = ["scheduler_followup_deferred"] if scheduler_resolution_state == "deferred" else []
            current_state = await _read_current_slot_state(
                clickup, request.app.state.execution_list_id, store, settings
            )
            current_task_after = current_state.get("current_task_ref")
            followup_read_state = str(current_state.get("followup_read_state") or "not_needed")
            next_task_resolution_state = _derive_next_task_resolution_state(
                action="break",
                current_task_before=current_task_before,
                current_state=current_state,
                scheduler_resolution_state=scheduler_resolution_state,
            )
            if (
                current_state.get("followup_read_state") == "succeeded"
                and current_task_after
                and str(current_task_after.get("id") or "") != str((current_task_before or {}).get("id") or "")
            ):
                redirect_to = f"/checkin/{current_task_after['id']}"
                next_task = current_task_after
            return _build_action_result(
                action="break",
                message=f"\u2713 Break for {minutes}m. Come back when ready.",
                block=None,
                redirect_to=redirect_to,
                next_task=next_task,
                action_result=action_result,
                verification_status=verification_status,
                verification_details=verification_details,
                extra_failures=extra_failures,
                current_task_before=current_task_before,
                current_task_after=current_task_after,
                followup_read_state=followup_read_state,
                current_task_closed=True,
                next_task_resolution_state=next_task_resolution_state,
            )
        if action == "switch":
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
            action_result = await handle_switch(
                clickup,
                store,
                settings,
                task,
                fields,
                status_map,
                progress=progress,
                energy=energy,
                friction=friction,
            )
            verification_status, verification_details, _ = await _verify_scheduler_state_result(
                clickup, task_id, fields, settings, "queued"
            )
            redirect_to, next_task, scheduler_resolution_state = await _run_scheduler_and_resolve()
            extra_failures = ["scheduler_followup_deferred"] if scheduler_resolution_state == "deferred" and verification_status != "failed" else []
            current_state = await _read_current_slot_state(
                clickup, request.app.state.execution_list_id, store, settings
            )
            current_task_after = current_state.get("current_task_ref")
            followup_read_state = str(current_state.get("followup_read_state") or "not_needed")
            next_task_resolution_state = _derive_next_task_resolution_state(
                action="switch",
                current_task_before=current_task_before,
                current_state=current_state,
                scheduler_resolution_state=scheduler_resolution_state,
            )
            if (
                current_state.get("followup_read_state") == "succeeded"
                and current_task_after
                and str(current_task_after.get("id") or "") != str((current_task_before or {}).get("id") or "")
            ):
                redirect_to = f"/checkin/{current_task_after['id']}"
                next_task = current_task_after
            return _build_action_result(
                action="switch",
                message="\u2713 Switching\u2026",
                block=None,
                redirect_to=redirect_to,
                next_task=next_task,
                action_result=action_result,
                verification_status=verification_status,
                verification_details=verification_details,
                extra_failures=extra_failures,
                current_task_before=current_task_before,
                current_task_after=current_task_after,
                followup_read_state=followup_read_state,
                current_task_closed=True,
                next_task_resolution_state=next_task_resolution_state,
            )
        if action == "blocked":
            status_map = await resolve_runtime_status_map(clickup, request.app.state.execution_list_id, settings, require_active=False)
            action_result = await handle_blocked(
                clickup,
                store,
                settings,
                task,
                fields,
                status_map,
                settings.blocked_cooldown_minutes,
                progress=progress,
                energy=energy,
                friction=friction,
            )
            verification_status, verification_details, _ = await _verify_scheduler_state_result(
                clickup, task_id, fields, settings, "blocked"
            )
            redirect_to, next_task, scheduler_resolution_state = (
                await _run_scheduler_and_resolve() if _primary_write_succeeded(action_result) else (None, None, "unverified")
            )
            extra_failures = ["scheduler_followup_deferred"] if scheduler_resolution_state == "deferred" and verification_status != "failed" else []
            current_state = await _read_current_slot_state(
                clickup, request.app.state.execution_list_id, store, settings
            )
            current_task_after = current_state.get("current_task_ref")
            followup_read_state = str(current_state.get("followup_read_state") or "not_needed")
            next_task_resolution_state = _derive_next_task_resolution_state(
                action="blocked",
                current_task_before=current_task_before,
                current_state=current_state,
                scheduler_resolution_state=scheduler_resolution_state,
            )
            if (
                current_state.get("followup_read_state") == "succeeded"
                and current_task_after
                and str(current_task_after.get("id") or "") != str((current_task_before or {}).get("id") or "")
            ):
                redirect_to = f"/checkin/{current_task_after['id']}"
                next_task = current_task_after
            return _build_action_result(
                action="blocked",
                message="\u2713 Marked blocked. Loading next task\u2026",
                block=None,
                redirect_to=redirect_to,
                next_task=next_task,
                action_result=action_result,
                verification_status=verification_status,
                verification_details=verification_details,
                extra_failures=extra_failures,
                current_task_before=current_task_before,
                current_task_after=current_task_after,
                followup_read_state=followup_read_state,
                current_task_closed=True,
                next_task_resolution_state=next_task_resolution_state,
            )
    except ClickUpError as exc:
        return checkin_error_response(exc)

    raise HTTPException(status_code=400, detail="Unknown action")
