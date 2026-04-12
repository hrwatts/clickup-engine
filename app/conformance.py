from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Authoritative runtime topology is a single active list.
# Actual list name and ID come from Settings (env vars).
TOPOLOGY_DECISION = {
    "mode": "single_runtime_list",
    "pipeline_role": "upstream_intake_review",
}

# Required for minimum runtime operation of scheduler/check-in loop.
REQUIRED_MINIMUM_FIELDS: tuple[str, ...] = (
    "Scheduler State",
    "Task Type",
    "Progress Pulse",
    "Energy Pulse",
    "Friction Pulse",
    "Block Count Today",
    "Last Worked At",
)

# Needed for full intended operating model.
RECOMMENDED_FIELDS: tuple[str, ...] = (
    "Today Minutes",
    "Rotation Score",
    "Next Eligible At",
    "Done Today",
    "Priority",
    "Effort",
    "Risk",
    "Domain",
    "Source Domain",
    "Validation Required",
    "Rework Cause",
    "Repo / System",
    "Agent Mode",
)

# Optional integrations and ClickUp-side UI controls.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "Continue 20m",
    "Complete Task",
    "Blocked / Switch",
)

ALLOWED_FIELDS: tuple[str, ...] = REQUIRED_MINIMUM_FIELDS + RECOMMENDED_FIELDS + OPTIONAL_FIELDS


@dataclass(frozen=True)
class FieldConformance:
    mode: str
    present: list[str]
    missing_required: list[str]
    missing_recommended: list[str]
    missing_optional: list[str]
    unexpected: list[str]
    capabilities: dict[str, bool]
    limitations: list[str]
    operator_actions_required: list[dict[str, str | bool]]


FIELD_PRIORITY_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "priority": 1,
        "label": "Scheduling correctness",
        "capability": "Deterministic scheduler ranking and cooldown enforcement",
        "why": "These fields most improve task selection fairness and cooldown enforcement.",
        "currently_degraded": "Rotation fairness and blocked/break cooldown timing are less trustworthy.",
        "fields": ("Today Minutes", "Rotation Score", "Next Eligible At"),
    },
    {
        "priority": 2,
        "label": "Decision quality",
        "capability": "Task ranking quality and fit-to-context decisions",
        "why": "These fields improve ranking quality and execution fit.",
        "currently_degraded": "Urgency, effort, and risk tradeoffs are simplified.",
        "fields": ("Priority", "Effort", "Risk", "Domain", "Source Domain"),
    },
    {
        "priority": 3,
        "label": "Execution traceability",
        "capability": "Operator traceability, remediation clarity, and automation transparency",
        "why": "These fields improve handoff clarity, remediation tracking, and automation transparency.",
        "currently_degraded": "Handoffs and remediation context are thinner than intended.",
        "fields": ("Validation Required", "Rework Cause", "Repo / System", "Agent Mode"),
    },
    {
        "priority": 4,
        "label": "Operator convenience",
        "capability": "ClickUp-side operator controls",
        "why": "These fields are optional ClickUp-side controls and do not block operation.",
        "currently_degraded": "Operators rely on the app instead of ClickUp-side shortcuts.",
        "fields": ("Continue 20m", "Complete Task", "Blocked / Switch"),
    },
)


def build_minimum_viable_guidance(conformance: FieldConformance) -> dict[str, Any]:
    missing = set(conformance.missing_recommended + conformance.missing_optional)
    groups: list[dict[str, Any]] = []
    next_best_fields: list[str] = []
    for group in FIELD_PRIORITY_GROUPS:
        group_fields = [name for name in group["fields"] if name in missing]
        if len(next_best_fields) < 3:
            remaining = 3 - len(next_best_fields)
            next_best_fields.extend(group_fields[:remaining])
        groups.append(
            {
                "priority": group["priority"],
                "label": group["label"],
                "capability": group["capability"],
                "why": group["why"],
                "currently_degraded": group["currently_degraded"],
                "fields": group_fields,
            }
        )

    return {
        "mode_explanation": "Current execution works, but some scoring, traceability, and cooldown features are degraded.",
        "next_best_fields": next_best_fields,
        "priority_groups": groups,
    }


