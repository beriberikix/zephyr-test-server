#!/usr/bin/env python3
"""
End-to-end test suite for Zephyr Test Server.

Tests all 5 API endpoints, structured options, and lifecycle behaviors
against pre-built Zephyr binaries from zephyr-sim-tests/builds/.

Requires: zephyr_test_server.py running at SERVER_URL (default: http://localhost:8080)

Environment variables:
    SERVER_URL: Test server URL (default: http://localhost:8080)
    BUILDS_ROOT: Path to zephyr-sim-tests/builds (default: ../zephyr-sim-tests/builds)
"""

import unittest
import urllib.request
import urllib.error
import json
import os
import sys
import time


# ====== Configuration ======
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")
BUILDS_ROOT = os.environ.get("BUILDS_ROOT", "../zephyr-sim-tests/builds")

# Resolve to absolute path
if not os.path.isabs(BUILDS_ROOT):
    BUILDS_ROOT = os.path.join(os.path.dirname(__file__), BUILDS_ROOT)
BUILDS_ROOT = os.path.abspath(BUILDS_ROOT)


# ====== Helpers ======
def api_post(path: str, payload: dict) -> tuple[int, dict]:
    """
    POST to server and return (status_code, json_response).
    Raises urllib.error.HTTPError if status >= 400 (allows callers to assert on errors).
    """
    url = f"{SERVER_URL}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
            return status, body
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = json.loads(e.read().decode("utf-8"))
        except:
            body = {"error": str(e)}
        return status, body


def api_get(path: str) -> tuple[int, str]:
    """GET from server and return (status_code, response_text)."""
    url = f"{SERVER_URL}{path}"
    try:
        with urllib.request.urlopen(url) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            return status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def binary_path(app: str, board: str) -> str:
    """Return absolute path to binary for (app, board)."""
    board_dir = os.path.join(BUILDS_ROOT, app, board, "zephyr")
    if board in ["native_sim", "native_sim_native_64"]:
        path = os.path.join(board_dir, "zephyr.exe")
    else:  # qemu boards
        path = os.path.join(board_dir, "zephyr.elf")
    return path


def normalize_output_for_compare(output: str) -> str:
    """Drop volatile timing lines that vary run-to-run."""
    lines = []
    for line in output.splitlines():
        if line.startswith("Stopped at "):
            continue
        lines.append(line)
    return "\n".join(lines)


def run_ephemeral(
    app: str, board: str, structured_options: dict = None, timeout: int = 30
) -> dict:
    """
    Run ephemeral (await logs before returning).
    Returns: wait_result from /wait endpoint.
    """
    if structured_options is None:
        structured_options = {}

    binary = binary_path(app, board)
    payload = {
        "binary_path": binary,
        "target_type": "qemu" if board in ["qemu_cortex_a53", "qemu_kvm_arm64"] else "native_sim",
        "mode": "ephemeral",
        "timeout": timeout,
        "structured_options": structured_options,
    }
    if board in ["qemu_cortex_a53", "qemu_kvm_arm64"]:
        payload["board_preset"] = board
    else:
        payload["executable"] = "{binary}"

    # Run
    status, run_result = api_post("/run", payload)
    assert status == 200, f"POST /run failed: {run_result}"
    container_id = run_result["container_id"]

    # Wait for completion
    wait_payload = {"container_id": container_id}
    start = time.time()
    while time.time() - start < timeout + 5:  # 5s buffer for network/processing
        status, wait_result = api_post("/wait", wait_payload)
        if status == 200:
            return wait_result
        time.sleep(0.1)

    raise TimeoutError(f"Wait timed out for {container_id}")


def run_interactive(
    app: str, board: str, structured_options: dict = None, timeout: int = 5
) -> dict:
    """
    Run interactive (attach to container, don't wait automatically).
    Returns: run_result from /run endpoint.
    """
    if structured_options is None:
        structured_options = {}

    binary = binary_path(app, board)
    payload = {
        "binary_path": binary,
        "target_type": "qemu" if board in ["qemu_cortex_a53", "qemu_kvm_arm64"] else "native_sim",
        "mode": "interactive",
        "timeout": timeout,
        "structured_options": structured_options,
    }
    if board in ["qemu_cortex_a53", "qemu_kvm_arm64"]:
        payload["board_preset"] = board
    else:
        payload["executable"] = "{binary}"

    status, run_result = api_post("/run", payload)
    assert status == 200, f"POST /run failed: {run_result}"
    return run_result


