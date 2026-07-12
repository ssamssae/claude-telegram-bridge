#!/usr/bin/env python3
"""Watch and recover the local Claude Telegram Bridge service."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


APP_NAME = "claude-telegram-bridge"
SERVICE_NAME = f"{APP_NAME}.service"
LAUNCHD_LABEL = "com.user.claude-telegram-bridge"
RunCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]
NativeHostProbe = Callable[[], dict[str, object]]
BridgeRunning = Callable[[RunCommand], bool]


def default_status_file() -> Path:
    return Path.home() / ".local" / "state" / APP_NAME / "watchdog.status"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def status_text(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or proc.stderr or "").strip()


def probe_native_host(run: RunCommand = run_command) -> dict[str, object]:
    proc = run(
        [sys.executable or "python", "-m", "bridge_setup", "run", "--health-check"]
    )
    raw = status_text(proc)
    try:
        payload = json.loads(raw.splitlines()[-1]) if raw else {}
    except (IndexError, json.JSONDecodeError):
        payload = {}
    if proc.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True:
        return payload
    error_code = payload.get("error_code") if isinstance(payload, dict) else None
    return {"ok": False, "error_code": str(error_code or "native_host_unavailable")}


WINDOWS_BRIDGE_PROCESS_PATTERN = (
    r"claude_telegram_bridge\.py|(?:^|\s)-m\s+claude_telegram_bridge\b|"
    r"(?:^|\s)-m\s+bridge_setup\s+run\b|"
    r"claude-telegram-bridge(?:\.exe)?\s+run\b"
)


def windows_bridge_process_running(run: RunCommand) -> bool:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$p = Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        f"$_.CommandLine -match '{WINDOWS_BRIDGE_PROCESS_PATTERN}' "
        "}; if ($p) { 'running'; exit 0 } else { 'missing'; exit 1 }"
    )
    proc = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    return proc.returncode == 0


def windows_bridge_start_command() -> list[str]:
    executable = Path(sys.executable or "python.exe")
    pythonw = executable.with_name("pythonw.exe")
    runner = str(pythonw) if pythonw.is_file() else (shutil.which("pythonw.exe") or str(executable))
    return [runner, "-m", "bridge_setup", "run"]


def write_status(path: Path, status: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ts={time.strftime('%Y-%m-%d %H:%M:%S %z')}\nstatus={status}\ndetail={detail}\n",
        encoding="utf-8",
    )


def watch_linux(service: str, status_file: Path, run: RunCommand) -> int:
    probe = run(["systemctl", "--user", "is-active", service])
    if probe.returncode == 0 and (probe.stdout or "").strip() == "active":
        write_status(status_file, "active", f"systemd:{service}")
        return 0
    run(["systemctl", "--user", "start", service])
    probe = run(["systemctl", "--user", "is-active", service])
    if probe.returncode == 0 and (probe.stdout or "").strip() == "active":
        write_status(status_file, "recovered", f"systemd:{service}")
        return 0
    write_status(status_file, "failed", f"systemd:{service}")
    return 1


def watch_macos(label: str, plist: Path, status_file: Path, run: RunCommand) -> int:
    domain = f"gui/{os.getuid()}"
    probe = run(["launchctl", "print", f"{domain}/{label}"])
    if probe.returncode == 0 and "state = running" in (probe.stdout or ""):
        write_status(status_file, "active", f"launchd:{label}")
        return 0
    if probe.returncode != 0 and plist.exists():
        run(["launchctl", "bootstrap", domain, str(plist)])
    run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    probe = run(["launchctl", "print", f"{domain}/{label}"])
    if probe.returncode == 0 and "state = running" in (probe.stdout or ""):
        write_status(status_file, "recovered", f"launchd:{label}")
        return 0
    write_status(status_file, "failed", f"launchd:{label}")
    return 1


def watch_windows(
    status_file: Path,
    run: RunCommand,
    *,
    native_host_probe: NativeHostProbe,
    bridge_running: BridgeRunning,
    settle_seconds: float,
) -> int:
    host = native_host_probe()
    if host.get("ok") is not True:
        detail = str(host.get("error_code") or "native_host_unavailable")
        write_status(status_file, "host-down", detail)
        return 1
    if bridge_running(run):
        write_status(status_file, "active", "windows-native-bridge")
        return 0
    start = run(windows_bridge_start_command())
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    if start.returncode == 0 and bridge_running(run):
        write_status(status_file, "recovered", "windows-native-bridge")
        return 0
    write_status(status_file, "failed", status_text(start) or "windows-native-bridge:start-failed")
    return 1


def watch_once(
    *,
    os_name: str | None = None,
    service_name: str = SERVICE_NAME,
    launchd_label: str = LAUNCHD_LABEL,
    launchd_plist: Path | None = None,
    status_file: Path | None = None,
    run: RunCommand = run_command,
    native_host_probe: NativeHostProbe | None = None,
    bridge_running: BridgeRunning = windows_bridge_process_running,
    settle_seconds: float = 2.0,
) -> int:
    os_name = os_name or platform.system()
    status_file = status_file or default_status_file()
    if os_name == "Linux":
        return watch_linux(service_name, status_file, run)
    if os_name == "Darwin":
        plist = launchd_plist or Path.home() / "Library" / "LaunchAgents" / f"{launchd_label}.plist"
        return watch_macos(launchd_label, plist, status_file, run)
    if os_name == "Windows":
        return watch_windows(
            status_file,
            run,
            native_host_probe=native_host_probe or (lambda: probe_native_host(run)),
            bridge_running=bridge_running,
            settle_seconds=settle_seconds,
        )
    write_status(status_file, "unsupported", os_name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover a stopped Claude Telegram Bridge service")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--launchd-label", default=LAUNCHD_LABEL)
    parser.add_argument("--launchd-plist", type=Path)
    parser.add_argument("--status-file", type=Path, default=default_status_file())
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return watch_once(
        service_name=args.service_name,
        launchd_label=args.launchd_label,
        launchd_plist=args.launchd_plist,
        status_file=args.status_file,
        settle_seconds=args.settle_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
