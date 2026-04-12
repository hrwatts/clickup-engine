from __future__ import annotations

from typing import Any


class RuntimeSessionStore:
    """
    Lightweight in-memory runtime state.

    This is intentionally not a second task database. It only tracks
    ephemeral harmonic block session data that ClickUp does not model well
    as durable task state, and it can be safely lost on restart.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def get(self, task_id: str) -> dict[str, Any]:
        return dict(self._sessions.get(task_id, {}))

    def set_many(self, task_id: str, values: dict[str, Any]) -> None:
        current = self._sessions.get(task_id, {})
        merged = {**current, **values}
        self._sessions[task_id] = merged

    def clear(self, task_id: str) -> None:
        self._sessions.pop(task_id, None)

    def clear_many(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            self.clear(task_id)

    def attach_many(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**task, "_runtime": self.get(task["id"])} for task in tasks]
