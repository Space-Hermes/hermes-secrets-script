import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from maintenance_inventory import (  # noqa: E402
    GitHubClient,
    InventoryConfig,
    collect_inventory,
    load_config,
    render_markdown,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []
        self.methods = []

    def __call__(self, request):
        path = request.full_url.split("api.github.com", 1)[1]
        self.paths.append(path)
        self.methods.append(request.method)
        return self.responses[path]


def response(payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return Response()


def test_inventory_is_read_only_and_identifies_stale_issue_candidates():
    transport = FakeTransport(
        {
            "/repos/acme/demo": response(
                {"default_branch": "main", "visibility": "public", "archived": False}
            ),
            "/repos/acme/demo/branches/main": response({"protected": False}),
            "/repos/acme/demo/issues?state=open&per_page=50": response(
                [
                    {
                        "number": 7,
                        "title": "Old issue",
                        "updated_at": "2026-06-01T00:00:00Z",
                        "pull_request": None,
                    },
                    {
                        "number": 8,
                        "title": "Recent issue",
                        "updated_at": "2026-07-20T00:00:00Z",
                        "pull_request": None,
                    },
                ]
            ),
            "/repos/acme/demo/pulls?state=open&per_page=50": response(
                [{"number": 9, "title": "A PR", "updated_at": "2026-07-19T00:00:00Z"}]
            ),
            "/repos/acme/demo/actions/workflows?per_page=50": response(
                {"total_count": 2, "workflows": [{"name": "CI"}, {"name": "Maintenance"}]}
            ),
            "/repos/acme/demo/releases?per_page=1": response([]),
        }
    )
    client = GitHubClient("secret-token", transport=transport)
    config = InventoryConfig(max_items=50, stale_after_days=30)

    inventory = collect_inventory(
        client,
        config,
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert inventory["repository"]["default_branch"] == "main"
    assert inventory["open_issues"] == 2
    assert inventory["open_pull_requests"] == 1
    assert inventory["stale_issue_candidates"] == [{"number": 7, "title": "Old issue"}]
    assert inventory["branch"]["protected"] is False
    assert inventory["workflow_count"] == 2
    assert transport.methods == ["GET"] * 6
    assert all("POST" not in path and "PATCH" not in path for path in transport.paths)
    assert all("secret-token" not in path for path in transport.paths)


def test_markdown_report_is_advisory_and_never_contains_token():
    report = render_markdown(
        {
            "repository": {"name": "acme/demo", "default_branch": "main", "visibility": "public"},
            "branch": {"protected": False},
            "open_issues": 0,
            "open_pull_requests": 0,
            "workflow_count": 1,
            "release_count": 0,
            "api_requests": 0,
            "stale_issue_candidates": [{"number": 1, "title": "secret-token"}],
        },
        token="secret-token",
    )

    assert "READ-ONLY ADVISORY" in report
    assert "No changes were made" in report
    assert "secret-token" not in report
    assert "[REDACTED]" in report
    assert "stale" in report.lower()


def test_config_requires_dry_run_and_rejects_unsupported_values(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text(
        json.dumps({"dry_run": False, "max_items": 50, "stale_after_days": 30}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dry_run"):
        load_config(config_path)


def test_config_defaults_are_bounded(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")

    config = load_config(config_path)

    assert config.dry_run is True
    assert 1 <= config.max_items <= 100
    assert 1 <= config.stale_after_days <= 3650


def test_workflow_is_scheduled_manual_and_read_only():
    workflow_path = Path(__file__).parents[1] / ".github/workflows/github-maintenance.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))

    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True
    assert dry_run["required"] is True
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["inventory"]["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
        "actions": "read",
    }
    text = workflow_path.read_text(encoding="utf-8")
    assert "actions/checkout@" in text
    assert "inputs.dry_run" in text
    assert "inputs.dry_run ||" not in text
    assert "Only dry-run mode is supported" in text
    assert "issues: write" not in text
    assert "pull-requests: write" not in text
    assert "contents: write" not in text
    assert "DELETE" not in text
    assert "secrets.GITHUB_TOKEN" in text


def test_scheduled_report_delivery_is_telegram_secret_gated():
    workflow_path = Path(__file__).parents[1] / ".github/workflows/github-maintenance.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["inventory"]["steps"]
    telegram_steps = [step for step in steps if "Telegram" in step.get("name", "")]

    assert len(telegram_steps) == 1
    telegram = telegram_steps[0]
    assert "github.event_name == 'schedule'" in telegram["if"]
    assert "workflow_dispatch" not in telegram["if"]
    run = telegram["run"]
    assert "secrets.TELEGRAM_BOT_TOKEN" in str(telegram["env"])
    assert "secrets.TELEGRAM_CHAT_ID" in str(telegram["env"])
    assert "sendDocument" in run
    assert "--fail" in run
    assert "add-mask" in run
    assert "set -euo pipefail" in run

    failure_steps = [step for step in steps if step.get("name") == "Escalate failed scheduled run"]
    assert len(failure_steps) == 1
    assert "failure()" in failure_steps[0]["if"]
    assert "github.event_name == 'schedule'" in failure_steps[0]["if"]
    assert "sendMessage" in failure_steps[0]["run"]


def test_readme_documents_telegram_setup_and_disable_path():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "TELEGRAM_BOT_TOKEN" in readme
    assert "TELEGRAM_CHAT_ID" in readme
    assert "Disable / rollback" in readme
    assert "weekly report" in readme.lower()


@pytest.mark.parametrize("hostile_title", [
    "![external image](https://example.invalid/tracker.png)",
    "[link text](https://example.invalid/phish)",
    "<script>alert(1)</script>",
    "# Heading injection",
    "**bold** and _italic_ and `code`",
    "back\\slash and [brackets] and (parens)",
])
def test_clean_title_escapes_hostile_markdown(hostile_title):
    """Untrusted titles must not invoke Markdown rendering (W-3 correction)."""
    from maintenance_inventory import _clean_title
    cleaned = _clean_title(hostile_title)
    # After escaping, no raw Markdown syntax should survive unescaped.
    # Check that the specific dangerous patterns are absent or escaped.
    assert "![external" not in cleaned  # image injection broken
    assert "<script>" not in cleaned     # HTML injection broken
    assert cleaned.startswith("\\#") or "Heading" not in cleaned  # heading escaped
    assert "\\*\\*" in cleaned or "bold" not in cleaned           # emphasis escaped
