from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.integrations.teradata import connection as teradata_connection
from app.main import create_app
from app.routers.jobs import split_sensitive_job_payload
from app.services import workflow_jobs
from app.services.job_worker import _deep_merge


class WorkflowJobTests(unittest.TestCase):
    def test_http_route_queues_owned_encrypted_job_and_allows_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            "os.environ",
            {
                "EVSUI_ENVIRONMENT": "test",
                "EVSUI_DATABASE_PATH": str(Path(tmpdir) / "evsui.db"),
                "EVSUI_CREDENTIAL_KEY_FILE": str(Path(tmpdir) / "credentials.key"),
                "EVSUI_EXTERNAL_API_ENABLED": "false",
            },
        ):
            app = create_app(Settings.from_env(project_dir=Path(tmpdir)))
            owner = app.state.auth_store.create_user(username="job-owner", password="owner-password", role="operator")
            other = app.state.auth_store.create_user(username="other-user", password="other-password")
            owner_session = app.state.auth_store.create_session(owner)
            other_session = app.state.auth_store.create_session(other)

            with TestClient(app) as client:
                client.cookies.set("evsui_sid", owner_session)
                # Parsing now correctly requires an uploaded document. Seed a
                # real isolated file; this test focuses on queue encryption and
                # ownership, while browser/upload suites exercise multipart IO.
                self.assertEqual(client.get("/").status_code, 200)
                document = Path(tmpdir) / "fixture.txt"
                document.write_text("Workflow queue fixture", encoding="utf-8")
                app.state.user_sessions[owner_session]["document_uploads"] = [{
                    "doc_id": "queue-fixture", "filename": document.name,
                    "saved_path": str(document), "size": document.stat().st_size,
                }]
                response = client.post(
                    "/ui/create/parse-documents",
                    data={
                        "vector_store_name": "demo",
                        "multi_format_bookrag_vlm_provider_api_key": "encrypted-provider-key",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("Queued", response.text)
                job = app.state.job_repository.list_recent(owner_user_id=owner.user_id, limit=1)[0]
                with app.state.auth_store.database.connect() as connection:
                    row = connection.execute(
                        "SELECT payload_json, secret_payload_ciphertext FROM jobs WHERE id=?",
                        (job["id"],),
                    ).fetchone()
                self.assertNotIn("encrypted-provider-key", row["payload_json"])
                self.assertNotIn("encrypted-provider-key", row["secret_payload_ciphertext"])

                client.cookies.set("evsui_sid", other_session)
                self.assertEqual(client.get(f"/ui/jobs/{job['id']}").status_code, 404)

                client.cookies.set("evsui_sid", owner_session)
                cancelled = client.post(f"/ui/jobs/{job['id']}/cancel")
                self.assertEqual(cancelled.status_code, 200)
                self.assertIn("Cancelled", cancelled.text)

    def test_nested_provider_key_is_split_from_public_job_payload(self) -> None:
        public, secret = split_sensitive_job_payload(
            {
                "create_values": {
                    "multi_format_vlm_provider_api_key": "private",
                    "multi_format_strategy": "hi_res",
                }
            }
        )

        self.assertNotIn("multi_format_vlm_provider_api_key", public["create_values"])
        self.assertEqual(secret["create_values"]["multi_format_vlm_provider_api_key"], "private")

    def test_provider_key_inside_a_list_is_split_and_reconstructed_only_for_execution(self) -> None:
        public, secret = split_sensitive_job_payload(
            {
                "providers": [
                    {"name": "primary", "api_key": "list-secret"},
                    {"name": "secondary"},
                ]
            }
        )

        self.assertEqual(public["providers"], [{"name": "primary"}, {"name": "secondary"}])
        self.assertEqual(secret["providers"], [{"api_key": "list-secret"}, None])
        self.assertEqual(
            _deep_merge(public, secret)["providers"],
            [
                {"name": "primary", "api_key": "list-secret"},
                {"name": "secondary"},
            ],
        )

    def test_bookrag_parse_loads_external_secret_at_execution_time(self) -> None:
        class AuthStore:
            @staticmethod
            def get_unstructured_config():
                return {"api_url": "https://example.invalid", "api_key": "runtime-secret"}

        captured = {}

        def parse(**kwargs):
            captured.update(kwargs)
            return {"status": "ok", "diagnostic": "runtime-secret"}

        with mock.patch.object(workflow_jobs, "run_bookrag_document_parsing", side_effect=parse):
            handler = workflow_jobs.build_workflow_job_handlers(AuthStore())[
                workflow_jobs.BOOKRAG_PARSE_JOB
            ]
            result = handler(
                {
                    "create_values": {"strategy": "hi_res"},
                    "vector_store_name": "demo",
                    "uploaded_documents": [{"saved_path": "upload.pdf"}],
                    "_job": {"connection_profile_id": 7},
                },
                lambda _progress: None,
            )

        self.assertEqual(result["summary"], {"status": "ok", "diagnostic": "[REDACTED]"})
        self.assertEqual(result["artifact_count"], 0)
        self.assertEqual(captured["connection_params"]["unstructured_api_key"], "runtime-secret")

    def test_runtime_external_secret_is_redacted_from_handler_errors(self) -> None:
        class AuthStore:
            @staticmethod
            def get_unstructured_config():
                return {"api_url": "https://example.invalid", "api_key": "runtime-secret"}

        with mock.patch.object(
            workflow_jobs,
            "run_bookrag_document_parsing",
            side_effect=RuntimeError("request rejected for runtime-secret"),
        ):
            handler = workflow_jobs.build_workflow_job_handlers(AuthStore())[
                workflow_jobs.BOOKRAG_PARSE_JOB
            ]
            with self.assertRaises(RuntimeError) as raised:
                handler({}, lambda _progress: None)

        self.assertNotIn("runtime-secret", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_csv_handlers_forward_document_progress_to_job_heartbeat(self) -> None:
        handlers = workflow_jobs.build_workflow_job_handlers(object())
        payload = {
            "parse_run_id": "parse-run",
            "create_values": {},
            "vector_store_name": "demo",
            "target_database": "demo_schema",
        }

        for kind, function_name in (
            (workflow_jobs.BOOKRAG_CSV_GENERATE_JOB, "run_bookrag_json_to_csv"),
            (workflow_jobs.MULTI_FORMAT_CSV_GENERATE_JOB, "run_multi_format_json_to_csv"),
        ):
            with self.subTest(kind=kind):
                progress_updates = []

                def generate(**kwargs):
                    kwargs["progress_callback"](50)
                    return {"status": "ready"}

                with mock.patch.object(workflow_jobs, function_name, side_effect=generate):
                    result = handlers[kind](payload, progress_updates.append)

                self.assertEqual(progress_updates, [10, 50, 95])
                self.assertEqual(result["summary"], {"status": "ready"})

    def test_teradata_profile_secrets_are_redacted_from_runtime_errors(self) -> None:
        class AuthStore:
            @staticmethod
            def get_connection_profile(_profile_id):
                return {
                    "host": "db.invalid",
                    "username": "user",
                    "password": "database-secret",
                    "ues_url": "https://ues.invalid/open-analytics",
                    "pat_token": "pat-secret",
                    "pem_file": "",
                }

        with mock.patch.object(
            teradata_connection,
            "create_context",
            side_effect=RuntimeError("password=database-secret"),
        ), mock.patch.object(teradata_connection, "set_auth_token", mock.Mock()), mock.patch.object(
            teradata_connection, "execute_sql", mock.Mock()
        ):
            with self.assertRaises(RuntimeError) as raised:
                with teradata_connection.activated_connection(AuthStore(), 1):
                    pass

        self.assertNotIn("database-secret", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_vector_create_runs_inside_background_connection_and_reaches_ready(self) -> None:
        @contextmanager
        def activated(_auth_store, profile_id):
            self.assertEqual(profile_id, 12)
            yield {"execute_sql": object(), "profile": {
                "host": "fixture.invalid", "username": "fixture", "ues_url": "https://ues.invalid/open-analytics",
            }}

        class Mode:
            MODE = "text_core"

            @staticmethod
            def preprocess_create_payload(**kwargs):
                return kwargs["exec_payload"], None

        class VectorStore:
            def __init__(self, name):
                self.name = name

            @staticmethod
            def create(**_kwargs):
                return {"submitted": True}

            @staticmethod
            def status():
                return "Ready"

        class AuthStore:
            @staticmethod
            def get_unstructured_config():
                return {"api_url": "", "api_key": ""}

        with mock.patch.object(workflow_jobs, "activated_connection", side_effect=activated), mock.patch.object(
            workflow_jobs, "get_doc_pipeline_handler", return_value=Mode
        ), mock.patch.object(workflow_jobs, "VectorStore", VectorStore), mock.patch.object(
            workflow_jobs, "_vector_store_exists", return_value=False
        ):
            handler = workflow_jobs.build_workflow_job_handlers(AuthStore())[
                workflow_jobs.VECTOR_STORE_CREATE_JOB
            ]
            result = handler(
                {
                    "vector_store_name": "demo",
                    "doc_pipeline_mode": "text_core",
                    "create_values": {},
                    "create_payload": {"embeddings_model": "model"},
                    "exec_payload": {"embeddings_model": "model"},
                    "_job": {"connection_profile_id": 12},
                },
                lambda _progress: None,
            )

        self.assertEqual(result["create_result"]["status"], "ok")
        self.assertIn("Ready", result["create_result"]["message"])


if __name__ == "__main__":
    unittest.main()
