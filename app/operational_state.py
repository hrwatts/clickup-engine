from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


SOURCE_FAILURE_CLASSES = {
    "timeout",
    "auth_error",
    "request_error",
    "not_found",
    "misconfigured",
    "unknown",
}


@dataclass(frozen=True)
class OperationalState:
    health: str
    reasons: list[str]
    data_freshness: str
    snapshot_timestamp: str
    retry_recommended: bool
    retryable_failure: bool
    usable_despite_failure: bool
    source_failure: dict[str, Any] | None
    current_task_resolution_state: str
    current_task_resolution_next_action: str
    promotion_attempted: bool
    promotion_verified: bool | None
    promotion_reason: str | None
    selection_guidance: dict[str, Any]
    field_conformance: dict[str, Any]
    pipeline_drift: dict[str, Any]
    next_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "status": self.health,
            "reasons": self.reasons,
            "data_freshness": self.data_freshness,
            "snapshot_timestamp": self.snapshot_timestamp,
            "retry_recommended": self.retry_recommended,
            "retryable_failure": self.retryable_failure,
            "usable_despite_failure": self.usable_despite_failure,
            "source_failure": self.source_failure,
            "current_task_resolution_state": self.current_task_resolution_state,
            "current_task_resolution_next_action": self.current_task_resolution_next_action,
            "promotion_attempted": self.promotion_attempted,
            "promotion_verified": self.promotion_verified,
            "promotion_reason": self.promotion_reason,
            "selection_guidance": self.selection_guidance,
            "field_conformance": self.field_conformance,
            "pipeline_drift": self.pipeline_drift,
            "next_action": self.next_action,
        }


def classify_source_failure(runtime_error: Any) -> dict[str, Any]:
    failure_class = "unknown"
    code = str(getattr(runtime_error, "code", "") or "")
    message = str(getattr(runtime_error, "message", "") or "")
    if code == "clickup_connectivity_error":
        failure_class = "timeout" if "timed out" in message.lower() else "request_error"
    elif code == "clickup_auth_error":
        failure_class = "auth_error"
    elif code == "runtime_list_not_found":
        failure_class = "not_found"
    elif code in {"runtime_list_misconfigured", "insufficient_field_configuration"}:
        failure_class = "misconfigured"
    if failure_class not in SOURCE_FAILURE_CLASSES:
        failure_class = "unknown"
    return {
        "source": "clickup",
        "class": failure_class,
        "message": message or "ClickUp read failed.",
        "at": datetime.now().astimezone().isoformat(),
    }


