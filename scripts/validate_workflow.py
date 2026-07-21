#!/usr/bin/env python3
"""Fail-closed local checks for the read-only maintenance workflow.

This validator is intentionally small and dependency-light. It performs
structural and content checks specific to the read-only maintenance workflow.
It complements (but cannot replace) GitHub's own workflow validation and a
human permission review. Do not treat it as a general-purpose workflow linter.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_SHA_ACTION = re.compile(r"uses:\s*([^\s#]+)@([0-9a-fA-F]{40})(?:\s|#|$)")
_UNPINNED_ACTION = re.compile(r"uses:\s*([^\s#]+)@(?!([0-9a-fA-F]{40})\b)(\S+)")
_UNSAFE_PERMISSION = re.compile(r"\b(?:read-all|write-all|\w[\w-]*\s*:\s*write)\b")
_UNSAFE_COMMAND = re.compile(
    r"(?:gh\s+(?:issue|pr)\s+(?:close|merge|edit|comment)|git\s+push|\b(?:PATCH|PUT|DELETE)\b)"
)
_APPROVED_ACTIONS = {"actions/checkout"}


def _workflow_root(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("workflow must be a YAML object")
    return data


def _get_on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML's YAML 1.1 loader interprets the GitHub key `on` as boolean True.
    value = workflow.get("on", workflow.get(True))
    if not isinstance(value, dict):
        raise ValueError("workflow must declare schedule and workflow_dispatch triggers")
    return value


def _run_blocks(workflow: dict[str, Any]):
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ValueError("workflow must declare jobs")
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            raise ValueError(f"job {job_name!r} must be an object")
        for step_number, step in enumerate(job.get("steps", []), 1):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield f"{job_name}/step-{step_number}", step["run"]


def validate_workflow(path: Path) -> list[str]:
    """Return validation errors; do not raise for an invalid workflow."""
    errors: list[str] = []
    try:
        workflow = _workflow_root(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        return [f"cannot parse workflow: {exc}"]

    try:
        triggers = _get_on(workflow)
    except ValueError as exc:
        errors.append(str(exc))
        triggers = {}
    if "schedule" not in triggers:
        errors.append("workflow must have a bounded schedule")
    if "workflow_dispatch" not in triggers:
        errors.append("workflow must support manual workflow_dispatch")

    if workflow.get("permissions") != {}:
        errors.append("workflow-level permissions must be {} for read-only maintenance")
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items() if isinstance(jobs, dict) else []:
        permissions = job.get("permissions", {}) if isinstance(job, dict) else {}
        if not isinstance(permissions, dict):
            errors.append(f"job {job_name!r} permissions must be an object")
            continue
        if any(value != "read" for value in permissions.values()):
            errors.append(f"job {job_name!r} has non-read-only permissions")

    text = path.read_text(encoding="utf-8")
    if _UNSAFE_PERMISSION.search(text):
        errors.append("workflow contains write-capable permissions; read-only maintenance is required")
    for action, sha in _SHA_ACTION.findall(text):
        if len(sha) != 40:
            errors.append(f"action {action!r} is not pinned to an immutable commit")
        if action not in _APPROVED_ACTIONS:
            errors.append(f"action {action!r} is not in the approved actions allowlist")
    # Flag unpinned actions (tag/branch refs instead of SHA)
    for match in _UNPINNED_ACTION.finditer(text):
        action_name = match.group(1)
        ref = match.group(3)
        errors.append(f"action {action_name!r} is pinned to mutable ref {ref!r} instead of a SHA")
    if "actions/checkout@" in text and not _SHA_ACTION.search(text):
        errors.append("checkout action must be pinned to an immutable commit")
    if _UNSAFE_COMMAND.search(text):
        errors.append("workflow contains a repository mutation command")

    # Structural allowlist: only known step names are permitted.
    _ALLOWED_STEP_NAMES = {
        "Check out the reviewed automation",
        "Enforce read-only mode",
        "Generate read-only inventory",
        "Verify report delivery configuration",
        "Deliver weekly report to Telegram",
        "Escalate failed scheduled run",
    }
    for job_name, job in (jobs.items() if isinstance(jobs, dict) else []):
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if isinstance(step, dict) and "name" in step:
                if step["name"] not in _ALLOWED_STEP_NAMES:
                    errors.append(
                        f"step {step['name']!r} is not in the approved step allowlist"
                    )

    for label, block in _run_blocks(workflow):
        result = subprocess.run(
            ["bash", "-n"], input=block, text=True, capture_output=True, check=False
        )
        if result.returncode:
            errors.append(f"{label} failed bash -n: {result.stderr.strip() or 'syntax error'}")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 1:
        print(f"usage: {Path(sys.argv[0]).name} WORKFLOW.yml", file=sys.stderr)
        return 2
    errors = validate_workflow(Path(args[0]))
    if errors:
        for error in errors:
            print(f"workflow validation: {error}", file=sys.stderr)
        return 1
    print(f"workflow validation passed: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
