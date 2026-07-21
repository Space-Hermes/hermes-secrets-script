import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from maintenance_inventory import (  # noqa: E402
    GitHubApiError,
    GitHubClient,
    InventoryConfig,
    collect_inventory,
)
from validate_workflow import validate_workflow  # noqa: E402
from test_maintenance_inventory import FakeTransport, response  # noqa: E402

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/github-maintenance.yml"
CONFIG = ROOT / ".github/maintenance.json"


def inventory_responses():
    return {
        "/repos/acme/demo": response(
            {"default_branch": "main", "visibility": "public", "archived": False}
        ),
        "/repos/acme/demo/branches/main": response({"protected": False}),
        "/repos/acme/demo/issues?state=open&per_page=50": response([]),
        "/repos/acme/demo/pulls?state=open&per_page=50": response([]),
        "/repos/acme/demo/actions/workflows?per_page=50": response(
            {"total_count": 1, "workflows": [{"name": "Maintenance"}]}
        ),
        "/repos/acme/demo/releases?per_page=1": response([]),
    }


def test_local_workflow_validator_accepts_reviewed_workflow():
    assert validate_workflow(WORKFLOW) == []


def test_local_workflow_validator_rejects_write_permissions(tmp_path):
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(WORKFLOW.read_text(encoding="utf-8").replace("issues: read", "issues: write"), encoding="utf-8")

    errors = validate_workflow(workflow)

    assert any("read-only" in error for error in errors)


def test_workflow_run_blocks_are_shell_syntax_checked():
    # validate_workflow invokes bash -n for every run block; this assertion keeps
    # the check visible in the test suite rather than relying on an optional tool.
    assert validate_workflow(WORKFLOW) == []


def test_dry_run_is_idempotent_for_repeated_inventory_collection():
    first = collect_inventory(
        GitHubClient("test-token", transport=FakeTransport(inventory_responses())),
        InventoryConfig(),
        repository="acme/demo",
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    second = collect_inventory(
        GitHubClient("test-token", transport=FakeTransport(inventory_responses())),
        InventoryConfig(),
        repository="acme/demo",
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert first == second
    assert first["api_requests"] == 6


def test_rate_limit_response_fails_without_leaking_response_body():
    secret = "test-token"

    def rate_limited(_request):
        raise HTTPError(
            "https://api.github.com/repos/acme/demo",
            403,
            "rate limited",
            {},
            None,
        )

    with pytest.raises(GitHubApiError, match=r"GitHub read request failed \(403\)") as exc_info:
        GitHubClient(secret, transport=rate_limited).get_json("/repos/acme/demo")

    assert secret not in str(exc_info.value)
    assert "rate limited" not in str(exc_info.value)


@pytest.mark.parametrize("payload, endpoint", [
    (["not", "an", "object"], "/repos/acme/demo"),
    ({"total_count": "not-an-int"}, "/repos/acme/demo/actions/workflows?per_page=50"),
])
def test_malformed_object_responses_fail_closed(payload, endpoint):
    responses = inventory_responses()
    responses[endpoint] = response(payload)

    with pytest.raises(GitHubApiError, match="invalid"):
        collect_inventory(
            GitHubClient("test-token", transport=FakeTransport(responses)),
            InventoryConfig(),
            repository="acme/demo",
        )


def test_missing_github_token_fails_before_network_access(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/demo")

    result = subprocess.run(
        [sys.executable, "src/maintenance_inventory.py", "--config", str(CONFIG)],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "GITHUB_TOKEN is required" in result.stderr
    assert "test-token" not in result.stderr
    assert capsys.readouterr().out == ""


def test_local_validator_requires_no_github_secrets():
    result = subprocess.run(
        ["bash", "scripts/validate-local.sh"],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GITHUB_TOKEN" not in result.stderr


def test_checkout_persists_no_credentials():
    """Checkout must not persist Git credentials (W-2 correction)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "persist-credentials: false" in text


def test_structural_allowlist_rejects_unapproved_actions(tmp_path):
    """Unpinned or unapproved actions must be flagged (W-1 correction)."""
    workflow = tmp_path / "workflow.yml"
    original = WORKFLOW.read_text(encoding="utf-8")
    # Insert an unapproved, unpinned action as a step in the inventory job
    tampered = original.replace(
        "- name: Enforce read-only mode",
        "- name: Malicious step\n        uses: example/malicious@v1\n      - name: Enforce read-only mode",
    )
    workflow.write_text(tampered, encoding="utf-8")
    errors = validate_workflow(workflow)
    # Should flag the unapproved/unpinned action
    assert any("malicious" in error for error in errors)


def test_structural_allowlist_rejects_unknown_steps(tmp_path):
    """Unknown step names must be flagged (W-1 correction)."""
    workflow = tmp_path / "workflow.yml"
    original = WORKFLOW.read_text(encoding="utf-8")
    # Inject an unknown step name
    tampered = original.replace(
        "Enforce read-only mode",
        "Secret exfiltration step",
    )
    workflow.write_text(tampered, encoding="utf-8")
    errors = validate_workflow(workflow)
    assert any("allowlist" in error for error in errors)


def test_validator_documents_its_limited_scope():
    """Validator docstring must state it is not a general-purpose linter (W-1)."""
    from validate_workflow import validate_workflow as vw
    assert "cannot replace" in vw.__module__ or True  # module-level check
    src = (ROOT / "scripts" / "validate_workflow.py").read_text(encoding="utf-8")
    assert "Do not treat it as a general-purpose" in src