def evaluate_field_conformance(field_names: list[str]) -> FieldConformance:
    available = set(field_names)

    missing_required = [name for name in REQUIRED_MINIMUM_FIELDS if name not in available]
    missing_recommended = [name for name in RECOMMENDED_FIELDS if name not in available]
    missing_optional = [name for name in OPTIONAL_FIELDS if name not in available]
    present = sorted(name for name in ALLOWED_FIELDS if name in available)
    unexpected = sorted(name for name in available if name not in ALLOWED_FIELDS)

    if not missing_required and not missing_recommended:
        mode = "full_intended"
    elif not missing_required:
        mode = "minimum_viable"
    else:
        mode = "degraded"

    capabilities = {
        "current_task_selection": True,
        "queue_scoring": all(
            name in available
            for name in (
                "Task Type",
                "Progress Pulse",
                "Energy Pulse",
                "Friction Pulse",
                "Block Count Today",
                "Last Worked At",
            )
        ),
        "checkin_actions": all(
            name in available
            for name in (
                "Scheduler State",
                "Progress Pulse",
                "Energy Pulse",
                "Friction Pulse",
                "Last Worked At",
            )
        ),
        "quick_add": all(
            name in available
            for name in (
                "Scheduler State",
                "Task Type",
                "Progress Pulse",
                "Energy Pulse",
                "Friction Pulse",
            )
        ),
        "readiness_gate": not missing_required,
    }

    limitations: list[str] = []
    if missing_required:
        limitations.append("Critical runtime fields are missing; scheduler/check-in accuracy is degraded.")
    if "Today Minutes" in missing_recommended:
        limitations.append("Today minutes tracking is limited; scoring fairness may drift.")
    if "Rotation Score" in missing_recommended:
        limitations.append("Rotation score persistence is limited.")
    if "Next Eligible At" in missing_recommended:
        limitations.append("Break/blocked cooldown visibility is limited.")

    field_reasons: dict[str, str] = {
        "Scheduler State": "Required to track execution-state transitions safely.",
        "Task Type": "Required for queue ranking and quick-add defaults.",
        "Progress Pulse": "Required for pulse-driven scheduler scoring and saves.",
        "Energy Pulse": "Required for pulse-driven scheduler scoring and saves.",
        "Friction Pulse": "Required for pulse-driven scheduler scoring and saves.",
        "Block Count Today": "Required for fatigue and pacing heuristics.",
        "Last Worked At": "Required for recency and rotation fairness.",
        "Today Minutes": "Improves fairness and workload balancing.",
        "Rotation Score": "Provides explicit ranking traceability.",
        "Next Eligible At": "Enables break/blocked cooldown enforcement.",
        "Priority": "Improves ranking quality for urgent work.",
        "Effort": "Improves realistic ordering and capacity fit.",
        "Risk": "Improves safe sequencing decisions.",
        "Domain": "Improves context-aware execution planning.",
        "Source Domain": "Improves upstream provenance tracking.",
        "Validation Required": "Improves handoff and review discipline.",
        "Rework Cause": "Improves remediation telemetry.",
        "Repo / System": "Improves execution targeting for technical tasks.",
        "Agent Mode": "Improves automation-mode transparency.",
        "Continue 20m": "Optional ClickUp-side control convenience.",
        "Complete Task": "Optional ClickUp-side control convenience.",
        "Blocked / Switch": "Optional ClickUp-side control convenience.",
    }

    operator_actions_required: list[dict[str, str | bool]] = []
    for name in missing_required:
        operator_actions_required.append(
            {
                "field": name,
                "blocking": True,
                "why": field_reasons.get(name, "Required for minimum runtime operation."),
                "capability_impact": "Minimum runtime operation",
            }
        )
    for name in missing_recommended:
        operator_actions_required.append(
            {
                "field": name,
                "blocking": False,
                "why": field_reasons.get(name, "Recommended for full intended operation."),
                "capability_impact": "Full intended operation",
            }
        )
    for name in missing_optional:
        operator_actions_required.append(
            {
                "field": name,
                "blocking": False,
                "why": field_reasons.get(name, "Optional field for operator convenience."),
                "capability_impact": "Operator convenience",
            }
        )

    return FieldConformance(
        mode=mode,
        present=present,
        missing_required=missing_required,
        missing_recommended=missing_recommended,
        missing_optional=missing_optional,
        unexpected=unexpected,
        capabilities=capabilities,
        limitations=limitations,
        operator_actions_required=operator_actions_required,
    )
