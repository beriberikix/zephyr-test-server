#!/usr/bin/env python3
"""Zephyr host-side test orchestrator.

Environment setup (dependency-light):
1) uv venv .venv
2) source .venv/bin/activate
3) uv pip install docker
4) python zephyr_server.py
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound

HOST = "0.0.0.0"
PORT = 8080
IMAGE_NAME = "zephyr-runner"

NATIVE_SIM_OPTIONS: dict[str, dict[str, str]] = {
    "real_time": {"type": "bool", "flag": "-rt"},
    "stop_at": {"type": "value", "flag": "-stop_at"},
    "no_color": {"type": "bool", "flag": "-no-color"},
    "force_color": {"type": "bool", "flag": "-force-color"},
    "rt_ratio": {"type": "value", "flag": "-rt-ratio"},
    "rtc_reset": {"type": "bool", "flag": "-rtc-reset"},
    "rtc_offset": {"type": "value", "flag": "-rtc-offset"},
    "seed": {"type": "value", "flag": "-seed"},
    "flash": {"type": "value", "flag": "-flash"},
    "eeprom": {"type": "value", "flag": "-eeprom"},
    "bt_dev": {"type": "value", "flag": "-bt-dev"},
    "testargs": {"type": "value", "flag": "-testargs"},
}

QEMU_PRESETS: dict[str, dict[str, Any]] = {
    "qemu_cortex_a53": {
        "executable": "qemu-system-aarch64",
        "base_args": [
            "-global",
            "virtio-mmio.force-legacy=false",
            "-cpu",
            "cortex-a53",
            "-machine",
            "virt,gic-version=3",
            "-nographic",
            "-chardev",
            "stdio,id=con,mux=on",
            "-serial",
            "chardev:con",
            "-mon",
            "chardev=con,mode=readline",
        ],
        "kernel_arg": ["-kernel", "{binary}"],
        "bios_arg": ["-bios", "{binary}"],
        "machine_base": "virt,gic-version=3",
    },
    "qemu_kvm_arm64": {
        "executable": "qemu-system-aarch64",
        "base_args": [
            "-cpu",
            "host",
            "-machine",
            "virt,gic-version=3,accel=kvm",
            "-nographic",
            "-chardev",
            "stdio,id=con,mux=on",
            "-serial",
            "chardev:con",
            "-mon",
            "chardev=con,mode=readline",
        ],
        "kernel_arg": ["-kernel", "{binary}"],
        "bios_arg": ["-bios", "{binary}"],
        "machine_base": "virt,gic-version=3,accel=kvm",
    },
}


def parse_extra_args(extra_args: Any) -> list[str]:
    if extra_args is None:
        return []
    if isinstance(extra_args, str):
        return shlex.split(extra_args.strip()) if extra_args.strip() else []
    if isinstance(extra_args, list):
        out: list[str] = []
        for arg in extra_args:
            if not isinstance(arg, str):
                raise ValueError("extra_args list items must be strings")
            out.append(arg)
        return out
    raise ValueError("extra_args must be a string or list of strings")


def normalize_target_type(payload: dict[str, Any]) -> str:
    target_type = str(payload.get("target_type", "")).strip().lower()
    if target_type and target_type not in {"native_sim", "qemu"} and target_type not in QEMU_PRESETS:
        raise ValueError(f"Unsupported target_type: {target_type}")

    if target_type in {"native_sim", "qemu"}:
        return target_type

    if target_type in QEMU_PRESETS:
        return "qemu"

    board_preset = str(payload.get("board_preset", "")).strip().lower()
    if board_preset in QEMU_PRESETS:
        return "qemu"

    executable = str(payload.get("executable", "")).strip().lower()
    if executable.startswith("qemu-system"):
        return "qemu"

    return "native_sim"


def build_native_sim_command(
    executable: str,
    structured_options: dict[str, Any],
    extra_args: Any,
) -> list[str]:
    cmd = [executable]

    for key, rule in NATIVE_SIM_OPTIONS.items():
        value = structured_options.get(key)
        if rule["type"] == "bool":
            if bool(value):
                cmd.append(rule["flag"])
            continue
        if value is None or value == "":
            continue
        cmd.append(f"{rule['flag']}={value}")

    cmd.extend(parse_extra_args(extra_args))
    return cmd


def update_machine_arg(base_args: list[str], machine_value: str) -> list[str]:
    args = list(base_args)
    for idx, token in enumerate(args):
        if token == "-machine" and idx + 1 < len(args):
            args[idx + 1] = machine_value
            return args
    args.extend(["-machine", machine_value])
    return args


def build_qemu_command(
    board_preset: str,
    structured_options: dict[str, Any],
    extra_args: Any,
) -> list[str]:
    if board_preset not in QEMU_PRESETS:
        raise ValueError(f"Unsupported board_preset: {board_preset}")

    preset = QEMU_PRESETS[board_preset]
    cmd = [preset["executable"]]

    machine_value = preset["machine_base"]
    if bool(structured_options.get("secure_mode")) and "secure=on" not in machine_value:
        machine_value = f"{machine_value},secure=on"

    qemu_args = update_machine_arg(preset["base_args"], machine_value)

    memory_mb = structured_options.get("memory_mb")
    if memory_mb not in (None, ""):
        qemu_args.extend(["-m", str(memory_mb)])

    smp_cpus = structured_options.get("smp_cpus")
    if smp_cpus not in (None, ""):
        qemu_args.extend(["-smp", f"cpus={smp_cpus}"])

    if bool(structured_options.get("gdb_debug")):
        qemu_args.extend(["-s", "-S"])

    if bool(structured_options.get("deterministic")):
        qemu_args.extend(["-icount", "shift=6,align=off,sleep=on", "-rtc", "clock=vm"])

    if bool(structured_options.get("xip_mode")):
        qemu_args.extend(preset["bios_arg"])
    else:
        qemu_args.extend(preset["kernel_arg"])

    qemu_args.extend(parse_extra_args(extra_args))
    cmd.extend(qemu_args)
    return cmd


def allocate_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


def has_port_placeholder(command: list[str]) -> bool:
    return any("{port}" in part for part in command)


def replace_placeholders(command: list[str], binary_inside_container: str, port: int | None) -> list[str]:
    replaced: list[str] = []
    for part in command:
        out = part.replace("{binary}", binary_inside_container)
        if port is not None:
            out = out.replace("{port}", str(port))
        replaced.append(out)
    return replaced


def build_container_ports(command: list[str], allocated_port: int | None) -> dict[str, int]:
    if allocated_port is None:
        return {}
    return {f"{allocated_port}/tcp": allocated_port}


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length) if length > 0 else b""
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


class ZephyrHandler(BaseHTTPRequestHandler):
    docker_client = docker.from_env()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        index_path = Path(__file__).resolve().parent / "index.html"
        if not index_path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html not found"})
            return

        content = index_path.read_bytes()
        self._send_text(HTTPStatus.OK, content, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/run":
            self._handle_run()
            return
        if self.path == "/wait":
            self._handle_wait()
            return
        if self.path == "/stop":
            self._handle_stop()
            return
        if self.path == "/kill":
            self._handle_kill()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_run(self) -> None:
        try:
            payload = parse_json_body(self)
            binary_path = str(payload.get("binary_path", "")).strip()
            if not binary_path:
                raise ValueError("binary_path is required")
            if not os.path.isabs(binary_path):
                raise ValueError("binary_path must be an absolute host path")
            if not os.path.exists(binary_path):
                raise ValueError("binary_path does not exist")

            mode = str(payload.get("mode", "ephemeral")).strip().lower()
            if mode not in {"ephemeral", "interactive"}:
                raise ValueError("mode must be 'ephemeral' or 'interactive'")

            structured_options = payload.get("structured_options", {})
            if structured_options is None:
                structured_options = {}
            if not isinstance(structured_options, dict):
                raise ValueError("structured_options must be an object")

            target_type = normalize_target_type(payload)

            if target_type == "qemu":
                board_preset = str(payload.get("board_preset", "qemu_cortex_a53")).strip()
                command = build_qemu_command(
                    board_preset=board_preset,
                    structured_options=structured_options,
                    extra_args=payload.get("extra_args"),
                )
            else:
                executable = str(payload.get("executable", "{binary}")).strip() or "{binary}"
                command = build_native_sim_command(
                    executable=executable,
                    structured_options=structured_options,
                    extra_args=payload.get("extra_args"),
                )

            binary_name = os.path.basename(binary_path)
            binary_inside_container = f"/app/build/{binary_name}"

            allocated_port = allocate_ephemeral_port() if has_port_placeholder(command) else None
            command = replace_placeholders(command, binary_inside_container, allocated_port)

            ephemeral_timeout = max(1, int(payload.get("timeout") or 30))

            binary_dir = os.path.dirname(binary_path)
            volumes = {binary_dir: {"bind": "/app/build", "mode": "ro"}}

            run_kwargs: dict[str, Any] = {
                "image": IMAGE_NAME,
                "command": command,
                "volumes": volumes,
                "detach": True,
                "stdout": True,
                "stderr": True,
            }

            ports = build_container_ports(command, allocated_port)
            if ports:
                run_kwargs["ports"] = ports

            if bool(structured_options.get("disable_network")):
                run_kwargs["network_mode"] = "none"

            if os.path.exists("/dev/kvm"):
                run_kwargs["devices"] = ["/dev/kvm:/dev/kvm"]

            if mode == "interactive":
                run_kwargs["stdin_open"] = True
                run_kwargs["tty"] = True
                run_kwargs["auto_remove"] = True

            container = self.docker_client.containers.run(**run_kwargs)
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "running",
                    "mode": mode,
                    "target_type": target_type,
                    "container_id": container.id,
                    "allocated_port": allocated_port,
                    "command": command,
                    "timeout": ephemeral_timeout,
                },
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except DockerException as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_wait(self) -> None:
        try:
            payload = parse_json_body(self)
            container_id = str(payload.get("container_id", "")).strip()
            if not container_id:
                raise ValueError("container_id is required")
            timeout = max(1, int(payload.get("timeout") or 30))

            container = self.docker_client.containers.get(container_id)
            timed_out = False
            try:
                wait_result = container.wait(timeout=timeout)
                exit_code = int(wait_result.get("StatusCode", 1))
            except Exception:  # noqa: BLE001 - catches requests.ReadTimeout
                timed_out = True
                exit_code = -1
                try:
                    container.stop(timeout=2)
                except Exception:  # noqa: BLE001
                    pass

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

            # exit_code 137 = killed by SIGKILL
            if exit_code == 137 and not timed_out:
                final_status = "killed"
            elif timed_out:
                final_status = "timeout"
            else:
                final_status = "completed"

            self._send_json(
                HTTPStatus.OK,
                {
                    "status": final_status,
                    "exit_code": exit_code,
                    "output": logs,
                },
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except NotFound:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "container not found"})
        except DockerException as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_stop(self) -> None:
        try:
            payload = parse_json_body(self)
            container_id = str(payload.get("container_id", "")).strip()
            if not container_id:
                raise ValueError("container_id is required")

            container = self.docker_client.containers.get(container_id)
            container.stop(timeout=5)
            self._send_json(HTTPStatus.OK, {"status": "stopped", "container_id": container_id})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except NotFound:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "container not found"})
        except DockerException as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_kill(self) -> None:
        try:
            payload = parse_json_body(self)
            container_id = str(payload.get("container_id", "")).strip()
            if not container_id:
                raise ValueError("container_id is required")

            container = self.docker_client.containers.get(container_id)
            container.kill()
            self._send_json(HTTPStatus.OK, {"status": "killed", "container_id": container_id})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except NotFound:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "container not found"})
        except DockerException as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Handle each request in a dedicated thread so /kill can reach a blocking /wait."""

    daemon_threads = True


def main() -> None:
    server = ThreadedHTTPServer((HOST, PORT), ZephyrHandler)
    print(f"Zephyr orchestrator listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
