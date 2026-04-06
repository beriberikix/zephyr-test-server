# Zephyr Test Server

Host-side testing orchestrator for Zephyr binaries using Docker.

## What this does

- Runs Zephyr binaries through an HTTP API on port 8080.
- Supports two execution styles:
  - `ephemeral`: run and return logs + exit code
  - `interactive`: run with live WebSocket I/O and stop later
- Supports:
  - `native_sim` binaries (run directly inside container)
  - QEMU aarch64 presets (`qemu_cortex_a53`, `qemu_kvm_arm64`)
- For interactive native_sim shell apps, use Advanced native_sim options:
  - `Map UART to stdin/stdout` to pass `-<stem>_stdinout` at runtime.
  - `stdinout option stem` defaults to `uart` (some builds may use other stems).
  - Default behavior for `mode=interactive` + `target_type=native_sim` enables this mapping unless overridden in `structured_options`.
- Serves a single-file web UI from `index.html`.

## Project files

- `Dockerfile`
- `zephyr_test_server.py`
- `index.html`

## Prerequisites

- Linux host with Docker installed and running
- Python 3.10+
- `uv` installed
- Optional for acceleration: `/dev/kvm` available

## 1) Create Python environment

```bash
cd /home/jonathan.beri@canonical.com/code/zephyr-test-server
uv venv .venv
source .venv/bin/activate
uv pip install docker simple-websocket
```

## 2) Build the runtime image

```bash
docker build -t zephyr-runner .
```

## 3) Start the server

```bash
source .venv/bin/activate
python zephyr_test_server.py
```

Server URL:

- `http://localhost:8080`

## 4) Use the web UI

Open in browser:

- `http://localhost:8080`

Then fill:

- `Target Type`: `native_sim` or `qemu`
- `QEMU Board Preset` (when qemu): `qemu_cortex_a53` or `qemu_kvm_arm64`
- `Binary Path`: absolute path on host
- `Mode`: `ephemeral` or `interactive`

## 5) Validate with API directly (curl)

### A) Health check (serves UI)

```bash
curl -i http://localhost:8080/
```

### B) Validation error check (expected 400)

```bash
curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"binary_path":"/does/not/exist","target_type":"native_sim"}' | jq
```

### C) native_sim ephemeral run

Use a real path to your `zephyr.exe`:

```bash
curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/build/zephyr/zephyr.exe",
    "target_type": "native_sim",
    "mode": "ephemeral",
    "structured_options": {
      "real_time": true,
      "stop_at": 10,
      "no_color": true
    },
    "extra_args": ""
  }' | jq
```

Expected JSON fields:

- `status`
- `exit_code`
- `output`

### D) QEMU aarch64 ephemeral run (cortex_a53)

Use a real path to your Zephyr ELF:

```bash
curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/build/zephyr/zephyr.elf",
    "target_type": "qemu",
    "board_preset": "qemu_cortex_a53",
    "mode": "ephemeral",
    "structured_options": {
      "gdb_debug": false,
      "deterministic": true,
      "memory_mb": 256
    },
    "extra_args": ""
  }' | jq
```

### E) Interactive run + stop

Start interactive:

```bash
RESP=$(curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/build/zephyr/zephyr.elf",
    "target_type": "qemu",
    "board_preset": "qemu_cortex_a53",
    "mode": "interactive",
    "structured_options": {},
    "extra_args": ""
  }')

echo "$RESP" | jq
CID=$(echo "$RESP" | jq -r '.container_id')
```

Stop interactive container:

```bash
curl -sS -X POST http://localhost:8080/stop \
  -H 'Content-Type: application/json' \
  -d "{\"container_id\":\"$CID\"}" | jq
```

### F) Networking: expose ports to localhost

Run a TCP echo server with port 4242 published to 127.0.0.1:

```bash
RESP=$(curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/tcp_echo_server/zephyr.exe",
    "target_type": "native_sim",
    "mode": "interactive",
    "network": {
      "expose": [{"port": 4242, "protocol": "tcp"}]
    }
  }')

echo "$RESP" | jq
# allocated_ports shows {"4242/tcp": <host_port>}
HOST_PORT=$(echo "$RESP" | jq -r '.allocated_ports["4242/tcp"]')
echo "hello" | nc 127.0.0.1 "$HOST_PORT"
```

### G) Networking: shared group (inter-session)

Start a server in a named group:

```bash
RESP_A=$(curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/tcp_echo_server/zephyr.exe",
    "target_type": "native_sim",
    "mode": "interactive",
    "network": {
      "group": "my-group",
      "hostname": "server"
    }
  }')
echo "$RESP_A" | jq
```

Start a client in the same group (it can reach `server` by hostname):

