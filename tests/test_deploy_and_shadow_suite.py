from __future__ import annotations

import os
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
from scripts.run_shadow_verification import run_shadow_suite

REPO_ROOT = Path(__file__).resolve().parent.parent


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
            self.assertFalse(set(intake_env.keys()) & INTAKE_FORBIDDEN_KEYS)
            self.assertFalse(set(actions_env.keys()) & ACTIONS_FORBIDDEN_KEYS)
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
        self.assertNotIn("docker ", suite_text.lower())
        self.assertNotIn("0.0.0.0", suite_text)
        self.assertNotIn("/tmp/", suite_text)
        self.assertIn("UMask=0077", suite_text)
        self.assertIn("NoNewPrivileges=true", suite_text)
        self.assertIn("--host 127.0.0.1", suite_text)
        self.assertIn("--no-access-log", suite_text)
        self.assertIn("app-ports free", suite_text)
        self.assertIn('systemctl --user restart "$ACTIONS_SERVICE" "$INTAKE_SERVICE"', suite_text)
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
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": fake_home,
                "PORT_INTAKE": "40720",
                "PORT_ACTIONS": "40721",
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

        # Nginx template security invariants (§7.3)
        nginx_text = nginx_tpl.read_text(encoding="utf-8")
        self.assertEqual(nginx_text.count("access_log off;"), 2)
        self.assertIn("client_max_body_size 1m;", nginx_text)
        self.assertIn("proxy_pass http://127.0.0.1:<PORT_INTAKE>/webhook/;", nginx_text)
        self.assertIn("proxy_pass http://127.0.0.1:<PORT_ACTIONS>/action/;", nginx_text)
        self.assertNotIn("location ^~ /internal", nginx_text)
        self.assertNotIn("location ^~ /movie-intel/internal", nginx_text)

    async def test_five_case_shadow_verification_runner_passes_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shadow-test-") as tmpdir:
            res = await run_shadow_suite(Path(tmpdir))
            self.assertTrue(res.passed, f"Shadow suite failed: {res.cases}")
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
