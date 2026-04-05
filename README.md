# Zephyr Test Server

Host-side testing orchestrator for Zephyr binaries using Docker.

## What this does

- Runs Zephyr binaries through an HTTP API on port 8080.
- Supports two execution styles:
  - `ephemeral`: run and return logs + exit code
  - `interactive`: run detached and stop later
- Supports:
  - `native_sim` binaries (run directly inside container)
  - QEMU aarch64 presets (`qemu_cortex_a53`, `qemu_kvm_arm64`)
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
uv pip install docker
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
