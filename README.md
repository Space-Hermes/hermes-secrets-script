# Hermes Secrets: Script Provider

A pluggable secret source for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that executes any local script or CLI command at startup and injects its JSON output as environment variables.

Use it to wire in **Vaultwarden** (via `bw`), **pass**, **1Password CLI**, **Bitwarden CLI**, AWS Secrets Manager, or a custom fetch script — without modifying Hermes core.

## How it Works

When Hermes starts, it loads `.env`, then calls every enabled secret source. The script provider:

1. Runs your configured command
2. Parses stdout as JSON (`{"ENV_VAR": "value", ...}`) or `KEY=VALUE` lines
3. Injects the values into `os.environ`
4. Caches the result in-process for `cache_ttl` seconds

Failures never block Hermes startup. Missing binary, timeout, bad JSON — all emit a one-line warning and continue.

## Installation

```bash
# 1. Copy the module into your Hermes agent directory
cp -r agent/ ~/.hermes/hermes-agent/

# 2. Patch the env_loader dispatch (or load manually)
#    See env_loader.py.patch for the ~30-line change, or
#    add the config section below and use the loader manually
```

## Configuration

Add to `~/.hermes/config.yaml` (or any profile's `config.yaml`):

```yaml
secrets:
  script:
    enabled: true
    command: "/path/to/your/fetch-script.sh"
    parse: json              # "json" or "env" (key=value lines)
    timeout: 30              # seconds per execution attempt
    retry_delays: [5, 10]    # seconds between retries (empty = no retry)
    cache_ttl: 300           # in-process cache TTL in seconds
    override_existing: false # overwrite env vars already set from .env
```

### JSON mode (default)

Your script outputs:

```json
{
  "OPENCODE_GO_API_KEY": "sk-...",
  "ANTHROPIC_API_KEY": "sk-ant-..."
}
```

### ENV mode

Your script outputs `KEY=VALUE` lines (shell-safe quoting supported):

```bash
OPENCODE_GO_API_KEY='sk-...'
ANTHROPIC_API_KEY='sk-ant-...'
```

## Example: Vaultwarden with `bw`

```bash
#!/bin/bash
# fetch-vaultwarden.sh
bw login --apikey --passwordenv BW_PASSWORD > /dev/null 2>&1
BW_SESSION=$(bw unlock --raw --passwordenv BW_PASSWORD)

bw list items --folderid YOUR_FOLDER_ID --session "$BW_SESSION" \
  | python3 -c "
import json, sys
items = json.load(sys.stdin)
secrets = {}
for item in items:
    name = item.get('name', '')
    password = item.get('login', {}).get('password', '')
    if password:
        secrets[name.upper().replace(' ', '_')] = password
json.dump(secrets, sys.stdout)
"

bw logout > /dev/null 2>&1
```

## Example: pass (password-store)

```bash
#!/bin/bash
# fetch-pass.sh
echo '{
  "OPENAI_API_KEY": "'$(pass show api/openai)'",
  "ANTHROPIC_API_KEY": "'$(pass show api/anthropic)'"
}'
```

## Example: 1Password CLI

```bash
#!/bin/bash
# fetch-1password.sh
op read "op://Vault/Hermes OpenCode Go/credential"
```

## Integration with Hermes

The provider module lives at `agent/secret_sources/script.py`. After copying it into your Hermes install, apply the ~30-line patch to `hermes_cli/env_loader.py` that adds the script source dispatch:

```diff
# See env_loader.py.patch for the complete change.
# The patch adds:
# 1. A _try_script_source() function
# 2. A dispatch block in _apply_external_secret_sources()
# 3. Support for multiple secret sources in parallel
```

## Why not contribute this upstream?

The Hermes project [invites new secret backends](https://hermes-agent.nousresearch.com/docs/user-guide/secrets/) — "the lift is one module in `agent/secret_sources/` and one CLI handler." The script provider is a clean generic backend that covers every vault/CLI without Hermes needing to know about each one.

## License

MIT
