import json
import tempfile
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard import (
    CopilotUsage, PowerHistory, TelemetryServer, TelemetryService, counter_rate,
    make_handler, parse_copilot_usage, parse_sensors, read_copilot_usage, read_gpus,
)


def copilot_payload(**quota):
    return {
        "copilot_plan": "individual",
        "token_based_billing": True,
        "quota_reset_date_utc": "2026-11-01T00:00:00Z",
        "quota_snapshots": {"premium_interactions": {
            "unlimited": False, "entitlement": "1500",
            "quota_remaining": "1125", "percent_remaining": 75,
            "has_quota": False, **quota,
        }},
    }


class CopilotUsageTests(unittest.TestCase):
    @patch("dashboard.fetch_copilot_usage", return_value={"used_percent": 10})
    @patch("dashboard.read_copilot_usage", return_value={"used_percent": 90})
    def test_browser_sign_in_replaces_cache_and_disconnect_removes_account_usage(self, read, fetch):
        cache = CopilotUsage()
        self.assertEqual(cache.snapshot()[0]["usage"]["used_percent"], 90)
        cache.auth.credentials = Mock(return_value=(1, "browser-token"))
        self.assertEqual(cache.snapshot()[0]["usage"]["used_percent"], 10)
        fetch.assert_called_once_with("browser-token")
        cache.auth.credentials.return_value = (2, None)
        self.assertEqual(cache.snapshot()[0]["usage"]["used_percent"], 90)
        self.assertEqual(read.call_count, 2)

    @patch("dashboard.fetch_copilot_usage", return_value={"used_percent": 10})
    def test_sign_in_change_during_inflight_request_does_not_publish_old_account(self, fetch):
        cache = CopilotUsage()
        cache.auth.credentials = Mock(side_effect=[(1, "old-token"), (2, None)])
        data, status = cache.snapshot()
        self.assertEqual(status, 503)
        self.assertIsNone(data["usage"])
        self.assertIn("sign-in changed", data["error"])

    def test_credit_percentage_and_counts_use_the_same_allowance(self):
        usage = parse_copilot_usage(copilot_payload(credits_used=9000))
        self.assertEqual(usage["unit"], "AI credits")
        self.assertEqual(usage["used_percent"], 25)
        self.assertEqual(usage["used"], 375)
        self.assertEqual(usage["total"], 1500)
        self.assertEqual(usage["reset_date"], "2026-11-01T00:00:00Z")

    def test_legacy_requests_are_not_mislabeled_as_credits(self):
        payload = copilot_payload()
        del payload["token_based_billing"]
        self.assertEqual(parse_copilot_usage(payload)["unit"], "premium requests")

    def test_zero_full_and_fractional_usage(self):
        for remaining, expected in [(100, 0), (0, 100), (63.25, 36.75)]:
            with self.subTest(remaining=remaining):
                usage = parse_copilot_usage(copilot_payload(
                    percent_remaining=remaining, quota_remaining=None,
                ))
                self.assertEqual(usage["used_percent"], expected)
                self.assertAlmostEqual(usage["used"], 1500 * expected / 100)

    def test_percentage_without_reported_allowance(self):
        usage = parse_copilot_usage(copilot_payload(entitlement=None, quota_remaining=None))
        self.assertEqual(usage["used_percent"], 25)
        self.assertIsNone(usage["used"])
        self.assertIsNone(usage["total"])

    def test_uncapped_org_reports_consumed_credits_not_a_percentage(self):
        usage = parse_copilot_usage(copilot_payload(
            unlimited=True, has_quota=True, credits_used="123.45",
        ))
        self.assertIsNone(usage["used_percent"])
        self.assertIsNone(usage["total"])
        self.assertEqual(usage["used"], 123.45)
        usage = parse_copilot_usage(copilot_payload(
            unlimited=True, has_quota=False, credits_used=123,
        ))
        self.assertIsNone(usage["used"])

    def test_snapshot_reset_timestamp_is_preserved(self):
        usage = parse_copilot_usage(copilot_payload(quota_reset_at=1793491200))
        self.assertEqual(usage["reset_at"], 1793491200)

    def test_invalid_or_missing_data_is_not_zero_usage(self):
        payloads = [None, [], {}, {"quota_snapshots": []}]
        payloads.extend(copilot_payload(**quota) for quota in [
            {"percent_remaining": -1}, {"percent_remaining": 101},
            {"percent_remaining": float("nan")}, {"percent_remaining": None},
            {"percent_remaining": True}, {"entitlement": "invalid"},
            {"entitlement": 0}, {"quota_remaining": 1501},
            {"quota_remaining": float("inf")}, {"unlimited": "false"},
        ])
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_copilot_usage(payload)

    @patch("dashboard.urlopen")
    @patch.dict("dashboard.os.environ", {"COPILOT_TOKEN": "test-secret"}, clear=True)
    def test_request_uses_server_token_and_only_returns_normalized_quota(self, open_url):
        payload = copilot_payload()
        payload["private_field"] = "must-not-be-returned"
        open_url.return_value.__enter__.return_value.read.return_value = json.dumps(payload)
        usage = read_copilot_usage()
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/copilot_internal/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)
        self.assertNotIn("private_field", usage)
        self.assertNotIn("test-secret", json.dumps(usage))

    @patch("dashboard.urlopen")
    @patch("dashboard.subprocess.run")
    @patch("dashboard.shutil.which", return_value="gh")
    @patch.dict("dashboard.os.environ", {}, clear=True)
    def test_github_cli_authentication(self, which, run, open_url):
        run.return_value = Mock(returncode=0, stdout="cli-token\n")
        open_url.return_value.__enter__.return_value.read.return_value = json.dumps(copilot_payload())
        read_copilot_usage()
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertEqual(open_url.call_args.args[0].get_header("Authorization"), "Bearer cli-token")

    @patch("dashboard.shutil.which", return_value=None)
    @patch.dict("dashboard.os.environ", {}, clear=True)
    def test_missing_credentials_are_actionable(self, which):
        with self.assertRaisesRegex(ValueError, "gh auth login"):
            read_copilot_usage()

    @patch("dashboard.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr="secret"))
    @patch("dashboard.shutil.which", return_value="gh")
    @patch.dict("dashboard.os.environ", {}, clear=True)
    def test_cli_errors_do_not_expose_output(self, which, run):
        with self.assertRaisesRegex(ValueError, "not signed in") as error:
            read_copilot_usage()
        self.assertNotIn("secret", str(error.exception))

    @patch("dashboard.urlopen")
    @patch.dict("dashboard.os.environ", {"COPILOT_TOKEN": "secret\ninvalid"}, clear=True)
    def test_invalid_token_cannot_leak_through_http_header_errors(self, open_url):
        with self.assertRaisesRegex(ValueError, "invalid characters") as error:
            read_copilot_usage()
        self.assertNotIn("secret", str(error.exception))
        open_url.assert_not_called()

    @patch("dashboard.urlopen")
    @patch.dict("dashboard.os.environ", {"GH_TOKEN": "test-token"}, clear=True)
    def test_http_and_json_errors_are_explicit(self, open_url):
        for code in (401, 403, 404, 429, 500):
            with self.subTest(code=code):
                open_url.side_effect = HTTPError("url", code, "private", {}, None)
                with self.assertRaises(ValueError) as error:
                    read_copilot_usage()
                self.assertNotIn("test-token", str(error.exception))
                self.assertNotIn("private", str(error.exception))
        open_url.side_effect = None
        open_url.return_value.__enter__.return_value.read.return_value = "not JSON"
        with self.assertRaisesRegex(ValueError, "unreadable"):
            read_copilot_usage()

    @patch("dashboard.read_copilot_usage")
    @patch("dashboard.time.monotonic")
    def test_cache_enforces_exact_sixty_second_boundary(self, monotonic, read):
        monotonic.return_value = 100
        read.return_value = {"used_percent": 25}
        cache = CopilotUsage()
        self.assertEqual(cache.snapshot()[1], 200)
        monotonic.return_value = 159.999
        self.assertEqual(cache.snapshot()[0]["usage"]["used_percent"], 25)
        read.assert_called_once()
        monotonic.return_value = 160
        cache.snapshot()
        self.assertEqual(read.call_count, 2)

    @patch("dashboard.read_copilot_usage")
    @patch("dashboard.time.monotonic")
    def test_failed_refresh_preserves_stale_values_and_is_also_cached(self, monotonic, read):
        monotonic.return_value = 100
        read.return_value = {"used_percent": 25}
        cache = CopilotUsage()
        first, _ = cache.snapshot()
        monotonic.return_value = 160
        read.side_effect = ValueError("GitHub authentication expired")
        failed, status = cache.snapshot()
        self.assertEqual(status, 503)
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["usage"], first["usage"])
        self.assertEqual(failed["updated"], first["updated"])
        self.assertIn("authentication", failed["error"])
        monotonic.return_value = 219.999
        cache.snapshot()
        self.assertEqual(read.call_count, 2)
        monotonic.return_value = 220
        read.side_effect = None
        read.return_value = {"used_percent": 30}
        recovered, status = cache.snapshot()
        self.assertEqual(status, 200)
        self.assertFalse(recovered["stale"])
        self.assertIsNone(recovered["error"])
        self.assertEqual(recovered["usage"]["used_percent"], 30)

    @patch("dashboard.read_copilot_usage", side_effect=ValueError("Sign in first"))
    def test_first_failure_has_no_fabricated_usage(self, read):
        data, status = CopilotUsage().snapshot()
        self.assertEqual(status, 503)
        self.assertIsNone(data["usage"])
        self.assertIsNone(data["updated"])
        self.assertFalse(data["stale"])

    @patch("dashboard.read_copilot_usage")
    def test_concurrent_tabs_share_one_request(self, read):
        read.return_value = {"used_percent": 25}
        cache = CopilotUsage()
        threads = [threading.Thread(target=cache.snapshot) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        read.assert_called_once()

    @patch("dashboard.read_copilot_usage", return_value={"used_percent": 25})
    def test_separate_http_route_does_not_collect_hardware(self, read):
        collector = Mock()
        service = TelemetryService(collector, PowerHistory())
        with TelemetryServer(("127.0.0.1", 0), make_handler(service)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/api/copilot", timeout=3) as response:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(json.load(response)["usage"]["used_percent"], 25)
                service.clear_cache()
                with urlopen(base + "/api/copilot", timeout=3) as response:
                    self.assertEqual(json.load(response)["usage"]["used_percent"], 25)
                read.assert_called_once()
                collector.collect.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=3)


class GitHubAuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.service = TelemetryService(Mock(), PowerHistory())
        self.server = TelemetryServer(("127.0.0.1", 0), make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, path, method="GET", data=None, headers=None):
        return Request(
            self.base + path, method=method, data=data,
            headers=headers if headers is not None else {"X-Telemetry-Request": "github-auth"},
        )

    def test_auth_routes_require_same_origin_loopback_and_custom_header(self):
        for path, method, headers in [
            ("/api/copilot/auth", "GET", {}),
            ("/api/copilot/auth/start", "POST", {}),
            ("/api/copilot/auth/poll", "POST", {"X-Telemetry-Request": "github-auth", "Origin": "https://untrusted.example"}),
            ("/api/copilot/auth/disconnect", "POST", {"X-Telemetry-Request": "github-auth", "Host": "untrusted.example"}),
        ]:
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.request(path, method, headers=headers), timeout=3)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_bad_start_payload_is_rejected_before_contacting_github(self):
        with patch("github_auth.oauth_request") as oauth:
            for data, content_type in [
                (b"{}", "application/json"), (b"[]", "application/json"),
                (b"{", "application/json"), (b"x" * 2049, "application/json"),
                (b'{"client_id":"Public123"}', "text/plain"),
                (b'{"client_id":123}', "application/json"),
            ]:
                with self.subTest(data=data[:20]), self.assertRaises(HTTPError) as error:
                    urlopen(self.request(
                        "/api/copilot/auth/start", "POST", data,
                        {"X-Telemetry-Request": "github-auth", "Content-Type": content_type},
                    ), timeout=3)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            oauth.assert_not_called()

    def test_start_status_poll_and_cancel_keep_device_secret_server_side(self):
        with patch("github_auth.oauth_request", return_value={
            "device_code": "private-device", "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device", "interval": 5, "expires_in": 900,
        }) as oauth:
            with urlopen(self.request(
                "/api/copilot/auth/start", "POST", b'{"client_id":"Public123"}',
                {"X-Telemetry-Request": "github-auth", "Content-Type": "application/json", "Origin": self.base},
            ), timeout=3) as response:
                data = json.load(response)
                self.assertEqual(data["state"], "pending")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("private-device", json.dumps(data))
            for path, method, expected in [
                ("/api/copilot/auth", "GET", "pending"),
                ("/api/copilot/auth/poll", "POST", "pending"),
                ("/api/copilot/auth/disconnect", "POST", "signed_out"),
            ]:
                with urlopen(self.request(path, method), timeout=3) as response:
                    self.assertEqual(json.load(response)["state"], expected)
            oauth.assert_called_once()

    def test_github_setup_errors_are_explicit_http_errors(self):
        with patch("github_auth.oauth_request", return_value={"error": "device_flow_disabled"}):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.request(
                    "/api/copilot/auth/start", "POST", b'{"client_id":"Public123"}',
                    {"X-Telemetry-Request": "github-auth", "Content-Type": "application/json"},
                ), timeout=3)
            self.assertEqual(error.exception.code, 502)
            self.assertIn("Enable Device Flow", json.load(error.exception)["message"])
            error.exception.close()


class PowerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.power = PowerHistory()

    def record(self, seconds, watts):
        return self.power.record(
            1_000 + seconds, seconds, [{"power": watts}]
        )

    def test_integrates_watts_to_watt_hours(self):
        self.record(0, 100)
        result = self.record(2, 200)
        self.assertAlmostEqual(result["gpu_energy_wh"], 150 * 2 / 3600)
        self.assertEqual(result["measured_seconds"], 2)
        self.assertIsNone(result["computer_watts"])
        self.assertIsNone(result["computer_energy_wh"])

    def test_sums_all_devices(self):
        result = self.power.record(1_000, 0, [{"power": 50}, {"power": 100}])
        self.assertEqual(result["gpu_watts"], 150)

    def test_unknown_device_power_is_not_zero_or_a_partial_total(self):
        self.record(0, 100)
        result = self.power.record(1_002, 2, [{"power": 100}, {"power": None}])
        self.assertIsNone(result["gpu_watts"])
        self.assertEqual(result["gpu_energy_wh"], 0)
        result = self.record(4, 100)
        self.assertEqual(result["gpu_energy_wh"], 0)
        result = self.record(6, 100)
        self.assertAlmostEqual(result["gpu_energy_wh"], 100 * 2 / 3600)

    def test_no_devices_is_unavailable(self):
        result = self.power.record(1_000, 0, [])
        self.assertIsNone(result["gpu_watts"])

    def test_invalid_readings_are_unavailable(self):
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(self.record(0, value)["gpu_watts"])

    def test_large_gap_is_not_integrated(self):
        self.record(0, 100)
        result = self.record(120, 100)
        self.assertEqual(result["gpu_energy_wh"], 0)
        self.assertEqual(result["measured_seconds"], 0)

    def test_restart_clears_history_totals_and_measured_time(self):
        self.record(0, 100)
        self.record(2, 100)
        self.power = PowerHistory()
        result = self.record(9_000, 300)
        self.assertEqual(result["gpu_energy_wh"], 0)
        self.assertEqual(result["measured_seconds"], 0)
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["history"][0]["timestamp"], 10_000)

    def test_power_tracking_does_not_access_storage(self):
        with patch("sqlite3.connect") as connect:
            with patch("builtins.open") as open_file:
                with patch("pathlib.Path.open") as open_path:
                    power = PowerHistory()
                    power.record(1_000, 0, [{"power": 100}])
                    power.record(1_002, 2, [{"power": 100}])
        connect.assert_not_called()
        open_file.assert_not_called()
        open_path.assert_not_called()

    def test_published_history_is_not_mutated_by_new_samples(self):
        previous = self.record(0, 100)
        self.record(2, 100)
        self.assertEqual(len(previous["history"]), 1)
        self.assertEqual(previous["history"][0]["energy_wh"], 0)

    def test_zero_watts_is_a_valid_measurement(self):
        self.record(0, 0)
        result = self.record(2, 0)
        self.assertEqual(result["gpu_watts"], 0)
        self.assertEqual(result["gpu_energy_wh"], 0)
        self.assertEqual(result["measured_seconds"], 2)

    def test_history_is_bounded_but_energy_keeps_accumulating(self):
        for second in range(305):
            result = self.record(second, 100)
        self.assertEqual(len(result["history"]), 300)
        self.assertEqual(result["history"][0]["timestamp"], 1_005)
        self.assertAlmostEqual(result["gpu_energy_wh"], 100 * 304 / 3600)