def build_operational_state(
    *,
    current_task_resolution_state: str,
    current_task_resolution_next_action: str,
    conformance: dict[str, Any],
    pipeline_drift: dict[str, Any],
    data_freshness: str,
    snapshot_timestamp: str,
    retry_recommended: bool,
    retryable_failure: bool,
    usable_despite_failure: bool,
    source_failure: dict[str, Any] | None,
    promotion_attempted: bool,
    promotion_verified: bool | None,
    promotion_reason: str | None,
    top_candidates: list[dict[str, Any]],
    selection_attempted: bool,
    selection_not_attempted_reason: str | None,
    config_mismatch: dict[str, Any] | None = None,
    current_invariant: dict[str, Any] | None = None,
) -> OperationalState:
    config_mismatch = config_mismatch or {}
    current_invariant = current_invariant or {}
    reasons: list[str] = []
    if current_task_resolution_state == "multi_current_violation":
        reasons.append("multi_current_violation")
    if current_task_resolution_state == "zero_current_candidates_available":
        reasons.append("zero_current_with_candidates")
    if current_task_resolution_state == "zero_current_no_eligible_candidates":
        reasons.append("zero_current_no_eligible")
    if current_task_resolution_state == "promotion_failed":
        reasons.append("promotion_failed")
    if current_task_resolution_state == "resolution_blocked_by_source_failure":
        reasons.append("live_clickup_read_failed")
    if conformance.get("missing_required_fields"):
        reasons.append("missing_required_fields")
    if conformance.get("missing_recommended_fields"):
        reasons.append("missing_recommended_fields")
    if config_mismatch.get("configured_vs_resolved_list_id"):
        reasons.append("configured_resolved_list_mismatch")
    if pipeline_drift.get("has_drift") is True:
        reasons.append("pipeline_drift_detected")
    if data_freshness == "stale":
        reasons.append("stale_snapshot")

    if current_task_resolution_state in {"multi_current_violation", "promotion_failed", "resolution_blocked_by_source_failure"}:
        health = "blocking"
    elif conformance.get("missing_required_fields") or config_mismatch.get("configured_vs_resolved_list_id"):
        health = "blocking"
    elif data_freshness == "stale" and not usable_despite_failure:
        health = "blocking"
    elif reasons:
        health = "degraded"
    else:
        health = "healthy"

    selection_guidance = {
        "top_candidates": top_candidates,
        "auto_selection_available": bool(top_candidates),
        "manual_selection_supported": bool(top_candidates),
        "selection_attempted": selection_attempted,
        "selection_not_attempted_reason": selection_not_attempted_reason,
        "manual_selection_note": (
            "Move one of the listed candidates to the current status in ClickUp when you want to override auto-selection."
            if top_candidates
            else "No eligible candidate is available for manual promotion yet."
        ),
    }

    next_action = current_task_resolution_next_action
    if data_freshness == "stale" and source_failure:
        next_action = "Use the stale snapshot cautiously, then retry live ClickUp reads before making trust-sensitive decisions."
    elif current_task_resolution_state == "zero_current_candidates_available" and not selection_attempted:
        next_action = "Choose a listed candidate manually in ClickUp or open Check-in to trigger deterministic auto-selection."
    elif current_task_resolution_state == "promotion_failed":
        next_action = "Promotion was attempted but not verified; inspect Operations before retrying."
    elif pipeline_drift.get("has_drift") is True:
        next_action = "Review drifted intake tasks and move runtime-ready work to the authoritative execution list when ready."

    return OperationalState(
        health=health,
        reasons=list(dict.fromkeys(reasons)),
        data_freshness=data_freshness,
        snapshot_timestamp=snapshot_timestamp,
        retry_recommended=retry_recommended,
        retryable_failure=retryable_failure,
        usable_despite_failure=usable_despite_failure,
        source_failure=source_failure,
        current_task_resolution_state=current_task_resolution_state,
        current_task_resolution_next_action=current_task_resolution_next_action,
        promotion_attempted=promotion_attempted,
        promotion_verified=promotion_verified,
        promotion_reason=promotion_reason,
        selection_guidance=selection_guidance,
        field_conformance=conformance,
        pipeline_drift=pipeline_drift,
        next_action=next_action,
    )


def operational_state_from_dict(data: dict[str, Any]) -> OperationalState:
    return OperationalState(
        health=str(data.get("health") or data.get("status") or "unknown"),
        reasons=list(data.get("reasons") or []),
        data_freshness=str(data.get("data_freshness") or "unknown"),
        snapshot_timestamp=str(data.get("snapshot_timestamp") or ""),
        retry_recommended=bool(data.get("retry_recommended")),
        retryable_failure=bool(data.get("retryable_failure")),
        usable_despite_failure=bool(data.get("usable_despite_failure")),
        source_failure=data.get("source_failure"),
        current_task_resolution_state=str(data.get("current_task_resolution_state") or "unknown"),
        current_task_resolution_next_action=str(data.get("current_task_resolution_next_action") or ""),
        promotion_attempted=bool(data.get("promotion_attempted")),
        promotion_verified=data.get("promotion_verified"),
        promotion_reason=data.get("promotion_reason"),
        selection_guidance=dict(data.get("selection_guidance") or {}),
        field_conformance=dict(data.get("field_conformance") or {}),
        pipeline_drift=dict(data.get("pipeline_drift") or {}),
        next_action=str(data.get("next_action") or ""),
    )
