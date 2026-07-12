#!/usr/bin/env python3
"""Owned native Windows ConPTY host for one Claude Code TUI.

The host reuses the Windows ConPTY, local-user named-pipe, capability, and
ordered input primitives from ``codex_repl_host_windows``. It must launch
Claude itself; it cannot attach to an arbitrary pre-existing terminal pane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import codex_repl_host_windows as conpty


def safe_log(message: str) -> None:
    """Log lifecycle metadata only; callers must not pass prompts or credentials."""

    print(f"[claude-repl-host] {message}", file=sys.stderr, flush=True)


class ClaudeSessionBinder:
    """Bind one Claude transcript created or changed after the owned child launch.

    Claude stores interactive transcripts below ``~/.claude/projects`` on each
    platform. A new session creates a JSONL file; ``--resume`` changes an
    existing one. Concurrent changes are ambiguous and fail closed rather than
    risking Telegram input being associated with another Claude process.
    """

    def __init__(self, roots: Iterable[Path], launched_ns: int | None = None) -> None:
        self.roots = tuple(Path(root).expanduser() for root in roots)
        self.launched_ns = launched_ns if launched_ns is not None else time.time_ns()
        self.baseline = self._snapshot()
        self.first_input_ns: int | None = None

    def note_first_input(self, when_ns: int | None = None) -> None:
        if self.first_input_ns is None:
            self.first_input_ns = time.time_ns() if when_ns is None else int(when_ns)

    def _paths(self) -> set[Path]:
        result: set[Path] = set()
        for root in self.roots:
            try:
                result.update(path.resolve() for path in root.rglob("*.jsonl"))
            except OSError:
                continue
        return result

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _snapshot(self) -> dict[Path, tuple[int, int]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in self._paths():
            identity = self._identity(path)
            if identity is not None:
                snapshot[path] = identity
        return snapshot

    def candidates(self) -> list[Path]:
        if self.first_input_ns is None:
            return []
        candidates: list[Path] = []
        current = self._snapshot()
        for path, identity in current.items():
            baseline = self.baseline.get(path)
            if baseline == identity:
                continue
            if identity[0] >= self.first_input_ns and (
                path not in self.baseline or identity[1] != baseline[1] or identity[0] != baseline[0]
            ):
                candidates.append(path)
        return sorted(candidates, key=lambda item: (current[item][0], str(item)))

    def bind_once(self) -> Path | None:
        candidates = self.candidates()
        if len(candidates) > 1:
            raise conpty.AmbiguousSessionError("multiple_changed_sessions")
        return candidates[0] if candidates else None


class ClaudeHostProtocol(conpty.HostProtocol):
    """Use the shared authenticated protocol with Claude's Enter submit default."""

    def __init__(self, *args: Any, on_first_input: Callable[[], None] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_first_input = on_first_input

    def handle(self, request: Any) -> dict[str, Any]:
        if isinstance(request, dict) and request.get("op") == "paste" and "submit_key" not in request:
            request = dict(request)
            request["submit_key"] = "Enter"
            request.setdefault("enter_count", 1)
        response = super().handle(request)
        if (
            response.get("ok") is True
            and isinstance(request, dict)
            and request.get("op") == "paste"
            and self.on_first_input
        ):
            self.on_first_input()
        return response


def build_descriptor(
    *,
    generation: str,
    capability: str,
    pipe_name: str,
    host_pid: int,
    child_pid: int,
    created_at_ns: int,
) -> dict[str, Any]:
    return {
        "schema": conpty.SCHEMA,
        "runtime": "claude",
        "transport": "conpty-owned",
        "generation": generation,
        "capability": capability,
        "pipe_name": pipe_name,
        "host_pid": host_pid,
        "child_pid": child_pid,
        "session_bound": False,
        "created_at_ns": created_at_ns,
    }


def load_host_descriptor(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("native host descriptor unavailable") from exc
    required = ("generation", "capability", "pipe_name")
    if payload.get("schema") != conpty.SCHEMA or payload.get("runtime") != "claude":
        raise RuntimeError("native host descriptor mismatch")
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise RuntimeError("native host descriptor incomplete")
    return payload


def named_pipe_request(pipe_name: str, request: dict[str, Any]) -> dict[str, Any]:
    conpty._require_windows()
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > conpty.MAX_FRAME_BYTES:
        raise RuntimeError("native host request too large")
    try:
        with open(pipe_name, "r+b", buffering=0) as handle:
            handle.write(encoded)
            raw = handle.readline(conpty.MAX_FRAME_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("native host IPC unavailable") from exc
    if len(raw) > conpty.MAX_FRAME_BYTES:
        raise RuntimeError("native host response too large")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("native host response invalid") from exc
    if not isinstance(response, dict):
        raise RuntimeError("native host response invalid")
    return response


def inject_prompt(
    state_path: Path,
    text: str,
    *,
    requester: Callable[[str, dict[str, Any]], dict[str, Any]] = named_pipe_request,
) -> dict[str, Any]:
    if not text.strip():
        raise RuntimeError("prompt is empty")
    descriptor = load_host_descriptor(Path(state_path).expanduser())
    request_id = secrets.token_hex(16)
    request = {
        "schema": conpty.SCHEMA,
        "request_id": request_id,
        "generation": descriptor["generation"],
        "capability": descriptor["capability"],
        "op": "paste",
        "text": text,
        "clear_before": True,
        "submit_key": "Enter",
        "enter_count": 1,
    }
    response = requester(descriptor["pipe_name"], request)
    if response.get("request_id") != request_id:
        raise RuntimeError("native host response request mismatch")
    if response.get("generation") != descriptor["generation"]:
        raise RuntimeError("native host generation changed")
    if response.get("ok") is not True:
        raise RuntimeError(f"native host rejected request: {response.get('error', 'unknown')}")
    return response


def run_host(args: argparse.Namespace) -> int:
    conpty._require_windows()
    workdir = Path(args.workdir).expanduser().resolve()
    session_roots = [Path(item).expanduser() for item in args.session_root]
    generation = secrets.token_hex(16)
    capability = secrets.token_urlsafe(32)
    sid_hash = hashlib.sha256(conpty.current_user_sid().encode("ascii")).hexdigest()[:12]
    pipe_name = rf"\\.\pipe\claude-repl-host-{sid_hash}-{generation}"
    state_path = Path(args.state_path).expanduser()
    columns, rows = conpty.console_size(args.columns, args.rows)
    binder = ClaudeSessionBinder(session_roots)
    pty = conpty.WindowsConPty(columns, rows)
    child_pid = pty.launch(conpty.windows_command(args.claude_bin, args.claude_arg), workdir)
    stop = threading.Event()
    inputs = conpty.OrderedInputQueue()
    output = conpty.RawOutputBuffer(args.capture_bytes)
    session_holder: dict[str, Path | None] = {"path": None}

    output_thread = threading.Thread(
        target=conpty.output_loop,
        args=(pty, output, stop),
        daemon=True,
        name="claude-conpty-output",
    )
    output_thread.start()
    threading.Thread(
        target=conpty.keyboard_loop,
        args=(inputs, stop),
        daemon=True,
        name="claude-local-keyboard",
    ).start()
    threading.Thread(
        target=conpty.resize_loop,
        args=(pty, stop, args.columns, args.rows),
        daemon=True,
        name="claude-conpty-resize",
    ).start()
    threading.Thread(
        target=conpty.writer_loop,
        args=(pty, inputs, stop),
        daemon=True,
        name="claude-conpty-input",
    ).start()

    descriptor = build_descriptor(
        generation=generation,
        capability=capability,
        pipe_name=pipe_name,
        host_pid=os.getpid(),
        child_pid=child_pid,
        created_at_ns=time.time_ns(),
    )
    protocol = ClaudeHostProtocol(
        generation,
        capability,
        inputs,
        output,
        lambda: session_holder["path"],
        pty.alive,
        on_first_input=binder.note_first_input,
    )

    try:
        conpty.write_descriptor_secure(state_path, descriptor)
        server = conpty.NamedPipeServer(pipe_name, protocol, stop)
        threading.Thread(
            target=server.serve,
            daemon=True,
            name="claude-conpty-ipc",
        ).start()
        safe_log(f"awaiting session generation={generation[:8]} child_pid={child_pid}")
        session_holder["path"] = conpty.wait_for_session(
            binder,
            args.bind_timeout,
            lambda: pty.alive() and not stop.is_set(),
        )
        descriptor["session_bound"] = True
        descriptor["session_file"] = str(session_holder["path"])
        conpty.write_descriptor_secure(state_path, descriptor)
        safe_log(f"ready generation={generation[:8]} child_pid={child_pid}")
        while pty.alive() and not stop.wait(0.25):
            pass
        if pty.alive():
            return 0
        pty.close_input()
        exit_code = pty.wait(5000)
        pty.close_pseudoconsole()
        output_thread.join(timeout=2.0)
        return exit_code
    except (conpty.AmbiguousSessionError, TimeoutError) as exc:
        safe_log(str(exc))
        return 3
    finally:
        stop.set()
        conpty.remove_descriptor_if_generation(state_path, generation)
        pty.close()


def run_self_test() -> int:
    if os.name != "nt":
        paste, submit = conpty.encode_paste(
            "Claude host import self-test",
            clear_before=True,
            submit_key="Enter",
        )
        if not paste.endswith(conpty.BRACKETED_PASTE_END) or not submit:
            return 4
        print("PASS Claude owned-host package/protocol self-test (Windows ConPTY deferred)")
        return 0
    conpty._require_windows()
    result = conpty.run_self_test()
    if result == 0:
        print("PASS Claude owned-host ConPTY and authenticated IPC primitives")
    return result


def parser() -> argparse.ArgumentParser:
    default_state = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "claude-telegram-bridge"
        / "native-repl-host.json"
    )
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--claude-bin", default="claude")
    value.add_argument("--claude-arg", action="append", default=[])
    value.add_argument("--workdir", default=str(Path.cwd()))
    value.add_argument("--state-path", default=str(default_state))
    value.add_argument(
        "--session-root",
        action="append",
        default=[str(Path.home() / ".claude" / "projects")],
    )
    value.add_argument(
        "--bind-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for one owned Claude transcript (0 waits for child exit)",
    )
    value.add_argument("--columns", type=int, default=120)
    value.add_argument("--rows", type=int, default=40)
    value.add_argument("--capture-bytes", type=int, default=conpty.DEFAULT_CAPTURE_BYTES)
    mode = value.add_mutually_exclusive_group()
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument(
        "--inject-stdin",
        action="store_true",
        help="read one prompt from stdin and inject through the authenticated host descriptor",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        if args.self_test:
            return run_self_test()
        if args.inject_stdin:
            response = inject_prompt(Path(args.state_path), sys.stdin.read())
            print(f"PASS prompt accepted sequence={int(response.get('sequence', 0))}")
            return 0
        return run_host(args)
    except Exception as exc:  # noqa: BLE001
        safe_log(f"fatal: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
