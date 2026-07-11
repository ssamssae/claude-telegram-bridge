#!/usr/bin/env python3
"""Watch and recover the local Claude Telegram Bridge service."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Callable


APP_NAME = "claude-telegram-bridge"
SERVICE_NAME = f"{APP_NAME}.service"
LAUNCHD_LABEL = "com.user.claude-telegram-bridge"
RunCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]


def default_status_file() -> Path:
    return Path.home() / ".local" / "state" / APP_NAME / "watchdog.status"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


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


def watch_once(
    *,
    os_name: str | None = None,
    service_name: str = SERVICE_NAME,
    launchd_label: str = LAUNCHD_LABEL,
    launchd_plist: Path | None = None,
    status_file: Path | None = None,
    run: RunCommand = run_command,
) -> int:
    os_name = os_name or platform.system()
    status_file = status_file or default_status_file()
    if os_name == "Linux":
        return watch_linux(service_name, status_file, run)
    if os_name == "Darwin":
        plist = launchd_plist or Path.home() / "Library" / "LaunchAgents" / f"{launchd_label}.plist"
        return watch_macos(launchd_label, plist, status_file, run)
    write_status(status_file, "unsupported", os_name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover a stopped Claude Telegram Bridge service")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--launchd-label", default=LAUNCHD_LABEL)
    parser.add_argument("--launchd-plist", type=Path)
    parser.add_argument("--status-file", type=Path, default=default_status_file())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return watch_once(
        service_name=args.service_name,
        launchd_label=args.launchd_label,
        launchd_plist=args.launchd_plist,
        status_file=args.status_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