```bash
RESP_B=$(curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/tcp_echo_client/zephyr.exe",
    "target_type": "native_sim",
    "mode": "interactive",
    "network": {
      "group": "my-group",
      "hostname": "client"
    },
    "structured_options": {
      "testargs": "server 4242"
    }
  }')
echo "$RESP_B" | jq
```

### H) Networking: combined expose + group

A session can have both — ports published to localhost AND inter-session connectivity:

```bash
curl -sS -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{
    "binary_path": "/absolute/path/to/tcp_echo_server/zephyr.exe",
    "target_type": "native_sim",
    "mode": "interactive",
    "network": {
      "expose": [{"port": 4242, "protocol": "tcp"}],
      "group": "my-group",
      "hostname": "server"
    }
  }' | jq
```

## Network configuration reference

The top-level `network` field in `/run` controls container networking. By default (omitted or `{}`), containers have **no network access**.

| Field | Type | Description |
|-------|------|-------------|
| `expose` | `[{port, protocol}]` | Publish ports to `127.0.0.1`. Max 10 entries. |
| `group` | `string` | Join a named internal Docker bridge. Requires `mode=interactive`. |
| `hostname` | `string` | Container hostname within the group (for DNS). Requires `group`. |

**Behavior matrix:**

| `expose` | `group` | Result |
|----------|---------|--------|
| omitted | omitted | Fully isolated (`network_mode=none`) |
| set | omitted | Per-session bridge + ports on 127.0.0.1 |
| omitted | set | Internal shared bridge, no host ports |
| set | set | Internal shared bridge + ports on 127.0.0.1 |

**Constraints:**
- Port numbers: 1–65535, protocol: `tcp` or `udp`
- Group: alphanumeric + hyphen, 1–64 chars
- Hostname: alphanumeric + hyphen, 1–64 chars, requires group
- Max 20 active networks across all sessions (503 if exceeded)
- Stale networks from prior server crashes are cleaned on startup

## 6) Run E2E test suite

The repository includes a comprehensive end-to-end test suite (`test_e2e.py`) that validates all API endpoints, structured options, and lifecycle behaviors against 16 pre-built Zephyr test binaries.

### Prerequisites for tests

1. Server running on `http://localhost:8080`:

```bash
python zephyr_test_server.py
```

2. Zephyr test binaries available at `../zephyr-sim-tests/builds/` (or set `BUILDS_ROOT` env var):

```bash
# Clone if not already present
cd ..
git clone https://github.com/beriberikix/zephyr-sim-tests.git
cd zephyr-test-server
```

### Run tests

```bash
python test_e2e.py
```

To use custom paths:

```bash
SERVER_URL=http://localhost:8080 \
BUILDS_ROOT=/path/to/zephyr-sim-tests/builds \
python test_e2e.py
```

### Test Coverage

The test suite (`test_e2e.py`) includes 40+ test methods across 9 test classes:

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestServeIndex` | 1 | GET / endpoint, HTML serving |
| `TestValidation` | 7 | Error cases: missing path, relative paths, invalid modes, nonexistent containers |
| `TestNativeSimEphemeral` | 4 | hello_world, exit_codes, ztest_pass, ztest_fail |
| `TestNativeSimOptions` | 6 | stop_at, seed, rtc_reset, testargs, network_none_default, network_none_explicit |
| `TestLifecycle` | 6 | Timeout+kill, interactive ws_path, stdinout mapping, stop/kill, partial output |
| `TestQemuEphemeral` | 3 | qemu_cortex_a53, SMP, GDB debug (skipped if unavailable) |
| `TestNetworkValidation` | 7 | Invalid port range, invalid protocol, invalid group, group requires interactive, too many ports, hostname requires group |
| `TestNetworkExpose` | 1 | TCP echo via exposed port on 127.0.0.1 |
| `TestNetworkGroup` | 3 | Group-only inter-session, combined expose+group, cross-group isolation |

```

## Validation checklist

- Python compiles:

```bash
/home/jonathan.beri@canonical.com/code/zephyr-test-server/.venv/bin/python -m py_compile zephyr_test_server.py
```

- Docker image exists:

```bash
docker image ls | grep zephyr-runner
```

- Server responds:

```bash
curl -sS http://localhost:8080/ > /dev/null && echo OK
```

- UI behavior:
  - Theme toggle works
  - Submitting ephemeral run updates console output
  - Interactive run appears in Active Sessions
  - Stop button removes session row

## Troubleshooting

- `Import "docker" could not be resolved` in editor:
  - Activate `.venv` and run `uv pip install docker`.
- `docker error: ... image not found`:
  - Run `docker build -t zephyr-runner .`.
- QEMU KVM preset is slow or fails:
  - Use `qemu_cortex_a53` preset if `/dev/kvm` is unavailable.
- Permission denied on Docker socket:
  - Ensure your user can access Docker daemon (or run with proper permissions).
