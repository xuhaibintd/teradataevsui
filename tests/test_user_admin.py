from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.auth_store import AuthStore
from app.core.settings import Settings
from app.routers.api import router as api_router
from app.routers.web import router as web_router
from app.runtime import STATIC_DIR, TEMPLATES_DIR
from app.web_support import initialize_app_state


class UserAdminRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        initialize_app_state(
            app,
            Jinja2Templates(directory=str(TEMPLATES_DIR)),
            settings=Settings.from_env(project_dir=Path(self.tempdir.name)),
        )
        store = AuthStore(Path(self.tempdir.name) / "users.db", session_ttl_seconds=600)
        store.initialize()
        store.create_user(username="admin", password="admin-password", role="admin")
        store.create_user(username="reader", password="reader-password", role="viewer")
        app.state.auth_store = store
        app.state.user_sessions = {}
        app.include_router(web_router)
        app.include_router(api_router)
        self.app = app
        self.store = store
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _login(self, username: str, password: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("evsui_sid", response.cookies)

    def test_admin_can_create_and_export_users(self) -> None:
        self._login("admin", "admin-password")

        page = self.client.get("/admin/users")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<h1>System Configuration</h1>", page.text)
        self.assertIn("<title>Teradata Enterprise AI Platform</title>", page.text)
        self.assertIn('<p class="brand-sub">Enterprise AI Platform</p>', page.text)
        self.assertNotIn("teradataevsui", page.text.lower())
        self.assertIn('class="system-config-tab-nav"', page.text)
        self.assertIn('for="system-config-tab-connection">Database Connections</label>', page.text)
        self.assertIn('for="system-config-tab-unstructured">Unstructured IO</label>', page.text)
        self.assertIn('for="system-config-tab-users">User Management</label>', page.text)
        self.assertIn('id="system-config-tab-connection" checked', page.text)
        self.assertIn("Database connections", page.text)
        self.assertIn("Unstructured IO", page.text)
        self.assertIn('action="/admin/unstructured-config"', page.text)
        self.assertIn('name="unstructured_api_url"', page.text)
        self.assertIn('name="unstructured_api_key"', page.text)
        self.assertIn("User Management", page.text)
        self.assertIn('<a class="chip-btn" href="/admin/users">System Configuration</a>', page.text)
        self.assertIn('class="brand-home-link" href="/"', page.text)
        self.assertIn("Back to Teradata Vector Store", page.text)
        self.assertNotIn("Back to EVSUI", page.text)
        self.assertNotIn("unpkg.com/htmx", page.text)
        self.assertNotIn("static/js/app.js", page.text)
        self.assertNotIn("Import users JSON", page.text)
        self.assertNotIn("/admin/users/import", page.text)
        self.assertNotIn("Export users", page.text)
        self.assertIn('value="viewer" selected', page.text)
        self.assertIn('type="text" name="username"', page.text)
        self.assertIn('type="text" name="display_name"', page.text)

        created = self.client.post(
            "/admin/users/create",
            data={
                "username": "operator",
                "display_name": "Operator",
                "password": "operator-password",
                "role": "operator",
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn("operator", created.text)
        self.assertIn("created", created.text)
        self.assertIn('id="system-config-tab-users" checked', created.text)

        exported = self.client.get("/admin/users/export")
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn("operator-password", exported.text)
        self.assertIn("$argon2", exported.text)

    def test_home_keeps_interactive_application_scripts(self) -> None:
        self._login("admin", "admin-password")

        page = self.client.get("/")

        self.assertEqual(page.status_code, 200)
        self.assertIn("unpkg.com/htmx", page.text)
        self.assertIn("static/js/app.js", page.text)
        self.assertNotIn('hx-get="/ui/admin/document-metadata?refresh=true"', page.text)
        self.assertNotIn('hx-get="/ui/admin/document-relations?refresh=true"', page.text)
        self.assertIn('hx-get="/ui/admin/document-governance?refresh=true"', page.text)
        self.assertEqual(page.text.count(">Refresh Vector Stores</button>"), 1)

    def test_login_page_does_not_load_application_scripts(self) -> None:
        page = self.client.get("/login")

        self.assertEqual(page.status_code, 200)
        self.assertNotIn("unpkg.com/htmx", page.text)
        self.assertNotIn("static/js/app.js", page.text)

    def test_empty_installation_requires_one_time_admin_setup(self) -> None:
        with self.store._connect() as connection:
            connection.execute("DELETE FROM users")

        login = self.client.get("/login", follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/setup")

        setup = self.client.get("/setup")
        self.assertEqual(setup.status_code, 200)
        self.assertIn("Create administrator", setup.text)
        self.assertIn('name="confirm_password"', setup.text)
        self.assertNotIn("unpkg.com/htmx", setup.text)
        self.assertNotIn("static/js/app.js", setup.text)

        mismatch = self.client.post(
            "/setup",
            data={
                "username": "first_admin",
                "password": "first-admin-password",
                "confirm_password": "different-password",
            },
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("Passwords do not match.", mismatch.text)
        self.assertNotIn("first-admin-password", mismatch.text)
        self.assertEqual(self.store.count_users(), 0)

        too_short = self.client.post(
            "/setup",
            data={
                "username": "first_admin",
                "password": "short",
                "confirm_password": "short",
            },
        )
        self.assertEqual(too_short.status_code, 400)
        self.assertIn("at least 8 characters", too_short.text)
        self.assertNotIn('value="short"', too_short.text)
        self.assertEqual(self.store.count_users(), 0)

        created = self.client.post(
            "/setup",
            data={
                "username": "first_admin",
                "password": "first-admin-password",
                "confirm_password": "first-admin-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        self.assertEqual(created.headers["location"], "/login?setup=complete")
        self.assertEqual(self.store.count_users(), 1)

        login_after_setup = self.client.get(created.headers["location"])
        self.assertIn("Administrator created. Sign in with the new account.", login_after_setup.text)
        closed = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(closed.status_code, 303)
        self.assertEqual(closed.headers["location"], "/login")
        self._login("first_admin", "first-admin-password")

    def test_viewer_cannot_open_user_admin(self) -> None:
        self._login("reader", "reader-password")

        response = self.client.get("/admin/users")

        self.assertEqual(response.status_code, 403)

    def test_forged_legacy_cookies_do_not_authenticate(self) -> None:
        self.client.cookies.set("evsui_auth", "1")
        self.client.cookies.set("evsui_user", "admin")
        self.client.cookies.set("evsui_sid", "forged")

        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_invalid_password_does_not_create_session(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "wrong-password"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid username or password.", response.text)
        self.assertNotIn("evsui_sid", response.cookies)

    def test_login_cookie_is_http_only_and_same_site(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-password"},
            follow_redirects=False,
        )

        set_cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)
        self.assertIn("path=/", set_cookie)
        self.assertIn("max-age=600", set_cookie)

    def test_logout_revokes_server_session(self) -> None:
        self._login("admin", "admin-password")
        sid = self.client.cookies.get("evsui_sid")
        self.assertIsNotNone(self.store.get_session(sid))

        response = self.client.post("/logout", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIsNone(self.store.get_session(sid))
        self.assertNotIn("evsui_sid", response.cookies)

    def test_system_connection_configuration_is_saved_and_loaded_for_all_users(self) -> None:
        self._login("admin", "admin-password")

        response = self.client.post(
            "/admin/connection",
            data={
                "connection_name": "Japan Lake",
                "is_default": "true",
                "host": "db.example.com",
                "username": "db_admin",
                "password": "database-password",
                "ues_url": "https://example.com/open-analytics",
                "pat_token": "private-pat-token",
            },
            files={
                "pem_file": (
                    "admin.pem",
                    b"-----BEGIN CERTIFICATE-----\nroute-certificate\n-----END CERTIFICATE-----\n",
                    "application/x-pem-file",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Connection &#39;Japan Lake&#39; saved.", response.text)
        self.assertNotIn('value="database-password"', response.text)
        self.assertNotIn('value="private-pat-token"', response.text)
        self.assertIn("Stored encrypted in SQLite: admin.pem", response.text)
        self.assertNotIn("Saved path:", response.text)
        admin = self.store.authenticate("admin", "admin-password")
        saved = self.store.get_connection_profile()
        self.assertEqual(saved["name"], "Japan Lake")
        self.assertEqual(saved["host"], "db.example.com")
        self.assertEqual(saved["password"], "database-password")
        self.assertEqual(saved["pat_token"], "private-pat-token")
        self.assertEqual(saved["pem_filename"], "admin.pem")
        self.assertTrue(saved["pem_in_database"])
        self.assertEqual(Path(saved["pem_file"]).read_bytes(), b"-----BEGIN CERTIFICATE-----\nroute-certificate\n-----END CERTIFICATE-----\n")

        self.client.post("/logout")
        self._login("reader", "reader-password")
        sid = self.client.cookies.get("evsui_sid")
        params = self.app.state.user_sessions[sid]["evs_state"]["params"]
        self.assertEqual(params["host"], "db.example.com")
        self.assertEqual(params["username"], "db_admin")
        self.assertEqual(params["password"], "database-password")
        self.assertEqual(params["pem_filename"], "admin.pem")

        home = self.client.get("/")
        self.assertIn('name="connection_id"', home.text)
        self.assertIn("Japan Lake · db.example.com · db_admin", home.text)

    def test_admin_can_save_shared_unstructured_configuration(self) -> None:
        self._login("admin", "admin-password")

        response = self.client.post(
            "/admin/unstructured-config",
            data={
                "unstructured_api_url": " https://session.example/api ",
                "unstructured_api_key": " session-key ",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Shared Unstructured IO configuration saved.", response.text)
        self.assertIn('id="system-config-tab-unstructured" checked', response.text)
        sid = self.client.cookies.get("evsui_sid")
        params = self.app.state.user_sessions[sid]["evs_state"]["params"]
        self.assertEqual(params["unstructured_api_url"], "https://session.example/api")
        self.assertEqual(params["unstructured_api_key"], "session-key")
        stored = self.store.get_unstructured_config()
        self.assertEqual(stored["api_url"], "https://session.example/api")
        self.assertEqual(stored["api_key"], "session-key")
        self.assertNotIn("session-key", response.text)
        self.assertIn("Saved — leave blank to keep", response.text)

        retained = self.client.post(
            "/admin/unstructured-config",
            data={
                "unstructured_api_url": "https://updated.example/api",
                "unstructured_api_key": "",
            },
        )
        self.assertEqual(retained.status_code, 200)
        self.assertEqual(params["unstructured_api_key"], "session-key")
        self.client.post("/logout")
        self._login("reader", "reader-password")
        reader_sid = self.client.cookies.get("evsui_sid")
        reader_params = self.app.state.user_sessions[reader_sid]["evs_state"]["params"]
        self.assertEqual(reader_params["unstructured_api_url"], "https://updated.example/api")
        self.assertEqual(reader_params["unstructured_api_key"], "session-key")

    def test_connect_uses_database_pem_with_its_original_key_id_filename(self) -> None:
        self._login("admin", "admin-password")
        pem_payload = b"synthetic-test-pem-content"
        saved = self.client.post(
            "/admin/connection",
            data={
                "connection_name": "Lake Key Test",
                "is_default": "true",
                "host": "db.example.com",
                "username": "db_admin",
                "password": "database-password",
                "ues_url": "https://example.com/open-analytics",
                "pat_token": "private-pat-token",
            },
            files={"pem_file": ("lake-key.pem", pem_payload, "application/x-pem-file")},
        )
        self.assertEqual(saved.status_code, 200)

        with mock.patch("app.routers.web.create_context") as create_context_mock, mock.patch(
            "app.routers.web.set_auth_token"
        ) as set_auth_token_mock, mock.patch(
            "app.routers.web._cleanup_context", return_value={"vs_disconnect": "ok", "remove_context": "ok"}
        ), mock.patch("app.routers.web.VSManager", object()):
            response = self.client.post("/ui/evs/connect")

        self.assertEqual(response.status_code, 200)
        create_context_mock.assert_called_once_with(
            host="db.example.com",
            username="db_admin",
            password="database-password",
        )
        pem_path = Path(set_auth_token_mock.call_args.kwargs["pem_file"])
        self.assertEqual(pem_path.name, "lake-key.pem")
        self.assertEqual(pem_path.read_bytes(), pem_payload)

    def test_home_connects_with_the_selected_connection_profile(self) -> None:
        self._login("admin", "admin-password")
        admin = self.store.authenticate("admin", "admin-password")
        pem_payload = b"synthetic-test-pem-content"
        common = {
            "password": "database-password",
            "pat_token": "private-pat-token",
            "pem_filename": "lake-key.pem",
            "pem_content": pem_payload,
        }
        self.store.save_connection_profile(
            admin.user_id,
            common | {
                "name": "Japan Lake",
                "host": "japan.example.com",
                "username": "japan_user",
                "ues_url": "https://japan.example.com/open-analytics",
                "is_default": True,
            },
        )
        us_profile = self.store.save_connection_profile(
            admin.user_id,
            common | {
                "name": "US Lake",
                "host": "us.example.com",
                "username": "us_user",
                "ues_url": "https://us.example.com/open-analytics",
            },
        )

        with mock.patch("app.routers.web.create_context") as create_context_mock, mock.patch(
            "app.routers.web.set_auth_token"
        ), mock.patch(
            "app.routers.web._cleanup_context", return_value={"vs_disconnect": "ok", "remove_context": "ok"}
        ), mock.patch("app.routers.web.VSManager", object()):
            response = self.client.post("/ui/evs/connect", data={"connection_id": us_profile["id"]})

        self.assertEqual(response.status_code, 200)
        create_context_mock.assert_called_once_with(
            host="us.example.com",
            username="us_user",
            password="database-password",
        )
        sid = self.client.cookies.get("evsui_sid")
        state = self.app.state.user_sessions[sid]["evs_state"]
        self.assertEqual(state["selected_connection_id"], us_profile["id"])
        self.assertEqual(state["selected_connection_name"], "US Lake")

    def test_viewer_cannot_modify_unstructured_configuration(self) -> None:
        self._login("reader", "reader-password")

        response = self.client.post(
            "/admin/unstructured-config",
            data={
                "unstructured_api_url": "https://forged.example/api",
                "unstructured_api_key": "forged-key",
            },
        )

        self.assertEqual(response.status_code, 403)
        sid = self.client.cookies.get("evsui_sid")
        params = self.app.state.user_sessions[sid]["evs_state"]["params"]
        self.assertNotEqual(params.get("unstructured_api_url"), "https://forged.example/api")
        self.assertNotEqual(params.get("unstructured_api_key"), "forged-key")

    def test_viewer_cannot_modify_system_connection_configuration(self) -> None:
        self._login("reader", "reader-password")
        response = self.client.post(
            "/admin/connection",
            data={
                "connection_name": "Forged",
                "host": "forged-host",
                "username": "forged-user",
                "password": "forged-password",
                "ues_url": "https://example.com/open-analytics",
                "pat_token": "forged-token",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.list_connection_profiles(), [])


if __name__ == "__main__":
    unittest.main()
