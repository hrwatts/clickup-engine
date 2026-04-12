from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import time

import httpx


FIELD_SCHEDULER_STATE = "Scheduler State"
FIELD_TASK_TYPE = "Task Type"
FIELD_PROGRESS_PULSE = "Progress Pulse"
FIELD_ENERGY_PULSE = "Energy Pulse"
FIELD_FRICTION_PULSE = "Friction Pulse"
FIELD_BLOCK_COUNT_TODAY = "Block Count Today"
FIELD_LAST_WORKED_AT = "Last Worked At"
FIELD_NEXT_ELIGIBLE_AT = "Next Eligible At"
FIELD_TODAY_MINUTES = "Today Minutes"
FIELD_ROTATION_SCORE = "Rotation Score"


@dataclass
class ClickUpFieldOption:
    id: str
    name: str


@dataclass
class ClickUpField:
    id: str
    name: str
    type: str
    type_config: dict[str, Any]


@dataclass(frozen=True)
class ClickUpStatusOption:
    status: str
    type: str


class ClickUpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        body_preview: str = "",
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.body_preview = body_preview
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "status_code": self.status_code,
            "error_code": self.error_code,
            "path": self.path,
            "body_preview": self.body_preview,
        }


class ClickUpConfigError(ClickUpError):
    pass


