#!/usr/bin/env python3
"""Setup, doctor, and uninstall helper for Claude Telegram Bridge."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import hashlib
import json
import ntpath
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


APP_NAME = "claude-telegram-bridge"
SERVICE_NAME = f"{APP_NAME}.service"
WATCHDOG_SERVICE_NAME = f"{APP_NAME}-watchdog.service"
WATCHDOG_TIMER_NAME = f"{APP_NAME}-watchdog.timer"
LAUNCHD_LABEL = "com.user.claude-telegram-bridge"
WATCHDOG_LAUNCHD_LABEL = "com.user.claude-telegram-bridge-watchdog"
REPO_DIR = Path(__file__).resolve().parent
BRIDGE_SCRIPT = REPO_DIR / "claude_telegram_bridge.py"
WATCHDOG_SCRIPT = REPO_DIR / "bridge_watchdog.py"
SETUP_TOTAL_STEPS = 6
NATIVE_WINDOWS_GUIDANCE = (
    "Native Windows tmux mode is unsupported: Claude cannot run the .sh SessionStart hook. "
    "Run the default setup inside WSL, or explicitly choose --transport conpty."
)
NATIVE_WINDOWS_CONPTY_GUIDANCE = (
    "Experimental native Windows ConPTY mode uses an owned foreground host. "
    "The host launches Claude itself and cannot attach to an existing Claude window."
)


class SetupError(RuntimeError):
    """Expected setup error shown without a traceback."""


from bridge_i18n import translate


@dataclass(frozen=True)
class SettingsChange:
    changed: bool
    backup: Path | None


@dataclass(frozen=True)
class SetupOptions:
    config_file: Path
    token_file: Path
    registry_file: Path
    settings_file: Path
    hook_file: Path
    runner_file: Path
    state_dir: Path
    token: str | None
    chat_id: str | None
    wait_timeout: int
    install_service: bool
    start_service: bool
    send_test: bool
    non_interactive: bool
    yes: bool
    transport: str = "tmux"
    conpty_state_path: Path | None = None
    create_session: bool = False
    language: str = "en"
    like_author: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class HookSpec:
    """One Claude Code hook the public bridge ships: file name, event, optional matcher."""

    name: str
    event: str
    matcher: str | None = None
    timeout: int | None = None


HOOK_BASE_URL = "https://raw.githubusercontent.com/ssamssae/claude-telegram-bridge/main/hooks/"
SESSION_START_HOOK_NAME = "claude-telegram-bridge-session-start.sh"
# Every hook the public repository ships. The plain wizard registers only the
# first one; the author preset registers all of them (each is self-filtering:
# the AskUserQuestion sidecar only reacts to that tool, the egress guard only
# to the Telegram MCP reply tool, so idle registrations cost nothing).
PUBLIC_HOOKS: tuple[HookSpec, ...] = (
    HookSpec(SESSION_START_HOOK_NAME, "SessionStart"),
    HookSpec("claude-askq-sidecar.sh", "PreToolUse", "AskUserQuestion", 5),
    HookSpec("claude-telegram-bridge-pretool-block.sh", "PreToolUse", "mcp__plugin_telegram_telegram__reply", 5),
)
# "Like the author": the switches that make a fresh install behave like the
# maintainer's own bridge. All four default to off in config.example.env.
AUTHOR_PRESET_ENV: tuple[tuple[str, str], ...] = (
    ("CLB_BUSY_INJECT", "1"),
    ("CLB_FLOW_MIRROR", "1"),
    ("CLB_ASKUSER_CARDS_ENABLED", "1"),
    ("CLB_SUGGESTED_LOOP", "1"),
)
AUTHOR_PRESET_QUESTION = "Use the author's setup (recommended)? [Y/n] "
ATTACH_COMMAND = "claude-tg"
SELF_CHECK_PHONE_TIMEOUT = 60.0
SELF_CHECK_TERMINAL_TIMEOUT = 90.0
SELF_CHECK_PROBE = "Reply with the single word OK. (bridge setup self-check)"
TERMINAL_MIRROR_MARKERS = ("sent ambient directive", "sent ambient flow", "sent ambient final")


ApiCall = Callable[..., dict[str, Any] | None]
RunCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]
HookLoader = Callable[[], str]
HookSource = Callable[[str], str]


def out(message: str = "") -> None:
    print(message, flush=True)


def ok(message: str) -> None:
    out(f"[ok] {message}")


def warn(message: str) -> None:
    out(f"[warn] {message}")


def fail(message: str) -> None:
    out(f"[fail] {message}")


def setup_step(number: int, title: str) -> None:
    out("")
    out(f"[{number}/{SETUP_TOTAL_STEPS}] {title}")


def setup_note(message: str) -> None:
    out(f"    {message}")


def expand_path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def is_native_windows(os_name: str | None = None) -> bool:
    if os_name is not None:
        return os_name == "Windows"
    return platform.system() == "Windows" or os.name == "nt" or sys.platform == "win32"


def show_native_windows_guidance() -> bool:
    if not is_native_windows():
        return False
    warn(NATIVE_WINDOWS_GUIDANCE)
    return True


def validate_setup_transport(mode: str, *, os_name: str | None = None) -> str:
    normalized = (mode or "tmux").strip().lower()
    native_windows = is_native_windows(os_name)
    if normalized == "tmux":
        if native_windows:
            raise SetupError(NATIVE_WINDOWS_GUIDANCE)
        return normalized
    if normalized == "conpty":
        if not native_windows:
            raise SetupError("CLB_REPL_TRANSPORT=conpty requires native Windows")
        return normalized
    raise SetupError(f"unsupported CLB_REPL_TRANSPORT: {mode!r}")


def show_cli_path_fallback(command: str) -> bool:
    if not is_native_windows() or shutil.which("claude-telegram-bridge"):
        return False
    warn(f"claude-telegram-bridge is not on PATH; use: py -m bridge_setup {command}")
    return True


def default_config_dir() -> Path:
    return Path.home() / ".config" / APP_NAME


def default_config_file() -> Path:
    return default_config_dir() / "bridge.env"


def default_token_file() -> Path:
    return default_config_dir() / "token.json"


def default_registry_file() -> Path:
    return default_config_dir() / "token-registry.json"


def default_hook_file() -> Path:
    return default_config_dir() / "claude-telegram-bridge-session-start.sh"


def default_settings_file() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_runner_file() -> Path:
    return Path.home() / ".local" / "bin" / f"{APP_NAME}-run"


def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / APP_NAME


def default_conpty_state_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or default_state_dir())
    if os.environ.get("LOCALAPPDATA"):
        return root / APP_NAME / "native-repl-host.json"
    return root / "native-repl-host.json"


def default_systemd_unit_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def default_watchdog_systemd_service_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / WATCHDOG_SERVICE_NAME


def default_watchdog_systemd_timer_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / WATCHDOG_TIMER_NAME


def default_launchd_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def default_watchdog_launchd_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{WATCHDOG_LAUNCHD_LABEL}.plist"


def shell_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def write_text_atomic(path: Path, value: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8", newline="\n")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def telegram_call(token: str, method: str, timeout: int = 60, **params: Any) -> dict[str, Any] | None:
    if "timeout_param" in params:
        params["timeout"] = params.pop("timeout_param")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(params).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def validate_bot_token(token: str, api_call: ApiCall = telegram_call) -> str:
    payload = api_call(token, "getMe", timeout=30)
    if not payload or not payload.get("ok"):
        raise SetupError("Telegram token validation failed")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = str(result.get("username") or "").strip()
    if not username:
        raise SetupError("Telegram getMe returned no bot username")
    return username


def current_update_offset(token: str, api_call: ApiCall = telegram_call) -> int:
    payload = api_call(token, "getUpdates", timeout=10, timeout_param=0)
    updates = payload.get("result") if payload and payload.get("ok") else None
    if not isinstance(updates, list):
        return 0
    ids = [item.get("update_id") for item in updates if isinstance(item, dict)]
    valid = [int(item) for item in ids if isinstance(item, int)]
    return max(valid) + 1 if valid else 0


def extract_chat_id(update: dict[str, Any]) -> tuple[str, str] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return None
    chat = message["chat"]
    if chat.get("id") is None:
        return None
    label = " ".join(
        str(chat.get(key) or "").strip() for key in ("first_name", "last_name")
    ).strip()
    if chat.get("username"):
        label = f"{label} (@{chat['username']})".strip()
    return str(chat["id"]), label or str(chat["id"])


def wait_for_chat_id(
    token: str,
    *,
    offset: int = 0,
    timeout_seconds: int = 180,
    api_call: ApiCall = telegram_call,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = api_call(token, "getUpdates", timeout=20, offset=offset, timeout_param=10)
        updates = payload.get("result") if payload and payload.get("ok") else None
        if not isinstance(updates, list):
            time.sleep(2)
            continue
        for update in updates:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("update_id"), int):
                offset = int(update["update_id"]) + 1
            result = extract_chat_id(update)
            if result:
                return result
    raise SetupError("Timed out waiting for /start in the bot chat")


def send_test_message(token: str, chat_id: str, api_call: ApiCall = telegram_call, *, language: str = "en") -> bool:
    payload = api_call(
        token,
        "sendMessage",
        timeout=30,
        chat_id=chat_id,
        text=translate("Claude Telegram Bridge setup complete. Send /ping, then a normal Claude prompt.", language),
    )
    return bool(payload and payload.get("ok"))


def token_registry(token: str) -> dict[str, Any]:
    return {
        "tokens": {
            "default": {
                "token_id": hashlib.sha256(token.encode()).hexdigest()[:16],
                "mode": "polling",
                "owner": APP_NAME,
                "expected_consumer": "claude",
                "allow_delete_webhook": False,
            }
        }
    }


def write_private_config(
    *,
    config_file: Path,
    token_file: Path,
    registry_file: Path,
    state_dir: Path,
    token: str,
    chat_id: str,
    transport: str = "tmux",
    conpty_state_path: Path | None = None,
    language: str = "en",
    like_author: bool = False,
) -> None:
    if language not in {"en", "ko"}:
        raise SetupError("language must be en or ko")
    write_text_atomic(token_file, json.dumps({"token": token}) + "\n", mode=0o600)
    write_text_atomic(registry_file, json.dumps(token_registry(token), indent=2) + "\n", mode=0o600)
    config_lines = [
            "# Claude Telegram Bridge private config",
            f"CLB_TOKEN_FILE={shell_quote(token_file)}",
            f"CLB_TOKEN_REGISTRY={shell_quote(registry_file)}",
            f"CLB_CHAT_ID={shell_quote(chat_id)}",
            f"CLB_STATE_DIR={shell_quote(state_dir)}",
            f"CLB_LANGUAGE={shell_quote(language)}",
            f"CLB_REPL_TRANSPORT={transport}",
            "SUGGESTED_REPLY_BUBBLE=1",
    ]
    if transport == "conpty":
        state_path = conpty_state_path or default_conpty_state_path()
        config_lines.append(f"CLB_CONPTY_STATE_PATH={shell_quote(state_path)}")
    else:
        config_lines.extend(
            [
            "CLB_TMUX_BIN=tmux",
            "CLB_TMUX_SOCKET=default",
            "CLB_TMUX_SESSION=claude",
            ]
        )
    if like_author:
        config_lines.extend(author_preset_lines())
    config_lines.extend(
        [
            "CLB_START_AT_END=1",
            "",
        ]
    )
    content = "\n".join(config_lines)
    write_text_atomic(config_file, content, mode=0o600)


def author_preset_lines() -> list[str]:
    return ["# Author preset (setup --like-author)", *(f"{key}={value}" for key, value in AUTHOR_PRESET_ENV)]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value.strip())
            values[key.strip()] = parts[0] if parts else ""
        except ValueError:
            values[key.strip()] = value.strip().strip("'\"")
    return values


def ensure_setup_tmux_session(
    options: SetupOptions,
    *,
    run: RunCommand = run_command,
) -> tuple[bool, str, str]:
    config = load_env_file(options.config_file)
    tmux_bin = config.get("CLB_TMUX_BIN") or "tmux"
    socket = config.get("CLB_TMUX_SOCKET") or "default"
    session = config.get("CLB_TMUX_SESSION") or "claude"
    target = f"={session}"
    attach_command = shlex.join([tmux_bin, "-L", socket, "attach", "-t", session])
    manual_command = shlex.join([tmux_bin, "-L", socket, "new", "-s", session])

    probe = run([tmux_bin, "-L", socket, "has-session", "-t", target])
    if probe.returncode == 0:
        ok(f"tmux session already running: {socket}/{session}")
        return True, attach_command, manual_command

    if options.non_interactive:
        should_create = options.create_session or options.like_author
    else:
        answer = input("Start the Claude tmux session now? [Y/n] ").strip().lower()
        should_create = answer not in {"n", "no"}
    if not should_create:
        return False, attach_command, manual_command

    claude_bin = shutil.which("claude")
    if not claude_bin:
        warn("Claude CLI not found on PATH; tmux session creation skipped")
        return False, attach_command, manual_command

    started = run(
        [tmux_bin, "-L", socket, "new-session", "-d", "-s", session, claude_bin]
    )
    if started.returncode == 0:
        ok(f"started Claude tmux session: {socket}/{session}")
        return True, attach_command, manual_command
    detail = (started.stderr or started.stdout or f"exit {started.returncode}").strip()
    warn(f"could not start Claude tmux session: {detail}; start it manually")
    return False, attach_command, manual_command


def download_hook(name: str) -> str:
    if name not in {spec.name for spec in PUBLIC_HOOKS}:
        raise SetupError(f"unknown hook: {name}")
    try:
        with urllib.request.urlopen(HOOK_BASE_URL + name, timeout=30) as response:
            text = response.read().decode("utf-8")
    except Exception as exc:
        raise SetupError(f"hook download failed ({name}): {exc}") from exc
    if not text.startswith("#!/") or "claude-telegram-bridge" not in text:
        raise SetupError(f"hook download returned unexpected content ({name})")
    return text


def download_session_start_hook() -> str:
    return download_hook(SESSION_START_HOOK_NAME)


def preset_hook_path(hook_file: Path, spec: HookSpec) -> Path:
    """The SessionStart hook keeps its configured path; the others sit beside it."""
    if spec.name == SESSION_START_HOOK_NAME:
        return hook_file
    return hook_file.with_name(spec.name)


def install_session_start_hook(path: Path, loader: HookLoader = download_session_start_hook) -> None:
    text = loader()
    if not text.endswith("\n"):
        text += "\n"
    write_text_atomic(path, text, mode=0o755)


def load_settings(path: Path) -> tuple[dict[str, Any], int]:
    if not path.exists():
        return {}, 0o600
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Claude settings JSON is invalid; left unchanged: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Claude settings root must be a JSON object: {path}")
    return value, file_mode(path) or 0o600


def normalized_hook_path(value: object) -> tuple[str, str]:
    raw = str(value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1]
    is_windows_path = (
        len(raw) >= 3 and raw[1] == ":" and raw[2] in {"\\", "/"}
    ) or raw.startswith("\\\\")
    if is_windows_path:
        return "windows", ntpath.normcase(ntpath.normpath(raw.replace("/", "\\")))
    expanded = os.path.expandvars(os.path.expanduser(raw))
    return "posix", os.path.normcase(os.path.normpath(os.path.abspath(expanded)))


def command_targets_hook(command: object, hook_file: object) -> bool:
    if not isinstance(command, str):
        return False
    candidates = [command]
    try:
        windows_command = "\\" in command or (len(command) >= 2 and command[1] == ":")
        parts = shlex.split(command, posix=not windows_command)
    except ValueError:
        parts = [command]
    if parts:
        candidates.append(parts[-1])
    target = normalized_hook_path(hook_file)
    return any(normalized_hook_path(candidate) == target for candidate in candidates)


def hook_registered(settings: dict[str, Any], hook_file: Path, event: str = "SessionStart") -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        commands = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(commands, list):
            continue
        for item in commands:
            if isinstance(item, dict) and command_targets_hook(item.get("command"), hook_file):
                return True
    return False


def backup_path(path: Path, stamp: str | None = None) -> Path:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    number = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{number}")
        number += 1
    return candidate


def write_settings_with_backup(
    path: Path,
    settings: dict[str, Any],
    *,
    mode: int,
    stamp: str | None = None,
) -> Path | None:
    backup = None
    if path.exists():
        backup = backup_path(path, stamp)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    write_text_atomic(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n", mode=mode)
    return backup


def merge_hooks(
    settings_file: Path,
    registrations: list[tuple[Path, HookSpec]],
    *,
    stamp: str | None = None,
) -> SettingsChange:
    """Register every (hook file, spec) not yet present; one settings write, one backup."""
    settings, mode = load_settings(settings_file)
    changed = False
    for hook_file, spec in registrations:
        if hook_registered(settings, hook_file, spec.event):
            continue
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise SetupError("Claude settings hooks must be a JSON object; left unchanged")
        entries = hooks.setdefault(spec.event, [])
        if not isinstance(entries, list):
            raise SetupError(f"Claude settings hooks.{spec.event} must be a JSON array; left unchanged")
        command: dict[str, Any] = {
            "type": "command",
            # Must match the normalization command_targets_hook() uses when
            # reading the command back (normalized_hook_path), not Path.resolve().
            # resolve() follows symlinks (e.g. macOS /tmp -> /private/tmp) while
            # normalized_hook_path() intentionally does not, so mixing the two
            # broke idempotent installs/doctor/uninstall on symlinked paths.
            "command": normalized_hook_path(hook_file)[1],
        }
        if spec.timeout is not None:
            command["timeout"] = spec.timeout
        entry: dict[str, Any] = {}
        if spec.matcher:
            entry["matcher"] = spec.matcher
        entry["hooks"] = [command]
        entries.append(entry)
        changed = True
    if not changed:
        return SettingsChange(False, None)
    backup = write_settings_with_backup(settings_file, settings, mode=mode, stamp=stamp)
    return SettingsChange(True, backup)


def merge_hook(
    settings_file: Path,
    hook_file: Path,
    *,
    event: str = "SessionStart",
    matcher: str | None = None,
    timeout: int | None = None,
    stamp: str | None = None,
) -> SettingsChange:
    return merge_hooks(settings_file, [(hook_file, HookSpec(hook_file.name, event, matcher, timeout))], stamp=stamp)


def merge_session_start_hook(
    settings_file: Path,
    hook_file: Path,
    *,
    stamp: str | None = None,
) -> SettingsChange:
    return merge_hook(settings_file, hook_file, event="SessionStart", stamp=stamp)


def remove_hook(
    settings_file: Path,
    hook_file: Path,
    *,
    event: str = "SessionStart",
    stamp: str | None = None,
) -> SettingsChange:
    if not settings_file.exists():
        return SettingsChange(False, None)
    settings, mode = load_settings(settings_file)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(event), list):
        return SettingsChange(False, None)
    changed = False
    kept_entries = []
    for entry in hooks[event]:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept_entries.append(entry)
            continue
        kept_commands = []
        for item in entry["hooks"]:
            if isinstance(item, dict) and command_targets_hook(item.get("command"), hook_file):
                changed = True
            else:
                kept_commands.append(item)
        if kept_commands:
            updated = dict(entry)
            updated["hooks"] = kept_commands
            kept_entries.append(updated)
    if not changed:
        return SettingsChange(False, None)
    if kept_entries:
        hooks[event] = kept_entries
    else:
        hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    backup = write_settings_with_backup(settings_file, settings, mode=mode, stamp=stamp)
    return SettingsChange(True, backup)


def remove_session_start_hook(
    settings_file: Path,
    hook_file: Path,
    *,
    stamp: str | None = None,
) -> SettingsChange:
    return remove_hook(settings_file, hook_file, event="SessionStart", stamp=stamp)


def install_runner(runner_file: Path, config_file: Path) -> None:
    python_bin = sys.executable or shutil.which("python3") or "python3"
    content = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"ENV_FILE={shell_quote(config_file)}",
            'test -f "$ENV_FILE" || { echo "config file missing: $ENV_FILE" >&2; exit 2; }',
            "set -a",
            '. "$ENV_FILE"',
            "set +a",
            f"exec {shell_quote(python_bin)} {shell_quote(BRIDGE_SCRIPT)}",
            "",
        ]
    )
    write_text_atomic(runner_file, content, mode=0o755)


def systemd_unit_content(runner_file: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Claude Telegram Bridge",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={runner_file}",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def watchdog_systemd_service_content() -> str:
    python_bin = sys.executable or shutil.which("python3") or "python3"
    return "\n".join(
        [
            "[Unit]",
            "Description=Claude Telegram Bridge watchdog",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={python_bin} {WATCHDOG_SCRIPT}",
            "",
        ]
    )


def watchdog_systemd_timer_content() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Run Claude Telegram Bridge watchdog",
            "",
            "[Timer]",
            "OnBootSec=30s",
            "OnUnitActiveSec=60s",
            f"Unit={WATCHDOG_SERVICE_NAME}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def launchd_plist_content(label: str, arguments: list[str], *, interval: int | None = None) -> str:
    log_file = Path("/tmp") / f"{label}.log"
    args = "\n".join(f"        <string>{item}</string>" for item in arguments)
    interval_xml = (
        f"    <key>StartInterval</key>\n    <integer>{interval}</integer>\n"
        if interval is not None
        else "    <key>KeepAlive</key>\n    <true/>\n"
    )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            "    <key>Label</key>",
            f"    <string>{label}</string>",
            "    <key>ProgramArguments</key>",
            "    <array>",
            args,
            "    </array>",
            "    <key>RunAtLoad</key>",
            "    <true/>",
            interval_xml.rstrip(),
            "    <key>StandardOutPath</key>",
            f"    <string>{log_file}</string>",
            "    <key>StandardErrorPath</key>",
            f"    <string>{log_file}</string>",
            "</dict>",
            "</plist>",
            "",
        ]
    )


def install_service(
    runner_file: Path,
    *,
    start: bool,
    os_name: str | None = None,
    run: RunCommand = run_command,
) -> Path | None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        path = default_systemd_unit_file()
        write_text_atomic(path, systemd_unit_content(runner_file), mode=0o644)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", SERVICE_NAME])
        if start:
            run(["systemctl", "--user", "restart", SERVICE_NAME])
        return path
    if os_name == "Darwin":
        path = default_launchd_plist_file()
        write_text_atomic(path, launchd_plist_content(LAUNCHD_LABEL, [str(runner_file)]), mode=0o644)
        if start:
            domain = f"gui/{os.getuid()}"
            run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
            run(["launchctl", "bootstrap", domain, str(path)])
        return path
    warn(f"service install is not automated for {os_name}; use {runner_file}")
    return None


def install_watchdog(
    *,
    start: bool,
    os_name: str | None = None,
    run: RunCommand = run_command,
) -> Path | None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        service = default_watchdog_systemd_service_file()
        timer = default_watchdog_systemd_timer_file()
        write_text_atomic(service, watchdog_systemd_service_content(), mode=0o644)
        write_text_atomic(timer, watchdog_systemd_timer_content(), mode=0o644)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", WATCHDOG_TIMER_NAME])
        if start:
            run(["systemctl", "--user", "start", WATCHDOG_TIMER_NAME])
        return timer
    if os_name == "Darwin":
        path = default_watchdog_launchd_plist_file()
        python_bin = sys.executable or shutil.which("python3") or "python3"
        write_text_atomic(
            path,
            launchd_plist_content(
                WATCHDOG_LAUNCHD_LABEL,
                [python_bin, str(WATCHDOG_SCRIPT)],
                interval=60,
            ),
            mode=0o644,
        )
        if start:
            domain = f"gui/{os.getuid()}"
            run(["launchctl", "bootout", f"{domain}/{WATCHDOG_LAUNCHD_LABEL}"])
            run(["launchctl", "bootstrap", domain, str(path)])
        return path
    warn(f"watchdog install is not automated for {os_name}")
    return None


def uninstall_service(*, os_name: str | None = None, run: RunCommand = run_command) -> None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        default_systemd_unit_file().unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
    elif os_name == "Darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        default_launchd_plist_file().unlink(missing_ok=True)


def uninstall_watchdog(*, os_name: str | None = None, run: RunCommand = run_command) -> None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        run(["systemctl", "--user", "disable", "--now", WATCHDOG_TIMER_NAME])
        default_watchdog_systemd_service_file().unlink(missing_ok=True)
        default_watchdog_systemd_timer_file().unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
    elif os_name == "Darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{WATCHDOG_LAUNCHD_LABEL}"])
        default_watchdog_launchd_plist_file().unlink(missing_ok=True)


def service_status(*, os_name: str | None = None, run: RunCommand = run_command) -> str:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        proc = run(["systemctl", "--user", "is-active", SERVICE_NAME])
        return (proc.stdout or proc.stderr or "unknown").strip()
    if os_name == "Darwin":
        proc = run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        return "loaded" if proc.returncode == 0 else "not-installed"
    return "manual"


def watchdog_status(*, os_name: str | None = None, run: RunCommand = run_command) -> str:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        proc = run(["systemctl", "--user", "is-active", WATCHDOG_TIMER_NAME])
        return (proc.stdout or proc.stderr or "unknown").strip()
    if os_name == "Darwin":
        proc = run(["launchctl", "print", f"gui/{os.getuid()}/{WATCHDOG_LAUNCHD_LABEL}"])
        return "loaded" if proc.returncode == 0 else "not-installed"
    return "manual"


def setup_plan_lines(options: SetupOptions) -> list[str]:
    """What `setup --dry-run` would do: files, switches, hooks, session. No I/O."""
    lines = [
        "Dry run: nothing is written and Telegram is not contacted.",
        f"config: {options.config_file}",
        f"token files: {options.token_file}, {options.registry_file}",
        f"state dir: {options.state_dir}",
    ]
    if options.like_author:
        lines.append("author preset switches (bridge.env):")
        lines.extend(f"  {key}={value}" for key, value in AUTHOR_PRESET_ENV)
        lines.append("hooks (downloaded, then merged into Claude settings):")
        for spec in PUBLIC_HOOKS:
            where = f"{spec.event} ({spec.matcher})" if spec.matcher else spec.event
            lines.append(f"  {where}: {preset_hook_path(options.hook_file, spec)}")
    else:
        lines.append("switches: defaults (all optional features off)")
        lines.append(f"hooks: SessionStart: {options.hook_file}")
    lines.append(f"Claude settings merge (backup first): {options.settings_file}")
    lines.append(
        f"runner: {options.runner_file}; service + watchdog: "
        + ("install" if options.install_service else "skip")
    )
    if options.transport == "conpty":
        lines.append("transport: native ConPTY host (no tmux session)")
    else:
        lines.append("tmux session: default/claude, Claude started inside it")
        lines.append(f"attach any time with: {ATTACH_COMMAND}")
    if options.like_author:
        lines.append("self-check after install: bot -> phone, phone -> terminal -> phone, terminal -> phone")
    return lines


def setup_bridge(
    options: SetupOptions,
    *,
    api_call: ApiCall = telegram_call,
    hook_loader: HookLoader = download_session_start_hook,
    hook_source: HookSource = download_hook,
    run: RunCommand = run_command,
) -> int:
    os_name = platform.system()
    transport = validate_setup_transport(options.transport, os_name=os_name)
    native_conpty = os_name == "Windows" and transport == "conpty"
    if options.dry_run:
        out("Claude Telegram Bridge setup (dry run)")
        for line in setup_plan_lines(options):
            out(line)
        return 0
    out("Claude Telegram Bridge setup")
    setup_note(
        "You need a BotFather token, Telegram /start, and Claude Code."
        + ("" if native_conpty else " tmux is also required.")
    )
    if native_conpty:
        warn(NATIVE_WINDOWS_CONPTY_GUIDANCE)
    like_author = options.like_author
    if not like_author and not options.non_interactive and not native_conpty:
        setup_note(
            "The author's setup turns on the flow card with its Stop button, question cards, "
            "busy-time injection and the suggested-reply loop, registers every bridge hook, "
            f"and adds the {ATTACH_COMMAND} command. Answer n to keep every optional switch off."
        )
        like_author = input(AUTHOR_PRESET_QUESTION).strip().lower() not in {"n", "no"}
    if like_author:
        options = dataclasses.replace(options, like_author=True)

    setup_step(1, "Paste and validate the BotFather token")
    token = (options.token or "").strip()
    if not token:
        if options.non_interactive:
            raise SetupError("--token is required in non-interactive mode")
        token = getpass.getpass("BotFather token (hidden): ").strip()
    username = validate_bot_token(token, api_call=api_call)
    ok(f"token valid: @{username}")

    setup_step(2, "Connect your Telegram chat")
    chat_id = (options.chat_id or "").strip()
    if not chat_id:
        if options.non_interactive:
            raise SetupError("--chat-id is required in non-interactive mode")
        offset = current_update_offset(token, api_call=api_call)
        setup_note(f"Open @{username}, send /start, and wait up to {options.wait_timeout}s.")
        chat_id, label = wait_for_chat_id(
            token,
            offset=offset,
            timeout_seconds=options.wait_timeout,
            api_call=api_call,
        )
        ok(f"chat id detected: {chat_id} ({label})")
    else:
        ok(f"chat id configured: {chat_id}")

    setup_step(3, "Write token.json, token-registry.json, and private config")
    if options.config_file.exists() and not options.yes and not options.non_interactive:
        answer = input(f"Overwrite existing config {options.config_file}? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            raise SetupError("setup cancelled")
    if options.like_author and options.config_file.exists():
        previous = backup_path(options.config_file)
        shutil.copy2(options.config_file, previous)
        ok(f"backed up previous config: {previous}")
    write_private_config(
        config_file=options.config_file,
        token_file=options.token_file,
        registry_file=options.registry_file,
        state_dir=options.state_dir,
        token=token,
        chat_id=chat_id,
        transport=transport,
        conpty_state_path=options.conpty_state_path,
        language=options.language,
        like_author=options.like_author,
    )
    ok(f"wrote private config: {options.config_file}")
    if options.like_author:
        ok("author preset on: " + ", ".join(f"{key}={value}" for key, value in AUTHOR_PRESET_ENV))
    ok(f"wrote token files: {options.token_file}, {options.registry_file}")

    setup_step(4, "Install and register the Claude SessionStart hook")
    if native_conpty:
        setup_note("SessionStart hook skipped: the owned host binds Claude JSONL directly.")
    else:
        install_session_start_hook(options.hook_file, loader=hook_loader)
        registrations: list[tuple[Path, HookSpec]] = [(options.hook_file, PUBLIC_HOOKS[0])]
        ok(f"installed SessionStart hook: {options.hook_file}")
        if options.like_author:
            for spec in PUBLIC_HOOKS[1:]:
                hook_path = preset_hook_path(options.hook_file, spec)
                install_session_start_hook(hook_path, loader=lambda name=spec.name: hook_source(name))
                registrations.append((hook_path, spec))
                where = f"{spec.event} ({spec.matcher})" if spec.matcher else spec.event
                ok(f"installed {where} hook: {hook_path}")
        change = merge_hooks(options.settings_file, registrations)
        if change.backup:
            ok(f"backed up Claude settings: {change.backup}")
        ok("Claude settings SessionStart hook registered")
        if options.like_author:
            ok("Claude settings question-card and egress-guard hooks registered")

    setup_step(5, "Install the bridge service and watchdog")
    if native_conpty:
        setup_note("Native host and bridge are not auto-started or installed as services.")
    else:
        install_runner(options.runner_file, options.config_file)
        ok(f"installed runner: {options.runner_file}")
    if options.install_service and not native_conpty:
        installed = install_service(options.runner_file, start=options.start_service)
        watchdog = install_watchdog(start=options.start_service)
        if installed:
            ok(f"installed service: {installed}")
        if watchdog:
            ok(f"installed watchdog: {watchdog}")
    elif not native_conpty:
        warn("service/watchdog install skipped")

    setup_step(6, "Send a setup-complete test message")
    if options.send_test:
        if send_test_message(token, chat_id, api_call=api_call, language=options.language):
            ok("sent setup-complete test message")
        else:
            warn("test message failed; run doctor")
    else:
        setup_note("Test message skipped by option.")

    tmux_ready = False
    attach_command = ""
    manual_command = ""
    if not native_conpty:
        tmux_ready, attach_command, manual_command = ensure_setup_tmux_session(options, run=run)

    out("")
    if native_conpty:
        out("Setup complete. Open two PowerShell windows and run these commands in order:")
        state_path = options.conpty_state_path or default_conpty_state_path()
        out(f"claude-telegram-bridge host --workdir <your-project-directory> --state-path {state_path}")
        out(f"claude-telegram-bridge run --config {options.config_file}")
        out("The host must stay visible; closing it ends the owned Claude session.")
    elif tmux_ready:
        out("Setup complete. Claude tmux session is ready.")
        out(f"Attach with: {attach_command}")
        if options.like_author:
            out(f"From now on, any terminal: {ATTACH_COMMAND}")
        out("Then send /ping to your bot. Run `claude-telegram-bridge doctor` any time.")
    else:
        out(f"Setup complete. Start Claude in: {manual_command}")
        if options.like_author:
            out(f"Or simply run: {ATTACH_COMMAND}")
        out("Then send /ping to your bot. Run `claude-telegram-bridge doctor` any time.")
    if options.like_author and not native_conpty:
        if tmux_ready and options.install_service and options.start_service:
            author_self_check(
                token=token,
                chat_id=chat_id,
                config=load_env_file(options.config_file),
                state_dir=options.state_dir,
                os_name=os_name,
                api_call=api_call,
                run=run,
                bot_message_sent=options.send_test,
            )
        else:
            setup_note("Self-check skipped: it needs the running service and the tmux session.")
    show_cli_path_fallback("setup")
    return 0


def claude_sessions_outside_bridge(pane_tty: str, ps_text: str) -> list[tuple[str, str]]:
    """(pid, tty) of Claude CLI processes whose tty is not the bridge pane's tty.

    `ps_text` is `ps -Ao pid=,tty=,comm=` output; the command is the last field,
    so a wider column set (ppid, ...) still parses. Daemons without a tty and the
    desktop app (command basename is not `claude`) are ignored.
    """
    pane = (pane_tty or "").strip()
    if not pane:
        return []
    pane_name = pane[len("/dev/"):] if pane.startswith("/dev/") else pane
    found: list[tuple[str, str]] = []
    for raw in ps_text.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        pid, tty, command = parts[0], parts[-2], parts[-1]
        if os.path.basename(command) != "claude":
            continue
        if tty in {"?", "??", "-", ""} or tty in {pane_name, pane}:
            continue
        found.append((pid, tty))
    return found


def warn_sessions_outside_bridge(*, run: RunCommand = run_command) -> int:
    pane = run(["tmux", "-L", "default", "display-message", "-p", "-t", "claude", "#{pane_tty}"])
    processes = run(["ps", "-Ao", "pid=,tty=,comm="])
    outside = claude_sessions_outside_bridge(pane.stdout or "", processes.stdout or "")
    if not outside:
        return 0
    listed = ", ".join(f"pid {pid} on {tty}" for pid, tty in outside)
    warn(
        f"a Claude session is running outside the bridge tmux session ({listed}); "
        f"use {ATTACH_COMMAND} so your phone and terminal share one session"
    )
    return 1


def attach_command(
    config_file: Path,
    *,
    run: RunCommand = run_command,
    exec_fn: Callable[[str, list[str]], Any] = os.execvp,
    create_missing: bool = True,
) -> int:
    """Attach this terminal to the Claude session the bridge controls (`claude-tg`)."""
    config = load_env_file(config_file)
    if (config.get("CLB_REPL_TRANSPORT") or "tmux").strip().lower() == "conpty":
        fail("attach works with the tmux transport; the native host owns its own window")
        return 2
    tmux_bin = config.get("CLB_TMUX_BIN") or "tmux"
    socket = config.get("CLB_TMUX_SOCKET") or "default"
    session = config.get("CLB_TMUX_SESSION") or "claude"
    probe = run([tmux_bin, "-L", socket, "has-session", "-t", f"={session}"])
    if probe.returncode != 0:
        if not create_missing:
            fail(f"tmux session missing: {socket}/{session}")
            return 2
        claude_bin = shutil.which("claude")
        if not claude_bin:
            fail(f"Claude CLI not found on PATH; install Claude Code, then run {ATTACH_COMMAND} again")
            return 2
        started = run([tmux_bin, "-L", socket, "new-session", "-d", "-s", session, claude_bin])
        if started.returncode != 0:
            detail = (started.stderr or started.stdout or f"exit {started.returncode}").strip()
            fail(f"could not start Claude tmux session: {detail}")
            return 2
        ok(f"started Claude tmux session: {socket}/{session}")
    clients = run([tmux_bin, "-L", socket, "list-clients", "-t", f"={session}"])
    if (clients.stdout or "").strip():
        setup_note(
            "another terminal is already attached; this one joins it. The pane keeps the "
            "smaller size until the other terminal detaches (Ctrl+B then D)."
        )
    exec_fn(tmux_bin, [tmux_bin, "-L", socket, "attach", "-t", session])
    return 0


def attach_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=ATTACH_COMMAND,
        description="Attach this terminal to the Claude session the bridge controls",
    )
    parser.add_argument("--config", type=expand_path, default=default_config_file())
    parser.add_argument("--no-create", action="store_true", help="fail instead of starting a missing session")
    args = parser.parse_args(argv)
    try:
        return attach_command(args.config, create_missing=not args.no_create)
    except SetupError as exc:
        fail(str(exc))
        return 2


def queue_records_since(path: Path, since: float) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            stamp = float(record.get("ts") or 0.0)
        except (TypeError, ValueError):
            stamp = 0.0
        if stamp >= since:
            records.append(record)
    return records


class BridgeLogTail:
    """New bridge log text since construction: launchd log file on macOS, journal on Linux."""

    def __init__(self, os_name: str, *, run: RunCommand = run_command, since: float | None = None) -> None:
        self.os_name = os_name
        self.run = run
        self.since = time.time() if since is None else since
        self.path = Path("/tmp") / f"{LAUNCHD_LABEL}.log" if os_name == "Darwin" else None
        self.offset = self.path.stat().st_size if self.path is not None and self.path.exists() else 0

    def read_new(self) -> str:
        if self.path is not None:
            if not self.path.exists():
                return ""
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
            self.offset += len(data)
            return data.decode("utf-8", "replace")
        if self.os_name == "Linux":
            proc = self.run(
                ["journalctl", "--user", "-u", SERVICE_NAME, "--since", f"@{int(self.since)}", "-o", "cat", "--no-pager"]
            )
            return proc.stdout or ""
        return ""


def author_self_check(
    *,
    token: str,
    chat_id: str,
    config: dict[str, str],
    state_dir: Path,
    os_name: str,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    log_tail: Any = None,
    phone_timeout: float = SELF_CHECK_PHONE_TIMEOUT,
    terminal_timeout: float = SELF_CHECK_TERMINAL_TIMEOUT,
    bot_message_sent: bool = True,
) -> list[str]:
    """Three live checks after an author-preset install; results go to the terminal and the phone."""
    out("")
    out("Self-check: bot -> phone, phone -> terminal -> phone, terminal -> phone")
    lines = [
        "1/3 bot -> phone: ok (setup message delivered)"
        if bot_message_sent
        else "1/3 bot -> phone: skipped (test message disabled)"
    ]
    node = config.get("CLB_NODE") or "claude"
    queue_path = Path(config.get("CLB_QUEUE_PATH") or state_dir / f"claude-telegram-bridge-{node}.queue.jsonl").expanduser()
    tail = log_tail if log_tail is not None else BridgeLogTail(os_name, run=run, since=clock())

    started = clock()
    api_call(
        token,
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"Self-check 2/3: send any text to this bot within {int(phone_timeout)} s. "
            "It should appear in the Claude terminal and the answer should come back here."
        ),
    )
    setup_note(f"waiting up to {int(phone_timeout)}s for a message from your phone ...")
    arrived = answered = False
    while clock() - started < phone_timeout:
        records = queue_records_since(queue_path, started)
        arrived = arrived or any(record.get("status") == "injected" for record in records)
        answered = any(record.get("status") == "sent" for record in records)
        if answered:
            break
        sleep(2)
    if answered:
        lines.append("2/3 phone -> terminal -> phone: ok")
    elif arrived:
        lines.append(
            "2/3 phone -> terminal -> phone: the message reached the terminal but no answer came back "
            "in time; look at the Claude session, then run `claude-telegram-bridge doctor`"
        )
    else:
        lines.append(
            f"2/3 phone -> terminal -> phone: no phone message within {int(phone_timeout)} s; "
            "send any text to the bot later and run `claude-telegram-bridge doctor`"
        )

    tmux_bin = config.get("CLB_TMUX_BIN") or "tmux"
    socket = config.get("CLB_TMUX_SOCKET") or "default"
    session = config.get("CLB_TMUX_SESSION") or "claude"
    target = f"={session}"
    run([tmux_bin, "-L", socket, "send-keys", "-t", target, "-l", SELF_CHECK_PROBE])
    run([tmux_bin, "-L", socket, "send-keys", "-t", target, "Enter"])
    setup_note(f"typed a probe into the Claude session; waiting up to {int(terminal_timeout)}s for its phone card ...")
    started = clock()
    mirrored = False
    while clock() - started < terminal_timeout:
        chunk = tail.read_new()
        if any(marker in chunk for marker in TERMINAL_MIRROR_MARKERS):
            mirrored = True
            break
        sleep(2)
    if mirrored:
        lines.append("3/3 terminal -> phone: ok")
    else:
        lines.append(
            f"3/3 terminal -> phone: no terminal mirror card within {int(terminal_timeout)} s; "
            "confirm CLB_FLOW_MIRROR=1 in bridge.env and that the service restarted, then run "
            "`claude-telegram-bridge doctor`"
        )
    for line in lines:
        out(line)
    api_call(token, "sendMessage", chat_id=chat_id, text="Setup self-check\n" + "\n".join(lines))
    return lines


def read_token(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""


def probe_native_host(state_path: Path) -> dict[str, Any]:
    try:
        from claude_telegram_bridge import ClaudeConPtyTransport, NativeSessionUnbound

        repl = ClaudeConPtyTransport(
            SimpleNamespace(
                conpty_state_path=state_path,
                state_dir=state_path.parent,
                conpty_timeout_ms=3000,
            )
        )
        repl.verify()
        identity = repl.host_identity()
        generation = hashlib.sha256(str(identity["generation"]).encode()).hexdigest()[:12]
        try:
            transcript = repl.session_file()
        except NativeSessionUnbound:
            transcript = None
        return {
            "status": "ok",
            "host_up": True,
            "session_bound": transcript is not None,
            "generation": generation,
            "transcript_exists": bool(transcript and transcript.exists()),
        }
    except Exception as exc:  # noqa: BLE001 - doctor converts native failures to redacted status
        name = type(exc).__name__
        if name == "NativeHostUnavailable":
            error_code = "native_host_unavailable"
        elif name == "NativeHostGenerationChanged":
            error_code = "native_host_generation_changed"
        elif "generation" in str(exc).lower():
            error_code = "native_host_generation_invalid"
        else:
            error_code = "native_host_invalid"
        return {"status": "error", "error_code": error_code}


def doctor(
    *,
    config_file: Path,
    token_file: Path,
    registry_file: Path,
    settings_file: Path,
    hook_file: Path,
    state_dir: Path,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    os_name: str | None = None,
    native_probe: Callable[[Path], dict[str, Any]] = probe_native_host,
) -> int:
    failures = 0
    warnings = 0
    os_name = os_name or platform.system()
    config = load_env_file(config_file)
    if config_file.exists():
        ok(f"config exists: {config_file}")
    else:
        fail(f"config missing: {config_file}")
        failures += 1

    token = read_token(token_file)
    if token:
        try:
            ok(f"token valid: @{validate_bot_token(token, api_call=api_call)}")
        except SetupError as exc:
            fail(str(exc))
            failures += 1
    else:
        fail(f"token missing or invalid: {token_file}")
        failures += 1

    chat_id = config.get("CLB_CHAT_ID", "")
    if chat_id:
        ok(f"chat_id configured: {chat_id}")
        if token:
            payload = api_call(token, "sendChatAction", timeout=10, chat_id=chat_id, action="typing")
            if payload and payload.get("ok"):
                ok("Telegram chat_id accepted sendChatAction")
            else:
                warn("Telegram chat_id validation failed; send /start and re-run setup")
                warnings += 1
    else:
        fail("CLB_CHAT_ID missing")
        failures += 1

    try:
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = {}
    expected_entry = token_registry(token).get("tokens", {}).get("default") if token else None
    registry_tokens = registry.get("tokens") if isinstance(registry, dict) else None
    registry_entry = registry_tokens.get("default") if isinstance(registry_tokens, dict) else None
    if registry_entry == expected_entry:
        ok(f"token registry valid: {registry_file}")
    else:
        fail(f"token registry missing or mismatched: {registry_file}")
        failures += 1

    transport = (config.get("CLB_REPL_TRANSPORT") or "tmux").strip().lower()
    try:
        validate_setup_transport(transport, os_name=os_name)
    except SetupError as exc:
        fail(str(exc))
        failures += 1
        if show_cli_path_fallback("doctor"):
            warnings += 1
        out(f"doctor complete: {failures} failure(s), {warnings} warning(s)")
        return 2

    if transport == "conpty":
        state_path = Path(config.get("CLB_CONPTY_STATE_PATH", "") or state_dir / "native-repl-host.json")
        probe = native_probe(state_path)
        if probe.get("status") == "ok" and probe.get("host_up"):
            ok(f"native host up; generation={probe.get('generation', 'unknown')}")
            if probe.get("session_bound"):
                ok("native host session bound")
            else:
                warn("native host session unbound until first bridge input")
                warnings += 1
        else:
            error_code = str(probe.get("error_code") or "native_host_invalid")
            if error_code == "native_host_unavailable":
                fail("native host unavailable; start the foreground host first")
            elif error_code in {"native_host_generation_changed", "native_host_generation_invalid"}:
                fail("native host generation invalid; restart the foreground host")
            else:
                fail("native host descriptor or IPC is invalid")
            failures += 1
        setup_note("Native ConPTY health checks complete.")
    else:
        proc = run(["tmux", "-L", "default", "has-session", "-t", "=claude"])
        if proc.returncode == 0:
            ok("tmux session found: default/claude")
            warnings += warn_sessions_outside_bridge(run=run)
        else:
            warn("tmux session missing: default/claude")
            warnings += 1

        try:
            settings, _mode = load_settings(settings_file)
        except SetupError as exc:
            fail(str(exc))
            settings = {}
            failures += 1
        if hook_file.is_file() and os.access(hook_file, os.X_OK):
            ok(f"SessionStart hook executable: {hook_file}")
        else:
            fail(f"SessionStart hook missing or not executable: {hook_file}")
            failures += 1
        if hook_registered(settings, hook_file):
            ok(f"SessionStart hook registered: {settings_file}")
        else:
            fail(f"SessionStart hook not registered: {settings_file}")
            failures += 1

        sidecar = Path(config.get("CLB_SESSION_SIDECAR", "") or state_dir / "claude-telegram-bridge-sessions.json")
        if sidecar.exists():
            ok(f"transcript sidecar exists: {sidecar}")
        else:
            warn(f"transcript sidecar missing until SessionStart fires: {sidecar}")
            warnings += 1

        status = service_status(os_name=os_name, run=run)
        watchdog = watchdog_status(os_name=os_name, run=run)
        (ok if status in {"active", "loaded"} else warn)(f"service status: {status}")
        (ok if watchdog in {"active", "loaded"} else warn)(f"watchdog status: {watchdog}")
        warnings += int(status not in {"active", "loaded"}) + int(watchdog not in {"active", "loaded"})
    if show_cli_path_fallback("doctor"):
        warnings += 1
    out(f"doctor complete: {failures} failure(s), {warnings} warning(s)")
    return 2 if failures else 0


def uninstall(
    *,
    config_file: Path,
    token_file: Path,
    registry_file: Path,
    settings_file: Path,
    hook_file: Path,
    runner_file: Path,
    purge: bool,
    yes: bool,
) -> int:
    if not yes:
        answer = input(f"Stop and remove {APP_NAME}? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise SetupError("uninstall cancelled")
    uninstall_watchdog()
    uninstall_service()
    runner_file.unlink(missing_ok=True)
    for spec in PUBLIC_HOOKS:
        change = remove_hook(settings_file, preset_hook_path(hook_file, spec), event=spec.event)
        if change.changed:
            ok(f"removed {spec.event} registration: {spec.name}")
            if change.backup:
                ok(f"backed up Claude settings: {change.backup}")
    if purge:
        for path in (config_file, token_file, registry_file, hook_file):
            path.unlink(missing_ok=True)
        for spec in PUBLIC_HOOKS:
            preset_hook_path(hook_file, spec).unlink(missing_ok=True)
        ok("purged private config and installed hooks")
    else:
        out(f"kept private config: {config_file.parent}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Claude Telegram Bridge or manage its local setup",
        epilog="With no subcommand, the bridge daemon starts normally.",
    )
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser("setup", help="interactive six-step setup wizard")
    doctor_parser = subparsers.add_parser("doctor", help="check token, tmux, hook, sidecar, and service")
    uninstall_parser = subparsers.add_parser("uninstall", help="remove service, runner, and hook registration")
    subparsers.add_parser("run", help="start the bridge daemon")
    subparsers.add_parser("host", add_help=False, help="start the native Windows owned Claude host")
    attach_parser = subparsers.add_parser(
        "attach", help=f"attach this terminal to the bridge's Claude tmux session (same as {ATTACH_COMMAND})"
    )
    attach_parser.add_argument("--config", type=expand_path, default=default_config_file())
    attach_parser.add_argument("--no-create", action="store_true", help="fail instead of starting a missing session")

    for child in (setup_parser, doctor_parser, uninstall_parser):
        child.add_argument("--config", type=expand_path, default=default_config_file())
        child.add_argument("--token-file", type=expand_path, default=default_token_file())
        child.add_argument("--registry", type=expand_path, default=default_registry_file())
        child.add_argument("--settings", type=expand_path, default=default_settings_file())
        child.add_argument("--hook", type=expand_path, default=default_hook_file())
    setup_parser.add_argument("--runner", type=expand_path, default=default_runner_file())
    setup_parser.add_argument("--state-dir", type=expand_path, default=default_state_dir())
    setup_parser.add_argument("--token")
    setup_parser.add_argument("--chat-id")
    setup_parser.add_argument("--language", choices=("en", "ko"), default="en", help="language for bridge messages and buttons")
    setup_parser.add_argument("--wait-timeout", type=int, default=180)
    setup_parser.add_argument("--no-service", action="store_true")
    setup_parser.add_argument("--no-start", action="store_true")
    setup_parser.add_argument("--no-test-message", action="store_true")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument(
        "--create-session",
        action="store_true",
        help="create a missing tmux Claude session (opt-in for non-interactive setup)",
    )
    setup_parser.add_argument("-y", "--yes", action="store_true")
    setup_parser.add_argument(
        "--like-author",
        action="store_true",
        help="author preset: flow card with Stop button, question cards, busy-time injection, "
        "suggested-reply loop, every bridge hook, the claude-tg command, and a self-check",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what setup would write and register, without touching files or Telegram",
    )
    setup_parser.add_argument(
        "--transport",
        choices=("tmux", "conpty"),
        default=os.environ.get("CLB_REPL_TRANSPORT", "tmux"),
    )
    setup_parser.add_argument("--conpty-state", type=expand_path, default=default_conpty_state_path())
    doctor_parser.add_argument("--state-dir", type=expand_path, default=default_state_dir())
    uninstall_parser.add_argument("--runner", type=expand_path, default=default_runner_file())
    uninstall_parser.add_argument("--purge", action="store_true")
    uninstall_parser.add_argument("-y", "--yes", action="store_true")
    return parser


def run_bridge_daemon() -> int:
    load_private_config_environment(default_config_file())
    try:
        from claude_telegram_bridge import main as bridge_main
    except ImportError as exc:
        raise SetupError(f"bridge module is unavailable: {exc}") from exc
    return int(bridge_main())


def load_private_config_environment(config_file: Path) -> None:
    for key, value in load_env_file(config_file).items():
        os.environ.setdefault(key, value)


def run_bridge_with_config(config_file: Path, daemon_args: list[str]) -> int:
    load_private_config_environment(config_file)
    original = sys.argv
    try:
        sys.argv = [original[0], *daemon_args]
        return run_bridge_daemon()
    finally:
        sys.argv = original


def run_native_host(argv: list[str]) -> int:
    try:
        import claude_repl_host_windows
    except ImportError as exc:
        raise SetupError(f"native host module is unavailable: {exc}") from exc
    original = sys.argv
    try:
        sys.argv = ["claude-telegram-bridge host", *argv]
        return int(claude_repl_host_windows.main())
    finally:
        sys.argv = original


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        try:
            return run_bridge_daemon()
        except SetupError as exc:
            fail(str(exc))
            return 2
    if values[0] == "host":
        try:
            return run_native_host(values[1:])
        except SetupError as exc:
            fail(str(exc))
            return 2
    if values[0] == "run":
        run_parser = argparse.ArgumentParser(prog="claude-telegram-bridge run")
        run_parser.add_argument("--config", type=expand_path, default=default_config_file())
        run_args, daemon_args = run_parser.parse_known_args(values[1:])
        return run_bridge_with_config(run_args.config, daemon_args)
    if values[0] not in {"setup", "doctor", "uninstall", "attach", "-h", "--help"}:
        return run_bridge_daemon()
    args = build_parser().parse_args(values)
    try:
        if args.command == "attach":
            return attach_command(args.config, create_missing=not args.no_create)
        if args.command == "setup":
            return setup_bridge(
                SetupOptions(
                    config_file=args.config,
                    token_file=args.token_file,
                    registry_file=args.registry,
                    settings_file=args.settings,
                    hook_file=args.hook,
                    runner_file=args.runner,
                    state_dir=args.state_dir,
                    token=args.token,
                    chat_id=args.chat_id,
                    wait_timeout=args.wait_timeout,
                    install_service=not args.no_service,
                    start_service=not args.no_start,
                    send_test=not args.no_test_message,
                    non_interactive=args.non_interactive,
                    yes=args.yes,
                    transport=args.transport,
                    conpty_state_path=args.conpty_state,
                    create_session=args.create_session,
                    language=args.language,
                    like_author=args.like_author,
                    dry_run=args.dry_run,
                )
            )
        if args.command == "doctor":
            return doctor(
                config_file=args.config,
                token_file=args.token_file,
                registry_file=args.registry,
                settings_file=args.settings,
                hook_file=args.hook,
                state_dir=args.state_dir,
            )
        if args.command == "uninstall":
            return uninstall(
                config_file=args.config,
                token_file=args.token_file,
                registry_file=args.registry,
                settings_file=args.settings,
                hook_file=args.hook,
                runner_file=args.runner,
                purge=args.purge,
                yes=args.yes,
            )
    except SetupError as exc:
        fail(str(exc))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
