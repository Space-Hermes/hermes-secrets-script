# Hermes Secrets: Script Provider

A pluggable secret source for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that executes any local script or CLI command at startup and injects its JSON output as environment variables.

Use it to wire in **Vaultwarden** (via `bw`), **pass**, **1Password CLI**, **Bitwarden CLI**, AWS Secrets Manager, or a custom fetch script — without modifying Hermes core.

> **Upstream proposal:** [NousResearch/hermes-agent#57062](https://github.com/NousResearch/hermes-agent/issues/57062)

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

### Quick script

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

### Production-grade setup with resilience

What follows is a complete architecture for a self-hosted Vaultwarden backend that keeps credentials off the filesystem except when Hermes is running, survives reboots gracefully, and never blocks startup.

#### Architecture overview

```
Boot → systemd starts Docker
         │
         ├── After=docker.service
         │
         ├── ExecStartPre: fetch script
         │     ├── Attempt bw login with retry (5s, 10s, 20s backoff)
         │     ├── On success: pull items from Vaultwarden → write auth.json pool
         │     └── On failure:
         │           ├── Check if auth.json already has entries → skip
         │           └── If pool is empty → decrypt local AES backup → populate pool
         │
         └── Hermes gateway starts with populated credential pool
```

#### 1. Vaultwarden credentials file

Store your Vaultwarden machine credentials in a restricted file:

```bash
mkdir -p ~/.vaultwarden
cat > ~/.vaultwarden/credentials << 'EOF'
BW_CLIENTID=user.your-client-id
BW_CLIENTSECRET=your-client-secret
BW_PASSWORD=your-vault-password
EOF
chmod 400 ~/.vaultwarden/credentials
```

#### 2. Fetch script

Create a `fetch-vaultwarden.sh` that authenticates with Vaultwarden, retrieves items from a designated folder, and outputs JSON that Hermes can inject:

```bash
#!/bin/bash
set -e

CREDENTIALS="$HOME/.vaultwarden/credentials"
VW_ENDPOINT="https://vw.your-domain.com"   # your Vaultwarden URL

if [ ! -f "$CREDENTIALS" ]; then
    echo '{}'
    exit 0
fi

source "$CREDENTIALS"
export BW_CLIENTID BW_CLIENTSECRET BW_PASSWORD

bw logout > /dev/null 2>&1 || true

if ! curl -sf -o /dev/null --max-time 5 "$VW_ENDPOINT"; then
    echo '{}'
    exit 0
fi

if ! bw login --apikey --passwordenv BW_PASSWORD > /dev/null 2>&1; then
    echo '{}'
    exit 0
fi

BW_SESSION=$(bw unlock --raw --passwordenv BW_PASSWORD 2>/dev/null) || {
    bw logout > /dev/null 2>&1 || true
    echo '{}'
    exit 0
}
export BW_SESSION
bw sync > /dev/null 2>&1 || true

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

bw logout > /dev/null 2>&1 || true
unset BW_SESSION
```

Make it executable: `chmod 700 fetch-vaultwarden.sh`.

#### 3. Hermes config

Enable the script source:

```yaml
secrets:
  script:
    enabled: true
    command: "/path/to/fetch-vaultwarden.sh"
    parse: json
    timeout: 15
    retry_delays: [5, 10]
    cache_ttl: 300
```

#### 4. Systemd integration (gateway dependency chain)

Run the fetch script as `ExecStartPre` before the Hermes gateway, ensuring Docker is up first:

```
# /etc/systemd/system/hermes-gateway.service.d/docker-dep.conf
[Unit]
After=docker.service
Wants=docker.service

# /etc/systemd/system/hermes-gateway.service.d/secret-fetch.conf
[Service]
ExecStartPre=/path/to/loader.sh
```

Where `loader.sh` wraps the fetch with retry backoff:

```bash
#!/bin/bash
# loader.sh — ExecStartPre entry point
set -e

CREDENTIALS="$HOME/.vaultwarden/credentials"
FETCH_SCRIPT="/path/to/fetch-vaultwarden.sh"
VW_ENDPOINT="https://vw.your-domain.com"
RETRY_DELAYS=(5 10 20)

if [ ! -f "$CREDENTIALS" ]; then
    echo "loader: no credentials at $CREDENTIALS — skipping" >&2
    exit 0
fi

source "$CREDENTIALS"
export BW_CLIENTID BW_CLIENTSECRET BW_PASSWORD

vw_ready() {
    curl -sf -o /dev/null --max-time 5 "$VW_ENDPOINT" 2>/dev/null
}

bw logout > /dev/null 2>&1 || true

attempt=0
max_attempts=$(( ${#RETRY_DELAYS[@]} + 1 ))
login_ok=false

while [ $attempt -lt $max_attempts ]; do
    attempt=$((attempt + 1))
    if ! vw_ready; then
        echo "loader: Vaultwarden not responding (attempt $attempt/$max_attempts)" >&2
    elif bw login --apikey --passwordenv BW_PASSWORD > /dev/null 2>&1; then
        login_ok=true
        break
    else
        echo "loader: bw login failed (attempt $attempt/$max_attempts)" >&2
    fi
    if [ $attempt -lt $max_attempts ]; then
        delay=${RETRY_DELAYS[$(( attempt - 1 ))]}
        echo "loader: retrying in ${delay}s..." >&2
        sleep "$delay"
    fi
done

if [ "$login_ok" = false ]; then
    echo "loader: all attempts exhausted — Hermes will use existing pool" >&2
    exit 0
fi

BW_SESSION=$(bw unlock --raw --passwordenv BW_PASSWORD 2>/dev/null) || {
    echo "loader: bw unlock failed — skipping" >&2
    bw logout > /dev/null 2>&1 || true
    exit 0
}
export BW_SESSION
bw sync > /dev/null 2>&1 || true

python3 "$FETCH_SCRIPT" || {
    echo "loader: fetch script failed — skipping" >&2
    bw logout > /dev/null 2>&1 || true
    exit 0
}

bw logout > /dev/null 2>&1 || true
unset BW_SESSION
```

This retries 4 times over ~35 seconds, handles cold-boot races where Docker/SWAG are still starting, and exits 0 on failure so Hermes still starts with whatever credentials are already in the pool.

#### 5. Encrypted local backup (optional but recommended)

When Vaultwarden is unreachable and the credential pool is empty, a fallback can decrypt a local AES-256 backup:

```bash
# Generate a 256-bit key
openssl rand -hex 32 > ~/.vaultwarden/local.key
chmod 400 ~/.vaultwarden/local.key

# Encrypt the 4 API keys as a JSON blob
# (Run this after a successful Vaultwarden fetch when the pool is populated)
python3 << 'EOF'
import json, subprocess, os
KEYFILE = os.path.expanduser("~/.vaultwarden/local.key")
ENCFILE = os.path.expanduser("~/.hermes/secrets/opencode-go.keys.enc")
os.makedirs(os.path.dirname(ENCFILE), exist_ok=True)

secrets = {
    "version": 1,
    "created_at": __import__("time").time(),
    "keys": {
        "OPENAI_API_KEY": "sk-...",
        "ANTHROPIC_API_KEY": "sk-ant-...",
    }
}

tmp = "/tmp/vault-backup.json"
with open(tmp, "w") as f:
    json.dump(secrets, f)

subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2",
    "-in", tmp, "-out", ENCFILE, "-pass", f"file:{KEYFILE}"], check=True)
os.remove(tmp)
os.chmod(ENCFILE, 0o600)
print(f"Backup written to {ENCFILE}")
EOF
```

In the loader script, add after the failed-login block:

```bash
# Inside the failed-login block, before exit 0:
LOCAL_KEYFILE="$HOME/.vaultwarden/local.key"
LOCAL_ENCFILE="$HOME/.hermes/secrets/opencode-go.keys.enc"

if [ -f "$LOCAL_KEYFILE" ] && [ -f "$LOCAL_ENCFILE" ]; then
    python3 -c "
import json, subprocess, os, uuid, time
KEYFILE = '$LOCAL_KEYFILE'
ENCFILE = '$LOCAL_ENCFILE'
AUTH = os.path.expanduser('~/.hermes/auth.json')

with open(AUTH) as f:
    auth = json.load(f)
if auth.get('credential_pool', {}).get('opencode-go', []):
    exit(0)  # pool already has entries

r = subprocess.run(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2',
    '-in', ENCFILE, '-pass', f'file:{KEYFILE}'], capture_output=True, text=True)
if r.returncode != 0:
    exit(0)

secrets = json.loads(r.stdout)
entries = []
for i, (var, val) in enumerate(sorted(secrets['keys'].items()), 1):
    entries.append({
        'id': uuid.uuid4().hex[:8],
        'auth_type': 'api_key',
        'priority': i,
        'source': f'backup:{var.lower()}',
        'access_token': val,
        'secret_source': 'backup',
    })
auth.setdefault('credential_pool', {})['opencode-go'] = entries
with open(AUTH, 'w') as f:
    json.dump(auth, f, indent=2)
os.chmod(AUTH, 0o600)
print(f'Restored {len(entries)} keys from encrypted backup')
" 2>&1 || true
fi
```

#### 6. Security hardening checklist

| Layer | What to do |
|-------|-----------|
| **Credentials file** | `~/.vaultwarden/credentials` at mode 400, root-owned |
| **Vaultwarden URL** | Use a Tailscale IP or VPN-only domain — never expose directly |
| **Auth pool** | `/root/.hermes/auth.json` at mode 600, root-owned |
| **Backup key** | `local.key` at mode 400, different directory from encrypted blob |
| **Encrypted blob** | `secrets/*.keys.enc` at mode 600 |
| **Env files** | `~/.hermes/.env` at mode 600 — contains only the bootstrap credential |
| **Fetch script** | Mode 700 — no world-readable tokens |
| **Systemd** | `ExecStartPre` scripts run as root; secure the service unit |
| **Dependency** | `After=docker.service` prevents boot races |
| **Retry** | Always exit 0 on failure — never block Hermes startup |

#### 7. Key rotation

To rotate a credential:

1. Update the item in Vaultwarden
2. Run the fetch script manually to refresh `auth.json`:
   ```bash
   ./loader.sh
   ```
3. Restart the Hermes gateway:
   ```bash
   systemctl restart hermes-gateway
   ```
4. (Optional) Re-encrypt the local backup with the new keys

The credential pool handles mid-session rotation automatically — new keys loaded into `auth.json` are picked up on the next `load_pool()` call without a restart.

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
