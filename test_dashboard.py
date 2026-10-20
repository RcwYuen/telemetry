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
    PowerHistory, TelemetryServer, TelemetryService, counter_rate, make_handler,
    parse_sensors, read_gpus,
)


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
