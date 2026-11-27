from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deploy.env_slicer import (
    ACTIONS_ALLOWED_KEYS,
    ACTIONS_FORBIDDEN_KEYS,
    INTAKE_ALLOWED_KEYS,
    INTAKE_FORBIDDEN_KEYS,
    EnvSecurityError,
    atomic_write_env_file,
    build_sliced_envs,
    read_owner_private_env,
)
from deploy.port_check import (
    is_port_listening,
    is_port_owned_by_pid,
    parse_app_ports_output,
    verify_health_payload,
)
from scripts.run_shadow_verification import run_shadow_suite

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pick_free_loopback_ports(count: int, *, exclude: set[int] | frozenset[int] = frozenset()) -> list[int]:
    """Allocate `count` distinct currently-free loopback TCP ports not in `exclude`."""
    picked: list[int] = []
    held: list[socket.socket] = []
    try:
        for _ in range(max(count * 20, 50)):
            if len(picked) == count:
                break
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            except OSError:
                sock.close()
                continue
            if port in exclude or port in picked:
                sock.close()
                continue
            # Also skip if ss reports wildcard/IPv6 listener on the same port
            if is_port_listening(port, probe_bind=False):
                sock.close()
                continue
            held.append(sock)
            picked.append(port)
    finally:
        for sock in held:
            sock.close()
    if len(picked) != count:
        raise RuntimeError(f"Could not find {count} free loopback ports")
    return picked


