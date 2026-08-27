"""T-NEW-119 followup — unit tests for cluster/entrypoint.py.

The entrypoint is the seam that determines whether a sophia container
can actually open its vault on restart. We test all three boot modes
(warm / cold / recovered) with a mocked PyKeyStore so we don't need
the real PyKeyStore installed in the test venv.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_entrypoint():
    """Load cluster/entrypoint.py as a module without importing the
    rest of the sophia package (entrypoint is a standalone script —
    the import side effects would pull in pycurity + ladyol which
    aren't installed in the test venv).
    """
    spec = importlib.util.spec_from_file_location(
        "sophia_entrypoint",
        Path(__file__).parent.parent / "cluster" / "entrypoint.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeVaultFilesystem:
    """Simulates the on-disk layout under VAULT_DIR.

    Use ``with FakeFs(state) as fs`` then read ``fs.passphrase_file`` /
    ``fs.temp_key`` / ``fs.vault_db``.
    """

    def __init__(self, state: str) -> None:
        self.state = state
        self.tmpdir = tempfile.mkdtemp(prefix="sophia-entrypoint-test-")
        self.vault_dir = Path(self.tmpdir) / "pycurity"
        self.vault_dir.mkdir(parents=True)
        self.vault_db = self.vault_dir / "vault.db"
        self.salt_bin = self.vault_dir / "salt.bin"
        self.salt_bin.write_bytes(b"\x00" * 16)
        self.passphrase_file = self.vault_dir / ".master_passphrase"
        self.temp_key = self.vault_dir / "temp.key"

        if state == "warm":
            self.vault_db.touch()
            self.temp_key.write_text("warm-pass")
        elif state == "cold":
            # nothing on disk
            pass
        elif state == "recoverable":
            # vault exists, no temp.key, but the host knows the passphrase
            self.vault_db.touch()
        else:
            raise ValueError(f"unknown state: {state}")

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class EntrypointBootModes(unittest.TestCase):
    """Each test exercises one boot mode and asserts the side-effects."""

    def setUp(self) -> None:
        self.mod = _load_entrypoint()
        # entrypoint reads VAULT_DIR at module import — patch the
        # constants before each test sets up its own dir.
        self._orig_dir = self.mod.VAULT_DIR
        self._orig_db = self.mod.VAULT_DB
        self._orig_salt = self.mod.SALT_BIN
        self._orig_temp = self.mod.TEMP_KEY
        self._orig_pass = self.mod.PASSPHRASE_FILE

    def tearDown(self) -> None:
        self.mod.VAULT_DIR = self._orig_dir
        self.mod.VAULT_DB = self._orig_db
        self.mod.SALT_BIN = self._orig_salt
        self.mod.TEMP_KEY = self._orig_temp
        self.mod.PASSPHRASE_FILE = self._orig_pass

    def _mount(self, fs: _FakeVaultFilesystem) -> None:
        """Repoint the module's constants at a temp directory."""
        self.mod.VAULT_DIR = fs.vault_dir
        self.mod.VAULT_DB = fs.vault_db
        self.mod.SALT_BIN = fs.salt_bin
        self.mod.TEMP_KEY = fs.temp_key
        self.mod.PASSPHRASE_FILE = fs.passphrase_file

    def test_warm_start(self) -> None:
        """vault.db + temp.key present → no-op (write 'Vault ready' log)."""
        fs = _FakeVaultFilesystem("warm")
        self._mount(fs)
        with patch("builtins.print") as print_mock:  # noqa: F841
            self.mod._ensure_vault()
        try:
            self.assertTrue(fs.vault_db.exists())
            self.assertTrue(fs.temp_key.exists())
            self.assertEqual(fs.temp_key.read_text(), "warm-pass")
        finally:
            fs.cleanup()

    def test_cold_start_generates_passphrase(self) -> None:
        """No vault.db → generates random passphrase, writes both files,
        prints the diagnostic token so the operator can capture it."""
        fs = _FakeVaultFilesystem("cold")
        self._mount(fs)
        # Stub PyKeyStore to avoid the real import chain.
        fake_ks = MagicMock()
        fake_ks.list.return_value = [("foo", {})]
        fake_ks_class = MagicMock(return_value=fake_ks)
        with patch.dict(sys.modules, {"pycurity.pykey": MagicMock(PyKeyStore=fake_ks_class)}):
            self.mod._ensure_vault()
        try:
            # Both files exist + hold the same passphrase (auto-recoverable next boot).
            self.assertTrue(fs.passphrase_file.exists())
            self.assertTrue(fs.temp_key.exists())
            pp = fs.passphrase_file.read_text().strip()
            self.assertEqual(fs.temp_key.read_text(), pp)
            self.assertGreater(len(pp), 30, "passphrase should be >= 32 url-safe chars")
        finally:
            fs.cleanup()

    def test_recovery_with_cluster_master_env(self) -> None:
        """vault.db present, temp.key missing, env passphrase set →
        entrypoint unlocks with the env passphrase and writes fresh temp.key."""
        fs = _FakeVaultFilesystem("recoverable")
        self._mount(fs)
        env_pass = "the-cluster-master-passphrase"
        fake_ks = MagicMock()
        fake_ks.list.return_value = [("foo", {})]
        fake_ks_class = MagicMock(return_value=fake_ks)
        with patch.dict(os.environ, {"SOPHIA_CLUSTER_MASTER_PASSPHRASE": env_pass}), \
             patch.dict(sys.modules, {"pycurity.pykey": MagicMock(PyKeyStore=fake_ks_class)}):
            self.mod._ensure_vault()
        try:
            # Recovery wrote both files; subsequent warm boots don't need the env var.
            self.assertTrue(fs.temp_key.exists())
            self.assertEqual(fs.passphrase_file.read_text().strip(), env_pass)
            self.assertEqual(fs.temp_key.read_text(), env_pass)
        finally:
            fs.cleanup()

    def test_recovery_without_env_exits_2(self) -> None:
        """vault.db present, temp.key missing, no env var → sys.exit(2)
        with a recovery message that points the operator at
        seed-keys.sh + the destructive fallback."""
        fs = _FakeVaultFilesystem("recoverable")
        self._mount(fs)
        env = os.environ.copy()
        env.pop("SOPHIA_CLUSTER_MASTER_PASSPHRASE", None)
        try:
            with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as cm:
                self.mod._ensure_vault()
            self.assertEqual(cm.exception.code, 2)
        finally:
            fs.cleanup()

    def test_recovery_with_wrong_passphrase_exits_5(self) -> None:
        """vault.db present, temp.key missing, env passphrase supplied but
        wrong → sys.exit(5) (different code from the no-env case so
        operators can distinguish 'never seeded' from 'seed is stale')."""
        fs = _FakeVaultFilesystem("recoverable")
        self._mount(fs)
        fake_ks_class = MagicMock(side_effect=PermissionError("Invalid passphrase."))
        try:
            with patch.dict(os.environ, {"SOPHIA_CLUSTER_MASTER_PASSPHRASE": "wrong"}), \
                 patch.dict(sys.modules, {"pycurity.pykey": MagicMock(PyKeyStore=fake_ks_class)}), \
                 self.assertRaises(SystemExit) as cm:
                self.mod._ensure_vault()
            self.assertEqual(cm.exception.code, 5)
        finally:
            fs.cleanup()


class EntrypointDiagnosticTokens(unittest.TestCase):
    """The operator greps for VAULT_STATE= / CLUSTER_MASTER_PASSPHRASE=
    / MERGED_COUNT= in docker logs. Verify the tokens fire on each
    mode so the operator workflow doesn't break."""

    def test_cold_start_emits_passphrase_token(self) -> None:
        mod = _load_entrypoint()
        with tempfile.TemporaryDirectory(prefix="sophia-entry-token-") as tmp:
            fs = _FakeVaultFilesystem("cold")
            fs.tmpdir = tmp
            fs.vault_dir = Path(tmp) / "pycurity"
            fs.vault_dir.mkdir(parents=True)
            fs.vault_db = fs.vault_dir / "vault.db"
            fs.salt_bin = fs.vault_dir / "salt.bin"
            fs.salt_bin.write_bytes(b"\x00" * 16)
            fs.passphrase_file = fs.vault_dir / ".master_passphrase"
            fs.temp_key = fs.vault_dir / "temp.key"
            mod.VAULT_DIR = fs.vault_dir
            mod.VAULT_DB = fs.vault_db
            mod.SALT_BIN = fs.salt_bin
            mod.TEMP_KEY = fs.temp_key
            mod.PASSPHRASE_FILE = fs.passphrase_file
            fake_ks = MagicMock()
            fake_ks.list.return_value = []
            fake_ks_class = MagicMock(return_value=fake_ks)
            with patch.dict(sys.modules, {"pycurity.pykey": MagicMock(PyKeyStore=fake_ks_class)}):
                import io
                buf = io.StringIO()
                with patch("sys.stderr", buf):
                    mod._ensure_vault()
                stderr = buf.getvalue()
        self.assertIn("VAULT_STATE=cold", stderr)
        # CLUSTER_MASTER_PASSPHRASE= line is the recoverable one-shot
        # capture token; the operator greps for it.
        self.assertRegex(stderr, r"CLUSTER_MASTER_PASSPHRASE=\S+")


if __name__ == "__main__":
    unittest.main()