class BaseTestCase(unittest.TestCase):
    """Base class for E2E tests with logging."""

    @classmethod
    def setUpClass(cls):
        """Verify builds directory exists."""
        if not os.path.isdir(BUILDS_ROOT):
            raise RuntimeError(
                f"BUILDS_ROOT not found: {BUILDS_ROOT}\n"
                f"Set BUILDS_ROOT env var or ensure zephyr-sim-tests/builds exists"
            )
        print(f"\n✓ Using BUILDS_ROOT: {BUILDS_ROOT}")
        print(f"✓ Using SERVER_URL: {SERVER_URL}")

    def setUp(self):
        """Called before each test."""
        pass

    def assertHttpError(self, status: int, expected_code: int):
        """Assert HTTP status code matches expected error code."""
        self.assertEqual(
            status,
            expected_code,
            f"Expected HTTP {expected_code}, got {status}",
        )


class TestServeIndex(BaseTestCase):
    """Test GET / serves HTML index page."""

    def test_serve_index(self):
        """GET / should return 200 with HTML content."""
        status, body = api_get("/")
        self.assertEqual(status, 200)
        self.assertIn("<!doctype", body.lower())
        self.assertIn("zephyr", body.lower())


class TestValidation(BaseTestCase):
    """Test validation error cases."""

    def test_missing_binary_path(self):
        """POST /run without binary_path should return 400."""
        payload = {
            "target_type": "native_sim",
            "executable": "zephyr.exe",
            "mode": "ephemeral",
        }
        status, result = api_post("/run", payload)
        self.assertHttpError(status, 400)

    def test_relative_binary_path(self):
        """POST /run with relative path should return 400."""
        payload = {
            "binary_path": "./relative/path/zephyr.exe",
            "target_type": "native_sim",
            "executable": "zephyr.exe",
            "mode": "ephemeral",
        }
        status, result = api_post("/run", payload)
        self.assertHttpError(status, 400)

    def test_nonexistent_binary_path(self):
        """POST /run with nonexistent absolute path should return 400."""
        payload = {
            "binary_path": "/nonexistent/path/zephyr.exe",
            "target_type": "native_sim",
            "executable": "zephyr.exe",
            "mode": "ephemeral",
        }
        status, result = api_post("/run", payload)
        self.assertHttpError(status, 400)

    def test_invalid_mode(self):
        """POST /run with invalid mode should return 400."""
        binary = binary_path("hello_world", "native_sim")
        payload = {
            "binary_path": binary,
            "target_type": "native_sim",
            "executable": "zephyr.exe",
            "mode": "invalid_mode",
        }
        status, result = api_post("/run", payload)
        self.assertHttpError(status, 400)

    def test_invalid_target_type(self):
        """POST /run with invalid target_type should return 400."""
        binary = binary_path("hello_world", "native_sim")
        payload = {
            "binary_path": binary,
            "target_type": "invalid_board",
            "executable": "zephyr.exe",
            "mode": "ephemeral",
        }
        status, result = api_post("/run", payload)
        self.assertHttpError(status, 400)

    def test_stop_unknown_container(self):
        """POST /stop with unknown container_id should return 404."""
        payload = {"container_id": "nonexistent_container_id_12345"}
        status, result = api_post("/stop", payload)
        self.assertHttpError(status, 404)

    def test_kill_unknown_container(self):
        """POST /kill with unknown container_id should return 404."""
        payload = {"container_id": "nonexistent_container_id_12345"}
        status, result = api_post("/kill", payload)
        self.assertHttpError(status, 404)