class TestDeployAndShadowSuite(unittest.IsolatedAsyncioTestCase):
    def test_env_slicer_least_privilege_boundaries_escaping_and_symlink_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            home = base / "home"
            home.mkdir(mode=0o700)

            legacy_file = base / "legacy.env"
            legacy_file.write_text(
                "\n".join(
                    [
                        'MOVIE_INTEL_WEBHOOK_SECRET="sec#with spaces\\"and\\\\slash"',
                        "MOVIE_INTEL_TL_COOKIE_FILE=/custom/tl-cookies.json",
                        "TMDB_API_TOKEN=tmdb-token-xyz",
                        "TMDB_CACHE_PATH=/home/legacy/.local/state/movie-intel/tmdb-cache.json",
                        "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG=/home/legacy/.config/movie-intel/alternative-grab.json",
                        "RADARR_URL=https://seedbox.invalid/u/radarr",
                        "RADARR_API_KEY=radarr-secret-123",
                        "TELEGRAM_BOT_TOKEN=123456:AAH_BOT_SECRET",
                        "TELEGRAM_CHAT_ID=-100999",
                        "GEMINI_API_KEY=gemini-should-never-copy",
                        "MOVIE_INTEL_PUBLIC_BASE_URL=https://seedbox.invalid/movie-intel",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            # World-readable env file MUST be rejected
            os.chmod(legacy_file, 0o644)
            with self.assertRaises(EnvSecurityError):
                read_owner_private_env(legacy_file)

            # Symlinked env file MUST be rejected
            os.chmod(legacy_file, 0o600)
            sym_env = base / "symlink.env"
            sym_env.symlink_to(legacy_file)
            with self.assertRaises(EnvSecurityError):
                read_owner_private_env(sym_env)

            # Valid 0600 read + least-privilege slice
            legacy = read_owner_private_env(legacy_file)
            self.assertEqual(legacy["MOVIE_INTEL_WEBHOOK_SECRET"], 'sec#with spaces"and\\slash')

            intake_env, actions_env = build_sliced_envs(
                home_dir=home,
                port_intake=40720,
                port_actions=40721,
                port_triage=40716,
                internal_api_secret="shared-internal-token",
                legacy_env=legacy,
                existing_intake_env={},
                existing_actions_env={},
                override_env={},
                shadow_mode=False,
            )
            # Strict least-privilege isolation: zero forbidden secrets cross boundaries
            self.assertIn("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG", INTAKE_FORBIDDEN_KEYS)
            self.assertFalse(set(intake_env.keys()) & INTAKE_FORBIDDEN_KEYS)
            self.assertFalse(set(actions_env.keys()) & ACTIONS_FORBIDDEN_KEYS)
            self.assertNotIn("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG", intake_env)
            self.assertEqual(
                actions_env["MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG"],
                "/home/legacy/.config/movie-intel/alternative-grab.json",
            )
            # Must NOT inherit legacy tmdb-cache.json (incompatible schema); use distinct intake-tmdb-cache.json
            self.assertTrue(intake_env["TMDB_CACHE_PATH"].endswith("intake-tmdb-cache.json"))
            self.assertNotIn("/home/legacy/", intake_env["TMDB_CACHE_PATH"])
            self.assertEqual(intake_env["MOVIE_INTEL_WEBHOOK_SECRET"], 'sec#with spaces"and\\slash')
            self.assertEqual(actions_env["RADARR_API_KEY"], "radarr-secret-123")
            self.assertEqual(actions_env["TELEGRAM_BOT_TOKEN"], "123456:AAH_BOT_SECRET")
            self.assertEqual(actions_env["MOVIE_INTEL_STANDALONE_SKIP_ENABLED"], "true")

            # Atomic write (0600) and idempotent round-trip with special characters
            intake_out = base / "out" / "intake.env"
            actions_out = base / "out" / "actions.env"
            atomic_write_env_file(intake_out, INTAKE_ALLOWED_KEYS, intake_env)
            atomic_write_env_file(actions_out, ACTIONS_ALLOWED_KEYS, actions_env)
            self.assertEqual(os.lstat(intake_out).st_mode & 0o777, 0o600)
            self.assertEqual(os.lstat(actions_out).st_mode & 0o777, 0o600)
            reread_intake = read_owner_private_env(intake_out)
            self.assertEqual(reread_intake["MOVIE_INTEL_WEBHOOK_SECRET"], 'sec#with spaces"and\\slash')

            # Refuses overwriting symlinked output target
            sym_out = base / "out" / "sym.env"
            sym_out.symlink_to(intake_out)
            with self.assertRaises(EnvSecurityError):
                atomic_write_env_file(sym_out, INTAKE_ALLOWED_KEYS, intake_env)

            # Newline injection rejected
            with self.assertRaises(EnvSecurityError):
                build_sliced_envs(
                    home_dir=home,
                    port_intake=40720,
                    port_actions=40721,
                    port_triage=40716,
                    internal_api_secret="bad\nINJECTED=1",
                    legacy_env={},
                    existing_intake_env={},
                    existing_actions_env={},
                    override_env={},
                )

            # Shadow mode inherits ZERO live credentials and uses isolated shadow state paths
            sh_intake, sh_actions = build_sliced_envs(
                home_dir=home,
                port_intake=40730,
                port_actions=40731,
                port_triage=40716,
                internal_api_secret="shadow-secret",
                legacy_env=legacy,
                existing_intake_env={},
                existing_actions_env={},
                override_env={"RADARR_API_KEY": "live-key-should-not-leak"},
                shadow_mode=True,
            )
            self.assertEqual(sh_intake["SHADOW_REPLAY_ENABLED"], "true")
            self.assertEqual(sh_actions["SHADOW_REPLAY_ENABLED"], "true")
            self.assertEqual(sh_intake["MOVIE_INTEL_TL_METADATA_ENABLED"], "false")
            self.assertEqual(sh_intake["TRIAGE_API_URL"], "http://shadow-triage.invalid")
            self.assertEqual(sh_actions["TRIAGE_API_URL"], "http://shadow-triage.invalid")
            self.assertEqual(sh_intake["TMDB_API_TOKEN"], "")
            self.assertEqual(sh_actions["RADARR_API_KEY"], "")
            self.assertEqual(sh_actions["TELEGRAM_BOT_TOKEN"], "")
            self.assertEqual(sh_actions["MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG"], "")
            self.assertTrue(sh_intake["TMDB_CACHE_PATH"].endswith("intake-tmdb-cache.json"))
            self.assertIn("movie-intel-shadow", sh_intake["INTAKE_DB_PATH"])
            self.assertIn("movie-intel-shadow", sh_actions["ACTIONS_DB_PATH"])

            # Shadow CLI must never inherit ambient INTERNAL_API_SECRET
            sh_intake_out = base / "shadow_out" / "intake.env"
            sh_actions_out = base / "shadow_out" / "actions.env"
            cli_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "INTERNAL_API_SECRET": "PROD_INTERNAL_SECRET_MUST_NOT_LEAK",
            }
            cli = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "deploy" / "env_slicer.py"),
                    "--home",
                    str(home),
                    "--port-intake",
                    "40730",
                    "--port-actions",
                    "40731",
                    "--intake-env-out",
                    str(sh_intake_out),
                    "--actions-env-out",
                    str(sh_actions_out),
                    "--shadow",
                ],
                env=cli_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertNotIn("PROD_INTERNAL_SECRET_MUST_NOT_LEAK", sh_intake_out.read_text(encoding="utf-8"))
            self.assertNotIn("PROD_INTERNAL_SECRET_MUST_NOT_LEAK", sh_actions_out.read_text(encoding="utf-8"))

            # PR #18 Blocker 4: Custom autobrr filter name and ID must survive fresh install AND upgrade regeneration
            custom_intake, custom_actions = build_sliced_envs(
                home_dir=home,
                port_intake=40720,
                port_actions=40721,
                port_triage=40716,
                internal_api_secret="sec-filter-test",
                legacy_env={},
                existing_intake_env={},
                existing_actions_env={},
                override_env={
                    "MOVIE_INTEL_ALLOWED_FILTERS": "Custom UHD Remux",
                    "MOVIE_INTEL_ALLOWED_FILTER_IDS": "17",
                },
            )
            self.assertEqual(custom_intake["MOVIE_INTEL_ALLOWED_FILTERS"], "Custom UHD Remux")
            self.assertEqual(custom_intake["MOVIE_INTEL_ALLOWED_FILTER_IDS"], "17")
            self.assertNotIn("MOVIE_INTEL_ALLOWED_FILTERS", custom_actions)
            self.assertNotIn("MOVIE_INTEL_ALLOWED_FILTER_IDS", custom_actions)

            # Write to .env, re-read on simulated upgrade regeneration (empty overrides), and verify ID 17 still enforced!
            intake_env_file = base / "upgrade_out" / "intake.env"
            actions_env_file = base / "upgrade_out" / "actions.env"
            atomic_write_env_file(intake_env_file, INTAKE_ALLOWED_KEYS, custom_intake)
            atomic_write_env_file(actions_env_file, ACTIONS_ALLOWED_KEYS, custom_actions)
            parsed_existing_intake = read_owner_private_env(intake_env_file, required=True)
            upgraded_intake, _ = build_sliced_envs(
                home_dir=home,
                port_intake=40720,
                port_actions=40721,
                port_triage=40716,
                internal_api_secret="sec-filter-test",
                legacy_env={},
                existing_intake_env=parsed_existing_intake,
                existing_actions_env={},
                override_env={},
            )
            self.assertEqual(upgraded_intake["MOVIE_INTEL_ALLOWED_FILTERS"], "Custom UHD Remux")
            self.assertEqual(upgraded_intake["MOVIE_INTEL_ALLOWED_FILTER_IDS"], "17")

            from services.movie_intake_api.config import IntakeConfig

            loaded_cfg = IntakeConfig.from_env(upgraded_intake)
            self.assertEqual(loaded_cfg.allowed_filter_names, ("Custom UHD Remux",))
            self.assertEqual(loaded_cfg.allowed_filter_ids, (17,))

            # Invalid filter IDs must fail validation loudly
            for bad_ids in ("0", "-5", "abc", "17,notanint"):
                with self.assertRaises(EnvSecurityError):
                    build_sliced_envs(
                        home_dir=home,
                        port_intake=40720,
                        port_actions=40721,
                        port_triage=40716,
                        internal_api_secret="sec-filter-test",
                        legacy_env={},
                        existing_intake_env={},
                        existing_actions_env={},
                        override_env={"MOVIE_INTEL_ALLOWED_FILTER_IDS": bad_ids},
                    )

    def test_deploy_scripts_and_nginx_security_invariants(self) -> None:
        suite_script = REPO_ROOT / "deploy" / "deploy-suite-ultra.sh"
        triage_script = REPO_ROOT / "deploy-ultra.sh"
        nginx_tpl = REPO_ROOT / "deploy" / "nginx-movie-intel.conf.example"

        # Valid bash syntax
        for script in (triage_script, suite_script):
            proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"bash -n failed on {script}: {proc.stderr}")

        # Non-root Ultra.cc constraints: zero sudo invocation, zero Docker, loopback only, hardened systemd
        suite_text = suite_script.read_text(encoding="utf-8")
        triage_text = triage_script.read_text(encoding="utf-8")
        for text in (suite_text, triage_text):
            self.assertNotIn("docker ", text.lower())
            self.assertNotIn("0.0.0.0", text)
            self.assertNotIn("/tmp/", text)
            self.assertIn("UMask=0077", text)
            self.assertIn("NoNewPrivileges=true", text)
            self.assertIn("--host 127.0.0.1", text)
            self.assertIn("--no-access-log", text)
            self.assertIn("app-ports free", text)
            self.assertIn("deploy/port_check.py", text)
            self.assertIn("--max-time", text)
            self.assertIn("--connect-timeout", text)
            self.assertIn('HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-60}"', text)
            self.assertIn("is-failed", text)
            self.assertNotIn("enable --now", text)
            # Zero piped ss|grep that can fail with SIGPIPE 141 under pipefail
            self.assertNotIn("ss -tlnH 2>/dev/null |", text)
        self.assertIn('systemctl --user restart "$ACTIONS_SERVICE" "$INTAKE_SERVICE"', suite_text)
        self.assertIn('systemctl --user restart "$SERVICE_NAME"', triage_text)
        # Never stops or disables legacy movie-intel.service
        self.assertNotIn("disable movie-intel.service", suite_text)
        self.assertNotIn("stop movie-intel.service", suite_text)

        # Dry-run executes cleanly without touching systemd or live files, AND rejects port collisions
        with tempfile.TemporaryDirectory() as fake_home:
            systemd_dir = Path(fake_home) / ".config" / "systemd" / "user"
            systemd_dir.mkdir(parents=True, mode=0o700)
            (systemd_dir / "movie-intel.service").write_text(
                "[Service]\nExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 40714\n",
                encoding="utf-8",
            )
            free_intake, free_actions = _pick_free_loopback_ports(2, exclude={40714, 40716})
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": fake_home,
                "PORT_INTAKE": str(free_intake),
                "PORT_ACTIONS": str(free_actions),
                "PORT_TRIAGE": "40716",
            }
            dry = subprocess.run(
                ["bash", str(suite_script), "--shadow", "--dry-run"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry.returncode, 0, f"dry-run failed: {dry.stderr}\n{dry.stdout}")
            self.assertIn("DRY-RUN OK", dry.stdout)

            # Port colliding with existing legacy unit MUST fail fast
            env_conflict = dict(env, PORT_INTAKE="40714")
            dry_conflict = subprocess.run(
                ["bash", str(suite_script), "--shadow", "--dry-run"],
                env=env_conflict,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(dry_conflict.returncode, 0)
            self.assertIn("already configured", dry_conflict.stderr)

            # Invalid HEALTH_WAIT_SECONDS MUST fail fast even on --dry-run (and in deploy-ultra.sh before any action)
            for bad_wait in ("0", "008", "abc", "301"):
                dry_bad_wait = subprocess.run(
                    ["bash", str(suite_script), "--shadow", "--dry-run"],
                    env=dict(env, HEALTH_WAIT_SECONDS=bad_wait),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(dry_bad_wait.returncode, 0, msg=f"Should reject HEALTH_WAIT_SECONDS={bad_wait}")
                self.assertIn("HEALTH_WAIT_SECONDS", dry_bad_wait.stderr)
                triage_bad_wait = subprocess.run(
                    ["bash", str(triage_script)],
                    env=dict(env, HEALTH_WAIT_SECONDS=bad_wait),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(triage_bad_wait.returncode, 0, msg=f"deploy-ultra.sh should reject {bad_wait}")
                self.assertIn("HEALTH_WAIT_SECONDS", triage_bad_wait.stderr)

        # Nginx template security invariants (§7.3)
        nginx_text = nginx_tpl.read_text(encoding="utf-8")
        self.assertEqual(nginx_text.count("access_log off;"), 2)
        self.assertEqual(nginx_text.count("error_log /dev/null emerg;"), 2)
        self.assertNotIn("\n    error_log off;", nginx_text)
        self.assertIn("client_max_body_size 1m;", nginx_text)
        self.assertIn("proxy_pass http://127.0.0.1:<PORT_INTAKE>/webhook/;", nginx_text)
        self.assertIn("proxy_pass http://127.0.0.1:<PORT_ACTIONS>/action/;", nginx_text)
        self.assertNotIn("location ^~ /internal", nginx_text)
        self.assertNotIn("location ^~ /movie-intel/internal", nginx_text)

    def test_port_check_sigpipe_safety_live_bind_pid_ownership_and_health_schema(self) -> None:
        # 500-line ss output with occupied 40714 on line 1 (reproduces Ultra.cc SIGPIPE 141 case)
        ss_500 = "\n".join(
            ["LISTEN 0 128 127.0.0.1:40714 0.0.0.0:*"]
            + [f"LISTEN 0 128 127.0.0.1:{20000 + i} 0.0.0.0:*" for i in range(500)]
        )
        self.assertTrue(is_port_listening(40714, ss_output=ss_500, probe_bind=False))
        self.assertFalse(is_port_listening(40799, ss_output=ss_500, probe_bind=False))

        # Live loopback socket bind probe & exclusive PID ownership check
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            bound_port = sock.getsockname()[1]
            # Caught by bind probe even when ss output is empty
            self.assertTrue(is_port_listening(bound_port, ss_output="", probe_bind=True))
            # Live ss -tlnpH confirms current PID owns it, foreign PID does not
            self.assertTrue(is_port_owned_by_pid(bound_port, os.getpid()))
            self.assertFalse(is_port_owned_by_pid(bound_port, 999999))
        finally:
            sock.close()

        # Codex PR #8 Round 1 P2: TIME_WAIT socket from a stopped service must NOT block rebind
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        tw_port = srv.getsockname()[1]
        cli = socket.create_connection(("127.0.0.1", tw_port), timeout=2)
        conn, _ = srv.accept()
        # Server closes first -> leaves 127.0.0.1:tw_port in TIME_WAIT after client closes
        conn.close()
        srv.close()
        cli.close()
        self.assertFalse(is_port_listening(tw_port, ss_output="", probe_bind=True))

        # Non-loopback or shared-PID listener must be rejected by owned-by-pid
        self.assertFalse(
            is_port_owned_by_pid(
                40718,
                1234,
                ss_output='LISTEN 0 128 0.0.0.0:40718 0.0.0.0:* users:(("uvicorn",pid=1234,fd=6))\n',
                allowed_pids={1234},
            )
        )
        self.assertFalse(
            is_port_owned_by_pid(
                40718,
                1234,
                ss_output=(
                    'LISTEN 0 128 127.0.0.1:40718 0.0.0.0:* users:(("uvicorn",pid=1234,fd=6))\n'
                    'LISTEN 0 128 127.0.0.1:40718 0.0.0.0:* users:(("other",pid=9999,fd=7))\n'
                ),
                allowed_pids={1234},
            )
        )
        self.assertTrue(
            is_port_owned_by_pid(
                40718,
                1234,
                ss_output='LISTEN 0 128 127.0.0.1:40718 0.0.0.0:* users:(("uvicorn",pid=1234,fd=6))\n',
                allowed_pids={1234},
            )
        )

        # Ultra.cc `app-ports free` banner parsing
        banner = "Unallocated ports for use\n-------------------------\n40714\n40716\n40718\n40718\n"
        self.assertEqual(parse_app_ports_output(banner), [40714, 40716, 40718])

        # Strict /health schema verification rejects foreign service payloads (e.g. legacy movie-intel)
        legacy_health = {"status": "ok"}
        self.assertFalse(verify_health_payload(legacy_health, role="intake", expected_port=40714, expected_shadow=True)[0])
        self.assertFalse(verify_health_payload(legacy_health, role="actions", expected_port=40714, expected_shadow=True)[0])
        self.assertFalse(verify_health_payload(legacy_health, role="triage", expected_port=40716)[0])

        valid_intake = {
            "status": "ok",
            "port": 40718,
            "queued_releases": 0,
            "in_flight_releases": 0,
            "ambiguous_deliveries": 0,
            "archive_dir_ok": True,
            "webhook_configured": True,
            "internal_auth_configured": True,
            "shadow_replay_enabled": True,
        }
        self.assertTrue(verify_health_payload(valid_intake, role="intake", expected_port=40718, expected_shadow=True)[0])
        self.assertFalse(verify_health_payload(valid_intake, role="intake", expected_port=40718, expected_shadow=False)[0])
        self.assertFalse(verify_health_payload(valid_intake, role="intake", expected_port=40714, expected_shadow=True)[0])
        self.assertFalse(verify_health_payload(valid_intake, role="actions", expected_port=40718, expected_shadow=True)[0])

        valid_actions = {
            "status": "ok",
            "port": 40717,
            "pending_ambiguous_deliveries": 0,
            "pending_ambiguous_replies": 0,
            "pending_ambiguous_radarr": 0,
            "active_actions": 0,
            "internal_auth_configured": True,
            "shadow_replay_enabled": False,
        }
        self.assertTrue(verify_health_payload(valid_actions, role="actions", expected_port=40717, expected_shadow=False)[0])
        self.assertFalse(verify_health_payload(valid_actions, role="actions", expected_port=40717, expected_shadow=True)[0])

        valid_triage = {
            "status": "ok",
            "model": "gemini-4-hydrogen",
            "port": 40716,
            "cache_entries": 4,
            "tmdb_enabled": True,
            "auth_enabled": True,
        }
        self.assertTrue(verify_health_payload(valid_triage, role="triage", expected_port=40716)[0])
        self.assertFalse(
            verify_health_payload(dict(valid_triage, auth_enabled=False), role="triage", expected_port=40716)[0]
        )

    def test_deploy_suite_skips_occupied_port_under_500_line_ss_and_rejects_bound_override(self) -> None:
        suite_script = REPO_ROOT / "deploy" / "deploy-suite-ultra.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            fake_home = base / "home"
            fake_home.mkdir(mode=0o700)
            fake_bin = base / "bin"
            fake_bin.mkdir(mode=0o700)

            synthetic_busy = set(range(20001, 20501)) | {40714, 40716}
            free_intake, free_actions = _pick_free_loopback_ports(2, exclude=synthetic_busy)

            # Mock `app-ports free` emitting active 40714 and 40716 ahead of free loopback ports
            app_ports_bin = fake_bin / "app-ports"
            app_ports_bin.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'Unallocated ports for use'\n"
                "echo '-------------------------'\n"
                f"printf '%s\\n' 40714 40716 {free_intake} {free_actions}\n",
                encoding="utf-8",
            )
            os.chmod(app_ports_bin, 0o700)

            # Mock `ss` emitting 500 lines with 40714 on line 1 (triggered SIGPIPE 141 before PR #7)
            ss_bin = fake_bin / "ss"
            ss_bin.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'LISTEN 0 128 127.0.0.1:40714 0.0.0.0:*'\n"
                "for i in $(seq 20001 20500); do\n"
                "  echo \"LISTEN 0 128 127.0.0.1:${i} 0.0.0.0:*\"\n"
                "done\n",
                encoding="utf-8",
            )
            os.chmod(ss_bin, 0o700)

            env = {
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(fake_home),
                "PORT_TRIAGE": "40716",
            }
            dry = subprocess.run(
                ["bash", str(suite_script), "--shadow", "--dry-run"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry.returncode, 0, f"dry-run failed: {dry.stderr}\n{dry.stdout}")
            self.assertIn(f"intake=127.0.0.1:{free_intake}", dry.stdout)
            self.assertIn(f"actions=127.0.0.1:{free_actions}", dry.stdout)

            # Live bound socket passed as explicit override MUST fail fast
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                busy_port = sock.getsockname()[1]
                alt_actions, alt_triage = _pick_free_loopback_ports(
                    2, exclude={busy_port, free_intake, free_actions}
                )
                env_busy = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(fake_home),
                    "PORT_INTAKE": str(busy_port),
                    "PORT_ACTIONS": str(alt_actions),
                    "PORT_TRIAGE": str(alt_triage),
                }
                dry_busy = subprocess.run(
                    ["bash", str(suite_script), "--shadow", "--dry-run"],
                    env=env_busy,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(dry_busy.returncode, 0)
                self.assertIn("already listening under another process", dry_busy.stderr)
            finally:
                sock.close()

    async def test_five_case_shadow_verification_runner_passes_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shadow-test-") as tmpdir:
            res = await run_shadow_suite(Path(tmpdir))
            self.assertTrue(res.passed, f"Shadow suite failed: {res.cases}")
            self.assertTrue(res.cases["case_5_lost_acks_and_recovery"].get("restarted_process_verified"))
            self.assertEqual(
                set(res.cases.keys()),
                {
                    "case_1_duplicate_burst_and_non_collapse",
                    "case_2_wrong_tracker_imdb",
                    "case_3_unidentified_gate_and_controls",
                    "case_4_radarr_suppression_matrix",
                    "case_5_lost_acks_and_recovery",
                    "zero_secret_and_permission_audit",
                },
            )


if __name__ == "__main__":
    unittest.main()
