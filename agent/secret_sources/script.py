"""
External script/exec-based secret source.

Executes a user-configured command and parses its stdout as JSON key-value
pairs (``{ "ENV_VAR": "value", ... }``) that get injected into ``os.environ``
during Hermes startup.

This is a universal glue layer: you wire in *any* local credential source
(``bw`` to Vaultwarden, ``pass``, a custom fetch script, etc.) without
Hermes needing to know about your specific vault backend.

Config
------
Add to ``~/.hermes/config.yaml`` or a profile's ``config.yaml``:

.. code-block:: yaml

   secrets:
     script:
       enabled: true
       command: "/path/to/your/fetch-script.sh"
       parse: json              # ``json`` or ``env`` (key=value lines)
       timeout: 30              # seconds per execution attempt
       retry_delays: [5, 10]    # seconds between retries (empty = no retry)
       cache_ttl: 300           # in-process cache TTL in seconds
       override_existing: false # overwrite env vars already set from .env

The ``command`` must output JSON like:

.. code-block:: json

   {
     "OPENCODE_GO_API_KEY": "sk-...",
     "ANTHROPIC_API_KEY": "sk-ant-..."
   }

In ``env`` mode, each line is ``KEY=VALUE`` (shell-safe quoting supported).

Failures NEVER block Hermes startup — missing binary, timeout, bad output,
etc. all emit a one-line warning and continue with whatever credentials
``.env`` already had.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process cache so back-to-back load_hermes_dotenv() calls don't
# re-execute the script on every call.
# ---------------------------------------------------------------------------
_CacheKey = Tuple[str, float]  # (command, config_fingerprint)
_CACHE: Dict[_CacheKey, "_CachedFetch"] = {}


@dataclass
class _CachedFetch:
    secrets: Dict[str, str]
    fetched_at: float


@dataclass
class FetchResult:
    """Returned by :func:`apply_script_secrets`."""

    applied: List[str] = field(default_factory=list)
    """Env var names that were actually set by this source."""

    error: Optional[str] = None
    """Human-readable error message, or None on success."""

    warnings: List[str] = field(default_factory=list)
    """Non-fatal warnings (stale cache, partial output, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cache_key(command: str, parse: str, timeout: float) -> _CacheKey:
    return (command, hash((command, parse, timeout)))


def _read_from_cache(
    cache_key: _CacheKey,
    ttl_seconds: float,
) -> Optional[Dict[str, str]]:
    cached = _CACHE.get(cache_key)
    if cached is None:
        return None
    if time.time() - cached.fetched_at > ttl_seconds:
        return None
    return cached.secrets


def _write_to_cache(cache_key: _CacheKey, secrets: Dict[str, str]) -> None:
    _CACHE[cache_key] = _CachedFetch(
        secrets=dict(secrets),
        fetched_at=time.time(),
    )


def _parse_json_output(stdout: str) -> Dict[str, str]:
    """Parse JSON map of env var -> value."""
    raw = json.loads(stdout)
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object, got {type(raw).__name__}")
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if value is None:
            continue
        result[key.strip()] = str(value)
    return result


def _parse_env_output(stdout: str) -> Dict[str, str]:
    """Parse shell-style ``KEY=VALUE`` lines."""
    result = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, raw_val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        # Strip surrounding quotes (single or double)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        result[key] = val
    return result


def _execute_command(
    command: str,
    timeout: float,
    retry_delays: List[float],
    home_path: Optional[Path] = None,
) -> Tuple[int, str, str]:
    """Run the command, returning (exit_code, stdout, stderr)."""
    args = shlex.split(command)
    if not args:
        return -1, "", "empty command"

    env = os.environ.copy()
    if home_path is not None:
        env["HERMES_HOME"] = str(home_path.resolve())

    delays = list(retry_delays) if retry_delays else []
    attempts = 1 + len(delays)

    for attempt in range(1, attempts + 1):
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if r.returncode == 0:
                return 0, r.stdout, r.stderr

            # Non-zero exit — retry if we have delays left
            if attempt < attempts:
                delay = delays[attempt - 1]
                logger.debug(
                    "secrets.script: attempt %d/%d exit %d — "
                    "retrying in %.0fs",
                    attempt,
                    attempts,
                    r.returncode,
                    delay,
                )
                time.sleep(delay)
                continue

            return r.returncode, r.stdout, r.stderr

        except subprocess.TimeoutExpired:
            if attempt < attempts:
                delay = delays[attempt - 1]
                logger.debug(
                    "secrets.script: attempt %d/%d timed out (%.0fs) — "
                    "retrying in %.0fs",
                    attempt,
                    attempts,
                    timeout,
                    delay,
                )
                time.sleep(delay)
                continue
            return -1, "", f"timed out after {timeout}s"

        except FileNotFoundError:
            return -2, "", f"command not found: {args[0]}"

    return -1, "", "all attempts exhausted"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_script_secrets(
    *,
    enabled: bool,
    command: str = "",
    parse: str = "json",
    timeout: float = 30,
    retry_delays: Optional[List[float]] = None,
    override_existing: bool = False,
    cache_ttl_seconds: float = 300,
    home_path: Optional[Path] = None,
) -> FetchResult:
    """Execute a user-configured script and inject its output into ``os.environ``.

    Called by ``load_hermes_dotenv()`` after ``.env`` files have loaded.
    Defensive — any failure returns a :class:`FetchResult` with ``error`` set;
    never raises.

    Parameters mirror the ``secrets.script.*`` config keys.
    """
    result = FetchResult()

    if not enabled:
        return result

    command = command.strip()
    if not command:
        result.error = "secrets.script.command is empty — nothing to execute"
        return result

    # Check cache first
    cache_key = _build_cache_key(command, parse, timeout)
    cached = _read_from_cache(cache_key, cache_ttl_seconds)
    if cached is not None:
        # Apply cached secrets
        for name, value in cached.items():
            if not override_existing and name in os.environ:
                continue
            os.environ[name] = value
            result.applied.append(name)
        result.warnings.append("served from cache")
        return result

    # Run the command
    exit_code, stdout, stderr = _execute_command(
        command=command,
        timeout=timeout,
        retry_delays=retry_delays or [],
        home_path=home_path,
    )

    # Handle execution errors
    if exit_code != 0:
        summary = (stderr or stdout or "").strip()[:200]
        result.error = (
            f"secrets.script: command exited {exit_code} — {summary}"
        )
        logger.warning("secrets.script: %s", result.error)
        return result

    if not stdout.strip():
        result.error = "secrets.script: command produced no output"
        return result

    # Parse output
    try:
        if parse == "env":
            secrets = _parse_env_output(stdout)
        else:
            secrets = _parse_json_output(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        result.error = (
            f"secrets.script: failed to parse output as {parse}: {exc}"
        )
        logger.warning("secrets.script: %s", result.error)
        return result

    if not secrets:
        result.error = "secrets.script: parsed no env vars from output"
        return result

    # Apply to environment
    for name, value in secrets.items():
        if not override_existing and name in os.environ:
            continue
        os.environ[name] = value
        result.applied.append(name)

    # Cache
    _write_to_cache(cache_key, secrets)

    return result
