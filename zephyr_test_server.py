#!/usr/bin/env python3
"""Zephyr host-side test orchestrator.

Environment setup (dependency-light):
1) uv venv .venv
2) source .venv/bin/activate
3) uv pip install docker simple-websocket
4) python zephyr_server.py
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import socketserver
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import docker
from docker.errors import DockerException, NotFound
from simple_websocket import ConnectionClosed
from simple_websocket import Server as WebSocketServer

HOST = "0.0.0.0"
PORT = 8080
IMAGE_NAME = "zephyr-runner"
MAX_ACTIVE_NETWORKS = 20
_GROUP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")

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

    # For native_sim interactive automation, map a UART to process stdin/stdout
    # so shell input over the WebSocket path reaches the app directly.
    if bool(structured_options.get("stdinout_uart")):
        uart_name = str(structured_options.get("stdinout_uart_name") or "uart").strip()
        if uart_name:
            normalized = uart_name.lstrip("-")
            if not normalized.endswith("_stdinout"):
                normalized = f"{normalized}_stdinout"
            cmd.append(f"-{normalized}")

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


def validate_network_config(network: Any, mode: str) -> dict[str, Any]:
    """Validate and normalize the ``network`` field from a /run payload.

    Returns a dict with optional keys ``expose``, ``group``, ``hostname``.
    """
    if network is None:
        return {}
    if not isinstance(network, dict):
        raise ValueError("network must be an object")

    expose_raw = network.get("expose")
    group = network.get("group")
    hostname = network.get("hostname")

    # --- group requires interactive mode ---
    if group is not None:
        if mode != "interactive":
            raise ValueError("network.group requires mode='interactive'")
        group = str(group).strip()
        if not _GROUP_RE.match(group):
            raise ValueError(
                "network.group must be 1-64 alphanumeric/hyphen chars starting with alnum"
            )

    # --- hostname ---
    if hostname is not None:
        if group is None:
            raise ValueError("network.hostname requires network.group")
        hostname = str(hostname).strip()
        if not _HOSTNAME_RE.match(hostname):
            raise ValueError(
                "network.hostname must be 1-64 alphanumeric/hyphen chars starting with alnum"
            )

    # --- expose ---
    expose: list[dict[str, Any]] = []
    if expose_raw is not None:
        if not isinstance(expose_raw, list):
            raise ValueError("network.expose must be an array")
        if len(expose_raw) > 10:
            raise ValueError("network.expose limited to 10 entries")
        for entry in expose_raw:
            if not isinstance(entry, dict):
                raise ValueError("each network.expose entry must be an object")
            port = entry.get("port")
            protocol = str(entry.get("protocol", "tcp")).strip().lower()
            if not isinstance(port, int) or port < 1 or port > 65535:
                raise ValueError("network.expose port must be an integer 1-65535")
            if protocol not in {"tcp", "udp"}:
                raise ValueError("network.expose protocol must be 'tcp' or 'udp'")
            expose.append({"port": port, "protocol": protocol})

    result: dict[str, Any] = {}
    if expose:
        result["expose"] = expose
    if group:
        result["group"] = group
    if hostname:
        result["hostname"] = hostname
    return result


class ZephyrHandler(BaseHTTPRequestHandler):
    docker_client = docker.from_env()
    interactive_sessions: dict[str, dict[str, Any]] = {}
    interactive_sessions_lock = threading.Lock()

    # --- Network lifecycle state ---
    _network_lock = threading.Lock()
    _container_networks: dict[str, str] = {}      # container_id → network_name (expose-only)
    _container_groups: dict[str, str] = {}         # container_id → group_name
    _shared_networks: dict[str, str] = {}          # group_name → network_name
    _shared_network_refs: dict[str, set[str]] = {} # group_name → {container_ids}

    @classmethod
    def _create_session_network(cls, container_id: str) -> str:
        """Create a per-container bridge network for expose-only sessions."""
        with cls._network_lock:
            total = len(cls._container_networks) + len(cls._shared_networks)
            if total >= MAX_ACTIVE_NETWORKS:
                raise ValueError("network limit reached — stop existing sessions first")
            net_name = f"zephyr-session-{container_id[:12]}"
            cls.docker_client.networks.create(net_name, driver="bridge")
            cls._container_networks[container_id] = net_name
            return net_name

    @classmethod
    def _get_or_create_group_network(cls, group: str, container_id: str) -> str:
        """Get or create a shared internal bridge for a group."""
        with cls._network_lock:
            if group in cls._shared_networks:
                net_name = cls._shared_networks[group]
                cls._shared_network_refs[group].add(container_id)
                cls._container_groups[container_id] = group
                return net_name
            total = len(cls._container_networks) + len(cls._shared_networks)
            if total >= MAX_ACTIVE_NETWORKS:
                raise ValueError("network limit reached — stop existing sessions first")
            net_name = f"zephyr-group-{group}"
            cls.docker_client.networks.create(net_name, driver="bridge", internal=True)
            cls._shared_networks[group] = net_name
            cls._shared_network_refs[group] = {container_id}
            cls._container_groups[container_id] = group
            return net_name

    @classmethod
    def _cleanup_container_network(cls, container_id: str) -> None:
        """Remove per-session network or deref shared group network."""
        with cls._network_lock:
            # Per-session network (expose-only, no group)
            net_name = cls._container_networks.pop(container_id, None)
            if net_name:
                try:
                    net = cls.docker_client.networks.get(net_name)
                    net.remove()
                except Exception:  # noqa: BLE001
                    pass
                return

            # Shared group network
            group = cls._container_groups.pop(container_id, None)
            if not group:
                return
            refs = cls._shared_network_refs.get(group)
            if refs:
                refs.discard(container_id)
                if not refs:
                    del cls._shared_network_refs[group]
                    net_name = cls._shared_networks.pop(group, None)
                    if net_name:
                        try:
                            net = cls.docker_client.networks.get(net_name)
                            net.remove()
                        except Exception:  # noqa: BLE001
                            pass

    @classmethod
    def _cleanup_stale_networks(cls) -> None:
        """Remove leftover zephyr-session-*/zephyr-group-* networks from prior runs."""
        try:
            for net in cls.docker_client.networks.list():
                if net.name and (
                    net.name.startswith("zephyr-session-")
                    or net.name.startswith("zephyr-group-")
                ):
                    try:
                        net.remove()
                        print(f"  Removed stale network: {net.name}")
                    except Exception:  # noqa: BLE001
                        print(f"  Warning: could not remove stale network {net.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  Warning: stale network cleanup failed: {exc}")

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

    @classmethod
    def _set_session(cls, container_id: str, data: dict[str, Any]) -> None:
        with cls.interactive_sessions_lock:
            cls.interactive_sessions[container_id] = data

    @classmethod
    def _get_session(cls, container_id: str) -> dict[str, Any] | None:
        with cls.interactive_sessions_lock:
            return cls.interactive_sessions.get(container_id)

    @classmethod
    def _pop_session(cls, container_id: str) -> dict[str, Any] | None:
        with cls.interactive_sessions_lock:
            return cls.interactive_sessions.pop(container_id, None)

    @classmethod
    def _touch_session(cls, container_id: str) -> None:
        with cls.interactive_sessions_lock:
            session = cls.interactive_sessions.get(container_id)
            if session is not None:
                session["last_activity"] = time.time()

    @staticmethod
    def _to_http_environ(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        environ: dict[str, Any] = {
            "REQUEST_METHOD": "GET",
            "werkzeug.socket": handler.connection,
        }
        for key, value in handler.headers.items():
            header_key = f"HTTP_{key.upper().replace('-', '_')}"
            environ[header_key] = value
        return environ

    @staticmethod
    def _safe_close(resource: Any) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def _stop_transport(cls, container_id: str) -> None:
        session = cls._get_session(container_id)
        if not session:
            return
        stop_event = session.get("stop_event")
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        cls._safe_close(session.get("ws"))
        cls._safe_close(session.get("raw_socket"))
        cls._safe_close(session.get("attach_socket"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/ws/"):
            self._handle_ws(parsed.path)
            return

        if parsed.path != "/":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        index_path = Path(__file__).resolve().parent / "index.html"
        if not index_path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html not found"})
            return

        content = index_path.read_bytes()
        self._send_text(HTTPStatus.OK, content, "text/html; charset=utf-8")

    def _handle_ws(self, ws_path: str) -> None:
        container_id = ws_path.removeprefix("/ws/").strip()
        if not container_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "container_id is required"})
            return

        session = self._get_session(container_id)
        if not session:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "interactive session not found"})
            return

        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "websocket upgrade required"})
            return

        try:
            container = self.docker_client.containers.get(container_id)
        except NotFound:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "container not found"})
            return
        except DockerException as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
            return

        stop_event = session.get("stop_event")
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
            session["stop_event"] = stop_event

        try:
            ws = WebSocketServer.accept(
                self._to_http_environ(self),
                ping_interval=25,
                max_message_size=65536,
            )
            attach_socket = container.attach_socket(
                params={
                    "stdin": 1,
                    "stdout": 1,
                    "stderr": 1,
                    "stream": 1,
                    "logs": 1,
                }
            )
            raw_socket = getattr(attach_socket, "_sock", attach_socket)
            if hasattr(raw_socket, "settimeout"):
                raw_socket.settimeout(1.0)

            session["ws"] = ws
            session["attach_socket"] = attach_socket
            session["raw_socket"] = raw_socket

            def reader_loop() -> None:
                while not stop_event.is_set():
                    try:
                        chunk = raw_socket.recv(4096)
                    except socket.timeout:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                    if not chunk:
                        break
                    self._touch_session(container_id)
                    try:
                        ws.send(chunk.decode("utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001
                        break
                stop_event.set()

            def writer_loop() -> None:
                idle_timeout = max(1, int(session.get("idle_timeout") or 30))
                while not stop_event.is_set():
                    try:
                        message = ws.receive(timeout=1)
                    except ConnectionClosed:
                        break
                    except Exception:  # noqa: BLE001
                        break

                    if message is None:
                        last_activity = float(session.get("last_activity") or time.time())
                        if time.time() - last_activity > idle_timeout:
                            try:
                                container.stop(timeout=2)
                            except Exception:  # noqa: BLE001
                                pass
                            break
                        continue

                    self._touch_session(container_id)
                    payload = message.encode("utf-8") if isinstance(message, str) else message
                    if payload is None:
                        continue
                    try:
                        raw_socket.send(payload)
                    except Exception:  # noqa: BLE001
                        break
                stop_event.set()

            reader_thread = threading.Thread(target=reader_loop, daemon=True)
            writer_thread = threading.Thread(target=writer_loop, daemon=True)
            session["reader_thread"] = reader_thread
            session["writer_thread"] = writer_thread

            reader_thread.start()
            writer_thread.start()
            reader_thread.join()
            writer_thread.join()
        except Exception:  # noqa: BLE001
            self._safe_close(session.get("ws"))
        finally:
            stop_event.set()
            self._safe_close(session.get("ws"))
            self._safe_close(session.get("raw_socket"))
            self._safe_close(session.get("attach_socket"))
            try:
                container.reload()
                if container.status == "running":
                    container.stop(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._cleanup_container_network(container_id)
            self._pop_session(container_id)

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
        net_name_to_cleanup: str | None = None
        container_created = False
        container_id: str | None = None
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

            # --- Network config ---
            net_cfg = validate_network_config(payload.get("network"), mode)
            expose = net_cfg.get("expose", [])
            group = net_cfg.get("group")
            hostname = net_cfg.get("hostname")
            has_networking = bool(expose) or bool(group)

            target_type = normalize_target_type(payload)

            if mode == "interactive" and target_type == "native_sim" and not has_networking:
                structured_options.setdefault("stdinout_uart", True)
                structured_options.setdefault("stdinout_uart_name", "uart")

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

            # Reject {port} placeholder when no networking is configured
            if not has_networking and has_port_placeholder(command):
                raise ValueError("{port} placeholder requires network.expose or network.group")

            binary_name = os.path.basename(binary_path)
            binary_inside_container = f"/app/build/{binary_name}"

            # --- Port allocation from expose list ---
            allocated_ports: dict[str, int] = {}
            first_allocated_port: int | None = None
            for entry in expose:
                host_port = allocate_ephemeral_port()
                key = f"{entry['port']}/{entry['protocol']}"
                allocated_ports[key] = host_port
                if first_allocated_port is None:
                    first_allocated_port = host_port

            # Legacy {port} placeholder — use first allocated port
            if has_port_placeholder(command) and first_allocated_port is not None:
                command = replace_placeholders(command, binary_inside_container, first_allocated_port)
            else:
                command = replace_placeholders(command, binary_inside_container, None)

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

            # --- Apply network config ---
            group_net_name: str | None = None
            if not has_networking:
                run_kwargs["network_mode"] = "none"
            elif group and not expose:
                # Group-only: start directly on the internal group network.
                group_net_name = self._get_or_create_group_network(group, "pending")
                net_name_to_cleanup = group_net_name
                run_kwargs["network"] = group_net_name
                if hostname:
                    run_kwargs["hostname"] = hostname
            elif group and expose:
                # Expose + group: start on default bridge (so port publishing
                # works), then attach to the group network post-creation.
                group_net_name = self._get_or_create_group_network(group, "pending")
                net_name_to_cleanup = group_net_name
                # Don't set run_kwargs["network"] — use default bridge.
            else:
                # expose-only: start on default bridge, create per-session
                # network post-creation.
                pass

            # Build port bindings from expose list (bound to 127.0.0.1)
            if allocated_ports:
                port_bindings: dict[str, tuple[str, int]] = {}
                for entry in expose:
                    key = f"{entry['port']}/{entry['protocol']}"
                    host_port = allocated_ports[key]
                    port_bindings[f"{entry['port']}/{entry['protocol']}"] = ("127.0.0.1", host_port)
                run_kwargs["ports"] = port_bindings

            if os.path.exists("/dev/kvm"):
                run_kwargs["devices"] = ["/dev/kvm:/dev/kvm"]

            if mode == "interactive":
                run_kwargs["stdin_open"] = True
                run_kwargs["tty"] = True
                run_kwargs["auto_remove"] = True

            container = self.docker_client.containers.run(**run_kwargs)
            container_created = True
            container_id = container.id

            # For group networks: fix up the ref from "pending" to real container id
            if group:
                with self._network_lock:
                    refs = self._shared_network_refs.get(group)
                    if refs and "pending" in refs:
                        refs.discard("pending")
                        refs.add(container.id)
                    self._container_groups.pop("pending", None)
                    self._container_groups[container.id] = group
                net_name_to_cleanup = None  # success — don't rollback

                # expose+group: container started on default bridge, now
                # also connect to the internal group network.
                if expose and group_net_name:
                    try:
                        net_obj = self.docker_client.networks.get(group_net_name)
                        aliases = [hostname] if hostname else None
                        net_obj.connect(container, aliases=aliases)
                    except Exception:
                        pass  # port publishing still works via default bridge

            # For expose-only (no group): create per-session network and reconnect
            if expose and not group:
                try:
                    net_name = self._create_session_network(container.id)
                    net_obj = self.docker_client.networks.get(net_name)
                    net_obj.connect(container)
                except Exception:
                    # Container already running on default bridge; expose ports still work
                    pass

            if mode == "interactive":
                self._set_session(
                    container.id,
                    {
                        "container_id": container.id,
                        "idle_timeout": ephemeral_timeout,
                        "last_activity": time.time(),
                        "stop_event": threading.Event(),
                        "ws": None,
                        "attach_socket": None,
                        "raw_socket": None,
                    },
                )

            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "running",
                    "mode": mode,
                    "target_type": target_type,
                    "container_id": container.id,
                    "allocated_ports": allocated_ports if allocated_ports else None,
                    "network": net_cfg if net_cfg else None,
                    "command": command,
                    "timeout": ephemeral_timeout,
                    "ws_path": f"/ws/{container.id}" if mode == "interactive" else None,
                },
            )
        except ValueError as exc:
            if net_name_to_cleanup:
                self._cleanup_container_network("pending")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except DockerException as exc:
            if net_name_to_cleanup:
                self._cleanup_container_network("pending")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"docker error: {exc}"})
        except Exception as exc:  # noqa: BLE001
            if net_name_to_cleanup:
                self._cleanup_container_network("pending")
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
            self._cleanup_container_network(container_id)

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
            self._stop_transport(container_id)
            self._cleanup_container_network(container_id)
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
            self._stop_transport(container_id)
            self._cleanup_container_network(container_id)
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
    print("Cleaning up stale Docker networks...")
    ZephyrHandler._cleanup_stale_networks()
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