class HardwareMetricsTests(unittest.TestCase):
    def test_duplicate_server_cannot_bind_to_the_same_port(self):
        with TelemetryServer(("127.0.0.1", 0), make_handler(Mock())) as server:
            with self.assertRaises(OSError):
                with TelemetryServer(server.server_address, make_handler(Mock())):
                    pass

    def test_fan_zero_is_a_real_reading_and_invalid_sensors_are_excluded(self):
        sensors = parse_sensors([
            {"Name": "CPU fan", "SensorType": "Fan", "Value": 0, "Identifier": "/cpu/fan/0"},
            {"Name": "CPU package", "SensorType": "Temperature", "Value": 72.5},
            {"SensorType": "Fan", "Value": None},
            {"SensorType": "Temperature", "Value": float("nan")},
            {"SensorType": "Clock", "Value": 5000},
        ])
        self.assertEqual(len(sensors), 2)
        self.assertEqual(sensors[0]["value"], 0)
        self.assertEqual(sensors[0]["unit"], "RPM")
        self.assertEqual(sensors[1]["unit"], "°C")

    def test_counter_rates_handle_resets_and_zero_elapsed(self):
        self.assertEqual(counter_rate(300, 100, 2), 100)
        self.assertEqual(counter_rate(0, 100, 2), 0)
        self.assertEqual(counter_rate(1, 0, 0), 1000)

    @patch("dashboard.shutil.which", return_value="nvidia-smi")
    @patch("dashboard.subprocess.run")
    def test_extended_gpu_fields_and_fan_duty(self, run, which):
        run.side_effect = [
            Mock(stdout="0, Test GPU, 75, 20, 2048, 8192, 62, 90, 150, 2000, 7000, 0"),
            Mock(stdout="0, P0, 12, 3, 4, 16, 2500"),
            Mock(stdout="123, 1024"),
        ]
        gpus, process_memory, error = read_gpus()
        self.assertIsNone(error)
        self.assertEqual(gpus[0]["fan_speed"], 0)
        self.assertEqual(gpus[0]["encoder"], 12)
        self.assertEqual(gpus[0]["pcie_width"], 16)
        self.assertEqual(gpus[0]["performance_state"], "P0")
        self.assertEqual(process_memory[123], 1024)

    @patch("dashboard.shutil.which", return_value="nvidia-smi")
    @patch("dashboard.subprocess.run")
    def test_optional_gpu_query_failure_preserves_base_metrics(self, run, which):
        run.side_effect = [
            Mock(stdout="0, Test GPU, 75, 20, 2048, 8192, 62, 90, 150, 2000, 7000, 30"),
            subprocess.CalledProcessError(1, "nvidia-smi"),
            Mock(stdout=""),
        ]
        gpus, _, error = read_gpus()
        self.assertIsNone(error)
        self.assertEqual(gpus[0]["utilization"], 75)
        self.assertIn("unavailable", gpus[0]["extra_metrics_error"])

    def test_server_history_includes_memory_io_and_separate_devices(self):
        collector = Mock()
        collector.collect.return_value = {
            "timestamp": 1000,
            "system": {
                "cpu_percent": 40, "memory_percent": 60, "cpu_temperature": None,
                "swap_used": 1, "swap_total": 4,
                "disk": {"read": 2 * 1024 ** 2, "write": 0},
                "network": {"received": 0, "sent": 1024 ** 2},
            },
            "gpus": [
                {"index": 0, "utilization": 80, "memory_used": 2048, "memory_total": 8192,
                 "temperature": 65, "fan_speed": 0, "power": 100},
                {"index": 1, "utilization": None, "memory_used": None, "memory_total": None,
                 "temperature": None, "fan_speed": None, "power": None},
            ],
        }
        service = TelemetryService(collector, PowerHistory())
        service._stop = Mock()
        service._stop.is_set.side_effect = [False, True]
        service._run()
        data, status = service.snapshot()
        self.assertEqual(status, 200)
        sample = data["history"][0]
        self.assertEqual(sample["ram"], 60)
        self.assertEqual(sample["swap"], 25)
        self.assertEqual(sample["read"], 2)
        self.assertEqual(sample["sent"], 1)
        self.assertEqual(sample["gpus"][0]["vram"], 25)
        self.assertEqual(sample["gpus"][0]["fan"], 0)
        self.assertIsNone(sample["gpus"][1]["vram"])
        self.assertIsNone(sample["gpus"][1]["temperature"])


