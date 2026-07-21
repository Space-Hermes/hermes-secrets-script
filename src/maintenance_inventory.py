#!/usr/bin/env python3
"""Read-only GitHub repository inventory for scheduled maintenance reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
_ALLOWED_CONFIG_KEYS = {"dry_run", "max_items", "stale_after_days"}


class GitHubApiError(RuntimeError):
    """An API request failed without exposing credentials in the message."""


@dataclass(frozen=True)
class InventoryConfig:
    dry_run: bool = True
    max_items: int = 50
    stale_after_days: int = 30


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Callable[[Request], Any] = urlopen,
        api_root: str = API_ROOT,
    ) -> None:
        self._token = token
        self._transport = transport
        self._api_root = api_root.rstrip("/")
        self.request_count = 0

    def get_json(self, path: str) -> Any:
        if not path.startswith("/repos/") or any(char in path for char in "\r\n"):
            raise ValueError("API paths must be absolute repository paths")
        request = Request(
            f"{self._api_root}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-maintenance-inventory/1.0",
            },
            method="GET",
        )
        self.request_count += 1
        try:
            with self._transport(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, ValueError, json.JSONDecodeError) as exc:
            # Do not include response bodies or request headers: either may contain
            # credentials or repository data that should not enter Actions logs.
            status = getattr(exc, "code", "network")
            raise GitHubApiError(f"GitHub read request failed ({status})") from exc


def load_config(path: Path) -> InventoryConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON configuration: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")
    unknown = set(raw) - _ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unsupported configuration keys: {', '.join(sorted(unknown))}")

    dry_run = raw.get("dry_run", True)
    if dry_run is not True:
        raise ValueError("dry_run must remain true; this implementation has no write mode")
    max_items = raw.get("max_items", 50)
    stale_after_days = raw.get("stale_after_days", 30)
    if type(max_items) is not int or not 1 <= max_items <= 100:
        raise ValueError("max_items must be an integer between 1 and 100")
    if type(stale_after_days) is not int or not 1 <= stale_after_days <= 3650:
        raise ValueError("stale_after_days must be an integer between 1 and 3650")
    return InventoryConfig(True, max_items, stale_after_days)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_issue_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GitHubApiError("GitHub returned an invalid issue timestamp")
    try:
        return _parse_timestamp(value)
    except ValueError as exc:
        raise GitHubApiError("GitHub returned an invalid issue timestamp") from exc


def _clean_title(value: Any) -> str:
    # Escape untrusted issue text for safe Markdown rendering and cap report size.
    # This prevents injection of images, links, headings, and emphasis from
    # malicious issue titles (W-3 correction from security review).
    text = " ".join(str(value or "(untitled)").split())
    # Escape backslash first, then all other Markdown-significant characters.
    # Note: '-' is NOT escaped because it is only special at line start (lists),
    # and escaping it would break token redaction in report rendering.
    for ch in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", ".", "!", "|", "<", ">"):
        text = text.replace(ch, f"\\{ch}")
    return text[:200]


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubApiError(f"GitHub returned an invalid {name} response")
    return value


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubApiError(f"GitHub returned an invalid {name} response")
    return value


def _require_object_list(value: Any, name: str) -> list[dict[str, Any]]:
    items = _require_list(value, name)
    if not all(isinstance(item, dict) for item in items):
        raise GitHubApiError(f"GitHub returned an invalid {name} response")
    return items


def _workflow_count(value: Any) -> int:
    workflows = _require_object(value, "workflows")
    _require_object_list(workflows.get("workflows", []), "workflows")
    total_count = workflows.get("total_count")
    if total_count is None:
        return len(workflows["workflows"])
    if type(total_count) is not int or total_count < 0:
        raise GitHubApiError("GitHub returned an invalid workflows response")
    return total_count


def collect_inventory(
    client: GitHubClient,
    config: InventoryConfig,
    *,
    repository: str = "acme/demo",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not config.dry_run:
        raise ValueError("inventory collection only supports dry_run=true")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPOSITORY")
    repo = _require_object(client.get_json(f"/repos/{repository}"), "repository")
    default_branch = repo.get("default_branch", "main")
    if not isinstance(default_branch, str) or not default_branch:
        raise GitHubApiError("GitHub returned an invalid repository response")
    issues_payload = client.get_json(
        f"/repos/{repository}/issues?state=open&per_page={config.max_items}"
    )
    pulls_payload = client.get_json(
        f"/repos/{repository}/pulls?state=open&per_page={config.max_items}"
    )
    workflows_payload = client.get_json(
        f"/repos/{repository}/actions/workflows?per_page={config.max_items}"
    )
    branch_payload = _require_object(
        client.get_json(
            f"/repos/{repository}/branches/{quote(default_branch, safe='')}"
        ),
        "branch",
    )
    releases_payload = client.get_json(f"/repos/{repository}/releases?per_page=1")

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.stale_after_days)
    issues = [item for item in _require_object_list(issues_payload, "issues") if not item.get("pull_request")]
    pulls = _require_object_list(pulls_payload, "pull requests")
    releases = _require_object_list(releases_payload, "releases")
    stale = [
        {"number": item.get("number"), "title": _clean_title(item.get("title"))}
        for item in issues
        if item.get("updated_at") and _parse_issue_timestamp(item["updated_at"]) < cutoff
    ]
    return {
        "repository": {
            "name": repository,
            "default_branch": default_branch,
            "visibility": repo.get("visibility", "unknown"),
            "archived": bool(repo.get("archived", False)),
        },
        "branch": {"protected": bool(branch_payload.get("protected", False))},
        "open_issues": len(issues),
        "open_pull_requests": len(pulls),
        "workflow_count": _workflow_count(workflows_payload),
        "release_count": len(releases),
        "stale_issue_candidates": stale,
        "api_requests": client.request_count,
    }


def render_markdown(inventory: dict[str, Any], *, token: str = "") -> str:
    repo = inventory["repository"]
    lines = [
        "# GitHub maintenance report",
        "",
        "**READ-ONLY ADVISORY** — No changes were made to the repository.",
        "",
        f"- Repository: `{repo['name']}`",
        f"- Visibility: `{repo['visibility']}`",
        f"- Default branch: `{repo['default_branch']}`",
        f"- Default branch protected: `{repo['branch_protected'] if 'branch_protected' in repo else inventory['branch']['protected']}`",
        f"- Open issues returned (bounded): `{inventory['open_issues']}`",
        f"- Open pull requests returned (bounded): `{inventory['open_pull_requests']}`",
        f"- Workflows: `{inventory['workflow_count']}`",
        f"- Releases returned by bounded check: `{inventory['release_count']}`",
        f"- API requests used: `{inventory.get('api_requests', 'unknown')}`",
        "",
        "## Stale issue candidates",
        "",
    ]
    stale = inventory["stale_issue_candidates"]
    if stale:
        lines.extend(f"- #{item['number']}: {_clean_title(item['title'])}" for item in stale)
        lines.extend(["", "These are suggestions only. No labels, comments, or closures are performed."])
    else:
        lines.append("None found by the configured age threshold.")
    lines.extend(
        [
            "",
            "## Human approval gate",
            "",
            "Any future action that comments, labels, closes, merges, deletes, publishes, changes settings, or uses secrets requires a separate reviewed change and explicit approval.",
        ]
    )
    output = "\n".join(lines) + "\n"
    return output.replace(token, "[REDACTED]") if token else output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.repository or "/" not in args.repository:
        parser.error("--repository or GITHUB_REPOSITORY must be OWNER/REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        parser.error("GITHUB_TOKEN is required")
    try:
        config = load_config(args.config)
        client = GitHubClient(token)
        inventory = collect_inventory(client, config, repository=args.repository)
        report = render_markdown(inventory, token=token)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        sys.stdout.write(report)
    except (OSError, ValueError, GitHubApiError) as exc:
        print(f"maintenance inventory failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
