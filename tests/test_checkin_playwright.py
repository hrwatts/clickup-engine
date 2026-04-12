from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings

playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright
Error = playwright_sync.Error


class FakeClickUpClient:
    def __init__(self, token: str) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.fields: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return

    async def get_list_fields(self, list_id: str) -> list[Any]:
        return list(self.fields)

    async def get_list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        return [dict(task) for task in self.tasks]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                return dict(task)
        raise KeyError(task_id)

    async def update_task(self, task_id: str, **payload: Any) -> dict[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                if "status" in payload:
                    task["status"] = {"status": payload["status"]}
                return dict(task)
        raise KeyError(task_id)

    async def set_custom_field(self, task_id: str, field_id: str, value: Any, *, time: bool | None = None) -> None:
        return

    async def validate_access(self, list_id: str) -> dict[str, Any]:
        return {"id": list_id, "name": "Execution Engine"}


class FakeNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        return

    async def aclose(self) -> None:
        return

    async def send_task_prompt(self, task: dict[str, Any], checkin_url: str) -> None:
        return

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        return

    async def send_message(self, text: str) -> None:
        return


def build_task(task_id: str, name: str, status: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": status},
        "custom_fields": [],
    }


class CheckinPlaywrightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_load_settings = main_module.load_settings
        self._orig_clickup_client = main_module.ClickUpClient
        self._orig_notifier = main_module.TelegramNotifier

        def fake_load_settings() -> Settings:
            return Settings(
                clickup_token="token",
                clickup_workspace_id="",
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
                default_continue_minutes=20,
                short_break_minutes=10,
                long_break_minutes=20,
                blocked_cooldown_minutes=90,
            )

        main_module.load_settings = fake_load_settings
        main_module.ClickUpClient = FakeClickUpClient
        main_module.TelegramNotifier = FakeNotifier

    def tearDown(self) -> None:
        main_module.load_settings = self._orig_load_settings
        main_module.ClickUpClient = self._orig_clickup_client
        main_module.TelegramNotifier = self._orig_notifier

    @contextmanager
    def _client_with_tasks(self, tasks: list[dict[str, Any]]):
        with TestClient(main_module.app) as client:
            client.app.state.clickup.tasks = tasks
            yield client

    def _login(self, client: TestClient) -> None:
        response = client.post("/login", data={"password": "open-sesame"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def _open_page(self, html: str, init_script: str, assertion_script: str) -> None:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content(html)
                page.evaluate(init_script)
                page.wait_for_selector('button[data-action="continue"]')
                page.wait_for_timeout(120)
                page.click('button[data-action="continue"]')
                page.evaluate(assertion_script)
                browser.close()
        except Error as exc:
            text = str(exc)
            if "Executable doesn't exist" in text or "Failed to launch" in text:
                pytest.skip(f"Playwright browser unavailable: {exc}")
            raise

    def test_playwright_slow_save_transition(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            html = client.get("/checkin/1").text

        init_script = """
            window.fetch = (url, opts) => new Promise((resolve) => {
                setTimeout(() => {
                    resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            ok: true,
                            message: '\\u2713 Logged \\u2014 20/60m',
                            action: 'continue',
                            block: {
                                slice_minutes: 20,
                                block_minutes: 20,
                                target_minutes: 60,
                                remaining_minutes: 40,
                                reached_target: false,
                                exceeded_max: false
                            },
                            redirect_to: null,
                            next_task: null,
                            partial_failure: false
                        })
                    });
                }, 3600);
            });
        """
        assertion_script = """
            (async () => {
                const statusEl = document.getElementById('status');
                const waitFor = (needle, timeoutMs) => new Promise((resolve, reject) => {
                    const started = Date.now();
                    const timer = setInterval(() => {
                        const text = statusEl.textContent || '';
                        if (text.includes(needle)) {
                            clearInterval(timer);
                            resolve(true);
                            return;
                        }
                        if (Date.now() - started > timeoutMs) {
                            clearInterval(timer);
                            reject(new Error('missing status: ' + needle + ' got=' + text));
                        }
                    }, 120);
                });
                await waitFor('Still working', 6500);
                await waitFor('Logged', 9000);
                return true;
            })()
        """
        self._open_page(html, init_script, assertion_script)

    def test_playwright_timeout_transition(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            html = client.get("/checkin/1").text

        init_script = """
            window.fetch = (url, opts) => {
                const asText = String(url || '');
                if (asText.includes('/checkin/')) {
                    return new Promise((resolve, reject) => {
                        setTimeout(() => {
                            const err = new Error('aborted');
                            err.name = 'AbortError';
                            reject(err);
                        }, 1200);
                    });
                }
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ tasks: [], count: 0 }) });
            };
        """
        assertion_script = """
            (async () => {
                const statusEl = document.getElementById('status');
                const waitFor = (needle, timeoutMs) => new Promise((resolve, reject) => {
                    const started = Date.now();
                    const timer = setInterval(() => {
                        const text = statusEl.textContent || '';
                        if (text.includes(needle)) {
                            clearInterval(timer);
                            resolve(true);
                            return;
                        }
                        if (Date.now() - started > timeoutMs) {
                            clearInterval(timer);
                            reject(new Error('missing status: ' + needle + ' got=' + text));
                        }
                    }, 120);
                });
                await waitFor('Timed out', 4000);
                return true;
            })()
        """
        self._open_page(html, init_script, assertion_script)

    def test_playwright_unverified_transition(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            html = client.get("/checkin/1").text

        init_script = """
            window.fetch = async () => ({
                ok: true,
                status: 200,
                json: async () => ({
                    ok: true,
                    message: 'Saved',
                    action: 'continue',
                    block: null,
                    redirect_to: null,
                    next_task: null,
                    partial_failure: false,
                    verification_status: 'unverified'
                })
            });
        """
        assertion_script = """
            (async () => {
                const statusEl = document.getElementById('status');
                const waitFor = (needle, timeoutMs) => new Promise((resolve, reject) => {
                    const started = Date.now();
                    const timer = setInterval(() => {
                        const text = statusEl.textContent || '';
                        if (text.includes(needle)) {
                            clearInterval(timer);
                            resolve(true);
                            return;
                        }
                        if (Date.now() - started > timeoutMs) {
                            clearInterval(timer);
                            reject(new Error('missing status: ' + needle + ' got=' + text));
                        }
                    }, 120);
                });
                await waitFor('before verification completed', 3000);
                return true;
            })()
        """
        self._open_page(html, init_script, assertion_script)

    def test_playwright_partial_success_uses_single_coherent_message(self) -> None:
        with self._client_with_tasks([build_task("1", "Task", "In progress")]) as client:
            self._login(client)
            html = client.get("/checkin/1").text

        init_script = """
            window.fetch = async () => ({
                ok: true,
                status: 200,
                json: async () => ({
                    ok: true,
                    message: 'legacy message',
                    ui_message: 'Break started. Follow-up read could not be completed yet. Reload to confirm.',
                    ui_severity: 'warn',
                    action: 'break',
                    block: null,
                    redirect_to: null,
                    next_task: null,
                    partial_failure: true,
                    verification_status: 'unverified'
                })
            });
        """
        assertion_script = """
            (async () => {
                const statusEl = document.getElementById('status');
                const waitFor = (needle, timeoutMs) => new Promise((resolve, reject) => {
                    const started = Date.now();
                    const timer = setInterval(() => {
                        const text = statusEl.textContent || '';
                        if (text.includes(needle)) {
                            clearInterval(timer);
                            resolve(text);
                            return;
                        }
                        if (Date.now() - started > timeoutMs) {
                            clearInterval(timer);
                            reject(new Error('missing status: ' + needle + ' got=' + text));
                        }
                    }, 120);
                });
                const text = await waitFor('Break started.', 3000);
                if (text.includes('✗ ✓')) throw new Error('mixed message icons found: ' + text);
                return true;
            })()
        """
        self._open_page(html, init_script, assertion_script)