class AppIconTests(unittest.TestCase):
    def test_favicon_route_serves_icon_and_keeps_homepage_working(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "static").mkdir()
            icon = b"\x00\x00\x01\x00\x00\x00"
            (root / "assets" / "telemetry.ico").write_bytes(icon)
            (root / "static" / "index.html").write_text("<title>Telemetry</title>")
            with patch("dashboard.ROOT", root):
                with TelemetryServer(("127.0.0.1", 0), make_handler(Mock())) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        base = f"http://127.0.0.1:{server.server_port}"
                        with urlopen(base + "/favicon.ico", timeout=3) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers["Content-Type"], "image/x-icon")
                            self.assertEqual(response.read(), icon)
                        with urlopen(base + "/", timeout=3) as response:
                            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
                            self.assertEqual(response.read(), b"<title>Telemetry</title>")
                        with self.assertRaises(HTTPError) as error:
                            urlopen(base + "/../app/Launcher.cs", timeout=3)
                        self.assertEqual(error.exception.code, 404)
                        error.exception.close()
                        (root / "assets" / "telemetry.ico").unlink()
                        with self.assertRaises(HTTPError) as error:
                            urlopen(base + "/favicon.ico", timeout=3)
                        self.assertEqual(error.exception.code, 404)
                        error.exception.close()
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.service = TelemetryService(Mock(), PowerHistory())

    def publish(self, timestamp):
        self.service._publish({
            "timestamp": timestamp,
            "system": {
                "cpu_percent": 40, "memory_percent": 60, "cpu_temperature": None,
                "swap_used": 0, "swap_total": 0,
                "disk": {"read": 0, "write": 0},
                "network": {"received": 0, "sent": 0},
            },
            "gpus": [{
                "index": 0, "power": 100, "utilization": 80,
                "memory_used": 2048, "memory_total": 8192,
                "temperature": 65, "fan_speed": 30,
            }],
        })
        return self.service.snapshot()[0]

    def test_size_grows_and_clear_resets_both_histories_and_energy(self):
        self.assertEqual(self.service.clear_cache()["cache"]["estimated_bytes"], 0)
        with patch("dashboard.time.monotonic", side_effect=[0, 2, 4, 6]):
            first = self.publish(1000)
            before = self.publish(1002)
            self.assertGreater(before["cache"]["estimated_bytes"],
                               first["cache"]["estimated_bytes"])
            self.assertGreater(before["power"]["gpu_energy_wh"], 0)
            cleared = self.service.clear_cache()
            self.assertEqual(cleared["cache"], {
                "estimated_bytes": 0, "samples": 0, "power_samples": 0,
            })
            self.assertEqual(cleared["history"], [])
            self.assertEqual(cleared["power"]["history"], [])
            self.assertEqual(cleared["power"]["gpu_energy_wh"], 0)
            self.assertEqual(cleared["power"]["measured_seconds"], 0)
            self.assertIsNone(self.service.power.previous)
            self.assertEqual(cleared["system"], before["system"])
            self.assertEqual(cleared["gpus"], before["gpus"])
            self.assertEqual(len(before["history"]), 2)
            self.assertEqual(len(before["power"]["history"]), 2)
            after = self.publish(1004)
            self.assertEqual(after["cache"]["samples"], 1)
            self.assertEqual(after["power"]["gpu_energy_wh"], 0)
            self.assertEqual(after["power"]["measured_seconds"], 0)
            resumed = self.publish(1006)
            self.assertAlmostEqual(resumed["power"]["gpu_energy_wh"], 100 * 2 / 3600)
        self.assertEqual(cleared["history"], [])
        self.assertEqual(cleared["power"]["history"], [])

    def test_cache_retention_stays_bounded(self):
        for index in range(305):
            data = self.publish(1000 + index)
        self.assertEqual(data["cache"]["samples"], 300)
        self.assertEqual(data["cache"]["power_samples"], 300)
        self.assertGreater(data["cache"]["estimated_bytes"], 0)
        self.assertEqual(data["history"][0]["timestamp"], 1005)

    def test_clear_does_not_hide_a_sampler_error(self):
        self.publish(1000)
        self.service._error = "sensor disconnected"
        self.service.clear_cache()
        data, status = self.service.snapshot()
        self.assertEqual(status, 503)
        self.assertIn("sensor disconnected", data["error"])

    def test_clear_http_route_and_stats(self):
        self.publish(1000)
        with TelemetryServer(("127.0.0.1", 0), make_handler(self.service)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for request, expected in [
                    (Request(base + "/api/cache/clear", method="GET"), 404),
                    (Request(base + "/api/cache/clear", method="POST"), 403),
                    (Request(base + "/api/unknown", method="POST"), 404),
                ]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=3)
                    self.assertEqual(error.exception.code, expected)
                    error.exception.close()
                self.assertEqual(self.service.snapshot()[0]["cache"]["samples"], 1)
                request = Request(base + "/api/cache/clear", method="POST",
                                  headers={"X-Telemetry-Request": "clear-cache"})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    data = json.load(response)
                self.assertEqual(data["cache"]["estimated_bytes"], 0)
                self.assertEqual(data["history"], [])
                self.assertEqual(data["power"]["history"], [])
                with urlopen(base + "/api/stats", timeout=3) as response:
                    self.assertEqual(json.load(response)["cache"]["estimated_bytes"], 0)
            finally:
                server.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
