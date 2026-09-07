from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.fernet import Fernet

from app.auth_store import AuthStore
from app.session_state import SessionAwareState, new_session_scope


class AuthStoreTests(unittest.TestCase):
    def _store(self, directory: str, name: str = "auth.db") -> AuthStore:
        store = AuthStore(Path(directory) / name, session_ttl_seconds=600)
        store.initialize()
        return store

    def test_explicit_credential_key_is_used_without_creating_a_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "must-not-be-created.key"
            store = AuthStore(
                Path(tmpdir) / "auth.db",
                credential_key=Fernet.generate_key().decode("ascii"),
                credential_key_file=key_file,
                allow_generated_credential_key=False,
            )

            encrypted = store._encrypt_credential("secret")

            self.assertEqual(store._decrypt_credential(encrypted), "secret")
            self.assertFalse(key_file.exists())

    def test_production_style_store_does_not_generate_a_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "missing.key"
            store = AuthStore(
                Path(tmpdir) / "auth.db",
                credential_key_file=key_file,
                allow_generated_credential_key=False,
            )

            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                store._encrypt_credential("secret")
            self.assertFalse(key_file.exists())

    def test_user_password_and_server_session_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            created = store.create_user(
                username="admin",
                password="strong-password",
                role="admin",
            )

            self.assertIsNone(store.authenticate("admin", "wrong-password"))
            principal = store.authenticate("admin", "strong-password")
            self.assertIsNotNone(principal)
            self.assertEqual(principal.role, "admin")

            session_id = store.create_session(created)
            self.assertEqual(store.get_session(session_id).username, "admin")
            store.revoke_session(session_id)
            self.assertIsNone(store.get_session(session_id))

    def test_export_import_preserves_argon2_hashes_and_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = self._store(tmpdir, "source.db")
            source.create_user(username="admin", password="strong-password", role="admin")
            source.create_user(username="reader", password="reader-password", role="viewer")
            payload = source.export_users()

            self.assertNotIn("strong-password", str(payload))
            self.assertTrue(payload["users"][0]["password_hash"].startswith("$argon2"))

            target = self._store(tmpdir, "target.db")
            self.assertEqual(target.import_users(payload), 2)
            self.assertEqual(target.authenticate("reader", "reader-password").role, "viewer")

    def test_last_enabled_admin_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            store.create_user(username="admin", password="strong-password", role="admin")

            with self.assertRaisesRegex(ValueError, "last enabled administrator"):
                store.set_enabled("admin", False)
            with self.assertRaisesRegex(ValueError, "last enabled administrator"):
                store.set_role("admin", "viewer")

    def test_five_failed_logins_lock_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            store.create_user(username="admin", password="strong-password", role="admin")

            for _ in range(5):
                self.assertIsNone(store.authenticate("admin", "wrong-password"))

            self.assertIsNone(store.authenticate("admin", "strong-password"))
            user = store.list_users()[0]
            self.assertEqual(user["failed_login_count"], 0)
            self.assertGreater(int(user["locked_until"]), int(time.time()))

    def test_concurrent_failed_logins_cannot_bypass_account_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            store.create_user(username="admin", password="strong-password", role="admin")

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(
                    executor.map(
                        lambda _: store.authenticate("admin", "wrong-password"),
                        range(20),
                    )
                )

            self.assertEqual(results, [None] * 20)
            self.assertIsNone(store.authenticate("admin", "strong-password"))
            user = store.list_users()[0]
            self.assertGreater(int(user["locked_until"]), int(time.time()))

    def test_disabling_user_and_resetting_password_revoke_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            store.create_user(username="admin", password="admin-password", role="admin")
            reader = store.create_user(username="reader", password="reader-password", role="viewer")

            disabled_session = store.create_session(reader)
            store.set_enabled("reader", False)
            self.assertIsNone(store.get_session(disabled_session))
            self.assertIsNone(store.authenticate("reader", "reader-password"))

            store.set_enabled("reader", True)
            reset_session = store.create_session(reader)
            store.reset_password("reader", "replacement-password")
            self.assertIsNone(store.get_session(reset_session))
            self.assertIsNone(store.authenticate("reader", "reader-password"))
            self.assertIsNotNone(store.authenticate("reader", "replacement-password"))

    def test_system_connection_config_is_encrypted_and_admin_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            reader = store.create_user(username="reader", password="reader-password", role="viewer")
            pem_payload = b"-----BEGIN CERTIFICATE-----\ntest-certificate\n-----END CERTIFICATE-----\n"
            admin_values = {
                "host": "db.example.com",
                "username": "db_admin",
                "password": "database-password",
                "ues_url": "https://example.com/open-analytics",
                "pat_token": "private-pat-token",
                "pem_file": "",
                "pem_filename": "admin.pem",
                "pem_content": pem_payload,
            }

            store.save_system_connection_config(admin.user_id, admin_values)

            saved = store.get_system_connection_config()
            self.assertEqual(saved["host"], "db.example.com")
            self.assertEqual(saved["pem_filename"], "admin.pem")
            self.assertTrue(saved["pem_in_database"])
            self.assertEqual(Path(saved["pem_file"]).name, "admin.pem")
            self.assertEqual(Path(saved["pem_file"]).read_bytes(), pem_payload)
            with self.assertRaises(PermissionError):
                store.save_system_connection_config(
                    reader.user_id,
                    admin_values | {"username": "db_reader"},
                )

            connection = sqlite3.connect(store.database_path)
            try:
                rows = connection.execute(
                    "SELECT password_ciphertext, pat_token_ciphertext, pem_ciphertext FROM system_connection_config"
                ).fetchall()
            finally:
                connection.close()
            raw_payload = str(rows)
            self.assertNotIn("database-password", raw_payload)
            self.assertNotIn("private-pat-token", raw_payload)
            self.assertNotIn("test-certificate", raw_payload)
            self.assertTrue(store.database_path.with_suffix(".credentials.key").exists())

    def test_unstructured_config_is_shared_encrypted_and_admin_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            reader = store.create_user(username="reader", password="reader-password", role="viewer")

            saved = store.save_unstructured_config(
                admin.user_id,
                api_url="https://unstructured.example/api/v1",
                api_key="private-unstructured-key",
            )

            self.assertEqual(saved["api_url"], "https://unstructured.example/api/v1")
            self.assertEqual(saved["api_key"], "private-unstructured-key")
            with self.assertRaises(PermissionError):
                store.save_unstructured_config(
                    reader.user_id,
                    api_url="https://forged.example/api",
                    api_key="forged-key",
                )
            connection = sqlite3.connect(store.database_path)
            try:
                raw = str(connection.execute("SELECT * FROM external_service_configs").fetchall())
            finally:
                connection.close()
            self.assertNotIn("private-unstructured-key", raw)
            self.assertIn("https://unstructured.example/api/v1", raw)

    def test_initial_admin_is_created_once_with_a_hashed_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            principal = store.create_initial_admin(
                username="first_admin",
                password="first-admin-password",
            )

            self.assertEqual(principal.role, "admin")
            self.assertIsNotNone(store.authenticate("first_admin", "first-admin-password"))
            with self.assertRaisesRegex(RuntimeError, "already configured"):
                store.create_initial_admin(
                    username="second_admin",
                    password="second-admin-password",
                )
            with store._connect() as connection:
                raw = str(connection.execute("SELECT * FROM users").fetchall())
                audit = connection.execute(
                    "SELECT action FROM audit_logs WHERE username='first_admin'"
                ).fetchone()
            self.assertNotIn("first-admin-password", raw)
            self.assertEqual(str(audit["action"]), "user.initial_admin")

    def test_concurrent_initial_admin_creation_allows_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            def create(username: str) -> str:
                try:
                    return store.create_initial_admin(
                        username=username,
                        password=f"{username}-password",
                    ).username
                except RuntimeError:
                    return "rejected"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ("first_admin", "second_admin")))

            self.assertEqual(results.count("rejected"), 1)
            self.assertEqual(store.count_users(), 1)

    def test_legacy_pem_path_is_migrated_into_encrypted_database_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            source = project_root / "uploads" / "pem" / "legacy.pem"
            source.parent.mkdir(parents=True)
            payload = b"-----BEGIN CERTIFICATE-----\nlegacy-certificate\n-----END CERTIFICATE-----\n"
            source.write_bytes(payload)
            store = AuthStore(
                project_root / "data" / "auth.db",
                session_ttl_seconds=600,
                pem_runtime_dir=project_root / "pem_runtime",
                legacy_file_root=project_root,
            )
            store.initialize()
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            store.save_system_connection_config(
                admin.user_id,
                {
                    "host": "db.example.com",
                    "username": "db_admin",
                    "password": "database-password",
                    "ues_url": "https://example.com/open-analytics",
                    "pat_token": "private-pat-token",
                    "pem_file": "uploads/pem/legacy.pem",
                },
            )

            self.assertTrue(store.migrate_legacy_system_pem())
            saved = store.get_system_connection_config()
            self.assertTrue(saved["pem_in_database"])
            self.assertEqual(saved["pem_filename"], "legacy.pem")
            self.assertEqual(Path(saved["pem_file"]).name, "legacy.pem")
            self.assertEqual(Path(saved["pem_file"]).read_bytes(), payload)
            connection = sqlite3.connect(store.database_path)
            try:
                row = connection.execute(
                    "SELECT pem_file, pem_ciphertext FROM system_connection_config WHERE config_id=1"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], "")
            self.assertNotIn("legacy-certificate", row[1])

    def test_legacy_connection_config_bootstraps_first_admin_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            values = {"host": "legacy-host", "username": "legacy-user", "password": "legacy-password"}

            self.assertEqual(store.bootstrap_connection_config(values), 1)
            self.assertEqual(store.bootstrap_connection_config({"host": "replacement"}), 0)
            self.assertEqual(store.get_system_connection_config()["host"], "legacy-host")

    def test_connection_profiles_support_crud_and_default_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            first = store.save_connection_profile(
                admin.user_id,
                {
                    "name": "Japan Lake",
                    "host": "japan.example.com",
                    "username": "japan_user",
                    "password": "japan-password",
                    "ues_url": "https://japan.example.com/open-analytics",
                    "pat_token": "japan-token",
                    "is_default": True,
                },
            )
            second = store.save_connection_profile(
                admin.user_id,
                {
                    "name": "US Lake",
                    "host": "us.example.com",
                    "username": "us_user",
                    "password": "us-password",
                    "ues_url": "https://us.example.com/open-analytics",
                    "pat_token": "us-token",
                    "pem_filename": "us-key.pem",
                    "pem_content": b"synthetic-test-pem-content",
                    "is_default": True,
                },
            )
            second_runtime_pem = Path(second["pem_file"])
            self.assertTrue(second_runtime_pem.is_file())

            self.assertEqual(len(store.list_connection_profiles()), 2)
            self.assertEqual(store.get_connection_profile()["id"], second["id"])
            updated = store.save_connection_profile(
                admin.user_id,
                {
                    "name": "US Lake Updated",
                    "host": "us2.example.com",
                    "username": "us_user",
                    "password": "",
                    "ues_url": "https://us.example.com/open-analytics",
                    "pat_token": "",
                },
                profile_id=second["id"],
            )
            self.assertEqual(updated["host"], "us2.example.com")
            self.assertEqual(updated["password"], "us-password")
            store.delete_connection_profile(admin.user_id, second["id"])
            self.assertFalse(second_runtime_pem.exists())
            self.assertEqual(store.get_connection_profile()["id"], first["id"])
            self.assertTrue(store.get_connection_profile()["is_default"])

    def test_previous_per_user_connection_is_migrated_to_system_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            admin = store.create_user(username="admin", password="admin-password", role="admin")
            now = 1
            with store._connect() as connection:
                connection.execute(
                    """INSERT INTO connection_configs(
                           user_id, host, username, password_ciphertext, ues_url,
                           pat_token_ciphertext, pem_file, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        admin.user_id,
                        "old-host",
                        "old-user",
                        store._encrypt_credential("old-password"),
                        "https://old.example.com/open-analytics",
                        store._encrypt_credential("old-pat"),
                        "uploads/pem/old.pem",
                        now,
                        now,
                    ),
                )

            self.assertEqual(store.bootstrap_connection_config({"host": "local-host"}), 1)
            self.assertEqual(store.get_system_connection_config()["host"], "old-host")
            with store._connect() as connection:
                legacy_count = connection.execute("SELECT COUNT(*) FROM connection_configs").fetchone()[0]
            self.assertEqual(legacy_count, 0)


class SessionAwareStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_tasks_keep_independent_ui_state(self) -> None:
        state = SessionAwareState()
        scope_a = new_session_scope("alice", lambda: {"value": "a"}, dict)
        scope_b = new_session_scope("bob", lambda: {"value": "b"}, dict)

        async def update(scope: dict, value: str) -> str:
            state.activate_session_scope(scope)
            state.evs_state["value"] = value
            await asyncio.sleep(0)
            return state.evs_state["value"]

        values = await asyncio.gather(update(scope_a, "alice"), update(scope_b, "bob"))

        self.assertEqual(values, ["alice", "bob"])
        self.assertEqual(scope_a["evs_state"]["value"], "alice")
        self.assertEqual(scope_b["evs_state"]["value"], "bob")


if __name__ == "__main__":
    unittest.main()
