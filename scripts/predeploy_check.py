from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as main_module
from app.clickup import ClickUpClient
from app.config import load_settings


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@contextmanager
def apply_env(overrides: dict[str, str]):
    original = os.environ.copy()
    try:
        os.environ.update(overrides)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def print_result(name: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "FAIL"
    safe_detail = detail.encode("ascii", errors="backslashreplace").decode("ascii")
    print(f"[{status}] {name}: {safe_detail}")


async def run_clickup_probes(settings) -> list[tuple[str, bool, str]]:
    token = settings.clickup_token
    list_id = settings.clickup_list_id
    workspace_id = settings.clickup_workspace_id
    results: list[tuple[str, bool, str]] = []
    headers = {"Authorization": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        user_response = await client.get("https://api.clickup.com/api/v2/user")
        results.append(("clickup_user", user_response.status_code == 200, f"status={user_response.status_code}"))

        if workspace_id:
            team_response = await client.get(f"https://api.clickup.com/api/v2/team/{workspace_id}")
            results.append(("clickup_team", team_response.status_code == 200, f"status={team_response.status_code}"))

        clickup = ClickUpClient(token)
        try:
            list_info = await clickup.validate_access(list_id)
            results.append(("clickup_list", True, f"id={list_info['id']} name={list_info['name'] or '(unnamed)'}"))
            fields = await clickup.get_list_fields(list_info["id"])
            tasks = await clickup.get_list_tasks(list_info["id"])
            results.append(("clickup_fields", True, f"count={len(fields)}"))
            results.append(("clickup_tasks", True, f"count={len(tasks)}"))
        except Exception as exc:
            results.append(("clickup_list", False, str(exc)))
        finally:
            await clickup.aclose()
    return results


def run_app_smoke() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    with TestClient(main_module.app, base_url="https://testserver") as client:
        health = client.get("/healthz")
        results.append(("healthz", health.status_code == 200, str(health.json())))
        ready = client.get("/readyz")
        results.append(("readyz", ready.status_code == 200, str(ready.json())))
        login = client.post("/login", data={"password": client.app.state.settings.app_shared_secret}, follow_redirects=False)
        results.append(("login", login.status_code == 303, f"status={login.status_code}"))
        startup = client.get("/reports/startup")
        startup_detail = startup.text
        if startup.headers.get("content-type", "").startswith("application/json"):
            startup_detail = str(startup.json())
        results.append(("startup_report", startup.status_code == 200, startup_detail))
    return results


def run_pytest() -> tuple[bool, str]:
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q"], check=False)
    return completed.returncode == 0, f"exit_code={completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live predeploy checks against ClickUp and the local app.")
    parser.add_argument("--env-file", default=".env.railway", help="Environment file to simulate deployment with.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest.")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print_result("env_file", False, f"missing: {env_path}")
        return 1

    env_values = load_env_file(env_path)
    failures = 0

    with apply_env(env_values):
        try:
            settings = load_settings()
            print_result("config", True, f"list_id={settings.clickup_list_id or '(resolved by name)'}")
        except Exception as exc:
            print_result("config", False, str(exc))
            return 1

        for name, ok, detail in asyncio.run(run_clickup_probes(settings)):
            print_result(name, ok, detail)
            failures += 0 if ok else 1

        for name, ok, detail in run_app_smoke():
            print_result(name, ok, detail)
            failures += 0 if ok else 1

        if not args.skip_tests:
            ok, detail = run_pytest()
            print_result("pytest", ok, detail)
            failures += 0 if ok else 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