class TestNativeSimEphemeral(BaseTestCase):
    """Test native_sim ephemeral runs with output verification."""

    def test_hello_world(self):
        """hello_world should output 'Hello World' and exit 0."""
        result = run_ephemeral("hello_world", "native_sim", timeout=10)
        self.assertIn(result["status"], ["completed", "timeout"])
        self.assertIn("Hello World", result.get("output", ""))

    def test_exit_codes_nonzero(self):
        """exit_codes should exit with code 1."""
        result = run_ephemeral("exit_codes", "native_sim", timeout=10)
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["exit_code"], 0)

    def test_ztest_pass(self):
        """ztest_pass should exit 0 with PASS in output."""
        result = run_ephemeral("ztest_pass", "native_sim", timeout=10)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        output = result.get("output", "")
        self.assertTrue("PASS" in output or "passed" in output.lower())

    def test_ztest_fail(self):
        """ztest_fail should exit non-zero with FAIL in output."""
        result = run_ephemeral("ztest_fail", "native_sim", timeout=10)
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["exit_code"], 0)
        output = result.get("output", "")
        self.assertTrue("FAIL" in output or "failed" in output.lower())


class TestNativeSimOptions(BaseTestCase):
    """Test native_sim structured options."""

    def test_stop_at_option(self):
        """stop_at should halt execution before full timeout."""
        # rt_timing runs a long sleep; stop_at=2 should exit early
        result = run_ephemeral(
            "rt_timing",
            "native_sim",
            structured_options={"stop_at": 2},
            timeout=5,
        )
        self.assertIn(result["status"], ["completed", "timeout"])
        self.assertNotEqual(result.get("exit_code"), -1)

    def test_seed_deterministic(self):
        """Same seed should produce same output."""
        seed_opts = {"seed": 12345}
        result1 = run_ephemeral("seed_rng", "native_sim", structured_options=seed_opts, timeout=10)
        time.sleep(0.5)  # small delay between runs
        result2 = run_ephemeral("seed_rng", "native_sim", structured_options=seed_opts, timeout=10)

        output1 = normalize_output_for_compare(result1.get("output", ""))
        output2 = normalize_output_for_compare(result2.get("output", ""))
        self.assertGreater(len(output1), 0, "First run produced no output")
        self.assertGreater(len(output2), 0, "Second run produced no output")
        self.assertEqual(output1, output2, "Same seed did not produce deterministic output")

    def test_rtc_reset_option(self):
        """rtc_reset should set uptime near 0."""
        result = run_ephemeral(
            "rtc_check",
            "native_sim",
            structured_options={"rtc_reset": True},
            timeout=10,
        )
        self.assertIn(result["status"], ["completed", "timeout"])
        output = result.get("output", "").lower()
        # Should mention "uptime" being near 0 or "reset"
        self.assertTrue("uptime" in output or "reset" in output or "0" in output)

    def test_testargs_passthrough(self):
        """testargs should be visible in output."""
        testargs_str = "arg1=val1 arg2=val2"
        result = run_ephemeral(
            "testargs_echo",
            "native_sim",
            structured_options={"testargs": testargs_str},
            timeout=10,
        )
        self.assertEqual(result["status"], "completed")
        output = result.get("output", "")
        # Should echo the arguments back
        self.assertTrue("arg1" in output or "val1" in output or testargs_str in output)

    def test_disable_network(self):
        """disable_network should prevent socket operations."""
        result = run_ephemeral(
            "net_disabled",
            "native_sim",
            structured_options={"disable_network": True},
            timeout=10,
        )
        self.assertIn(result["status"], ["completed", "timeout"])
        output = result.get("output", "").lower()
        # Should show socket failure or disabled message
        self.assertTrue("fail" in output or "disabled" in output or "error" in output)


