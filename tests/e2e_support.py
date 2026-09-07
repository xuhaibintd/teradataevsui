"""Real HTTP/HTML/JS test server with isolated data and fake external services."""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pandas as pd
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import runtime, web_support
from app.core.errors import configure_error_handlers
from app.core.runtime_manager import RuntimeIsolationMiddleware
from app.core.security import SecurityMiddleware
from app.core.settings import Settings
from app.routers import api, web
from app.services import unstructured_json_inspector


class BrowserApplication:
    """Patch SDK/service boundaries, never route responses, auth or browser JS."""

    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="evsui-browser-")))
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.calls = []
        self.documents = [
            {"doc_id": "doc-1", "filename": "manual.pdf", "document_series": "main",
             "document_role": "comprehensive", "metadata_status": "review", "revision_no": 1},
            {"doc_id": "doc-2", "filename": "update.pdf", "document_series": "summary",
             "document_role": "update", "metadata_status": "review", "revision_no": 1},
        ]
        self.relations = []
        self.initialized = False
        self.stores = ["e2e_store"]
        self.resource_descriptions = {
            "e2e_store": "Browser BookRAG corpus unstructured_bookrag_flg",
        }
        self.destroy_error = None
        self.destroy_delay_seconds = 0.0
        old_uploads = runtime.UPLOAD_DIR
        old_project = runtime.PROJECT_DIR
        # All loaded modules' runtime paths are redirected, while source templates/assets stay real.
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and module:
                for attribute, value in list(vars(module).items()):
                    if isinstance(value, Path):
                        if value == old_project:
                            self.stack.enter_context(mock.patch.object(module, attribute, self.root))
                        elif value == old_uploads or old_uploads in value.parents:
                            redirected = self.uploads / value.relative_to(old_uploads)
                            self.stack.enter_context(mock.patch.object(module, attribute, redirected))
        self.patch(unstructured_json_inspector, "INSPECTOR_SOURCES", {
            key: (label, self.uploads / "inspector" / key)
            for key, (label, _path) in unstructured_json_inspector.INSPECTOR_SOURCES.items()
        })
        self.stack.enter_context(mock.patch.dict("os.environ", {
            "EVSUI_ENVIRONMENT": "test", "WEB_CONCURRENCY": "1",
            "EVSUI_DATABASE_PATH": str(self.root / "app.db"),
            "EVSUI_CREDENTIAL_KEY_FILE": str(self.root / "credentials.key"),
            "EVSUI_CREDENTIAL_KEY": "", "EVSUI_EXTERNAL_API_ENABLED": "false",
            "EVSUI_BOOTSTRAP_ADMIN": "", "EVSUI_BOOTSTRAP_PASSWORD": "",
        }))
        self.patch(web_support, "_load_auth_users", lambda: {})
        self.patch(web_support, "poc_admin_credentials", lambda: ("", ""))
        defaults = {"host": "", "username": "", "password": "", "pat_token": "",
                    "pem_file": "", "ues_url": "", "unstructured_api_url": "", "unstructured_api_key": ""}
        self.patch(web_support, "_load_connect_defaults", lambda: dict(defaults))
        settings = Settings.from_env(project_dir=self.root)
        application = FastAPI()
        configure_error_handlers(application)
        application.add_middleware(SecurityMiddleware)
        application.add_middleware(RuntimeIsolationMiddleware)
        application.mount("/static", StaticFiles(directory=runtime.STATIC_DIR), name="static")
        web_support.initialize_app_state(application, Jinja2Templates(directory=str(runtime.TEMPLATES_DIR)), settings=settings)
        application.include_router(web.router)
        application.include_router(api.router)
        self.app = application
        self.store = application.state.auth_store
        self.admin = self.store.create_user(username="e2e_admin", password="browser-password", role="admin")
        self.viewer = self.store.create_user(username="e2e_viewer", password="browser-password", role="viewer")
        self.operator = self.store.create_user(username="e2e_operator", password="browser-password", role="operator")
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        self.pem = pem
        self.profile = self.store.save_connection_profile(self.admin.user_id, {
            "name": "Browser fixture", "host": "db.example.invalid", "username": "fixture",
            "password": "db-fixture-password", "pat_token": "pat-fixture-secret",
            "ues_url": "https://ues.example.invalid/open-analytics", "pem_filename": "fixture.pem",
            "pem_content": pem, "is_default": True,
        })
        self.store.save_unstructured_config(
            self.admin.user_id, api_url="https://unstructured.example.invalid/api/v1", api_key="fixture-api-key"
        )
        fixture = self

        class Manager:
            @staticmethod
            def list(**kwargs):
                fixture.calls.append("list")
                return pd.DataFrame({
                    "VS_Name": fixture.stores,
                    "Username": [""] * len(fixture.stores),
                    "Database_Name": ["fixture"] * len(fixture.stores),
                })

            @staticmethod
            def health():
                fixture.calls.append("health")
                return {"status": "ok"}

            @staticmethod
            def disconnect(**kwargs):
                return None

            @staticmethod
            def list_sessions(**kwargs):
                return {"session_details": [{"username": "fixture", "session_id": "session-v1", "status": "active"}]}

        class CollectionManager:
            def __init__(self, auth_data=None):
                self.auth_data = auth_data

            def health(self, **kwargs):
                fixture.calls.append("collection_health")
                return {"status": "ok"}

            def list(self, **kwargs):
                fixture.calls.append("collection_list")
                return []

            def list_sessions(self, **kwargs):
                fixture.calls.append("sessions")
                return {"session_details": [{"username": "fixture", "session_id": "session-v2", "status": "active"}]}

            def disconnect(self, user_names=None):
                fixture.calls.append("disconnect_sessions")
                return None

        class VectorStore:
            def __init__(self, name):
                self.name = name
                fixture.calls.append(f"vector_init:{name}")

            def destroy(self, **kwargs):
                fixture.calls.append("destroy")
                if fixture.destroy_delay_seconds:
                    time.sleep(fixture.destroy_delay_seconds)
                if fixture.destroy_error is not None:
                    raise RuntimeError(fixture.destroy_error)
                fixture.stores = [item for item in fixture.stores if item != self.name]
                return "Destroyed"

            def get_details(self, **kwargs):
                fixture.calls.append(f"details:{self.name}")
                return {"description": fixture.resource_descriptions.get(self.name, "")}

            def ask(self, *args, **kwargs):
                fixture.calls.append("ask")
                return "Fixture grounded answer"

            def similarity_search(self, *args, **kwargs):
                fixture.calls.append("search")
                return [{"text": "Fixture retrieval evidence", "score": 0.95}]

        # Patch the SDK in every consumer to retain the actual context activation workflow.
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and module:
                for attribute, replacement in {
                    "VSManager": Manager, "VectorStore": VectorStore, "CollectionManager": CollectionManager,
                    "create_context": lambda **kwargs: self.calls.append("connect"),
                    "set_auth_token": lambda **kwargs: self.calls.append("authenticate") or object(),
                    "remove_context": lambda **kwargs: None,
                    "execute_sql": self.sql,
                }.items():
                    if hasattr(module, attribute):
                        self.patch(module, attribute, replacement)
        self.patch(web, "fetch_document_metadata", lambda **kwargs: [dict(item) for item in self.documents])
        self.patch(web, "fetch_bookrag_documents", lambda **kwargs: [dict(item) for item in self.documents])
        self.patch(web, "save_document_metadata", self.save_metadata)
        self.patch(web, "backfill_document_metadata", lambda **kwargs: 0)
        self.patch(web, "document_relation_table_exists", lambda **kwargs: self.initialized)
        self.patch(web, "fetch_document_relations", lambda **kwargs: [dict(item) for item in self.relations])
        self.patch(web, "save_document_relation", self.save_relation)
        self.patch(web, "delete_document_relation", self.delete_relation)
        self.patch(web, "ensure_bookrag_retrieval_view", lambda **kwargs: None)
        self.patch(web, "ensure_document_relation_table", self.initialize_relations)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("Browser fixture server did not start")
        return self

    def patch(self, module, name, value):
        self.stack.enter_context(mock.patch.object(module, name, value))

    def sql(self, statement, **kwargs):
        self.calls.append("sql")
        raise AssertionError("Unexpected SQL outside the isolated service fixture")

    def save_metadata(self, *, doc_id, values, **kwargs):
        document = next(item for item in self.documents if item["doc_id"] == doc_id)
        document.update(values)
        document["publication_date_source"] = "manual"
        self.calls.append("save_metadata")
        return dict(document)

    def initialize_relations(self, **kwargs):
        self.initialized = True
        return "fixture.e2e_store_bdrel"

    def save_relation(self, *, relation, original_key=None, **kwargs):
        if original_key:
            self.relations = [item for item in self.relations if tuple(item[k] for k in (
                "from_doc_id", "relation_type", "to_doc_id")) != original_key]
        item = dict(relation)
        lookup = {item["doc_id"]: item["filename"] for item in self.documents}
        item.update(from_filename=lookup[item["from_doc_id"]], to_filename=lookup[item["to_doc_id"]])
        self.relations.append(item)
        self.calls.append("save_relation")
        return item

    def delete_relation(self, *, from_doc_id, relation_type, to_doc_id, **kwargs):
        self.relations = [item for item in self.relations if (
            item["from_doc_id"], item["relation_type"], item["to_doc_id"]
        ) != (from_doc_id, relation_type, to_doc_id)]
        self.calls.append("delete_relation")
        return 1

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.stack.close()
