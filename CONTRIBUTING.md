# Contributing

Thanks for your interest in contributing to the ClickUp Execution Engine Scheduler.

## Prerequisites

- Python 3.12+
- A ClickUp account with a paid plan that supports Custom Fields
- A ClickUp API token ([generate one here](https://app.clickup.com/settings/apps))

## Local setup

```bash
git clone https://github.com/<your-fork>/clickup-engine.git
cd clickup-engine
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

At minimum, set:

- `CLICKUP_API_TOKEN`
- `CLICKUP_LIST_ID` or (`CLICKUP_LIST_NAME` + `CLICKUP_WORKSPACE_ID`)
- `PUBLIC_BASE_URL`
- `APP_SHARED_SECRET`
- `SESSION_SECRET`

## Running locally

```bash
uvicorn app.main:app --reload
```

## Running tests

Unit / smoke tests (no ClickUp account needed):

```bash
pytest tests/test_app_smoke.py -v
```

End-to-end browser tests (requires Playwright browsers):

```bash
pip install playwright
playwright install chromium
pytest tests/test_checkin_playwright.py -v
```

## Pre-deploy validation

Before deploying, run the predeploy check against a live ClickUp account:

```bash
python scripts/predeploy_check.py --env-file .env
```

This verifies token access, workspace, list, fields, and app startup.

## Code style

- Match existing patterns and conventions.
- Prefer editing existing abstractions over introducing new frameworks.
- Keep changes minimal and focused on the issue at hand.
- Do not add dependencies unless clearly necessary.

## Submitting changes

1. Fork the repo and create a feature branch.
2. Make your changes.
3. Run the smoke test suite and confirm it passes.
4. Open a pull request with a clear description of the change.

For any public API change, update tests and documentation in the same PR.

## Reporting issues

Open a GitHub issue with:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Your Python version and deployment platform

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