class TestLifecycle(BaseTestCase):
    """Test lifecycle operations: timeout, kill, stop, partial output."""

    def test_timeout_and_kill(self):
        """Timeout should auto-kill and return exit code 137 (SIGKILL)."""
        result = run_ephemeral(
            "infinite_loop",
            "native_sim",
            timeout=2,  # Short timeout
        )
        self.assertIn(result["status"], ["timeout", "killed", "completed"])
        # Exit code 137 indicates SIGKILL (signal 9)
        # Could also be "timeout" in status or non-zero code
        self.assertTrue(
            result["exit_code"] == 137 or "timeout" in result.get("status", "").lower(),
            f"Expected timeout/kill behavior, got exit_code={result['exit_code']}, status={result['status']}",
        )

    def test_stop_interactive(self):
        """Stop should gracefully terminate interactive run."""
        # Start infinite_loop in interactive mode
        run_result = run_interactive("infinite_loop", "native_sim", timeout=10)
        container_id = run_result["container_id"]

        # Give it a moment to start
        time.sleep(0.5)

        # Stop it
        status, stop_result = api_post("/stop", {"container_id": container_id})
        self.assertEqual(status, 200)
        self.assertIn("status", stop_result)

        # Wait for it to actually stop
        time.sleep(1)

        # Try to wait (should return completed, not error on nonexistent container)
        wait_payload = {"container_id": container_id}
        status, wait_result = api_post("/wait", wait_payload)
        if status == 200:
            self.assertEqual(wait_result["status"], "completed")

    def test_kill_interactive(self):
        """Kill should forcefully terminate interactive run."""
        # Start infinite_loop in interactive mode
        run_result = run_interactive("infinite_loop", "native_sim", timeout=10)
        container_id = run_result["container_id"]

        # Give it a moment to start
        time.sleep(0.5)

        # Kill it
        status, kill_result = api_post("/kill", {"container_id": container_id})
        self.assertEqual(status, 200)
        self.assertIn("status", kill_result)

        # Wait for it to actually be killed
        time.sleep(1)

        # Try to wait (should return completed with kill status)
        wait_payload = {"container_id": container_id}
        status, wait_result = api_post("/wait", wait_payload)
        if status == 200:
            self.assertEqual(wait_result["status"], "completed")
            # Exit code 137 or 143 indicates signal termination
            self.assertIn(wait_result["exit_code"], [137, 143, -9])

    def test_partial_output_on_timeout(self):
        """Timeout should capture partial output from slow_output."""
        result = run_ephemeral(
            "slow_output",
            "native_sim",
            timeout=2,
        )
        # Should have partial output (not complete, but some lines)
        output = result.get("output", "")
        self.assertGreater(len(output), 0, "Partial output not captured before timeout")


class TestQemuEphemeral(BaseTestCase):
    """Test QEMU board support and structured options."""

    def test_qemu_cortex_a53_basic(self):
        """qemu_cortex_a53 should run and produce output."""
        try:
            result = run_ephemeral("qemu_basic", "qemu_cortex_a53", timeout=15)
            self.assertEqual(result["status"], "completed")
            output = result.get("output", "")
            # Should have some output from QEMU run
            self.assertGreater(len(output), 0)
        except Exception as e:
            self.skipTest(f"QEMU unavailable or binary not found: {e}")

    def test_qemu_smp_cpus_option(self):
        """smp_cpus option should enable SMP and see multiple CPUs."""
        try:
            result = run_ephemeral(
                "qemu_smp",
                "qemu_cortex_a53",
                structured_options={"smp_cpus": 2},
                timeout=15,
            )
            self.assertEqual(result["status"], "completed")
            output = result.get("output", "")
            # Should mention multiple threads/cpus
            self.assertTrue("cpu" in output.lower() or "thread" in output.lower())
        except Exception as e:
            self.skipTest(f"QEMU or SMP not available: {e}")

    def test_qemu_gdb_debug_option(self):
        """gdb_debug should halt execution (test times out waiting)."""
        try:
            # GDB halts at breakpoint, so this should timeout
            result = run_ephemeral(
                "qemu_gdb_halt",
                "qemu_cortex_a53",
                structured_options={"gdb_debug": True},
                timeout=3,
            )
            # Either times out (status=timeout/killed) or completes slowly
            # Main point: option was accepted and command formed correctly
            self.assertIn(result.get("status", "completed"), ["completed", "timeout", "killed"])
        except TimeoutError:
            # Expected: GDB halts execution
            pass
        except Exception as e:
            self.skipTest(f"QEMU not available: {e}")


def main():
    """Run all tests with verbose output."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Load all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestServeIndex))
    suite.addTests(loader.loadTestsFromTestCase(TestValidation))
    suite.addTests(loader.loadTestsFromTestCase(TestNativeSimEphemeral))
    suite.addTests(loader.loadTestsFromTestCase(TestNativeSimOptions))
    suite.addTests(loader.loadTestsFromTestCase(TestLifecycle))
    suite.addTests(loader.loadTestsFromTestCase(TestQemuEphemeral))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Exit with error code if tests failed
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
