#!/usr/bin/env bash
# Run local checks without needing GITHUB_TOKEN or any repository credentials.
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

python3 -m py_compile src/maintenance_inventory.py
python3 -m json.tool .github/maintenance.json >/dev/null
python3 scripts/validate_workflow.py .github/workflows/github-maintenance.yml
# The recursion guard is needed because the test suite invokes this script to
# prove that local validation does not need credentials.
if [[ "${MAINTENANCE_VALIDATION_RECURSION_GUARD:-}" != "1" ]]; then
  MAINTENANCE_VALIDATION_RECURSION_GUARD=1 pytest -q
else
  printf '%s\n' 'pytest skipped inside validator self-test'
fi
printf '%s\n' 'local maintenance validation passed (no GitHub credentials used)'
