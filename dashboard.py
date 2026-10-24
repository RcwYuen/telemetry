from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import psutil

from github_auth import GitHubDeviceAuth


ROOT = Path(__file__).resolve().parent
GPU_QUERY = (
    "index,name,utilization.gpu,utilization.memory,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,fan.speed"
)
GPU_HEADERS = [
    "index",
    "name",
    "utilization",
    "memory_utilization",
    "memory_used",
    "memory_total",
    "temperature",
    "power",
    "power_limit",
    "graphics_clock",
    "memory_clock",
    "fan_speed",
]
GPU_EXTRA_FIELDS = [
    "index",
    "pstate",
    "utilization.encoder",
    "utilization.decoder",
    "pcie.link.gen.current",
    "pcie.link.width.current",
    "clocks.max.gr",
]


def discover_conda_envs() -> list[tuple[str, str]]:
    conda = shutil.which("conda")
    if not conda:
        return []
    try:
        result = subprocess.run(
            [conda, "env", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        prefixes = json.loads(result.stdout).get("envs", [])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    envs = []
    for prefix in prefixes:
        path = os.path.normcase(os.path.abspath(prefix))
        name = (
            "base"
            if os.path.normcase(os.path.abspath(prefix))
            == os.path.normcase(os.path.abspath(os.environ.get("CONDA_ROOT", "")))
            or Path(prefix).name.lower() in {"anaconda3", "miniconda3"}
            else Path(prefix).name
        )
        envs.append((path.rstrip(os.sep) + os.sep, name))
    return sorted(envs, key=lambda entry: len(entry[0]), reverse=True)


def environment_for_path(path: str | None, envs: list[tuple[str, str]]) -> str:
    if path:
        normalized = os.path.normcase(os.path.abspath(path))
        for prefix, name in envs:
            if normalized.startswith(prefix):
                return name
    return "system"


def number_or_none(value: str) -> float | None:
    value = value.strip()
    if not value or value.lower() in {"n/a", "[not supported]"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_gpus() -> tuple[list[dict[str, Any]], dict[int, float], str | None]:
    command = shutil.which("nvidia-smi")
    if not command:
        return [], {}, "nvidia-smi not found"
    try:
        result = subprocess.run(
            [command, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        rows = list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))
        gpus = []
        for row in rows:
            if len(row) != len(GPU_HEADERS):
                continue
            values = dict(zip(GPU_HEADERS, row))
            gpus.append(
                {
                    "index": int(values["index"]),
                    "name": values["name"].strip(),
                    "utilization": number_or_none(values["utilization"]),
                    "memory_utilization": number_or_none(values["memory_utilization"]),
                    "memory_used": number_or_none(values["memory_used"]),
                    "memory_total": number_or_none(values["memory_total"]),
                    "temperature": number_or_none(values["temperature"]),
                    "power": number_or_none(values["power"]),
                    "power_limit": number_or_none(values["power_limit"]),
                    "graphics_clock": number_or_none(values["graphics_clock"]),
                    "memory_clock": number_or_none(values["memory_clock"]),
                    "fan_speed": number_or_none(values["fan_speed"]),
                    "processes": [],
                }
            )
    except (OSError, subprocess.SubprocessError) as error:
        return [], {}, f"GPU query failed: {error}"

    try:
        extra = subprocess.run(
            [
                command,
                f"--query-gpu={','.join(GPU_EXTRA_FIELDS)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        by_index = {gpu["index"]: gpu for gpu in gpus}
        for row in csv.reader(extra.stdout.splitlines(), skipinitialspace=True):
            if len(row) != len(GPU_EXTRA_FIELDS):
                continue
            gpu = by_index.get(int(row[0]))
            if gpu is not None:
                gpu.update(
                    {
                        "performance_state": row[1].strip(),
                        "encoder": number_or_none(row[2]),
                        "decoder": number_or_none(row[3]),
                        "pcie_generation": number_or_none(row[4]),
                        "pcie_width": number_or_none(row[5]),
                        "max_graphics_clock": number_or_none(row[6]),
                    }
                )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        for gpu in gpus:
            gpu["extra_metrics_error"] = f"Extended NVIDIA metrics unavailable: {error}"

    process_memory: dict[int, float] = {}
    try:
        processes_result = subprocess.run(
            [
                command,
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        for row in csv.reader(
            processes_result.stdout.splitlines(), skipinitialspace=True
        ):
            if len(row) == 2:
                pid = int(row[0].strip())
                memory = number_or_none(row[1])
                if memory is not None:
                    process_memory[pid] = process_memory.get(pid, 0) + memory
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return gpus, process_memory, None


def read_cpu_temperature() -> float | None:
    try:
        temperatures = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for sensor, readings in temperatures.items():
        if not any(
            name in sensor.lower()
            for name in ("cpu", "coretemp", "k10temp", "zenpower")
        ):
            continue
        for reading in readings:
            if reading.current is not None and any(
                token in reading.label.lower() for token in ("package", "tctl", "tdie")
            ):
                return float(reading.current)
    return None


def cpu_name() -> str:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError as error:
            print(f"CPU model unavailable from registry: {error}", flush=True)
    return platform.processor() or platform.machine()


def parse_sensors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sensors = []
    units = {"Temperature": "°C", "Fan": "RPM", "Power": "W"}
    for row in rows:
        kind = row.get("SensorType")
        value = row.get("Value")
        if (
            kind not in units
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            continue
        sensors.append(
            {
                "name": str(row.get("Name", "Sensor")),
                "identifier": str(row.get("Identifier", "")),
                "kind": kind,
                "value": value,
                "unit": units[kind],
            }
        )
    return sensors


class HardwareMonitor:
    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
foreach ($namespace in @('root\LibreHardwareMonitor', 'root\OpenHardwareMonitor')) {
    try {
        $sensors = @(Get-CimInstance -Namespace $namespace -ClassName Sensor -ErrorAction Stop |
            Where-Object { $_.SensorType -in @('Temperature', 'Fan', 'Power') } |
            Select-Object Name, SensorType, Value, Identifier)
        @{source=$namespace; sensors=$sensors} | ConvertTo-Json -Depth 4 -Compress
        exit
    } catch {
        if ($_.Exception.Message -notmatch 'Invalid namespace|Invalid class|0x8004100E|0x80041010') {
            [Console]::Error.WriteLine($_.Exception.Message)
        }
    }
}
@{source=$null; sensors=@()} | ConvertTo-Json -Compress
"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "source": None,
            "sensors": [],
            "updated": None,
            "message": "Checking optional hardware sensors…",
        }
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _read(self) -> dict[str, Any]:
        if os.name == "nt":
            powershell = shutil.which("powershell.exe")
            if not powershell:
                return {
                    "source": None,
                    "sensors": [],
                    "message": "PowerShell hardware sensor bridge unavailable",
                }
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            data = json.loads(result.stdout)
            sensors = parse_sensors(data["sensors"])
            message = result.stderr.strip() or (
                "Optional hardware sensors connected"
                if data["source"]
                else "CPU/chassis sensors unavailable. Run LibreHardwareMonitor as administrator to expose temperatures and fan RPM."
            )
            return {"source": data["source"], "sensors": sensors, "message": message}
        rows = []
        if hasattr(psutil, "sensors_temperatures"):
            for group, readings in psutil.sensors_temperatures().items():
                for reading in readings:
                    rows.append(
                        {
                            "Name": reading.label or group,
                            "Identifier": group,
                            "SensorType": "Temperature",
                            "Value": reading.current,
                        }
                    )
        if hasattr(psutil, "sensors_fans"):
            for group, readings in psutil.sensors_fans().items():
                for reading in readings:
                    rows.append(
                        {
                            "Name": reading.label or group,
                            "Identifier": group,
                            "SensorType": "Fan",
                            "Value": reading.current,
                        }
                    )
        sensors = parse_sensors(rows)
        return {
            "source": "OS sensors" if sensors else None,
            "sensors": sensors,
            "message": "OS hardware sensors"
            if sensors
            else "Hardware sensors not exposed by the OS",
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._read()
                data["updated"] = time.time()
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                data = {
                    "source": None,
                    "sensors": [],
                    "updated": None,
                    "message": f"Hardware sensor query failed: {error}",
                }
                print(data["message"], flush=True)
            with self._lock:
                self._data = data
            self._stop.wait(10)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._data.copy()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=9)


def counter_rate(current: int, previous: int, elapsed: float) -> float:
    return max(0, current - previous) / max(elapsed, 0.001)


class WindowsGpuMonitor:
    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ids = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
if (-not $ids.Count) { '{}' ; exit }
$idPattern = '\(pid_(' + (($ids | ForEach-Object { [regex]::Escape([string]$_) }) -join '|') + ')_'
$paths = @()
foreach ($setName in @('GPU Engine', 'GPU Process Memory')) {
    try {
        $set = Get-Counter -ListSet $setName -ErrorAction Stop
        $paths += $set.PathsWithInstances | Where-Object {
            $_ -match $idPattern -and
            (($_ -match 'GPU Engine' -and $_ -match 'Utilization Percentage') -or
             ($_ -match 'GPU Process Memory' -and $_ -match 'Dedicated Usage'))
        }
    } catch { }
}
if (-not $paths.Count) { '{}' ; exit }
$samples = Get-Counter -Counter $paths -MaxSamples 1 -ErrorAction SilentlyContinue
$result = @{}
foreach ($sample in $samples.CounterSamples) {
    $match = [regex]::Match($sample.Path, '\(pid_(\d+)_')
    if (-not $match.Success) { continue }
    $id = [int]$match.Groups[1].Value
    $key = [string]$id
    if (-not $result.ContainsKey($key)) {
        $result[$key] = @{ utilization = 0.0; memory_mb = 0.0 }
    }
    if ($sample.Path -match 'GPU Engine') {
        $result[$key].utilization += [math]::Max(0, $sample.CookedValue)
    } else {
        $result[$key].memory_mb += [math]::Max(0, $sample.CookedValue) / 1MB
    }
}
ConvertTo-Json -InputObject $result -Compress -Depth 4
"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pids: list[int] = []
        self._metrics: dict[int, dict[str, float]] = {}
        self.error: str | None = None
        self._powershell = shutil.which("powershell.exe") if os.name == "nt" else None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_pids(self, pids: list[int]) -> None:
        with self._lock:
            self._pids = pids
        self._wake.set()

    def snapshot(self) -> dict[int, dict[str, float]]:
        with self._lock:
            return dict(self._metrics)

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            delay = next_poll - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            with self._lock:
                pids = self._pids.copy()
            if not pids:
                continue
            next_poll = time.monotonic() + 5
            if not self._powershell:
                self.error = "Windows GPU engine counters are unavailable"
                continue
            try:
                result = subprocess.run(
                    [
                        self._powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        self._SCRIPT,
                    ],
                    input=json.dumps(pids),
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=True,
                )
                decoded = json.loads(result.stdout) if result.stdout.strip() else {}
                metrics = {
                    int(pid): {
                        "utilization": float(values.get("utilization", 0)),
                        "memory_mb": float(values.get("memory_mb", 0)),
                    }
                    for pid, values in decoded.items()
                }
                with self._lock:
                    self._metrics = metrics
                self.error = None
            except (
                OSError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
                ValueError,
            ) as error:
                self.error = f"Windows GPU process counters unavailable: {error}"

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1)


class ProcessMonitor:
    INTERVAL = 10

    def __init__(self, envs: list[tuple[str, str]], cpu_count: int) -> None:
        self.envs = envs
        self.cpu_count = cpu_count
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._data: dict[str, Any] = {
            "processes": [], "updated": None, "error": "Process details are warming up",
        }
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _read(self) -> list[dict[str, Any]]:
        processes = []
        for process in psutil.process_iter():
            try:
                with process.oneshot():
                    info = process.as_dict(attrs=[
                        "pid", "name", "exe", "memory_info", "num_threads",
                        "status", "create_time",
                    ])
                    if info["pid"] == 0:
                        continue
                    executable = info.get("exe")
                    if not executable:
                        try:
                            command_line = process.cmdline()
                        except (psutil.AccessDenied, OSError):
                            command_line = []
                        executable = command_line[0] if command_line else None
                    cpu = (process.cpu_percent(interval=None) or 0) / self.cpu_count
                rss = info.get("memory_info")
                processes.append({
                    "pid": info["pid"],
                    "name": info.get("name") or Path(executable or "process").name,
                    "environment": environment_for_path(executable, self.envs),
                    "cpu": round(cpu, 1),
                    "memory_mb": round(rss.rss / 1024**2, 1) if rss else 0,
                    "threads": info.get("num_threads"),
                    "status": info.get("status"),
                    "create_time": info.get("create_time"),
                })
            except (psutil.Error, OSError):
                continue
        return processes

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                processes = self._read()
                with self._lock:
                    self._data = {"processes": processes, "updated": time.time(), "error": None}
            except (psutil.Error, OSError) as error:
                print(f"Process sampling failed: {error}", flush=True)
                with self._lock:
                    self._data = {**self._data, "error": f"Process sampling failed: {error}"}
            self._stop.wait(max(0, self.INTERVAL - (time.monotonic() - started)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {**self._data, "processes": [dict(item) for item in self._data["processes"]]}
        if self._thread.ident is not None and not self._thread.is_alive() and not self._stop.is_set():
            data["error"] = "Process sampler stopped; restart the dashboard"
        elif data["updated"] is not None and time.time() - data["updated"] > 30 and not data["error"]:
            data["error"] = "Process details delayed; displayed workloads are stale"
        return data

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=15)


class StatsCollector:
    def __init__(self) -> None:
        self.conda_envs = discover_conda_envs()
        self.gpu_monitor = WindowsGpuMonitor()
        self.hardware_monitor = HardwareMonitor()
        self.cpu_name = cpu_name()
        self.previous_cpu_stats = psutil.cpu_stats()
        self.previous_interfaces = psutil.net_io_counters(pernic=True)
        self.volumes: list[dict[str, Any]] = []
        self.volumes_updated = 0.0
        self.cpu_count = psutil.cpu_count(logical=True) or 1
        self.process_monitor = ProcessMonitor(self.conda_envs, self.cpu_count)
        self.started = time.time()
        self.previous_network = psutil.net_io_counters()
        self.previous_disk = psutil.disk_io_counters()
        self.previous_io_time = time.monotonic()
        psutil.cpu_percent(interval=None)

    def collect(self) -> dict[str, Any]:
        cpu_percent = psutil.cpu_percent(interval=None)
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        frequency = psutil.cpu_freq()
        gpus, gpu_process_memory, gpu_error = read_gpus()
        processes = []
        environments: dict[str, dict[str, Any]] = {}
        conda_pids = []

        process_data = self.process_monitor.snapshot()
        for info in process_data["processes"]:
            env_name = info["environment"]
            if env_name != "system":
                conda_pids.append(info["pid"])
            item = {
                **info,
                "gpu_memory": round(gpu_process_memory.get(info["pid"], 0), 1),
                "gpu_utilization": 0.0,
                "elapsed": max(0, time.time() - info["create_time"])
                if info.get("create_time") else None,
            }
            processes.append(item)
            group = environments.setdefault(env_name, {
                "name": env_name, "cpu": 0, "memory_mb": 0, "gpu_memory": 0,
                "gpu_utilization": 0, "process_count": 0, "processes": [],
            })
            group["cpu"] += item["cpu"]
            group["memory_mb"] += item["memory_mb"]
            group["process_count"] += 1
            group["processes"].append(item)

        self.gpu_monitor.set_pids(conda_pids)
        windows_gpu_metrics = self.gpu_monitor.snapshot()
        for process in processes:
            gpu_metrics = windows_gpu_metrics.get(process["pid"], {})
            process["gpu_memory"] = round(
                process["gpu_memory"] or gpu_metrics.get("memory_mb", 0), 1
            )
            process["gpu_utilization"] = round(gpu_metrics.get("utilization", 0), 1)
            group = environments[process["environment"]]
            group["gpu_memory"] += process["gpu_memory"]
            group["gpu_utilization"] += process["gpu_utilization"]
        for gpu in gpus:
            gpu["processes"] = [
                process
                for process in processes
                if process["gpu_memory"] or process["gpu_utilization"]
            ]
        for group in environments.values():
            group["cpu"] = round(group["cpu"], 1)
            group["memory_mb"] = round(group["memory_mb"], 1)
            group["gpu_memory"] = round(group["gpu_memory"], 1)
            group["gpu_utilization"] = round(group["gpu_utilization"], 1)
            group["processes"].sort(
                key=lambda item: (
                    item["gpu_utilization"],
                    item["gpu_memory"],
                    item["cpu"],
                ),
                reverse=True,
            )

        now = time.monotonic()
        elapsed = max(now - self.previous_io_time, 0.001)
        network = psutil.net_io_counters()
        disk_io = psutil.disk_io_counters()
        network_rates = {
            "received": round(
                (network.bytes_recv - self.previous_network.bytes_recv) / elapsed
            ),
            "sent": round(
                (network.bytes_sent - self.previous_network.bytes_sent) / elapsed
            ),
            "total_received": network.bytes_recv,
            "total_sent": network.bytes_sent,
            "errors": network.errin + network.errout,
            "drops": network.dropin + network.dropout,
        }
        disk_rates = {
            "read": round(
                (disk_io.read_bytes - self.previous_disk.read_bytes) / elapsed
            )
            if disk_io and self.previous_disk
            else 0,
            "write": round(
                (disk_io.write_bytes - self.previous_disk.write_bytes) / elapsed
            )
            if disk_io and self.previous_disk
            else 0,
            "read_iops": counter_rate(
                disk_io.read_count, self.previous_disk.read_count, elapsed
            )
            if disk_io and self.previous_disk
            else None,
            "write_iops": counter_rate(
                disk_io.write_count, self.previous_disk.write_count, elapsed
            )
            if disk_io and self.previous_disk
            else None,
        }
        interfaces = psutil.net_io_counters(pernic=True)
        interface_stats = psutil.net_if_stats()
        network_rates["interfaces"] = [
            {
                "name": name,
                "speed_mbps": interface_stats[name].speed,
                "received": counter_rate(
                    counters.bytes_recv,
                    self.previous_interfaces[name].bytes_recv,
                    elapsed,
                ),
                "sent": counter_rate(
                    counters.bytes_sent,
                    self.previous_interfaces[name].bytes_sent,
                    elapsed,
                ),
            }
            for name, counters in interfaces.items()
            if name in self.previous_interfaces
            and name in interface_stats
            and interface_stats[name].isup
            and "loopback" not in name.lower()
            and name != "lo"
        ]
        cpu_stats = psutil.cpu_stats()
        context_switches = counter_rate(
            cpu_stats.ctx_switches, self.previous_cpu_stats.ctx_switches, elapsed
        )
        interrupts = counter_rate(
            cpu_stats.interrupts, self.previous_cpu_stats.interrupts, elapsed
        )
        self.previous_cpu_stats = cpu_stats
        self.previous_interfaces = interfaces
        if now - self.volumes_updated >= 30:
            self.volumes = []
            for partition in psutil.disk_partitions(all=False):
                if "removable" in partition.opts or partition.fstype.lower() in {
                    "cdrom",
                    "udf",
                }:
                    continue
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    self.volumes.append(
                        {
                            "mount": partition.mountpoint,
                            "filesystem": partition.fstype,
                            "used_gb": usage.used / 1024**3,
                            "total_gb": usage.total / 1024**3,
                            "percent": usage.percent,
                        }
                    )
                except OSError as error:
                    self.volumes.append(
                        {"mount": partition.mountpoint, "error": str(error)}
                    )
            self.volumes_updated = now
        self.previous_io_time = now
        self.previous_network = network
        self.previous_disk = disk_io

        hardware = self.hardware_monitor.snapshot()
        cpu_temperature = read_cpu_temperature()
        if cpu_temperature is None:
            package = [
                sensor["value"]
                for sensor in hardware["sensors"]
                if sensor["kind"] == "Temperature"
                and any(
                    token in sensor["identifier"].lower()
                    for token in ("cpu", "coretemp", "k10temp")
                )
                and any(
                    token in sensor["name"].lower()
                    for token in ("package", "tctl", "tdie")
                )
            ]
            cpu_temperature = max(package) if package else None
        return {
            "timestamp": time.time(),
            "system": {
                "cpu_percent": round(cpu_percent, 1),
                "per_cpu": [round(value, 1) for value in per_cpu],
                "cpu_cores": psutil.cpu_count(logical=False),
                "cpu_threads": self.cpu_count,
                "cpu_name": self.cpu_name,
                "hostname": platform.node(),
                "os": f"{platform.system()} {platform.release()}",
                "boot_uptime": round(time.time() - psutil.boot_time()),
                "cpu_temperature": cpu_temperature,
                "context_switches": round(context_switches),
                "interrupts": round(interrupts),
                "cpu_frequency": round(frequency.current, 0) if frequency else None,
                "memory_used": round(memory.used / (1024**3), 2),
                "memory_total": round(memory.total / (1024**3), 2),
                "memory_percent": memory.percent,
                "memory_available": round(memory.available / 1024**3, 2),
                "memory_cached": round(memory.cached / 1024**3, 2)
                if hasattr(memory, "cached")
                else None,
                "swap_used": round(swap.used / (1024**3), 2),
                "swap_total": round(swap.total / (1024**3), 2),
                "uptime": round(time.time() - self.started),
                "process_count": len(processes),
                "network": network_rates,
                "disk": disk_rates,
                "volumes": self.volumes,
                "thread_count": sum(item["threads"] or 0 for item in processes),
            },
            "hardware": hardware,
            "gpus": gpus,
            "gpu_error": gpu_error,
            "gpu_attribution_error": self.gpu_monitor.error,
            "process_sampling": {
                "updated": process_data["updated"], "error": process_data["error"],
                "interval_seconds": ProcessMonitor.INTERVAL,
            },
            "processes": sorted(
                processes,
                key=lambda item: (
                    item["gpu_utilization"],
                    item["gpu_memory"],
                    item["cpu"],
                ),
                reverse=True,
            ),
            "environments": sorted(
                environments.values(),
                key=lambda item: (
                    item["gpu_utilization"],
                    item["gpu_memory"],
                    item["cpu"],
                    item["memory_mb"],
                ),
                reverse=True,
            ),
        }


class PowerHistory:
    def __init__(self) -> None:
        self.energy_wh = 0.0
        self.measured_seconds = 0.0
        self._history: deque[dict[str, Any]] = deque(maxlen=300)
        self.previous: tuple[float, float] | None = None

    def clear(self) -> None:
        self._history.clear()
        self.energy_wh = 0.0
        self.measured_seconds = 0.0
        self.previous = None

    def record(
        self, timestamp: float, monotonic: float, gpus: list[dict[str, Any]]
    ) -> dict[str, Any]:
        readings = [gpu["power"] for gpu in gpus]
        watts = (
            sum(readings)
            if readings
            and all(
                value is not None and math.isfinite(value) and value >= 0
                for value in readings
            )
            else None
        )
        if self.previous is not None and watts is not None:
            previous_time, previous_watts = self.previous
            elapsed = monotonic - previous_time
            # Never interpolate across suspended or unmonitored intervals.
            if 0 < elapsed <= 10:
                self.energy_wh += (previous_watts + watts) / 2 * elapsed / 3600
                self.measured_seconds += elapsed
        self.previous = (monotonic, watts) if watts is not None else None
        self._history.append({
            "timestamp": timestamp, "watts": watts, "energy_wh": self.energy_wh,
        })
        return {
            "gpu_watts": watts,
            "gpu_energy_wh": self.energy_wh,
            "measured_seconds": self.measured_seconds,
            "computer_watts": None,
            "computer_energy_wh": None,
            "history": list(self._history),
        }


def parse_copilot_usage(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned an invalid Copilot response")
    snapshots = payload.get("quota_snapshots")
    quota = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(quota, dict):
        raise ValueError("GitHub did not return a Copilot premium quota for this account")

    def numeric(field: str) -> float | None:
        value = quota.get(field)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"GitHub returned an invalid {field}")
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"GitHub returned an invalid {field}") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"GitHub returned an invalid {field}")
        return number

    unlimited = quota.get("unlimited")
    if not isinstance(unlimited, bool):
        raise ValueError("GitHub did not specify whether the Copilot quota is unlimited")
    token_billing = payload.get("token_based_billing", False)
    if not isinstance(token_billing, bool):
        raise ValueError("GitHub did not specify the Copilot billing unit")
    remaining_percent = numeric("percent_remaining")
    total = numeric("entitlement")
    remaining = numeric("quota_remaining")
    used_percent = None
    used = None
    if unlimited:
        if quota.get("has_quota") is not False:
            used = numeric("credits_used")
        total = None
    elif total == 0:
        raise ValueError("This account has no allocated Copilot premium allowance")
    else:
        if remaining_percent is None or remaining_percent > 100:
            raise ValueError("GitHub did not return a valid percentage remaining")
        used_percent = 100 - remaining_percent
        if total is not None:
            if remaining is not None and remaining > total:
                raise ValueError("GitHub returned a remaining quota greater than its allowance")
            used = total - remaining if remaining is not None else total * used_percent / 100
    return {
        "plan": payload.get("copilot_plan"),
        "unit": "AI credits" if token_billing else "premium requests",
        "used_percent": used_percent,
        "used": used,
        "total": total,
        "unlimited": unlimited,
        "reset_at": numeric("quota_reset_at"),
        "reset_date": payload.get("quota_reset_date_utc") or payload.get("quota_reset_date"),
    }


def read_copilot_usage() -> dict[str, Any]:
    token = next(
        (os.environ[name].strip() for name in ("COPILOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
         if os.environ.get(name, "").strip()),
        None,
    )
    if token is None:
        gh = shutil.which("gh")
        if not gh:
            raise ValueError("Use Sign in with GitHub in this box, or set COPILOT_TOKEN (optional CLI: gh auth login)")
        result = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise ValueError("GitHub CLI is not signed in to github.com; run gh auth login")
        token = result.stdout.strip()
    return fetch_copilot_usage(token)


def fetch_copilot_usage(token: str) -> dict[str, Any]:
    if any(not 33 <= ord(character) <= 126 for character in token):
        raise ValueError("Configured GitHub token contains invalid characters")
    request = Request(
        "https://api.github.com/copilot_internal/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Telemetry",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            return parse_copilot_usage(json.load(response))
    except HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After")
        error.close()
        message = {
            401: "GitHub authentication expired or is invalid; sign in again",
            403: "GitHub denied Copilot quota access; check token permissions or rate limits",
            404: "Copilot usage is unavailable for this account or token",
            429: "GitHub rate limited Copilot usage requests",
        }.get(status, f"GitHub Copilot usage request failed (HTTP {status})")
        if retry_after:
            message += f"; Retry-After: {retry_after}"
        raise ValueError(message) from None
    except (URLError, OSError) as error:
        # Do not include request headers or credential-provider output in errors.
        raise ValueError(f"Cannot reach GitHub Copilot usage ({type(error).__name__})") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("GitHub returned an unreadable Copilot usage response") from None


class CopilotUsage:
    MIN_INTERVAL = 60

    def __init__(self) -> None:
        self.auth = GitHubDeviceAuth()
        self._auth_revision = 0
        self._lock = threading.Lock()
        self._next_poll = 0.0
        self._latest: dict[str, Any] | None = None
        self._updated: float | None = None
        self._error: str | None = None

    def snapshot(self) -> tuple[dict[str, Any], int]:
        # A shared cache also prevents multiple tabs from multiplying GitHub requests.
        with self._lock:
            revision, token = self.auth.credentials()
            if revision != self._auth_revision:
                self._auth_revision = revision
                self._latest = None
                self._updated = None
                self._error = None
                self._next_poll = 0.0
            if time.monotonic() >= self._next_poll:
                try:
                    self._latest = fetch_copilot_usage(token) if token else read_copilot_usage()
                    self._updated = time.time()
                    self._error = None
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    self._error = str(error)
                    print(f"Copilot usage polling failed: {error}", flush=True)
                self._next_poll = time.monotonic() + self.MIN_INTERVAL
                if self.auth.credentials()[0] != revision:
                    self._latest = None
                    self._updated = None
                    self._error = "GitHub sign-in changed during the request; retry usage."
                    self._next_poll = 0.0
            return {
                "usage": self._latest,
                "updated": self._updated,
                "error": self._error,
                "stale": self._error is not None and self._latest is not None,
                "min_interval_seconds": self.MIN_INTERVAL,
                "auth": self.auth.snapshot(),
            }, 503 if self._error else 200


class TelemetryService:
    def __init__(self, collector: StatsCollector, power: PowerHistory) -> None:
        self.collector = collector
        self.power = power
        self.copilot = CopilotUsage()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: dict[str, Any] | None = None
        self._error: str | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=300)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                data = self.collector.collect()
                self._publish(data)
            except (
                OSError,
                psutil.Error,
                subprocess.SubprocessError,
            ) as error:
                print(f"Telemetry sampling failed: {error}", flush=True)
                with self._lock:
                    self._error = str(error)
            self._stop.wait(max(0, 2 - (time.monotonic() - started)))

    def _publish(self, data: dict[str, Any]) -> None:
        with self._lock:
            data["power"] = self.power.record(
                data["timestamp"], time.monotonic(), data["gpus"]
            )
            system = data["system"]
            self._history.append(
                {
                    "timestamp": data["timestamp"],
                    "cpu": system["cpu_percent"],
                    "ram": system["memory_percent"],
                    "cpu_temperature": system["cpu_temperature"],
                    "swap": system["swap_used"] / system["swap_total"] * 100
                    if system["swap_total"]
                    else None,
                    "read": system["disk"]["read"] / 1024**2,
                    "write": system["disk"]["write"] / 1024**2,
                    "received": system["network"]["received"] / 1024**2,
                    "sent": system["network"]["sent"] / 1024**2,
                    "gpus": [
                        {
                            "index": gpu["index"],
                            "gpu": gpu["utilization"],
                            "vram": gpu["memory_used"] / gpu["memory_total"] * 100
                            if gpu["memory_used"] is not None
                            and gpu["memory_total"]
                            else None,
                            "temperature": gpu["temperature"],
                            "fan": gpu["fan_speed"],
                        }
                        for gpu in data["gpus"]
                    ],
                }
            )
            data["history"] = list(self._history)
            self._latest = data
            self._error = None

    def _cache_info(self) -> dict[str, int]:
        seen: set[int] = set()

        def size(value: Any) -> int:
            if id(value) in seen:
                return 0
            seen.add(id(value))
            total = sys.getsizeof(value)
            if isinstance(value, dict):
                total += sum(size(key) + size(item) for key, item in value.items())
            elif isinstance(value, (list, tuple, deque)):
                total += sum(size(item) for item in value)
            return total

        roots = [self._history, self.power._history, self.power.previous]
        if self._latest is not None:
            roots.extend([self._latest["history"], self._latest["power"]])
        return {
            "estimated_bytes": size(roots) if self._history or self.power._history else 0,
            "samples": len(self._history),
            "power_samples": len(self.power._history),
        }

    def clear_cache(self) -> dict[str, Any]:
        with self._lock:
            self._history.clear()
            self.power.clear()
            if self._latest is not None:
                self._latest = {
                    **self._latest,
                    "history": [],
                    "power": {
                        **self._latest["power"],
                        "history": [],
                        "gpu_energy_wh": 0.0,
                        "measured_seconds": 0.0,
                    },
                }
            return {**(self._latest or {}), "cache": self._cache_info()}

    def snapshot(self) -> tuple[dict[str, Any], int]:
        with self._lock:
            if self._thread.ident is not None and not self._thread.is_alive() and not self._stop.is_set():
                return {"error": "Telemetry sampler stopped; restart the dashboard"}, 503
            if self._error:
                return {"error": f"Telemetry sampling failed: {self._error}"}, 503
            if self._latest is None:
                return {"error": "Telemetry is warming up; retry shortly"}, 503
            return {**self._latest, "cache": self._cache_info()}, 200

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=15)
        self.collector.gpu_monitor.close()
        self.collector.hardware_monitor.close()
        self.collector.process_monitor.close()


def make_handler(service: TelemetryService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path in {
                "/api/copilot/auth/start", "/api/copilot/auth/poll",
                "/api/copilot/auth/disconnect",
            }:
                self.handle_github_auth(path)
                return
            if path != "/api/cache/clear":
                self.send_error(404, "Not found")
                return
            # Require a non-simple request so other sites cannot reset the session.
            if self.headers.get("X-Telemetry-Request") != "clear-cache":
                self.send_json({"error": "Missing cache-clear request header"}, 403)
                return
            self.send_json(service.clear_cache())

        def github_auth_allowed(self) -> bool:
            try:
                hostname = urlparse(f"http://{self.headers.get('Host', '')}").hostname
                local_host = hostname == "localhost" or (
                    hostname is not None and ipaddress.ip_address(hostname).is_loopback
                )
                allowed = (
                    local_host
                    and ipaddress.ip_address(self.client_address[0]).is_loopback
                    and self.headers.get("X-Telemetry-Request") == "github-auth"
                    and self.headers.get("Origin") in (None, f"http://{self.headers.get('Host')}")
                )
            except ValueError:
                allowed = False
            if not allowed:
                self.send_json({"error": "GitHub sign-in requires a same-origin localhost request"}, 403)
            return allowed

        def handle_github_auth(self, path: str) -> None:
            if not self.github_auth_allowed():
                return
            auth = service.copilot.auth
            try:
                if path.endswith("/start"):
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048 or self.headers.get_content_type() != "application/json":
                        raise ValueError("Supply a JSON Client ID request of at most 2048 bytes")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not isinstance(payload.get("client_id"), str):
                        raise ValueError("Supply a public OAuth app Client ID")
                    data = auth.start(payload["client_id"])
                elif path.endswith("/poll"):
                    data = auth.snapshot(poll=True)
                else:
                    data = auth.disconnect()
            except (ValueError, UnicodeError):
                self.send_json({"error": "Invalid sign-in request. Check the public Client ID and cancel any pending sign-in before retrying."}, 400)
                return
            self.send_json(data, 502 if data["state"] == "error" else 200)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/copilot/auth":
                if self.github_auth_allowed():
                    self.send_json(service.copilot.auth.snapshot())
                return
            if path == "/api/copilot":
                data, status = service.copilot.snapshot()
                self.send_json(data, status)
                return
            if path == "/api/stats":
                data, status = service.snapshot()
                self.send_json(data, status)
                return
            if path in {"/", "/index.html", "/favicon.ico"}:
                is_icon = path == "/favicon.ico"
                page = (
                    ROOT / "assets" / "telemetry.ico"
                    if is_icon
                    else ROOT / "static" / "index.html"
                )
                if not page.is_file():
                    self.send_error(
                        404,
                        "App icon missing; run build-app.ps1"
                        if is_icon
                        else "Dashboard page not found",
                    )
                    return
                payload = page.read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "image/x-icon" if is_icon else "text/html; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(404, "Not found")

        def send_json(self, data: dict[str, Any], status: int = 200) -> None:
            payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.log_date_time_string()} {format % args}")

    return Handler


class TelemetryServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telemetry hardware dashboard")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind address (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=6767, help="Local port (default: 6767)"
    )
    args = parser.parse_args()
    collector = StatsCollector()
    service = TelemetryService(collector, PowerHistory())
    try:
        server = TelemetryServer((args.host, args.port), make_handler(service))
    except OSError as error:
        collector.gpu_monitor.close()
        collector.hardware_monitor.close()
        collector.process_monitor.close()
        parser.exit(
            1, f"Unable to start dashboard on {args.host}:{args.port}: {error}\n"
        )
    service.start()
    print(f"Telemetry dashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