class _TTLCache:
    """Minimal in-memory per-process TTL cache, safe for single-threaded asyncio."""

    def __init__(self, ttl_seconds: float = 15.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> tuple[bool, Any]:
        entry = self._store.get(key)
        if entry is None:
            return False, None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._store[key]
            return False, None
        return True, value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


class ClickUpClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.clickup.com/api/v2",
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=20.0,
        )
        self._fields_cache: _TTLCache = _TTLCache(ttl_seconds=15.0)
        self._tasks_cache: _TTLCache = _TTLCache(ttl_seconds=10.0)
        self._task_cache: _TTLCache = _TTLCache(ttl_seconds=60.0)
        # Limit concurrent in-flight requests to avoid ClickUp 429 rate-limit bursts.
        self._sem: asyncio.Semaphore = asyncio.Semaphore(5)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_list_fields(self, list_id: str) -> list[ClickUpField]:
        list_id = normalize_clickup_list_id(list_id)
        hit, cached = self._fields_cache.get(list_id)
        if hit:
            return cached  # type: ignore[return-value]
        response = await self._request("GET", f"/list/{list_id}/field")
        data = response.json().get("fields", [])
        result = [
            ClickUpField(
                id=item["id"],
                name=item["name"],
                type=item["type"],
                type_config=item.get("type_config", {}),
            )
            for item in data
        ]
        self._fields_cache.set(list_id, result)
        return result

    async def get_list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        list_id = normalize_clickup_list_id(list_id)
        hit, cached = self._tasks_cache.get(list_id)
        if hit:
            return cached  # type: ignore[return-value]
        page = 0
        tasks: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "GET",
                f"/list/{list_id}/task",
                params={"include_closed": "true", "subtasks": "true", "include_timl": "true", "page": page},
            )
            batch = response.json().get("tasks", [])
            tasks.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        self._tasks_cache.set(list_id, tasks)
        return tasks

    async def get_task(self, task_id: str) -> dict[str, Any]:
        hit, cached = self._task_cache.get(task_id)
        if hit:
            return cached  # type: ignore[return-value]
        response = await self._request("GET", f"/task/{task_id}")
        result = response.json()
        self._task_cache.set(task_id, result)
        return result

    async def update_task(self, task_id: str, **payload: Any) -> dict[str, Any]:
        self._tasks_cache.clear()
        self._task_cache.invalidate(task_id)
        response = await self._request("PUT", f"/task/{task_id}", json=payload)
        return response.json()

    async def create_task(self, list_id: str, name: str, **payload: Any) -> dict[str, Any]:
        list_id = normalize_clickup_list_id(list_id)
        self._tasks_cache.clear()
        response = await self._request("POST", f"/list/{list_id}/task", json={"name": name, **payload})
        return response.json()

    async def set_custom_field(self, task_id: str, field_id: str, value: Any, *, time: bool | None = None) -> None:
        self._tasks_cache.clear()
        self._task_cache.invalidate(task_id)
        payload: dict[str, Any] = {"value": value}
        if time is not None:
            payload["value_options"] = {"time": time}
        await self._request("POST", f"/task/{task_id}/field/{field_id}", json=payload)

    async def get_spaces(self, team_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/team/{team_id}/space")
        return response.json().get("spaces", [])

    async def get_space_lists(self, space_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/space/{space_id}/list")
        return response.json().get("lists", [])

    async def get_space_folders(self, space_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/space/{space_id}/folder")
        return response.json().get("folders", [])

    async def get_folder_lists(self, folder_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/folder/{folder_id}/list")
        return response.json().get("lists", [])

    async def get_team_tasks(self, team_id: str, *, page: int = 0) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/team/{team_id}/task",
            params={"include_closed": "false", "subtasks": "false", "page": page},
        )
        return response.json().get("tasks", [])

    async def add_task_to_list(self, list_id: str, task_id: str) -> None:
        await self._request("POST", f"/list/{list_id}/task/{task_id}")

    async def resolve_list_id(self, team_id: str, list_name: str, *, space_id: str = "") -> str:
        target = normalize_name(list_name)
        if not target:
            raise ClickUpConfigError("CLICKUP_LIST_NAME is empty.")

        matches: list[dict[str, Any]] = []
        spaces: list[dict[str, Any]]
        if space_id:
            spaces = [{"id": space_id}]
        else:
            spaces = await self.get_spaces(team_id)

        for space in spaces:
            current_space_id = str(space.get("id") or "")
            for item in await self.get_space_lists(current_space_id):
                if normalize_name(item.get("name")) == target:
                    matches.append(item)
            for folder in await self.get_space_folders(current_space_id):
                folder_id = str(folder.get("id") or "")
                for item in await self.get_folder_lists(folder_id):
                    if normalize_name(item.get("name")) == target:
                        matches.append(item)

        unique_matches = {str(item.get("id")): item for item in matches}
        if not unique_matches:
            raise ClickUpConfigError(
                "Could not resolve CLICKUP_LIST_NAME. Set CLICKUP_LIST_ID directly or provide CLICKUP_SPACE_ID."
            )
        if len(unique_matches) > 1:
            raise ClickUpConfigError(
                "CLICKUP_LIST_NAME matched multiple lists. Set CLICKUP_LIST_ID directly or provide CLICKUP_SPACE_ID."
            )
        return normalize_clickup_list_id(next(iter(unique_matches.keys())))

    async def validate_access(self, list_id: str) -> dict[str, Any]:
        normalized_list_id = normalize_clickup_list_id(list_id)
        response = await self._request("GET", f"/list/{normalized_list_id}")
        data = response.json()
        list_data = data.get("list", data)
        return {
            "id": str(list_data.get("id") or normalized_list_id),
            "name": str(list_data.get("name") or ""),
            "raw": list_data,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with self._sem:
            for attempt in range(4):
                try:
                    response = await self._client.request(method, path, **kwargs)
                    if response.status_code == 429 and attempt < 3:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    response.raise_for_status()
                    return response
                except httpx.TimeoutException as exc:
                    raise ClickUpError("ClickUp request timed out.", path=path) from exc
                except httpx.HTTPStatusError as exc:
                    error_code = None
                    error_message = ""
                    body_preview = exc.response.text[:500].replace("\n", " ").strip()
                    try:
                        payload = exc.response.json()
                        error_code = str(payload.get("ECODE") or payload.get("ecode") or "") or None
                        error_message = str(payload.get("err") or payload.get("error") or "").strip()
                        body_preview = json.dumps(payload)[:500]
                    except ValueError:
                        pass
                    message = error_message or f"ClickUp request failed with status {exc.response.status_code}."
                    raise ClickUpError(
                        message,
                        status_code=exc.response.status_code,
                        error_code=error_code,
                        body_preview=body_preview,
                        path=path,
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ClickUpError("ClickUp request failed.", path=path) from exc
            raise ClickUpError("ClickUp request failed after 3 retries (rate limited).", path=path)  # pragma: no cover


def dropdown_options(field: ClickUpField) -> dict[str, ClickUpFieldOption]:
    options = {}
    for option in field.type_config.get("options", []):
        options[option["name"]] = ClickUpFieldOption(id=option["id"], name=option["name"])
    return options


def field_value(task: dict[str, Any], field_name: str) -> Any:
    for field in task.get("custom_fields", []):
        if field.get("name") == field_name:
            return field.get("value")
    return None


def field_by_name(fields: list[ClickUpField]) -> dict[str, ClickUpField]:
    return {field.name: field for field in fields}


def list_statuses(list_info: dict[str, Any]) -> list[ClickUpStatusOption]:
    raw = dict(list_info.get("raw") or list_info)
    statuses = list(raw.get("statuses") or [])
    results: list[ClickUpStatusOption] = []
    for item in statuses:
        name = str(item.get("status") or item.get("label") or item.get("name") or "").strip()
        if not name:
            continue
        results.append(ClickUpStatusOption(status=name, type=str(item.get("type") or "").strip().lower()))
    return results


def parse_clickup_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_name(value: str | None) -> str:
    return (value or "").strip().casefold()


def normalize_clickup_list_id(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"/v/li/([^/?#]+)", parsed.path)
        if match:
            raw = match.group(1)

    compact_match = re.fullmatch(r"6-([A-Za-z0-9]+)-1", raw)
    if compact_match:
        return compact_match.group(1)
    return raw


def option_by_label(field: ClickUpField | None, label: str) -> ClickUpFieldOption | None:
    if not field:
        return None
    target = normalize_name(label)
    for option in dropdown_options(field).values():
        if normalize_name(option.name) == target:
            return option
    return None


def is_closed_status(status: str) -> bool:
    return normalize_name(status) in {"complete", "completed", "closed", "done"}


def is_in_progress_status(status: str, configured_current_status: str) -> bool:
    normalized = normalize_name(status)
    return normalized in {"in progress", "in_progress"} or normalized == normalize_name(configured_current_status)


def is_blocked_status(status: str, configured_blocked_status: str) -> bool:
    normalized = normalize_name(status)
    if configured_blocked_status:
        return normalized == normalize_name(configured_blocked_status)
    return normalized == "blocked"
