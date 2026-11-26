#!/usr/bin/env python3
"""
Stdlib-only port collision, listener PID ownership, and /health schema verification
helpers for Ultra.cc non-root deployment scripts (`deploy-ultra.sh` & `deploy/deploy-suite-ultra.sh`).

Reads full `ss` output in memory (eliminating `pipefail` + `grep -q` SIGPIPE 141 false negatives),
probes loopback socket bind availability, verifies systemd MainPID ownership on 127.0.0.1:<port>,
and strictly validates each microservice's /health JSON contract so a foreign listener on an
occupied port can never produce a false-positive deployment check.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_PID_RE = re.compile(r"pid=(\d+)")
_STANDALONE_PORT_RE = re.compile(r"^\s*(\d{4,5})\s*$")
_TOKEN_PORT_RE = re.compile(r"(?<![0-9])(\d{4,5})(?![0-9])")


def parse_ss_local_host_and_port(local_addr: str) -> tuple[Optional[str], Optional[int]]:
    """Parse `ss -tlnH` column 4 (e.g. `127.0.0.1:40714`, `[::1]:40714`, `127.0.0.1%lo:40714`)."""
    raw = (local_addr or "").strip()
    if ":" not in raw:
        return None, None
    host_part, port_part = raw.rsplit(":", 1)
    if not port_part.isdigit():
        return None, None
    port = int(port_part)
    if not (1 <= port <= 65535):
        return None, None
    host = host_part.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if "%" in host:
        host = host.split("%", 1)[0]
    return host, port


def parse_ss_listening_ports(ss_output: str) -> set[int]:
    """Extract all listening TCP ports from complete `ss -tlnH` output."""
    ports: set[int] = set()
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _, port = parse_ss_local_host_and_port(parts[3])
        if port is not None:
            ports.add(port)
    return ports


def can_bind_loopback(port: int) -> bool:
    """Non-destructively verify `127.0.0.1:<port>` is bindable right now (zero TIME_WAIT created)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _run_ss(args: list[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["ss", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return proc.stdout


def is_port_listening(port: int, *, ss_output: Optional[str] = None, probe_bind: bool = True) -> bool:
    """
    Return True if `port` is currently occupied (either present in complete `ss -tlnH` output
    or rejected by a loopback bind probe). Never uses a partial-read shell pipe.
    """
    if not (1024 <= port <= 65535):
        raise ValueError(f"Invalid port: {port}")
    raw_ss = ss_output if ss_output is not None else _run_ss(["-tlnH"])
    if raw_ss is not None and port in parse_ss_listening_ports(raw_ss):
        return True
    if probe_bind and not can_bind_loopback(port):
        return True
    return False


def get_pid_tree(root_pid: int, *, proc_root: Path = Path("/proc")) -> set[int]:
    """Return `{root_pid}` plus any descendant PIDs visible under `/proc`."""
    if root_pid <= 0:
        return set()
    children_by_ppid: dict[int, list[int]] = {}
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rparen = stat_text.rfind(")")
            if rparen == -1:
                continue
            after = stat_text[rparen + 2 :].split()
            if len(after) >= 2 and after[1].isdigit():
                ppid = int(after[1])
                children_by_ppid.setdefault(ppid, []).append(pid)
    except OSError:
        return {root_pid}

    tree: set[int] = set()
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        if cur in tree:
            continue
        tree.add(cur)
        stack.extend(children_by_ppid.get(cur, ()))
    return tree


def is_port_owned_by_pid(
    port: int,
    root_pid: int,
    *,
    ss_output: Optional[str] = None,
    allowed_pids: Optional[set[int]] = None,
) -> bool:
    """
    Return True iff `ss -tlnpH` shows a loopback listener (`127.0.0.1` or `::1`) on `port`
    owned by `root_pid` (or one of its descendants).
    """
    if not (1024 <= port <= 65535) or root_pid <= 0:
        return False
    raw_ss = ss_output if ss_output is not None else _run_ss(["-tlnpH"])
    if not raw_ss:
        return False
    pids = allowed_pids if allowed_pids is not None else get_pid_tree(root_pid)
    if not pids:
        return False
    matched_own_loopback = False
    for line in raw_ss.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        host, parsed_port = parse_ss_local_host_and_port(parts[3])
        if parsed_port != port:
            continue
        if host not in ("127.0.0.1", "::1"):
            return False
        line_pids = {int(m.group(1)) for m in _PID_RE.finditer(line)}
        if not line_pids or not line_pids.issubset(pids):
            return False
        matched_own_loopback = True
    return matched_own_loopback


def parse_app_ports_output(text: str) -> list[int]:
    """Parse `app-ports free` output into deduplicated candidate ports (1024-65535)."""
    ports: list[int] = []
    seen: set[int] = set()
    for line in text.splitlines():
        m = _STANDALONE_PORT_RE.match(line)
        if m:
            p = int(m.group(1))
            if 1024 <= p <= 65535 and p not in seen:
                seen.add(p)
                ports.append(p)
    if ports:
        return ports
    for m in _TOKEN_PORT_RE.finditer(text):
        p = int(m.group(1))
        if 1024 <= p <= 65535 and p not in seen:
            seen.add(p)
            ports.append(p)
    return ports


def _is_strict_int(val: Any) -> bool:
    return type(val) is int


def _is_strict_bool(val: Any) -> bool:
    return type(val) is bool


def verify_health_payload(
    payload: Any,
    *,
    role: str,
    expected_port: int,
    expected_shadow: Optional[bool] = None,
) -> tuple[bool, str]:
    """Strictly verify a microservice's `/health` JSON response against its expected role, port, and mode."""
    if not isinstance(payload, dict):
        return False, "Health payload is not a JSON object"
    if payload.get("status") != "ok":
        return False, f"Unexpected status={payload.get('status')!r} (expected 'ok')"
    if not _is_strict_int(payload.get("port")) or payload.get("port") != expected_port:
        return False, f"Unexpected port={payload.get('port')!r} (expected {expected_port})"

    if role == "intake":
        for int_key in ("queued_releases", "in_flight_releases", "ambiguous_deliveries"):
            if not _is_strict_int(payload.get(int_key)):
                return False, f"Missing or invalid intake health field: {int_key}"
        if payload.get("archive_dir_ok") is not True:
            return False, f"Intake archive_dir_ok is not true: {payload.get('archive_dir_ok')!r}"
        if not _is_strict_bool(payload.get("webhook_configured")):
            return False, "Missing or invalid intake health field: webhook_configured"
        if payload.get("internal_auth_configured") is not True:
            return False, "Intake internal_auth_configured must be true"
        if not _is_strict_bool(payload.get("shadow_replay_enabled")):
            return False, "Missing or invalid intake health field: shadow_replay_enabled"
        if expected_shadow is not None and payload.get("shadow_replay_enabled") is not expected_shadow:
            return (
                False,
                f"Intake shadow_replay_enabled={payload.get('shadow_replay_enabled')!r} (expected {expected_shadow!r})",
            )
        return True, "ok"

    if role == "actions":
        for int_key in (
            "pending_ambiguous_deliveries",
            "pending_ambiguous_replies",
            "pending_ambiguous_radarr",
            "active_actions",
        ):
            if not _is_strict_int(payload.get(int_key)):
                return False, f"Missing or invalid actions health field: {int_key}"
        if payload.get("internal_auth_configured") is not True:
            return False, "Actions internal_auth_configured must be true"
        if not _is_strict_bool(payload.get("shadow_replay_enabled")):
            return False, "Missing or invalid actions health field: shadow_replay_enabled"
        if expected_shadow is not None and payload.get("shadow_replay_enabled") is not expected_shadow:
            return (
                False,
                f"Actions shadow_replay_enabled={payload.get('shadow_replay_enabled')!r} (expected {expected_shadow!r})",
            )
        return True, "ok"

    if role == "triage":
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            return False, "Missing or invalid triage health field: model"
        if not _is_strict_int(payload.get("cache_entries")):
            return False, "Missing or invalid triage health field: cache_entries"
        if not _is_strict_bool(payload.get("tmdb_enabled")):
            return False, "Missing or invalid triage health field: tmdb_enabled"
        if payload.get("auth_enabled") is not True:
            return False, "Triage auth_enabled must be true"
        return True, "ok"

    return False, f"Unknown health role: {role!r}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ultra.cc port, PID, and /health verification helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_listen = sub.add_parser("is-listening", help="Exit 0 if TCP port is listening or unbindable, else 1")
    p_listen.add_argument("port", type=int)
    p_listen.add_argument("--no-bind-probe", action="store_true")

    p_owner = sub.add_parser("owned-by-pid", help="Exit 0 if loopback port is owned by PID or descendant, else 1")
    p_owner.add_argument("port", type=int)
    p_owner.add_argument("pid", type=int)

    p_ports = sub.add_parser("parse-app-ports", help="Parse `app-ports free` output from stdin")

    p_health = sub.add_parser("verify-health", help="Validate /health JSON on stdin for role/port/mode")
    p_health.add_argument("--role", choices=("intake", "actions", "triage"), required=True)
    p_health.add_argument("--port", type=int, required=True)
    p_health.add_argument("--shadow", choices=("0", "1"), default=None)

    args = parser.parse_args(argv)

    if args.cmd == "is-listening":
        return 0 if is_port_listening(args.port, probe_bind=not args.no_bind_probe) else 1

    if args.cmd == "owned-by-pid":
        return 0 if is_port_owned_by_pid(args.port, args.pid) else 1

    if args.cmd == "parse-app-ports":
        for p in parse_app_ports_output(sys.stdin.read()):
            print(p)
        return 0

    if args.cmd == "verify-health":
        raw = sys.stdin.read()
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            print(f"Health JSON parse error: {exc}", file=sys.stderr)
            return 1
        expected_shadow: Optional[bool] = None
        if args.shadow is not None:
            expected_shadow = args.shadow == "1"
        ok, reason = verify_health_payload(
            parsed, role=args.role, expected_port=args.port, expected_shadow=expected_shadow
        )
        if not ok:
            print(f"Health validation failed: {reason}", file=sys.stderr)
            return 1
        print(json.dumps(parsed, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
