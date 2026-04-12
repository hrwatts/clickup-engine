from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

import app.main as main_module
from app.clickup import ClickUpField
from app.conformance import RECOMMENDED_FIELDS, REQUIRED_MINIMUM_FIELDS
from app.config import Settings


class FakeClickUpClient:
    def __init__(self, token: str) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.outside_tasks: list[dict[str, Any]] = []
        self.added_to_list: list[dict[str, Any]] = []
        self.fields: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.field_updates: list[dict[str, Any]] = []
        self.fail_list_tasks = False
        self.fail_list_fields = False
        self.fail_get_task = False
        self.fail_validate_access = False
        self.fail_team_tasks = False
        self._task_counter = 1000
        self.space_id = "space-1"
        self.folder_id = "folder-1"
        self.runtime_list_id = "list"
        self.pipeline_inbox_id = "pipeline-inbox"
        self.list_tasks_by_id: dict[str, list[dict[str, Any]]] = {
            self.runtime_list_id: self.tasks,
            self.pipeline_inbox_id: [],
        }
        self.statuses = [
            {"status": "To do", "type": "open"},
            {"status": "In progress", "type": "custom"},
            {"status": "Complete", "type": "closed"},
        ]

    async def aclose(self) -> None:
        return

    async def get_list_fields(self, list_id: str) -> list[Any]:
        if self.fail_list_fields:
            raise main_module.ClickUpError("boom")
        return list(self.fields)

    async def get_list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        if self.fail_list_tasks:
            raise main_module.ClickUpError("boom")
        source = self.list_tasks_by_id.get(list_id, self.tasks)
        return [dict(task) for task in source]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        if self.fail_get_task:
            raise main_module.ClickUpError("boom")
        for task in self.tasks:
            if task["id"] == task_id:
                return dict(task)
        for task in self.outside_tasks:
            if task["id"] == task_id:
                return dict(task)
        raise KeyError(task_id)

    async def update_task(self, task_id: str, **payload: Any) -> dict[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                if "status" in payload:
                    allowed = {str(item["status"]) for item in self.statuses}
                    if payload["status"] not in allowed:
                        raise main_module.ClickUpError("Status does not exist", status_code=400, error_code="STATUS_INVALID")
                    task["status"] = {"status": payload["status"]}
                    self.status_updates.append({"task_id": task_id, "status": payload["status"]})
                return dict(task)
        raise KeyError(task_id)

    async def create_task(self, list_id: str, name: str, **payload: Any) -> dict[str, Any]:
        self._task_counter += 1
        task_id = str(self._task_counter)
        task = {
            "id": task_id,
            "name": name,
            "url": f"https://app.clickup.com/t/{task_id}",
            "status": {"status": "To do"},
            "custom_fields": [],
        }
        self.list_tasks_by_id.setdefault(list_id, self.tasks).append(task)
        return dict(task)

    async def set_custom_field(self, task_id: str, field_id: str, value: Any, *, time: bool | None = None) -> None:
        self.field_updates.append({"task_id": task_id, "field_id": field_id, "value": value, "time": time})
        for task in self.tasks:
            if task["id"] != task_id:
                continue
            for field in task.get("custom_fields", []):
                if field.get("id") == field_id:
                    field["value"] = value
                    return
            field_name = next((str(f.name) for f in self.fields if getattr(f, "id", "") == field_id), "")
            task.setdefault("custom_fields", []).append({"id": field_id, "name": field_name, "value": value})
        return

    async def validate_access(self, list_id: str) -> dict[str, Any]:
        if self.fail_validate_access:
            raise main_module.ClickUpError("bad list", status_code=400, error_code="INPUT_003")
        return {"id": list_id, "name": "Execution Engine", "raw": {"id": list_id, "name": "Execution Engine", "statuses": list(self.statuses)}}

    async def get_spaces(self, team_id: str) -> list[dict[str, Any]]:
        return [{"id": self.space_id, "name": "Test Space"}]

    async def get_space_lists(self, space_id: str) -> list[dict[str, Any]]:
        return [{"id": self.runtime_list_id, "name": "⚙️ Execution Engine"}]

    async def get_space_folders(self, space_id: str) -> list[dict[str, Any]]:
        return [{"id": self.folder_id, "name": "Execution Engine"}]

    async def get_folder_lists(self, folder_id: str) -> list[dict[str, Any]]:
        return [
            {"id": self.pipeline_inbox_id, "name": "Inbox"},
            {"id": "pipeline-clarify", "name": "Clarify"},
            {"id": "pipeline-ready", "name": "Ready"},
            {"id": "pipeline-running", "name": "Agent Running"},
            {"id": "pipeline-human", "name": "Human Refinement"},
            {"id": "pipeline-review", "name": "Review"},
            {"id": "pipeline-failed", "name": "Validation Failed"},
            {"id": "pipeline-done", "name": "Done"},
        ]

    async def get_team_tasks(self, team_id: str, *, page: int = 0) -> list[dict[str, Any]]:
        if self.fail_team_tasks:
            raise main_module.ClickUpError("boom")
        return [dict(t) for t in self.outside_tasks]

    async def add_task_to_list(self, list_id: str, task_id: str) -> None:
        self.added_to_list.append({"list_id": list_id, "task_id": task_id})
        # Move the task from outside_tasks into the engine task list so subsequent
        # get_task calls can find it.
        for task in list(self.outside_tasks):
            if task["id"] == task_id:
                self.tasks.append(dict(task))
                self.outside_tasks.remove(task)
                return


class FakeNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.prompts: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return

    async def send_task_prompt(self, task: dict[str, Any], checkin_url: str) -> None:
        self.prompts.append({"task_id": task["id"], "checkin_url": checkin_url})

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        return

    async def send_message(self, text: str) -> None:
        return


def build_task(task_id: str, name: str, status: str, *, parent: str | None = None) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": status},
        "custom_fields": [],
        "parent": parent,
    }


class AppSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_load_settings = main_module.load_settings
        self._orig_clickup_client = main_module.ClickUpClient
        self._orig_notifier = main_module.TelegramNotifier

        def fake_load_settings() -> Settings:
            return Settings(
                clickup_token="token",
                clickup_workspace_id="workspace-1",
                clickup_list_id="list",
                clickup_list_name="",
                clickup_space_id="",
                clickup_webhook_secret="secret",
                telegram_bot_token="bot",
                telegram_chat_id="chat",
                telegram_webhook_secret="telegram-secret",
                public_base_url="https://example.test",
                app_shared_secret="open-sesame",
                session_secret="session-secret",
                session_cookie_name="execution_engine_session",
                session_max_age_seconds=86400,
                session_cookie_secure=False,
                session_cookie_samesite="lax",
                login_rate_limit_attempts=5,
                login_rate_limit_window_seconds=900,
                clickup_open_status="To do",
                clickup_current_status="In progress",
                clickup_completed_status="Complete",
                clickup_blocked_status="",
                checkin_slice_minutes=20,
                block_target_minutes=60,
                block_min_minutes=40,
                block_max_minutes=80,
                queue_target_min=3,
                queue_target_max=7,
                stale_queue_hours=72,
                resume_pack_required_markers=("Resume Pack", "Outcome:", "Next Step:", "Re-entry Cue:", "Context:"),
                field_scheduler_state_name="Scheduler State",
                field_task_type_name="Task Type",
                field_progress_pulse_name="Progress Pulse",
                field_energy_pulse_name="Energy Pulse",
                field_friction_pulse_name="Friction Pulse",
                field_block_count_today_name="Block Count Today",
                field_last_worked_at_name="Last Worked At",
                field_next_eligible_at_name="Next Eligible At",
                field_today_minutes_name="Today Minutes",
                field_rotation_score_name="Rotation Score",
                enable_builtin_scheduler=False,
                scheduler_tick_seconds=60,
                scheduler_min_interval_minutes=5,
                workday_start_hour=7,
                workday_end_hour=22,
                workday_weekdays=(0, 1, 2, 3, 4, 5, 6),
                enable_daily_summary=True,
                daily_summary_hour=21,
                enable_weekly_summary=True,
                weekly_summary_weekday=6,
                weekly_summary_hour=18,
                morning_start_hour=6,
                morning_end_hour=12,
                midday_end_hour=17,
                evening_end_hour=22,
                morning_deep_bonus=30,
                morning_paper_bonus=34,
                midday_medium_bonus=18,
                midday_reading_bonus=20,
                evening_light_bonus=16,
                evening_admin_bonus=18,
                current_momentum_bonus=22,
                medium_momentum_bonus=10,
                fatigue_target_penalty=18,
                fatigue_max_penalty=42,
                system_task_penalty=60,
                pipeline_space_name="Test Space",
                pipeline_folder_name="Execution Engine",
                default_continue_minutes=20,
                short_break_minutes=10,
                long_break_minutes=20,
                blocked_cooldown_minutes=90,
            )

        main_module.load_settings = fake_load_settings
        main_module.ClickUpClient = FakeClickUpClient
        main_module.TelegramNotifier = FakeNotifier

    def _set_broken_startup(self) -> None:
        def broken_load_settings():
            raise RuntimeError("bad config")
        main_module.load_settings = broken_load_settings

    def tearDown(self) -> None:
        main_module.load_settings = self._orig_load_settings
        main_module.ClickUpClient = self._orig_clickup_client
        main_module.TelegramNotifier = self._orig_notifier

    @contextmanager
    def _client_with_tasks(self, tasks: list[dict[str, Any]]):
        with TestClient(main_module.app) as client:
            client.app.state.clickup.tasks = tasks
            client.app.state.clickup.list_tasks_by_id[client.app.state.clickup.runtime_list_id] = tasks
            client.app.state.clickup.fields = self._execution_fields()
            yield client

    @contextmanager
    def _client_with_state(self, tasks: list[dict[str, Any]], *, fields: list[dict[str, Any]] | None = None):
        with TestClient(main_module.app) as client:
            client.app.state.clickup.tasks = tasks
            client.app.state.clickup.list_tasks_by_id[client.app.state.clickup.runtime_list_id] = tasks
            client.app.state.clickup.fields = fields or []
            yield client

    def _login(self, client: TestClient, password: str = "open-sesame"):
        return client.post("/login", data={"password": password}, follow_redirects=False)

    def _execution_fields(self) -> list[ClickUpField]:
        return [
            ClickUpField(id="f1", name="Scheduler State", type="drop_down", type_config={"options": [{"id": "i", "name": "Inbox"}, {"id": "c", "name": "Current"}, {"id": "q", "name": "Queued"}, {"id": "b", "name": "Break"}, {"id": "blk", "name": "Blocked"}, {"id": "d", "name": "Done today"}]}),
            ClickUpField(id="f2", name="Task Type", type="drop_down", type_config={"options": [{"id": "deep", "name": "deep"}, {"id": "med", "name": "medium"}, {"id": "light", "name": "light"}, {"id": "read", "name": "reading"}, {"id": "paper", "name": "paper"}, {"id": "admin", "name": "admin"}]}),
            ClickUpField(id="f3", name="Last Worked At", type="date", type_config={}),
            ClickUpField(id="f4", name="Today Minutes", type="number", type_config={}),
            ClickUpField(id="f5", name="Progress Pulse", type="drop_down", type_config={"options": [{"id": "m", "name": "medium"}, {"id": "l", "name": "low"}]}),
            ClickUpField(id="f6", name="Energy Pulse", type="drop_down", type_config={"options": [{"id": "m", "name": "medium"}, {"id": "h", "name": "high"}, {"id": "lo", "name": "low"}]}),
            ClickUpField(id="f7", name="Friction Pulse", type="drop_down", type_config={"options": [{"id": "n", "name": "none"}, {"id": "s", "name": "some"}, {"id": "hi", "name": "high"}]}),
            ClickUpField(id="f8", name="Next Eligible At", type="date", type_config={}),
            ClickUpField(id="f9", name="Block Count Today", type="number", type_config={}),
            ClickUpField(id="f10", name="Rotation Score", type="number", type_config={}),
        ]

    def _fields_from_names(self, names: list[str]) -> list[ClickUpField]:
        return [
            ClickUpField(id=f"f{i}", name=name, type="text", type_config={})
            for i, name in enumerate(names, start=1)
        ]

    def test_healthz(self) -> None:
        with self._client_with_tasks([]) as client:
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})

    def test_root_and_favicon(self) -> None:
        with self._client_with_tasks([]) as client:
            root = client.get("/")
            self.assertEqual(root.status_code, 200)
            self.assertEqual(root.json()["service"], "clickup-execution-engine")
            favicon = client.get("/favicon.ico")
            self.assertEqual(favicon.status_code, 204)

    def test_logged_in_root_shows_actionable_control_center(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Execution Control Center", response.text)
            self.assertIn("Operational state:", response.text)
            self.assertIn("Current task:", response.text)
            self.assertIn("Next action:", response.text)
            self.assertIn("Open Check-in", response.text)
            self.assertIn("Open Operations", response.text)

    def test_healthz_degraded_when_startup_fails(self) -> None:
        self._set_broken_startup()
        with TestClient(main_module.app) as client:
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "degraded")

    def test_readyz_degraded_when_startup_fails(self) -> None:
        self._set_broken_startup()
        with TestClient(main_module.app) as client:
            response = client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["status"], "not_ready")

    def test_startup_report_surfaces_degraded_detail(self) -> None:
        self._set_broken_startup()
        with TestClient(main_module.app) as client:
            response = client.get("/reports/startup")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "degraded")

    def test_unauthenticated_access_redirects_to_login(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "To do")]) as client:
            denied = client.get("/active/checkin", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertEqual(denied.headers["location"], "/login")

    def test_unauthenticated_action_endpoint_is_blocked(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            denied = client.post("/checkin/1", json={"action": "continue"})
            self.assertEqual(denied.status_code, 401)

    def test_unauthenticated_report_redirects_to_login(self) -> None:
        with self._client_with_tasks([]) as client:
            denied = client.get("/reports/daily", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertEqual(denied.headers["location"], "/login")

    def test_startup_report_requires_session(self) -> None:
        with self._client_with_tasks([]) as client:
            denied = client.get("/reports/startup", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertEqual(denied.headers["location"], "/login")

    def test_diagnostics_report_requires_session(self) -> None:
        with self._client_with_tasks([]) as client:
            denied = client.get("/reports/diagnostics", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertEqual(denied.headers["location"], "/login")

    def test_ops_runtime_requires_session(self) -> None:
        with self._client_with_tasks([]) as client:
            denied = client.get("/ops/runtime", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertEqual(denied.headers["location"], "/login")

    def test_ops_remediation_requires_session(self) -> None:
        with self._client_with_tasks([]) as client:
            denied = client.post("/ops/remediate/runtime-current", follow_redirects=False)
            self.assertEqual(denied.status_code, 401)

    def test_successful_login_sets_session_cookie_and_allows_access(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "To do")]) as client:
            login = self._login(client)
            self.assertEqual(login.status_code, 303)
            self.assertIn("execution_engine_session=", login.headers.get("set-cookie", ""))
            response = client.get("/active")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("checkin_url", payload)
            self.assertNotIn("token=", payload["checkin_url"])
            self.assertIn("block", payload)

    def test_invalid_login_rejected(self) -> None:
        with self._client_with_tasks([]) as client:
            response = self._login(client, password="wrong")
            self.assertEqual(response.status_code, 401)
            self.assertIn("Login failed.", response.text)

    def test_checkin_page_escapes_task_name(self) -> None:
        with self._client_with_tasks([build_task("1", "<script>alert(1)</script>", "In progress")]) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
            self.assertNotIn("<script>alert(1)</script>", response.text)

    def test_checkin_page_shows_parent_context(self) -> None:
        parent = build_task("10", "Parent Project", "To do")
        child = build_task("1", "Leaf Task", "In progress", parent="10")
        with self._client_with_tasks([parent, child]) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="task-parent"', response.text)
            self.assertIn("Parent Project", response.text)

    def test_checkin_page_no_parent_label_when_absent(self) -> None:
        task = build_task("1", "Top-level Task", "In progress")
        with self._client_with_tasks([task]) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('class="task-parent"', response.text)

    def test_submit_checkin_rejects_invalid_pulse(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            response = client.post(
                "/checkin/1",
                json={"action": "continue", "progress": "bad", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 422)

    def test_active_checkin_returns_friendly_404_when_none_eligible(self) -> None:
        tasks = [
            build_task("1", "Done", "Complete"),
            build_task("2", "Waiting", "Complete"),
            build_task("3", "Also done", "Complete"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/active/checkin")
            self.assertEqual(response.status_code, 404)
            self.assertIn("Nothing queued right now", response.text)

    def test_active_checkin_reports_empty_runtime_list(self) -> None:
        with self._client_with_state([], fields=self._execution_fields()) as client:
            self._login(client)
            response = client.get("/active/checkin")
            self.assertEqual(response.status_code, 404)
            self.assertIn("Execution list is empty", response.text)

    def test_active_checkin_reports_clickup_auth_failure(self) -> None:
        with self._client_with_state([build_task("1", "Task", "To do")], fields=self._execution_fields()) as client:
            self._login(client)

            async def auth_fail(_: str) -> list[dict[str, Any]]:
                raise main_module.ClickUpError("denied", status_code=401)

            client.app.state.clickup.get_list_tasks = auth_fail  # type: ignore[method-assign]
            response = client.get("/active/checkin")
            self.assertEqual(response.status_code, 502)
            self.assertIn("ClickUp Authorization Failed", response.text)

    def test_scheduler_run_reports_runtime_list_not_found(self) -> None:
        with self._client_with_state([build_task("1", "Task", "To do")], fields=self._execution_fields()) as client:
            self._login(client)

            async def not_found(_: str) -> list[Any]:
                raise main_module.ClickUpError("missing", status_code=404)

            client.app.state.clickup.get_list_fields = not_found  # type: ignore[method-assign]
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["error_code"], "runtime_list_not_found")

    def test_scheduler_run_reports_runtime_list_misconfigured(self) -> None:
        with self._client_with_state([build_task("1", "Task", "To do")], fields=self._execution_fields()) as client:
            self._login(client)

            async def misconfigured(_: str) -> list[Any]:
                raise main_module.ClickUpError("bad", status_code=400, error_code="INPUT_003")

            client.app.state.clickup.get_list_fields = misconfigured  # type: ignore[method-assign]
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["error_code"], "runtime_list_misconfigured")

    def test_scheduler_run_reports_clickup_connectivity_error(self) -> None:
        with self._client_with_state([build_task("1", "Task", "To do")], fields=self._execution_fields()) as client:
            self._login(client)

            async def connectivity(_: str) -> list[Any]:
                raise main_module.ClickUpError("upstream broke", status_code=503)

            client.app.state.clickup.get_list_fields = connectivity  # type: ignore[method-assign]
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["error_code"], "clickup_connectivity_error")

    def test_scheduler_run_reports_insufficient_field_configuration(self) -> None:
        tasks = [build_task("1", "Task", "To do")]
        with self._client_with_state(tasks, fields=[]) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["error_code"], "insufficient_field_configuration")

    def test_scheduler_run_reports_scheduler_internal_error(self) -> None:
        tasks = [build_task("1", "Task", "To do")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            original_choose = main_module.choose_current_task

            async def boom(*args: Any, **kwargs: Any):
                raise RuntimeError("unexpected")

            main_module.choose_current_task = boom  # type: ignore[assignment]
            try:
                response = client.post("/scheduler/run")
                self.assertEqual(response.status_code, 500)
                payload = response.json()
                self.assertEqual(payload["error_code"], "scheduler_internal_error")
            finally:
                main_module.choose_current_task = original_choose  # type: ignore[assignment]

    def test_hygiene_report_detects_duplicates(self) -> None:
        tasks = [
            build_task("1", "Dividend Reinvestment Check", "To do"),
            build_task("2", "Dividend Reinvestment Check", "To do"),
            build_task("3", "Math Session", "In progress"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/reports/hygiene")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertGreaterEqual(len(payload["duplicate_title_groups"]), 1)
            self.assertEqual(payload["current_count"], 1)
            self.assertIn("missing_fields", payload)

    def test_switch_action_rotates_without_blocking(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Queued Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post(
                "/checkin/1",
                json={"action": "switch", "progress": "low", "energy": "medium", "friction": "some"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("Switching", data["message"])
            self.assertIn("redirect_to", data)
            active = client.get("/active")
            self.assertEqual(active.status_code, 200)
            self.assertEqual(active.json()["current_task_id"], "2")

    def test_scheduler_ignores_system_task_when_normal_work_exists(self) -> None:
        tasks = [
            build_task("1", "[SYSTEM] Weekly Reset", "To do"),
            build_task("2", "Formalize logistic regression in Lean", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["current_task_id"], "2")

    def test_scheduler_collapses_multiple_current_tasks(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 200)
            active = client.get("/active")
            self.assertEqual(active.status_code, 200)
            self.assertEqual(active.json()["current_task_id"], "1")

    def test_hygiene_missing_fields_are_reported(self) -> None:
        tasks = [build_task("1", "Task", "To do")]
        with self._client_with_state(tasks, fields=[]) as client:
            self._login(client)
            response = client.get("/reports/hygiene")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("Scheduler State", payload["missing_fields"])

    def test_startup_report_includes_config_and_hygiene(self) -> None:
        tasks = [build_task("1", "Task", "To do")]
        with self._client_with_state(tasks, fields=[]) as client:
            self._login(client)
            response = client.get("/reports/startup")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["clickup"]["connectivity"], "ok")
            self.assertIn("config", payload)
            self.assertIn("hygiene", payload)
            self.assertIn("missing_fields", payload["clickup"])

    def test_diagnostics_report_includes_topology_conformance_and_degradation(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.degradation_events = [{"source": "test", "detail": "forced", "at": "now"}]
            response = client.get("/reports/diagnostics")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("topology", payload)
            self.assertIn("runtime_list", payload)
            self.assertIn("field_conformance", payload)
            self.assertIn("degradation_events", payload)
            self.assertIn("mode", payload["field_conformance"])
            self.assertIn("capabilities", payload["field_conformance"])
            self.assertIn("operator_actions_required", payload["field_conformance"])
            self.assertIn("selection_visibility", payload)
            self.assertIn("operational_state", payload)

    def test_diagnostics_flags_multi_current_violation(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            invariant = payload["runtime_list"]["current_task_invariant"]
            self.assertEqual(invariant["status"], "multi_current")
            self.assertTrue(invariant["violation"])

    def test_active_checkin_reports_multi_current_violation(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            response = client.get("/active/checkin")
            self.assertEqual(response.status_code, 409)
            self.assertIn("Multiple Current Tasks Detected", response.text)

    def test_operations_page_surfaces_blocking_multi_current_and_actions(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            response = client.get("/diagnostics")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Execution Operations", response.text)
            self.assertIn("Operational State", response.text)
            self.assertIn("operator actions required", response.text.lower())
            self.assertIn("Repair Multi-Current", response.text)

    def test_operations_page_shows_minimum_viable_guidance(self) -> None:
        names = list(REQUIRED_MINIMUM_FIELDS)
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._fields_from_names(names)) as client:
            self._login(client)
            response = client.get("/diagnostics")
            self.assertEqual(response.status_code, 200)
            self.assertIn("minimum_viable", response.text)
            self.assertIn("add missing recommended fields", response.text.lower())

    def test_runtime_remediation_keeps_exactly_one_current(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
            build_task("3", "Task C", "To do"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            response = client.post("/ops/remediate/runtime-current")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["changed"])
            self.assertTrue(payload["invariant_resolved"])
            self.assertEqual(payload["remediation_state"], "fully_repaired")
            self.assertEqual(payload["attempted_demotions"], 1)
            self.assertEqual(payload["successful_demotions"], 1)
            self.assertEqual(payload["failed_demotions"], 0)
            self.assertEqual(payload["post_check_current_count"], 1)
            self.assertIn("deterministic_rule", payload)
            self.assertIn("partial_failure", payload)
            self.assertIn("next_step", payload)
            statuses = {task["id"]: task["status"]["status"] for task in client.app.state.clickup.tasks}
            current = [tid for tid, status in statuses.items() if status == "In progress"]
            self.assertEqual(len(current), 1)

    def test_diagnostics_operational_state_blocking_for_multi_current(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            self.assertEqual(payload["operational_state"]["status"], "blocking")
            self.assertIn("multi_current_violation", payload["operational_state"]["reasons"])

    def test_diagnostics_selection_visibility_for_no_eligible_tasks(self) -> None:
        tasks = [
            build_task("1", "Done", "Complete"),
            build_task("2", "Done2", "Complete"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            summary = payload["selection_visibility"]["eligibility_summary"]
            self.assertEqual(summary["total_tasks"], 2)
            self.assertEqual(summary["eligible_candidate_count"], 0)

    def test_diagnostics_zero_current_with_eligible_candidates_is_promotable(self) -> None:
        tasks = [
            build_task("1", "Queued A", "To do"),
            build_task("2", "Queued B", "To do"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            resolution = payload["current_task_resolution_state"]
            self.assertEqual(resolution, "zero_current_candidates_available")
            self.assertFalse(payload["promotion_attempted"])
            self.assertIsNone(payload["promotion_verified"])
            self.assertEqual(payload["data_freshness"], "live")

    def test_diagnostics_zero_current_with_no_eligible_candidates_state(self) -> None:
        tasks = [
            build_task("1", "Done", "Complete"),
            build_task("2", "Done2", "Complete"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            resolution = payload["current_task_resolution_state"]
            self.assertEqual(resolution, "zero_current_no_eligible_candidates")

    def test_diagnostics_uses_stale_snapshot_on_transient_clickup_failure(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            live = client.get("/reports/diagnostics").json()
            self.assertEqual(live["data_freshness"], "live")

            client.app.state.clickup.fail_list_tasks = True
            stale = client.get("/reports/diagnostics").json()
            self.assertEqual(stale["data_freshness"], "stale")
            self.assertTrue(stale["retry_recommended"])
            self.assertTrue(stale["usable_despite_failure"])
            self.assertIn("source_failure", stale)
            self.assertEqual(stale["source_failure"]["source"], "clickup")
            self.assertIn(stale["source_failure"]["class"], {"timeout", "request_error", "auth_error", "not_found", "misconfigured", "unknown"})
            self.assertIn("at", stale["source_failure"])
            self.assertEqual(stale["runtime_list"]["task_count"], live["runtime_list"]["task_count"])

    def test_diagnostics_stale_failure_does_not_erase_last_known_good(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            _ = client.get("/reports/diagnostics")
            first_snapshot = client.app.state.last_known_operational_snapshot
            self.assertIsNotNone(first_snapshot)

            client.app.state.clickup.fail_list_tasks = True
            _ = client.get("/reports/diagnostics")
            second_snapshot = client.app.state.last_known_operational_snapshot
            self.assertEqual(first_snapshot["captured_at"], second_snapshot["captured_at"])

    def test_diagnostics_failure_without_snapshot_marks_unusable(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.clickup.fail_validate_access = True
            response = client.get("/reports/diagnostics")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["data_freshness"], "stale")
            self.assertFalse(payload["usable_despite_failure"])
            self.assertIn("source_failure", payload)
            self.assertEqual(payload["source_failure"]["class"], "misconfigured")
            self.assertEqual(payload["current_task_resolution_state"], "resolution_blocked_by_source_failure")

    def test_operations_page_shows_live_vs_stale_freshness(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            live_page = client.get("/diagnostics")
            self.assertEqual(live_page.status_code, 200)
            self.assertIn("Data freshness: live", live_page.text)

            client.app.state.clickup.fail_list_tasks = True
            stale_page = client.get("/diagnostics")
            self.assertEqual(stale_page.status_code, 200)
            self.assertIn("Data freshness: stale", stale_page.text)
            self.assertIn("Source failure:", stale_page.text)
            self.assertIn("snapshot:", stale_page.text)

    def test_active_reports_promotion_attempt_verified_for_zero_current(self) -> None:
        tasks = [
            build_task("1", "Queued A", "To do"),
            build_task("2", "Queued B", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/active")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["promotion_attempted"])
            self.assertTrue(payload["promotion_verified"])
            self.assertEqual(payload["current_task_resolution_state"], "promotion_succeeded")
            self.assertEqual(payload["operational_state"]["promotion_verified"], True)

    def test_active_reports_promotion_failure_when_write_not_verified(self) -> None:
        tasks = [
            build_task("1", "Queued A", "To do"),
            build_task("2", "Queued B", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)

            async def noop_update(task_id: str, **payload: Any) -> dict[str, Any]:
                for task in client.app.state.clickup.tasks:
                    if task["id"] == task_id:
                        return dict(task)
                raise KeyError(task_id)

            client.app.state.clickup.update_task = noop_update  # type: ignore[method-assign]
            response = client.get("/active")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIsNone(payload["current_task_id"])
            self.assertEqual(payload["state"]["current_task_resolution_state"], "promotion_failed")
            self.assertTrue(payload["state"]["promotion_attempted"])
            self.assertFalse(payload["state"]["promotion_verified"])

    def test_diagnostics_minimum_viable_guidance_prioritizes_missing_fields(self) -> None:
        names = list(REQUIRED_MINIMUM_FIELDS)
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._fields_from_names(names)) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            guidance = payload["field_conformance"]["minimum_viable_guidance"]
            self.assertIn("mode_explanation", guidance)
            self.assertEqual(guidance["next_best_fields"][:3], ["Today Minutes", "Rotation Score", "Next Eligible At"])
            groups = guidance["priority_groups"]
            self.assertEqual([g["priority"] for g in groups], [1, 2, 3, 4])
            by_label = {g["label"]: g for g in groups}
            self.assertIn("capability", by_label["Scheduling correctness"])
            self.assertIn("currently_degraded", by_label["Scheduling correctness"])
            self.assertIn("Next Eligible At", by_label["Scheduling correctness"]["fields"])
            self.assertIn("Domain", by_label["Decision quality"]["fields"])

    def test_runtime_remediation_is_deterministic(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            first = client.post("/ops/remediate/runtime-current").json()
            kept_first = first["kept_current_task_id"]

        tasks2 = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks2, fields=self._execution_fields()) as client:
            self._login(client)
            second = client.post("/ops/remediate/runtime-current").json()
            kept_second = second["kept_current_task_id"]

        self.assertEqual(kept_first, kept_second)

    def test_runtime_remediation_no_change_when_zero_or_one_current(self) -> None:
        with self._client_with_state([build_task("1", "Task", "To do")], fields=self._execution_fields()) as client:
            self._login(client)
            zero = client.post("/ops/remediate/runtime-current")
            self.assertEqual(zero.status_code, 200)
            self.assertFalse(zero.json()["changed"])

        with self._client_with_state([build_task("1", "Task", "In progress")], fields=self._execution_fields()) as client:
            self._login(client)
            one = client.post("/ops/remediate/runtime-current")
            self.assertEqual(one.status_code, 200)
            self.assertFalse(one.json()["changed"])

    def test_runtime_remediation_reports_unresolved_when_demotions_fail(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.clickup.update_task = self._failing_update_task  # type: ignore[method-assign]
            response = client.post("/ops/remediate/runtime-current")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["invariant_resolved"])
            self.assertFalse(payload["changed"])
            self.assertTrue(payload["partial_failure"])
            self.assertEqual(payload["attempted_demotions"], 1)
            self.assertEqual(payload["successful_demotions"], 0)
            self.assertEqual(payload["failed_demotions"], 1)
            self.assertEqual(payload["post_check_current_count"], 2)
            self.assertEqual(payload["remediation_state"], "attempted_no_live_change")
            self.assertIn("ClickUp rejected", payload["message"])
            self.assertEqual(payload["invariant"]["status"], "multi_current")
            self.assertTrue(any(str(w).startswith("demote_status:") for w in payload["warnings"]))

    def test_runtime_remediation_reports_attempted_successful_failed_demotions(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
            build_task("3", "Task C", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)

            async def selective_update(task_id: str, **payload: Any) -> dict[str, Any]:
                if task_id == "2":
                    raise main_module.ClickUpError("invalid status", status_code=400, error_code="STATUS_INVALID")
                return await FakeClickUpClient.update_task(client.app.state.clickup, task_id, **payload)

            client.app.state.clickup.update_task = selective_update  # type: ignore[method-assign]
            response = client.post("/ops/remediate/runtime-current")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["attempted_demotions"], 2)
            self.assertEqual(payload["successful_demotions"], 1)
            self.assertEqual(payload["failed_demotions"], 1)
            self.assertFalse(payload["invariant_resolved"])
            self.assertEqual(payload["post_check_current_count"], 2)
            self.assertEqual(set(payload["remaining_current_task_ids"]), {"2", payload["kept_current_task_id"]})
            self.assertEqual(len(payload["demotion_results"]), 2)
            failed = [item for item in payload["demotion_results"] if item["status_write"]["ok"] is False]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["task_id"], "2")
            self.assertEqual(failed[0]["status_write"]["error"]["class"], "invalid_status_or_payload")

    def test_runtime_remediation_reports_post_refresh_truth_fields(self) -> None:
        tasks = [
            build_task("1", "Task A", "In progress"),
            build_task("2", "Task B", "In progress"),
        ]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            payload = client.post("/ops/remediate/runtime-current").json()
            self.assertIn("remaining_current_task_ids", payload)
            self.assertIn("post_check_current_count", payload)
            self.assertIn("invariant_resolved", payload)
            self.assertEqual(payload["post_check_current_count"], len(payload["remaining_current_task_ids"]))
            self.assertTrue(payload["invariant_resolved"])

    def test_diagnostics_field_conformance_minimum_viable(self) -> None:
        names = list(REQUIRED_MINIMUM_FIELDS)
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._fields_from_names(names)) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            self.assertEqual(payload["field_conformance"]["mode"], "minimum_viable")

    def test_diagnostics_field_conformance_full_intended(self) -> None:
        names = list(REQUIRED_MINIMUM_FIELDS) + list(RECOMMENDED_FIELDS)
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._fields_from_names(names)) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            self.assertEqual(payload["field_conformance"]["mode"], "full_intended")

    def test_diagnostics_field_conformance_degraded_when_critical_missing(self) -> None:
        names = ["Task Type", "Progress Pulse"]
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._fields_from_names(names)) as client:
            self._login(client)
            payload = client.get("/reports/diagnostics").json()
            self.assertEqual(payload["field_conformance"]["mode"], "degraded")
            self.assertFalse(payload["field_conformance"]["capabilities"]["readiness_gate"])

    def test_diagnostics_detects_configured_vs_resolved_list_mismatch(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.execution_list_id = "resolved-other"
            payload = client.get("/reports/diagnostics").json()
            self.assertTrue(payload["runtime_list"]["config_mismatch"]["configured_vs_resolved_list_id"])

    def test_diagnostics_reports_pipeline_drift_visibility(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.clickup.list_tasks_by_id[client.app.state.clickup.pipeline_inbox_id] = [
                build_task("x1", "Pipeline Task", "To do")
            ]
            payload = client.get("/reports/diagnostics").json()
            self.assertEqual(payload["pipeline_drift"]["status"], "ok")
            self.assertTrue(payload["pipeline_drift"]["has_drift"])
            self.assertEqual(payload["pipeline_drift"]["drifted_tasks"][0]["label"], "intake")

    def test_operations_page_shows_drifted_tasks_and_guidance(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            client.app.state.clickup.list_tasks_by_id[client.app.state.clickup.pipeline_inbox_id] = [
                build_task("x1", "Pipeline Task", "To do")
            ]
            response = client.get("/diagnostics")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Pipeline Task", response.text)
            self.assertIn("move", response.text.lower())

    def test_source_failure_classification_matches_allowed_contract(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        cases = [
            ("timeout", main_module.ClickUpError("timed out")),
            ("auth_error", main_module.ClickUpError("denied", status_code=401)),
            ("request_error", main_module.ClickUpError("boom")),
            ("not_found", main_module.ClickUpError("missing", status_code=404)),
            ("misconfigured", main_module.ClickUpError("bad list", status_code=400, error_code="INPUT_003")),
        ]
        for expected, error in cases:
            with self.subTest(expected=expected):
                with self._client_with_state(tasks, fields=self._execution_fields()) as client:
                    self._login(client)

                    async def fail_validate_access(list_id: str) -> dict[str, Any]:
                        raise error

                    client.app.state.clickup.validate_access = fail_validate_access  # type: ignore[method-assign]
                    response = client.get("/reports/diagnostics")
                    payload = response.json()
                    self.assertEqual(payload["source_failure"]["class"], expected)
                    self.assertEqual(payload["retryable_failure"], expected in {"timeout", "request_error"})
                    self.assertEqual(payload["retry_recommended"], expected in {"timeout", "request_error"})

    def test_operational_state_consistent_between_diagnostics_and_active(self) -> None:
        tasks = [build_task("1", "Task", "In progress")]
        with self._client_with_state(tasks, fields=self._execution_fields()) as client:
            self._login(client)
            diagnostics = client.get("/reports/diagnostics").json()
            active = client.get("/active").json()
            self.assertEqual(diagnostics["current_task_resolution_state"], active["current_task_resolution_state"])
            self.assertEqual(diagnostics["data_freshness"], active["data_freshness"])
            self.assertEqual(diagnostics["operational_state"]["current_task_resolution_state"], active["operational_state"]["current_task_resolution_state"])

    def test_active_returns_503_on_clickup_failure(self) -> None:
        with self._client_with_tasks([]) as client:
            self._login(client)
            client.app.state.clickup.fail_list_tasks = True
            response = client.get("/active")
            self.assertEqual(response.status_code, 503)

    def test_active_returns_specific_detail_for_invalid_list(self) -> None:
        with self._client_with_tasks([]) as client:
            self._login(client)
            client.app.state.clickup.get_list_tasks = self._invalid_list_tasks  # type: ignore[method-assign]
            response = client.get("/active")
            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertEqual(payload["error_code"], "runtime_list_misconfigured")

    def test_malformed_checkin_payload_returns_400(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            response = client.post("/checkin/1", content="{bad", headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)

    def test_logout_clears_session(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "To do")]) as client:
            self._login(client)
            response = client.post("/logout", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            denied = client.get("/active/checkin", follow_redirects=False)
            self.assertEqual(denied.status_code, 303)

    def test_telegram_webhook_requires_secret_when_configured(self) -> None:
        with self._client_with_tasks([]) as client:
            response = client.post("/telegram/webhook", json={"callback_query": {"id": "1", "data": "continue:1"}})
            self.assertEqual(response.status_code, 401)

    def test_clickup_webhook_rejects_bad_payload(self) -> None:
        with self._client_with_tasks([]) as client:
            body = b"{bad"
            signature = main_module.hmac.new(b"secret", body, main_module.hashlib.sha256).hexdigest()
            response = client.post("/clickup/webhook", content=body, headers={"x-signature": signature})
            self.assertEqual(response.status_code, 400)

    def test_work_hours_helper(self) -> None:
        settings = main_module.load_settings()
        in_window = datetime(2026, 4, 13, 10, 0)
        out_window = datetime(2026, 4, 13, 23, 0)
        self.assertTrue(main_module.in_work_hours(in_window, settings))
        self.assertFalse(main_module.in_work_hours(out_window, settings))

    def test_weekly_summary_helper(self) -> None:
        settings = main_module.load_settings()
        due = datetime(2026, 4, 12, 18, 30)
        early = datetime(2026, 4, 12, 17, 30)
        self.assertTrue(main_module.should_send_weekly_summary(due, None, settings))
        self.assertFalse(main_module.should_send_weekly_summary(early, None, settings))

    def test_continue_response_includes_block_and_action(self) -> None:
        tasks = [build_task("1", "Active Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post(
                "/checkin/1",
                json={"action": "continue", "progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["action"], "continue")
            self.assertIn("block", data)
            self.assertIsNotNone(data["block"])
            self.assertIsNone(data["redirect_to"])
            self.assertFalse(data["partial_failure"])

    def test_complete_response_includes_redirect(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "complete"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["action"], "complete")
            self.assertIsNotNone(data["redirect_to"])
            self.assertIn("/checkin/", data["redirect_to"])
            self.assertIsNotNone(data["next_task"])
            self.assertEqual(client.app.state.clickup.status_updates[0]["status"], "Complete")

    def test_complete_returns_structured_error_when_configured_completed_status_missing(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "To do", "type": "open"},
                {"status": "In progress", "type": "custom"},
            ]
            response = client.post("/checkin/1", json={"action": "complete"})
            self.assertEqual(response.status_code, 503)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], "invalid_execution_config")
            self.assertIn("Configured completed status", data["message"])

    def test_complete_returns_specific_error_when_real_completion_status_write_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)

            async def reject_all_complete_statuses(task_id: str, **payload: Any) -> dict[str, Any]:
                raise main_module.ClickUpError("status invalid", status_code=422, error_code="STATUS_INVALID")

            client.app.state.clickup.update_task = reject_all_complete_statuses  # type: ignore[method-assign]
            response = client.post("/checkin/1", json={"action": "complete"})
            self.assertEqual(response.status_code, 422)
            data = response.json()
            self.assertEqual(data["error_code"], "invalid_input")
            self.assertIn("Completion task status update failed", data["message"])

    def test_blocked_response_includes_redirect(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "blocked"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["action"], "blocked")
            self.assertIsNotNone(data["redirect_to"])

    def test_break_response_has_no_redirect(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post(
                "/checkin/1",
                json={"action": "break", "break_minutes": 10},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["action"], "break")
            self.assertEqual(data["redirect_to"], "/active/checkin")
            self.assertEqual(data["next_task_resolution_state"], "zero_current")
            self.assertIn("Break", data["message"])
            self.assertEqual(client.app.state.clickup.status_updates[-1]["status"], "To do")

    def test_break_does_not_send_scheduler_state_as_clickup_status(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "break", "break_minutes": 10})
            self.assertEqual(response.status_code, 200)
            status_updates = [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "1"]
            self.assertIn("To do", status_updates)
            self.assertNotIn("Break", status_updates)

    def test_blocked_does_not_send_scheduler_state_as_clickup_status(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "blocked"})
            self.assertEqual(response.status_code, 200)
            status_updates = [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "1"]
            self.assertIn("To do", status_updates)
            self.assertNotIn("Blocked", status_updates)

    def test_break_succeeds_when_configured_open_status_missing_but_real_open_exists(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "Open", "type": "open"},
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            response = client.post("/checkin/1", json={"action": "break", "break_minutes": 10})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial_failure"])
            self.assertIn("Open", [item["status"] for item in client.app.state.clickup.status_updates])
            self.assertNotIn("To do", [item["status"] for item in client.app.state.clickup.status_updates])

    def test_blocked_succeeds_when_configured_open_status_missing_but_real_open_exists(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "Open"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "Open", "type": "open"},
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            response = client.post("/checkin/1", json={"action": "blocked"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial_failure"])
            self.assertIn("Open", [item["status"] for item in client.app.state.clickup.status_updates])

    def test_switch_succeeds_when_configured_open_status_missing_but_real_open_exists(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "Open"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "Open", "type": "open"},
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            response = client.post("/checkin/1", json={"action": "switch"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial_failure"])
            self.assertIn("Open", [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "1"])

    def test_scheduler_state_is_written_independently_from_task_status(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "break", "break_minutes": 10})
            self.assertEqual(response.status_code, 200)
            self.assertIn("To do", [item["status"] for item in client.app.state.clickup.status_updates])
            scheduler_field_id = next(field.id for field in client.app.state.clickup.fields if field.name == "Scheduler State")
            scheduler_updates = [item for item in client.app.state.clickup.field_updates if item["field_id"] == scheduler_field_id]
            self.assertTrue(scheduler_updates)
            break_option = next(
                option["id"]
                for field in client.app.state.clickup.fields
                if field.name == "Scheduler State"
                for option in field.type_config["options"]
                if option["name"] == "Break"
            )
            self.assertEqual(scheduler_updates[-1]["value"], break_option)

    def test_save_failure_returns_structured_error(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.update_task = self._failing_update_task  # type: ignore[method-assign]
            response = client.post(
                "/checkin/1",
                json={"action": "continue", "progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 503)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], "clickup_unavailable")
            self.assertTrue(data["retry_safe"])

    def test_continue_returns_partial_failure_when_secondary_write_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()
            client.app.state.clickup.set_custom_field = self._failing_set_custom_field  # type: ignore[method-assign]
            response = client.post(
                "/checkin/1",
                json={"action": "continue", "progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["partial_failure"])
            self.assertIn("warnings", data)
            self.assertIn("pulse_metrics", data["write_details"]["failure_groups"])

    def test_continue_returns_unverified_when_post_write_readback_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            seen = {"count": 0}

            async def flaky_get_task(task_id: str) -> dict[str, Any]:
                seen["count"] += 1
                if seen["count"] >= 2:
                    raise main_module.ClickUpError("boom", status_code=503)
                return await FakeClickUpClient.get_task(client.app.state.clickup, task_id)

            client.app.state.clickup.get_task = flaky_get_task  # type: ignore[method-assign]
            response = client.post("/checkin/1", json={"action": "continue"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["verification_status"], "unverified")
            self.assertFalse(data["partial_failure"])
            self.assertEqual(data["ui_severity"], "warn")
            self.assertEqual(data["semantic_outcome"], "unverified_but_probable_success")
            self.assertIn("Secondary verification is still pending", data["ui_message"])

    def test_continue_fails_only_when_no_active_status_can_be_resolved(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [{"status": "Complete", "type": "closed"}]
            response = client.post("/checkin/1", json={"action": "continue"})
            self.assertEqual(response.status_code, 503)
            data = response.json()
            self.assertEqual(data["error_code"], "invalid_execution_config")

    def test_complete_returns_partial_failure_when_secondary_write_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()
            client.app.state.clickup.set_custom_field = self._failing_set_custom_field  # type: ignore[method-assign]
            response = client.post("/checkin/1", json={"action": "complete"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["partial_failure"])

    def test_break_returns_partial_failure_when_secondary_write_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()
            client.app.state.clickup.set_custom_field = self._failing_set_custom_field  # type: ignore[method-assign]
            response = client.post(
                "/checkin/1",
                json={"action": "break", "break_minutes": 10, "progress": "low", "energy": "high", "friction": "some"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["verification_status"], "failed")

    def test_switch_returns_partial_failure_when_secondary_write_fails(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()
            client.app.state.clickup.set_custom_field = self._failing_set_custom_field  # type: ignore[method-assign]
            response = client.post(
                "/checkin/1",
                json={"action": "switch", "progress": "low", "energy": "high", "friction": "some"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["verification_status"], "failed")

    def test_blocked_returns_partial_failure_when_secondary_write_fails(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()
            client.app.state.clickup.set_custom_field = self._failing_set_custom_field  # type: ignore[method-assign]
            response = client.post(
                "/checkin/1",
                json={"action": "blocked", "progress": "low", "energy": "high", "friction": "some"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["verification_status"], "failed")

    def test_blocked_verifies_primary_success_when_optional_native_status_unresolved(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "In progress"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            response = client.post("/checkin/1", json={"action": "blocked"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["verification_status"], "verified")
            self.assertTrue(data["partial_failure"])
            self.assertIn("task_status_unresolved", data["warnings"])
            self.assertIn("scheduler_state", [item["label"] for item in data["write_details"]["field_writes"] if item["ok"] is True])
            self.assertEqual(data["ui_severity"], "warn")

    def test_api_queue_returns_tasks(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Queued A", "To do"),
            build_task("3", "Queued B", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/api/queue")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("tasks", data)
            self.assertIn("count", data)
            # Current task should be excluded
            ids = [t["id"] for t in data["tasks"]]
            self.assertNotIn("1", ids)

    def test_api_queue_includes_reasons(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Queued A", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/api/queue")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("reasons", data["tasks"][0])
            self.assertGreaterEqual(len(data["tasks"][0]["reasons"]), 1)

    def test_api_switch_to_promotes_target(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Target Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post(
                "/api/switch-to/2",
                json={"progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["redirect_to"], "/checkin/2")
            target_updates = [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "2"]
            self.assertEqual(target_updates[-1], "In progress")

    def test_api_switch_to_returns_partial_failure_when_target_scheduler_write_fails(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Target Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()

            async def fail_target_scheduler(task_id: str, field_id: str, value: Any, *, time: bool | None = None) -> None:
                if task_id == "2":
                    raise main_module.ClickUpError("boom", status_code=503)
                return

            client.app.state.clickup.set_custom_field = fail_target_scheduler  # type: ignore[method-assign]
            response = client.post(
                "/api/switch-to/2",
                json={"progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["partial_failure"])
            self.assertIn("target_scheduler_state", data["warnings"])

    def test_api_switch_to_returns_structured_error_when_target_promotion_fails(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Target Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.fields = self._execution_fields()

            async def fail_target_update(task_id: str, **payload: Any) -> dict[str, Any]:
                if task_id == "2":
                    raise main_module.ClickUpError("boom", status_code=503)
                for task in client.app.state.clickup.tasks:
                    if task["id"] == task_id and "status" in payload:
                        task["status"] = {"status": payload["status"]}
                return {"id": task_id}

            client.app.state.clickup.update_task = fail_target_update  # type: ignore[method-assign]
            response = client.post(
                "/api/switch-to/2",
                json={"progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 503)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], "clickup_unavailable")

    def test_api_switch_to_falls_back_when_configured_open_status_is_invalid(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Target Task", "Open"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "Open", "type": "open"},
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            response = client.post(
                "/api/switch-to/2",
                json={"progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertIn("Open", [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "1"])
            self.assertEqual(
                [item["status"] for item in client.app.state.clickup.status_updates if item["task_id"] == "2"][-1],
                "In progress",
            )

    def test_no_action_path_sends_scheduler_state_labels_as_native_statuses(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "Open"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.statuses = [
                {"status": "Open", "type": "open"},
                {"status": "In progress", "type": "custom"},
                {"status": "Complete", "type": "closed"},
            ]
            client.post("/checkin/1", json={"action": "break", "break_minutes": 10})
            client.post("/checkin/1", json={"action": "blocked"})
            client.post("/api/switch-to/2", json={"progress": "medium", "energy": "medium", "friction": "none"})
            written = {item["status"] for item in client.app.state.clickup.status_updates}
            self.assertTrue(written.isdisjoint({"Queued", "Current", "Break", "Blocked", "Done today"}))

    def test_api_switch_to_returns_invalid_input_when_real_switch_status_write_fails(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Target Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)

            async def reject_all_status_updates(task_id: str, **payload: Any) -> dict[str, Any]:
                raise main_module.ClickUpError("status invalid", status_code=422, error_code="STATUS_INVALID")

            client.app.state.clickup.update_task = reject_all_status_updates  # type: ignore[method-assign]
            response = client.post(
                "/api/switch-to/2",
                json={"progress": "medium", "energy": "medium", "friction": "none"},
            )
            self.assertEqual(response.status_code, 422)
            data = response.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error_code"], "invalid_input")
            self.assertIn("Switch target task status update failed", data["message"])

    def test_complete_bounded_when_scheduler_followup_is_slow(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            original_run_scheduler = main_module.run_scheduler

            async def slow_scheduler(*args: Any, **kwargs: Any) -> dict[str, Any]:
                await main_module.asyncio.sleep(5)
                return {"ok": True, "current_task_id": "2"}

            main_module.run_scheduler = slow_scheduler
            try:
                response = client.post("/checkin/1", json={"action": "complete"})
            finally:
                main_module.run_scheduler = original_run_scheduler
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["verification_status"], "verified")
            self.assertTrue(data["partial_failure"])
            self.assertIn("scheduler_followup_deferred", data["warnings"])
            self.assertEqual(data["redirect_to"], "/active/checkin")
            self.assertIn("Reloading check-in to resolve the next task", data["ui_message"])

    def test_blocked_verified_transition_reports_next_task_resolution_success(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Next Task", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post("/checkin/1", json={"action": "blocked"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["action_semantically_succeeded"])
            self.assertTrue(data["post_write_verified"])
            self.assertTrue(data["next_task_resolution_succeeded"])
            self.assertEqual(data["semantic_outcome"], "verified_success")
            self.assertEqual(data["current_task_before"]["id"], "1")
            self.assertEqual(data["current_task_after"]["id"], "2")
            self.assertEqual(data["next_task_resolution_state"], "resolved")
            self.assertEqual(data["ui_severity"], "success")

    def test_switch_returns_truthful_zero_current_when_no_next_task_is_available(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.post(
                "/checkin/1",
                json={"action": "switch", "progress": "low", "energy": "medium", "friction": "some"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["semantic_outcome"], "verified_success")
            self.assertEqual(data["next_task_resolution_state"], "zero_current")
            self.assertEqual(data["current_task_before"]["id"], "1")
            self.assertIsNone(data["current_task_after"])
            self.assertIn("No next task was eligible yet", data["ui_message"])

    def test_checkin_page_wraps_up_next_cards_safely(self) -> None:
        long_name = "Very long up next task title " * 10
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", long_name, "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("overflow-wrap: anywhere", response.text)
            self.assertIn("word-break: break-word", response.text)
            self.assertIn("flex-wrap: wrap", response.text)

    def test_clickup_webhook_records_degradation_when_scheduler_run_fails(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            original_run_scheduler = main_module.run_scheduler

            async def degraded_scheduler(*args: Any, **kwargs: Any) -> dict[str, Any]:
                return {"ok": False, "error": "forced_scheduler_failure"}

            main_module.run_scheduler = degraded_scheduler
            try:
                body = b'{"event":"taskUpdated"}'
                signature = main_module.hmac.new(b"secret", body, main_module.hashlib.sha256).hexdigest()
                response = client.post("/clickup/webhook", content=body, headers={"x-signature": signature})
                self.assertEqual(response.status_code, 200)
                events = client.app.state.degradation_events
                self.assertGreaterEqual(len(events), 1)
                self.assertEqual(events[-1]["source"], "clickup_webhook")
            finally:
                main_module.run_scheduler = original_run_scheduler

    def test_checkin_page_has_block_progress(self) -> None:
        tasks = [build_task("1", "Active Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("block-bar", response.text)
            self.assertIn("Current", response.text)
            self.assertIn("__CHECKIN_DATA__", response.text)

    def test_checkin_page_has_session_overlay(self) -> None:
        tasks = [build_task("1", "Active Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertIn("session-overlay", response.text)
            self.assertIn("Session expired", response.text)

    def test_no_task_page_has_run_scheduler_button(self) -> None:
        tasks = [
            build_task("1", "Done", "Complete"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/active/checkin")
            self.assertEqual(response.status_code, 404)
            self.assertIn("Nothing queued", response.text)
            self.assertIn("Run Scheduler", response.text)

    def test_quick_add_enrolls_existing_task(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        outside = build_task("99", "Backlog Item", "To do")
        outside["list"] = {"id": "other-list", "name": "Backlog"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.outside_tasks = [outside]
            response = client.post(
                "/api/tasks/quick-add",
                json={"task_id": "99"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["name"], "Backlog Item")
            self.assertIsNone(data["redirect_to"])

    def test_quick_add_adds_task_to_engine_list(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        outside = build_task("99", "Backlog Item", "To do")
        outside["list"] = {"id": "other-list", "name": "Backlog"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            fake = client.app.state.clickup
            fake.outside_tasks = [outside]
            client.post("/api/tasks/quick-add", json={"task_id": "99"})
            self.assertEqual(len(fake.added_to_list), 1)
            self.assertEqual(fake.added_to_list[0]["task_id"], "99")
            self.assertEqual(fake.added_to_list[0]["list_id"], client.app.state.execution_list_id)

    def test_quick_add_with_switch_returns_redirect(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        outside = build_task("99", "Urgent Backlog", "To do")
        outside["list"] = {"id": "other-list", "name": "Backlog"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.outside_tasks = [outside]
            response = client.post(
                "/api/tasks/quick-add",
                json={"task_id": "99", "switch_to": True},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertIn("/checkin/", data["redirect_to"])

    def test_quick_add_rejects_invalid_input(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            missing_id = client.post("/api/tasks/quick-add", json={"task_id": ""})
            self.assertEqual(missing_id.status_code, 422)
            no_field = client.post("/api/tasks/quick-add", json={})
            self.assertEqual(no_field.status_code, 422)

    def test_quick_add_reports_partial_failure_when_field_sync_unavailable(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        outside = build_task("99", "Backlog Item", "To do")
        outside["list"] = {"id": "other-list", "name": "Backlog"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            fake = client.app.state.clickup
            fake.outside_tasks = [outside]
            fake.fail_list_fields = True
            response = client.post(
                "/api/tasks/quick-add",
                json={"task_id": "99"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["partial_failure"])
            self.assertIn("field_lookup_failed", data["warnings"])

    def test_importable_tasks_returns_workspace_tasks_excluding_engine(self) -> None:
        tasks = [build_task("1", "Engine Task", "In progress")]
        outside_a = build_task("10", "Backlog A", "To do")
        outside_a["list"] = {"id": "backlog", "name": "Backlog"}
        outside_a["priority"] = {"priority": "high"}
        outside_b = build_task("11", "Backlog B", "To do")
        outside_b["list"] = {"id": "inbox", "name": "Inbox"}
        outside_b["priority"] = {"priority": "normal"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.outside_tasks = [outside_a, outside_b]
            response = client.get("/api/tasks/importable")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(len(data["tasks"]), 2)
            # High priority comes first
            self.assertEqual(data["tasks"][0]["id"], "10")
            self.assertEqual(data["tasks"][0]["list_name"], "Backlog")
            self.assertEqual(data["tasks"][0]["priority_label"], "high")

    def test_importable_tasks_excludes_tasks_already_in_engine(self) -> None:
        # A task whose list id matches execution_list_id should be filtered out
        engine_list_id = "list"  # matches fake_load_settings clickup_list_id
        tasks = [build_task("1", "Engine Task", "In progress")]
        outside = build_task("20", "Already In Engine", "To do")
        outside["list"] = {"id": engine_list_id, "name": "Execution Engine"}
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            client.app.state.clickup.outside_tasks = [outside]
            response = client.get("/api/tasks/importable")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["tasks"], [])

    def test_importable_tasks_empty_when_no_workspace_id(self) -> None:
        tasks = [build_task("1", "Engine Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            # Swap out workspace_id by patching the settings object
            import dataclasses
            client.app.state.settings = dataclasses.replace(
                client.app.state.settings, clickup_workspace_id=""
            )
            outside = build_task("99", "Backlog", "To do")
            outside["list"] = {"id": "other", "name": "Other"}
            client.app.state.clickup.outside_tasks = [outside]
            response = client.get("/api/tasks/importable")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["tasks"], [])

    def test_checkin_page_has_up_next_preview_and_quick_add_controls(self) -> None:
        tasks = [
            build_task("1", "Current Task", "In progress"),
            build_task("2", "Queued A", "To do"),
            build_task("3", "Queued B", "To do"),
        ]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Up Next", response.text)
            self.assertIn("queue_preview", response.text)
            self.assertIn("quick-add-list", response.text)
            self.assertIn("adding_task", response.text)

    def test_checkin_page_includes_slow_save_message_contract(self) -> None:
        tasks = [build_task("1", "Current Task", "In progress")]
        with self._client_with_tasks(tasks) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Still working", response.text)
            self.assertIn("SLOW_MS = 3000", response.text)

    async def _invalid_list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        raise main_module.ClickUpError("invalid list", status_code=400, error_code="INPUT_003")

    async def _failing_update_task(self, task_id: str, **payload: Any) -> dict[str, Any]:
        raise main_module.ClickUpError("boom", status_code=503)

    async def _failing_set_custom_field(self, task_id: str, field_id: str, value: Any, *, time: bool | None = None) -> None:
        raise main_module.ClickUpError("boom", status_code=503)

    # ------------------------------------------------------------------
    # New behavioural tests (BUG 1-3 / GAP 4-5 coverage)
    # ------------------------------------------------------------------

    def test_scheduler_excludes_inbox_tasks(self) -> None:
        """Tasks with Scheduler State = Inbox must never be promoted."""
        inbox_task = build_task("1", "Inbox Task", "To do")
        inbox_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "i"}]
        queued_task = build_task("2", "Queued Task", "To do")
        queued_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "q"}]
        with self._client_with_tasks([inbox_task, queued_task]) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["current_task_id"], "2")

    def test_sync_scheduler_state_preserves_break(self) -> None:
        """sync_scheduler_state must not overwrite Break state to Queued."""
        current_task = build_task("1", "Current Task", "In progress")
        break_task = build_task("2", "Break Task", "To do")
        break_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "b"}]
        with self._client_with_tasks([current_task, break_task]) as client:
            self._login(client)
            client.post("/scheduler/run")
            queued_writes_for_break = [
                upd
                for upd in client.app.state.clickup.field_updates
                if upd["task_id"] == "2" and upd["field_id"] == "f1" and upd["value"] == "q"
            ]
            self.assertEqual(queued_writes_for_break, [], "Break task must not be overwritten to Queued")

    def test_scheduler_hard_excludes_blocked_by_field(self) -> None:
        """A task blocked via Scheduler State field (not native status) must be excluded."""
        blocked_task = build_task("1", "Blocked Task", "To do")
        blocked_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "blk"}]
        queued_task = build_task("2", "Queued Task", "To do")
        queued_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "q"}]
        with self._client_with_tasks([blocked_task, queued_task]) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["current_task_id"], "2")

    def test_scheduler_priority_high_wins_over_normal(self) -> None:
        """A High-priority task beats a Normal-priority task of the same type."""
        high_task = build_task("1", "High Priority Task", "To do")
        high_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "q"},
                                       {"id": "f2", "name": "Task Type", "value": "med"}]
        high_task["priority"] = {"priority": "2"}  # High
        normal_task = build_task("2", "Normal Priority Task", "To do")
        normal_task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "q"},
                                         {"id": "f2", "name": "Task Type", "value": "med"}]
        normal_task["priority"] = {"priority": "3"}  # Normal
        with self._client_with_tasks([high_task, normal_task]) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["current_task_id"], "1")

    def test_dual_signal_current_detection_via_field_only(self) -> None:
        """detect_current_task_invariant must recognise tasks current via field-only signal."""
        task = build_task("42", "Field-Only Current", "To do")  # native status is NOT "In progress"
        task["custom_fields"] = [{"id": "f1", "name": "Scheduler State", "value": "c"}]
        settings = main_module.load_settings()
        fields = self._execution_fields()
        result = main_module.detect_current_task_invariant([task], settings, fields)
        self.assertEqual(result["status"], "one_current")
        self.assertIn("42", result["task_ids"])
        # Native "To do" ≠ field "current" → dual-signal drift must be reported.
        self.assertIn("42", result["dual_signal_drift"])

    def test_scheduler_null_priority_does_not_crash(self) -> None:
        """Tasks with priority=null (ClickUp API) must not cause a 500."""
        task = build_task("1", "No Priority Task", "To do")
        task["priority"] = None  # ClickUp returns null when no priority is set
        with self._client_with_tasks([task]) as client:
            self._login(client)
            response = client.post("/scheduler/run")
            self.assertNotEqual(response.status_code, 500)

    def test_checkin_mobile_viewport_meta(self) -> None:
        """Checkin page must include viewport meta tag and the 380px mobile breakpoint."""
        task = build_task("1", "Mobile Task", "In progress")
        with self._client_with_tasks([task]) as client:
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn('<meta name="viewport"', response.text)
            self.assertIn("width=device-width", response.text)
            self.assertIn("@media (max-width: 380px)", response.text)

    def test_checkin_parent_name_via_api_fallback(self) -> None:
        """Parent name must appear even when the parent task is not in the execution list."""
        parent = build_task("99", "Parent Project", "To do")
        child = build_task("1", "Child Task", "In progress", parent="99")
        with self._client_with_tasks([child]) as client:
            # Execution list only contains the child; parent is reachable via get_task
            client.app.state.clickup.list_tasks_by_id[client.app.state.clickup.runtime_list_id] = [child]
            client.app.state.clickup.tasks = [child, parent]
            self._login(client)
            response = client.get("/checkin/1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Parent Project", response.text)


class ConfigTests(unittest.TestCase):
    def test_load_settings_normalizes_clickup_ui_list_id(self) -> None:
        original = os.environ.copy()
        try:
            os.environ.update(
                {
                    "CLICKUP_API_TOKEN": "token",
                    "CLICKUP_WORKSPACE_ID": "workspace",
                    "CLICKUP_LIST_ID": "6-123456789012-1",
                    "PUBLIC_BASE_URL": "https://example.test",
                    "APP_SHARED_SECRET": "secret",
                    "SESSION_SECRET": "session",
                }
            )
            settings = main_module.load_settings()
            self.assertEqual(settings.clickup_list_id, "123456789012")
        finally:
            os.environ.clear()
            os.environ.update(original)


if __name__ == "__main__":
    unittest.main()
