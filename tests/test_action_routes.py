"""HTTP action regressions with real routing, middleware and isolated SQLite."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.core.errors import configure_error_handlers
from app.core.runtime_manager import RuntimeIsolationMiddleware
from app.core.security import SecurityMiddleware
from app.core.settings import Settings
from app.routers.api import router as api_router
from app.routers.web import router as web_router
from app.runtime import STATIC_DIR, TEMPLATES_DIR
from app.web_support import initialize_app_state
from app.utils.uploads import collect_upload_files


WRITE_ACTIONS = (
    "/ui/create/upload-documents",
    "/ui/create/parse-documents",
    "/ui/create/generate-csv",
    "/ui/create/load-csv-tables",
    "/ui/create/multi-format/parse-documents",
    "/ui/create/multi-format/generate-csv",
    "/ui/create/multi-format/load-csv-table",
    "/ui/create/upload",
    "/ui/evs/destroy",
    "/ui/evs/sessions",
    "/ui/evs/sessions/disconnect",
    "/ui/admin/bookrag-section-rules",
    "/ui/admin/document-metadata/autofill",
    "/ui/admin/document-metadata/save",
    "/ui/admin/document-metadata/import",
    "/ui/admin/document-relations/initialize",
    "/ui/admin/document-relations/save",
    "/ui/admin/document-relations/delete",
    "/ui/admin/document-relations/import",
)
ADMIN_ACTIONS = (
    "/admin/connection",
    "/admin/connections/1/delete",
    "/admin/unstructured-config",
    "/admin/users/create",
    "/admin/users/reader/toggle",
    "/admin/users/reader/password",
    "/admin/users/reader/role",
)
READ_ACTIONS = (
    "/ui/evs/health", "/ui/evs/list", "/ui/evs/refresh", "/ui/chat/vs-list",
    "/ui/evs/select", "/ui/chat", "/ui/chat/reset", "/ui/evs/reset",
)
GOVERNANCE_DB_WRITES = tuple(path for path in WRITE_ACTIONS if "/document-" in path)


class ActionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.resources.enter_context(mock.patch.dict("os.environ", {
            "EVSUI_ENVIRONMENT": "test",
            "EVSUI_DATABASE_PATH": str(root / "data" / "evsui.db"),
            "EVSUI_CREDENTIAL_KEY_FILE": str(root / "data" / "test.key"),
            "EVSUI_CREDENTIAL_KEY": "",
            "EVSUI_EXTERNAL_API_ENABLED": "false",
            "EVSUI_API_TOKEN": "",
            "EVSUI_BOOTSTRAP_ADMIN": "", "EVSUI_BOOTSTRAP_PASSWORD": "",
        }))
        self.resources.enter_context(mock.patch(
            "app.services.unstructured_json_inspector.INSPECTOR_SOURCES",
            {"raw_stage": ("Raw Stage", root / "raw"), "debug_output": ("Debug Output", root / "debug")},
        ))
        for name, value in (
            ("PROJECT_DIR", root),
            ("_load_auth_users", lambda: {}),
            ("_load_connect_defaults", lambda: {}),
            ("poc_admin_credentials", lambda: ("", "")),
        ):
            self.resources.enter_context(mock.patch(f"app.web_support.{name}", value))
        self.sql = self.resources.enter_context(mock.patch(
            "app.routers.web.execute_sql", side_effect=AssertionError("Unexpected external SQL")
        ))
        self.resources.enter_context(mock.patch(
            "app.web_support.create_context", side_effect=AssertionError("Unexpected external connection")
        ))
        self.app = FastAPI()
        configure_error_handlers(self.app)
        self.app.add_middleware(SecurityMiddleware)
        self.app.add_middleware(RuntimeIsolationMiddleware)
        self.app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        initialize_app_state(
            self.app,
            Jinja2Templates(directory=str(TEMPLATES_DIR)),
            settings=Settings.from_env(project_dir=root),
        )
        self.store = self.app.state.auth_store
        for name, role in (("admin", "admin"), ("operator", "operator"), ("reader", "viewer")):
            self.store.create_user(username=name, password=f"{name}-password", role=role)
        self.app.include_router(web_router)
        self.app.include_router(api_router)
        self.client = self.resources.enter_context(TestClient(self.app))

    def login(self, name: str = "admin", client: TestClient | None = None) -> str:
        client = client or self.client
        response = client.post("/login", data={"username": name, "password": f"{name}-password"})
        self.assertEqual(response.status_code, 200)
        return client.cookies.get("evsui_sid")

    def connection(self, name="First", **overrides):
        data = {
            "connection_name": name, "host": "db.invalid", "username": "db-user",
            "password": "database-secret", "pat_token": "pat-secret-value",
            "ues_url": "https://service.invalid/open-analytics",
        } | overrides
        return self.client.post("/admin/connection", data=data, files={
            "pem_file": ("fixture.pem", b"fixture-private-key", "application/x-pem-file")
        })

    def test_all_business_actions_reject_anonymous_requests(self):
        for path in WRITE_ACTIONS + READ_ACTIONS:
            with self.subTest(path=path):
                response = self.client.post(path, follow_redirects=False)
                self.assertEqual(response.status_code, 401)
        self.sql.assert_not_called()

    def test_viewer_cannot_execute_business_writes(self):
        self.login("reader")
        for path in WRITE_ACTIONS:
            with self.subTest(path=path):
                response = self.client.post(path)
                self.assertEqual(response.status_code, 403)
        self.sql.assert_not_called()
        self.assertEqual(self.store.jobs.list_recent(), [])

    def test_parse_rejects_missing_documents_without_queuing_jobs(self):
        self.login("operator")
        for path in ("/ui/create/parse-documents", "/ui/create/multi-format/parse-documents"):
            for data in ({}, {"vector_store_name": "fixture"}):
                with self.subTest(path=path, data=data):
                    response = self.client.post(path, data=data)
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.jobs.list_recent(), [])

    def test_uploaded_documents_can_be_parsed_before_choosing_a_vector_store_name(self):
        sid = self.login("operator")
        self.app.state.user_sessions[sid]["document_uploads"] = [{
            "doc_id": "fixture-doc", "filename": "fixture.txt", "saved_path": "fixture-upload.txt"
        }]
        for path in ("/ui/create/parse-documents", "/ui/create/multi-format/parse-documents"):
            with self.subTest(path=path):
                response = self.client.post(path)
                self.assertEqual(response.status_code, 200)
        jobs = self.store.jobs.list_recent()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job["payload"]["vector_store_name"] == "" for job in jobs))

    def test_csv_generation_rejects_loaded_target_without_queuing(self):
        self.login("operator")
        cases = (
            (
                "/ui/create/generate-csv",
                "app.routers.web.ensure_bookrag_csv_generation_target_available",
                {
                    "bookrag_parse_run_id": "parse-run",
                    "bookrag_csv_vector_store_name": "existing_store",
                    "bookrag_csv_target_database": "target_db",
                },
            ),
            (
                "/ui/create/multi-format/generate-csv",
                "app.routers.web.ensure_multi_format_csv_generation_target_available",
                {
                    "multi_format_parse_run_id": "parse-run",
                    "multi_format_csv_vector_store_name": "existing_store",
                    "multi_format_csv_target_database": "target_db",
                },
            ),
        )
        for path, check, data in cases:
            with self.subTest(path=path):
                job_count = len(self.store.jobs.list_recent())
                with mock.patch(
                    check,
                    side_effect=RuntimeError(
                        "Target Vector Store 'existing_store' already has loaded tables in database "
                        "'target_db'. Use a different Target Vector Store Name; CSV generation was not started."
                    ),
                ):
                    response = self.client.post(path, data=data)

                self.assertEqual(response.status_code, 200)
                self.assertIn("already has loaded tables", response.text)
                self.assertEqual(len(self.store.jobs.list_recent()), job_count)

    def test_multipart_uploads_close_after_success_failure_and_ignored_fields(self):
        sid = self.login("operator")
        self.app.state.user_sessions[sid]["evs_state"]["connected"] = True
        self.app.state.ensure_session_runtime = lambda *_: None
        for path in ("/ui/create/upload-documents", "/ui/create/upload"):
            for fail in (False, True):
                with self.subTest(path=path, fail=fail):
                    captured = []

                    def collect(form, field_name="files"):
                        captured.extend(value for _, value in form.multi_items() if isinstance(value, UploadFile))
                        return collect_upload_files(form, field_name)

                    async def save(_files):
                        if fail:
                            raise OSError("Fixture upload disk failure")
                        return [], []

                    client = self.resources.enter_context(TestClient(self.app, raise_server_exceptions=False))
                    client.cookies.update(self.client.cookies)
                    with mock.patch("app.routers.web._collect_upload_files", collect), mock.patch(
                        "app.routers.web._save_document_uploads", save
                    ):
                        if fail:
                            with self.assertLogs("evsui.errors", level="ERROR"):
                                response = client.post(path, files={
                                    "files": ("fixture.txt", b"content"), "ignored": ("ignored.txt", b"ignored")
                                })
                            self.assertEqual(response.status_code, 500)
                        else:
                            response = client.post(path, files={
                                "files": ("fixture.txt", b"content"), "ignored": ("ignored.txt", b"ignored")
                            })
                            self.assertEqual(response.status_code, 200)
                    self.assertEqual(len(captured), 2)
                    self.assertTrue(all(upload.file.closed for upload in captured))

    def test_parse_queues_long_provider_secret_encrypted_without_truncation(self):
        sid = self.login("operator")
        self.app.state.user_sessions[sid]["document_uploads"] = [{
            "doc_id": "fixture-doc", "filename": "fixture.txt", "saved_path": "fixture-upload.txt"
        }]
        secret = "fixture-provider-key-" + "x" * 256
        for path, field in (
            ("/ui/create/parse-documents", "multi_format_bookrag_vlm_provider_api_key"),
            ("/ui/create/multi-format/parse-documents", "multi_format_vlm_provider_api_key"),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, data={"vector_store_name": "fixture", field: secret})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(secret, response.text)
                claimed = self.store.jobs.claim_next()
                self.assertEqual(claimed["secret_payload"]["create_values"][field], secret)
                self.assertNotIn(secret, json.dumps(claimed["payload"]))

    def test_operator_and_viewer_cannot_execute_system_admin_actions(self):
        for username in ("operator", "reader"):
            self.login(username)
            for path in ADMIN_ACTIONS:
                with self.subTest(username=username, path=path):
                    self.assertEqual(self.client.post(path).status_code, 403)
            for path in ("/admin/users", "/admin/users/export"):
                self.assertEqual(self.client.get(path).status_code, 403)
            response = self.client.post("/admin/users/import", files={
                "users_file": ("users.json", b'{"users":[]}', "application/json")
            })
            self.assertEqual(response.status_code, 403)

    def test_session_management_is_admin_only(self):
        for username in ("operator", "reader"):
            self.login(username)
            for path in ("/ui/evs/sessions", "/ui/evs/sessions/disconnect"):
                with self.subTest(username=username, path=path):
                    self.assertEqual(self.client.post(path).status_code, 403)

    def test_viewer_can_read_and_reset_their_own_session(self):
        self.login("reader")
        with mock.patch("app.routers.web._cleanup_context", return_value={}):
            for path in READ_ACTIONS:
                with self.subTest(path=path):
                    self.assertEqual(self.client.post(path, data={"message": "fixture question"}).status_code, 200)
        for path in (
            "/",
            "/ui/admin/document-governance",
            "/ui/admin/document-metadata",
            "/ui/admin/document-relations",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.sql.assert_not_called()

    def test_json_inspector_requires_login_and_rejects_path_escape(self):
        self.assertEqual(self.client.get("/ui/admin/json-inspector").status_code, 401)
        self.login("reader")
        response = self.client.get("/ui/admin/json-inspector", params={"json_file": "raw_stage:../../outside.json"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("outside the allowed inspector roots", response.text)

    def test_disconnected_governance_actions_never_use_another_session_runtime(self):
        self.login("operator")
        for path in GOVERNANCE_DB_WRITES:
            with self.subTest(path=path):
                response = self.client.post(path, data={"vector_store_name": "safe_fixture"})
                self.assertEqual(response.status_code, 409)
                self.assertIn("Connect", response.text)
        for path in ("/ui/admin/document-metadata/export", "/ui/admin/document-relations/export"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, params={"vector_store_name": "safe_fixture"}).status_code, 409)
        self.sql.assert_not_called()

    def test_new_connection_does_not_inherit_existing_credentials(self):
        self.login()
        self.assertEqual(self.connection().status_code, 200)
        response = self.client.post("/admin/connection", data={
            "connection_name": "Second", "host": "second.invalid", "username": "second-user",
            "ues_url": "https://second.invalid/open-analytics",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.text)
        self.assertIn("pat_token", response.text)
        self.assertIn("pem_file", response.text)
        self.assertEqual(len(self.store.list_connection_profiles()), 1)

    def test_connection_update_preserves_secrets_and_delete_reassigns_default(self):
        self.login()
        self.assertEqual(self.connection().status_code, 200)
        first = self.store.get_connection_profile()
        response = self.client.post("/admin/connection", data={
            "connection_id": first["id"], "connection_name": "Renamed", "host": "renamed.invalid",
            "username": "db-user", "ues_url": "https://service.invalid/open-analytics",
        })
        self.assertEqual(response.status_code, 200)
        saved = self.store.get_connection_profile(first["id"])
        for key in ("password", "pat_token", "pem_filename"):
            self.assertEqual(saved[key], first[key])
        self.assertNotIn("database-secret", response.text)
        self.assertNotIn("pat-secret-value", response.text)
        self.assertEqual(self.connection("Second").status_code, 200)
        response = self.client.post(f"/admin/connections/{first['id']}/delete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get_connection_profile()["name"], "Second")
        self.assertTrue(self.store.get_connection_profile()["is_default"])

    def test_htmx_connect_refreshes_chat_creation_and_governance_gates(self):
        self.login()
        self.assertEqual(self.connection().status_code, 200)
        with mock.patch("app.routers.web.create_context"), mock.patch(
            "app.routers.web.set_auth_token"
        ), mock.patch("app.routers.web.VSManager", object()), mock.patch(
            "app.routers.web._cleanup_context", return_value={}
        ):
            response = self.client.post("/ui/evs/connect", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)
        for panel in ("create", "chat", "admin"):
            self.assertIn(f'id="section-{panel}-content" hx-swap-oob="innerHTML"', response.text)
        self.assertIn('<textarea id="chat-message" name="message" rows="2" required >', response.text)
        self.sql.assert_not_called()

    def test_empty_and_oversize_pem_are_rejected_without_saving_profile(self):
        self.login()
        for payload, message in ((b"", "empty"), (b"x" * (1024 * 1024 + 1), "1 MiB")):
            with self.subTest(size=len(payload)):
                response = self.client.post("/admin/connection", data={
                    "connection_name": "Invalid", "host": "db.invalid", "username": "db-user",
                    "password": "database-secret", "pat_token": "pat-secret-value",
                    "ues_url": "https://service.invalid/open-analytics",
                }, files={"pem_file": ("invalid.pem", payload)})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.text)
        self.assertEqual(self.store.list_connection_profiles(), [])

    def test_last_administrator_cannot_be_disabled_or_demoted(self):
        self.login()
        for path, data in (("toggle", {"enabled": "false"}), ("role", {"role": "viewer"})):
            with self.subTest(action=path):
                response = self.client.post(f"/admin/users/admin/{path}", data=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("last enabled administrator", response.text)
        self.assertEqual(self.store.count_enabled_admins(), 1)

    def test_password_reset_revokes_other_users_active_sessions(self):
        reader = self.resources.enter_context(TestClient(self.app))
        reader_sid = self.login("reader", reader)
        self.login()
        response = self.client.post("/admin/users/reader/password", data={"password": "new-reader-password"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_session(reader_sid))
        self.assertNotIn(reader_sid, self.app.state.user_sessions)
        self.assertEqual(reader.get("/", follow_redirects=False).status_code, 303)
        self.assertIsNone(self.store.authenticate("reader", "reader-password"))
        self.assertIsNotNone(self.store.authenticate("reader", "new-reader-password"))

    def test_invalid_user_import_leaves_existing_users_unchanged(self):
        self.login()
        for payload in ([], {"users": [{}]}, {"version": 2, "users": []}, {"users": [{
            "username": "newuser", "password": "long-enough-password", "enabled": "false"
        }]}):
            with self.subTest(payload=payload):
                response = self.client.post("/admin/users/import", files={
                    "users_file": ("users.json", json.dumps(payload), "application/json")
                })
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.store.count_users(), 3)

    def test_user_import_password_replacement_revokes_previous_sessions(self):
        reader = self.resources.enter_context(TestClient(self.app))
        sid = self.login("reader", reader)
        self.login()
        response = self.client.post("/admin/users/import", files={"users_file": (
            "users.json", json.dumps({"version": 1, "users": [{
                "username": "reader", "password": "imported-password", "role": "viewer"
            }]}), "application/json"
        )})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get_session(sid))
        self.assertNotIn(sid, self.app.state.user_sessions)
        self.assertEqual(reader.get("/", follow_redirects=False).status_code, 303)
        self.assertIsNotNone(self.store.authenticate("reader", "imported-password"))

    def test_import_replacing_current_admin_redirects_to_login(self):
        sid = self.login()
        response = self.client.post("/admin/users/import", follow_redirects=False, files={"users_file": (
            "users.json", json.dumps({"version": 1, "users": [{
                "username": "admin", "password": "imported-admin-password", "role": "admin"
            }]}), "application/json"
        )})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIsNone(self.store.get_session(sid))

    def test_role_demotion_takes_effect_on_existing_session(self):
        operator = self.resources.enter_context(TestClient(self.app))
        sid = self.login("operator", operator)
        self.login()
        response = self.client.post("/admin/users/operator/role", data={"role": "viewer"})
        self.assertEqual(response.status_code, 200)
        response = operator.post("/ui/create/parse-documents")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.app.state.user_sessions[sid]["role"], "viewer")

    def test_selected_governance_read_while_disconnected_never_queries_sql(self):
        self.login("reader")
        for path in (
            "/ui/admin/document-governance",
            "/ui/admin/document-metadata",
            "/ui/admin/document-relations",
        ):
            response = self.client.get(path, params={"vector_store_name": "safe_fixture"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Not Connected", response.text)
        self.sql.assert_not_called()

    def test_governance_load_uses_the_same_vector_store_for_both_sections(self):
        sid = self.login("reader")
        self.app.state.user_sessions[sid]["evs_state"]["connected"] = True
        self.app.state.ensure_session_runtime = lambda *_args, **_kwargs: None
        with mock.patch("app.routers.web.fetch_document_metadata", return_value=[]) as metadata, mock.patch(
            "app.routers.web.fetch_bookrag_documents", return_value=[]
        ) as documents, mock.patch(
            "app.routers.web.document_relation_table_exists", return_value=False
        ):
            response = self.client.get(
                "/ui/admin/document-governance",
                params={"vector_store_name": "shared_fixture"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(metadata.call_args.kwargs["vector_store_name"], "shared_fixture")
        self.assertEqual(documents.call_args.kwargs["vector_store_name"], "shared_fixture")
        self.assertEqual(
            self.app.state.user_sessions[sid]["evs_state"]["bookrag_governance_vs_name"],
            "shared_fixture",
        )
        self.assertEqual(response.text.count('<select name="vector_store_name" required>'), 1)
        self.assertEqual(response.text.count(">Refresh Vector Stores</button>"), 1)

    def test_governance_refresh_failure_keeps_one_picker_and_reports_at_the_top(self):
        self.login("reader")
        failure = {
            "kind": "error",
            "title": "Vector Store Refresh Failed",
            "detail": "Fixture list failure. Try again.",
        }
        with mock.patch(
            "app.routers.web._refresh_document_relation_vector_store_options",
            return_value=failure,
        ):
            response = self.client.get(
                "/ui/admin/document-governance",
                params={"refresh": "true"},
                headers={"HX-Request": "true"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="top-op-stack-shell"', response.text)
        self.assertIn('hx-swap-oob="outerHTML"', response.text)
        self.assertIn("Vector Store Refresh Failed", response.text)
        self.assertEqual(response.text.count('<select name="vector_store_name" required>'), 1)
        self.assertEqual(response.text.count(">Refresh Vector Stores</button>"), 1)
        self.sql.assert_not_called()

    def test_cross_site_and_malformed_origins_reject_writes_without_server_errors(self):
        self.login()
        for origin in ("https://attacker.invalid", "http://testserver:notaport", "http://[broken"):
            with self.subTest(origin=origin):
                response = self.client.post("/admin/users/create", headers={"Origin": origin}, data={
                    "username": "forbidden", "password": "forbidden-password", "role": "viewer"
                })
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.count_users(), 3)

    def test_api_methods_require_authentication(self):
        for endpoint in ("retrieve", "answer"):
            for method in ("GET", "POST"):
                with self.subTest(endpoint=endpoint, method=method):
                    response = self.client.request(
                        method, f"/api/bookrag/{endpoint}",
                        **({"json": {"question": "fixture", "vector_store_name": "fixture"}} if method == "POST" else {})
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.client.get("/api/bookrag/schema", params={"vector_store_name": "fixture"}).status_code, 401)

    def test_api_invalid_input_is_rejected_before_runtime_activation(self):
        self.login("reader")
        with mock.patch("app.routers.api._ensure_api_runtime") as runtime:
            for endpoint in ("retrieve", "answer"):
                for method in ("GET", "POST"):
                    with self.subTest(endpoint=endpoint, method=method):
                        values = {"question": "   ", "vector_store_name": "fixture"}
                        response = self.client.request(method, f"/api/bookrag/{endpoint}", **{
                            "json" if method == "POST" else "params": values
                        })
                        self.assertEqual(response.status_code, 422)
            runtime.assert_not_called()

    def test_api_disconnected_session_returns_actionable_conflict(self):
        self.login("reader")
        for endpoint in ("retrieve", "answer"):
            for method in ("GET", "POST"):
                with self.subTest(endpoint=endpoint, method=method):
                    response = self.client.request(method, f"/api/bookrag/{endpoint}", **{
                        "json" if method == "POST" else "params": {
                            "question": "fixture question", "vector_store_name": "fixture"
                        }
                    })
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("Connect", response.json()["detail"])

    def test_schema_contract_is_available_without_a_database_connection(self):
        self.login("reader")
        response = self.client.get("/api/bookrag/schema", params={"vector_store_name": "fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("document_relations", response.json()["contract"]["tables"])

    def test_external_api_uses_explicit_token_and_reports_missing_configuration(self):
        self.app.state.settings = replace(
            self.app.state.settings, external_api_enabled=True, external_api_token="fixture-api-secret"
        )
        for header in ({"Authorization": "Bearer fixture-api-secret"}, {"X-API-Key": "fixture-api-secret"}):
            with self.subTest(header=next(iter(header))):
                response = self.client.get("/api/bookrag/retrieve", headers=header, params={
                    "question": "fixture question", "vector_store_name": "fixture"
                })
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("fixture-api-secret", response.text)
        self.assertEqual(self.client.get("/api/bookrag/retrieve", headers={"X-API-Key": "wrong"}).status_code, 401)

    def test_non_ascii_origin_and_api_token_do_not_crash_security_checks(self):
        response = self.client.post("/login", headers=[(b"origin", b"http://t\xe9stserver")])
        self.assertEqual(response.status_code, 403)
        self.app.state.settings = replace(
            self.app.state.settings, external_api_enabled=True, external_api_token="fixture-api-secret"
        )
        for name, value in ((b"x-api-key", b"caf\xe9"), (b"authorization", b"Bearer caf\xe9")):
            with self.subTest(header=name):
                response = self.client.get("/api/bookrag/retrieve", headers=[(name, value)])
                self.assertEqual(response.status_code, 401)

    def test_retrieve_and_answer_http_contract_and_session_history_isolation(self):
        reader = self.resources.enter_context(TestClient(self.app))
        reader_sid = self.login("reader", reader)
        operator_sid = self.login("operator")
        for sid in (reader_sid, operator_sid):
            self.app.state.user_sessions[sid]["evs_state"]["connected"] = True
        evidence = {
            "vector_store_name": "fixture", "package_count": 1,
            "retrieval_scope": {"allowed_doc_ids": ["doc-1"]},
            "packages": [{
                "rank": 1, "match": {"doc_id": "doc-1", "node_id": "node-1", "content": "Fixture evidence"},
                "document": {"doc_id": "doc-1", "filename": "fixture.pdf"},
            }],
        }
        fake_vector_store = mock.Mock()
        fake_vector_store.prepare_response.return_value = "Grounded fixture answer"
        with mock.patch("app.routers.api._ensure_connected_runtime_for_session"), mock.patch(
            "app.routers.api.VectorStore", return_value=fake_vector_store
        ), mock.patch("app.routers.api.execute_sql", object()), mock.patch(
            "app.routers.api.retrieve_adaptive_bookrag_evidence", return_value=(evidence, "candidate")
        ), mock.patch("app.routers.api.lock_similarity_result_to_evidence", return_value="locked"):
            response = reader.post("/api/bookrag/retrieve", json={
                "question": "Reader fixture question", "vector_store_name": "fixture"
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["meta"]["principal"], "reader")
            self.assertEqual(response.json()["evidence"]["package_count"], 1)
            response = self.client.post("/api/bookrag/answer", json={
                "question": "Operator fixture question", "vector_store_name": "fixture"
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["meta"]["principal"], "operator")
            self.assertEqual(response.json()["answer"]["text"], "Grounded fixture answer")
            self.assertTrue(response.json()["answer"]["grounded"])
        histories = self.app.state.user_sessions
        self.assertEqual(len(histories[reader_sid]["chat_history"]), 2)
        self.assertEqual(len(histories[operator_sid]["chat_history"]), 2)
        self.assertEqual(histories[reader_sid]["chat_history"][0]["content"], "Reader fixture question")
        self.assertEqual(histories[operator_sid]["chat_history"][0]["content"], "Operator fixture question")

    def test_metadata_import_validates_every_row_before_saving_any_row(self):
        sid = self.login("operator")
        self.app.state.user_sessions[sid]["evs_state"]["connected"] = True
        self.app.state.ensure_session_runtime = lambda *_: None
        documents = [{"doc_id": "doc-1", "filename": "one.pdf"}, {"doc_id": "doc-2", "filename": "two.pdf"}]
        with mock.patch("app.routers.web.fetch_document_metadata", return_value=documents), mock.patch(
            "app.routers.web.save_document_metadata"
        ) as save, mock.patch("app.routers.web.ensure_bookrag_retrieval_view") as ensure_view:
            response = self.client.post("/ui/admin/document-metadata/import", data={"vector_store_name": "fixture"}, files={
                "metadata_csv": ("metadata.csv", "doc_id,publication_date\ndoc-1,2026-01-01\ndoc-2,not-a-date\n")
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Metadata CSV Import Failed", response.text)
        self.assertIn("YYYY-MM-DD", response.text)
        save.assert_not_called()
        ensure_view.assert_not_called()

    def test_governance_import_rejects_empty_csv_without_writes(self):
        sid = self.login("operator")
        self.app.state.user_sessions[sid]["evs_state"]["connected"] = True
        self.app.state.ensure_session_runtime = lambda *_: None
        with mock.patch("app.routers.web.fetch_document_metadata", return_value=[]), mock.patch(
            "app.routers.web.fetch_bookrag_documents", return_value=[]
        ), mock.patch("app.routers.web.document_relation_table_exists", return_value=False), mock.patch(
            "app.routers.web.ensure_bookrag_retrieval_view"
        ) as ensure_view:
            for suffix, field in (("document-metadata", "metadata_csv"), ("document-relations", "relation_csv")):
                with self.subTest(kind=suffix):
                    response = self.client.post(f"/ui/admin/{suffix}/import", data={"vector_store_name": "fixture"}, files={
                        field: ("empty.csv", b"")
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("Import Failed", response.text)
                    self.assertIn("at least one", response.text)
            ensure_view.assert_not_called()
