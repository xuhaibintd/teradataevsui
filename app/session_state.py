from __future__ import annotations

import hmac
import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from app.services.vector_management import empty_management_state

from fastapi import Request
from starlette.datastructures import State

from app.local_config import local_auth_users, local_connection_defaults, local_login_defaults, local_unstructured_defaults


PROJECT_DIR = Path(__file__).resolve().parents[1]
SESSION_SCOPE_KEYS = {
    "evs_state",
    "create_form_values",
    "last_create_operation",
    "document_uploads",
    "document_upload_notices",
    "chat_history",
}
class SessionAwareState(State):
    """Starlette state whose mutable UI fields are isolated per async request."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        super().__init__(state)
        object.__setattr__(
            self,
            "_session_scope_context",
            ContextVar(f"evsui_active_session_scope_{id(self)}", default=None),
        )

    def activate_session_scope(self, scope: dict[str, Any]) -> None:
        self._session_scope_context.set(scope)

    def active_session_scope(self) -> dict[str, Any] | None:
        return self._session_scope_context.get()

    def __getattr__(self, key: Any) -> Any:
        if key in SESSION_SCOPE_KEYS:
            scope = self._session_scope_context.get()
            if scope is not None and key in scope:
                return scope[key]
        return super().__getattr__(key)

    def __setattr__(self, key: Any, value: Any) -> None:
        if key in SESSION_SCOPE_KEYS:
            scope = self._session_scope_context.get()
            if scope is not None:
                scope[key] = value
                return
        super().__setattr__(key, value)


def _configured_path_exists(path_hint: str, vs_basics_dir: Path) -> bool:
    value = str(path_hint or "").strip()
    if not value:
        return False
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.exists()
    return any(
        (base / candidate).exists()
        for base in (PROJECT_DIR, PROJECT_DIR.parent, vs_basics_dir)
    )


def load_connect_defaults(
    latest_uploaded_pem_relative: Callable[[], str],
    vs_basics_dir: Path,
    default_pat_token: str,
) -> dict[str, str]:
    _ = vs_basics_dir
    config_defaults = local_connection_defaults()
    unstructured_defaults = local_unstructured_defaults()
    pem_path = config_defaults.get("pem_file", "")
    if pem_path and not _configured_path_exists(pem_path, vs_basics_dir):
        pem_path = ""
    if not pem_path:
        try:
            pem_path = latest_uploaded_pem_relative() or ""
        except Exception:
            pem_path = ""
    return {
        "host": config_defaults.get("host", ""),
        "username": config_defaults.get("username", ""),
        "password": config_defaults.get("password", ""),
        "ues_url": config_defaults.get("ues_url", ""),
        "pat_token": config_defaults.get("pat_token", "") or default_pat_token,
        "pem_file": pem_path,
        "unstructured_api_url": str(
            unstructured_defaults.get("api_url")
            or unstructured_defaults.get("UNSTRUCTURED_API_URL")
            or unstructured_defaults.get("UNSTRUCTURED_PLATFORM_URL")
            or "https://platform.unstructuredapp.io/api/v1"
        ).strip(),
        "unstructured_api_key": str(
            unstructured_defaults.get("api_key")
            or unstructured_defaults.get("key_id")
            or unstructured_defaults.get("UNSTRUCTURED_API_KEY")
            or unstructured_defaults.get("UNSTRUCTURED_API_KEY_AUTH")
            or ""
        ).strip(),
    }


def default_evs_state(load_defaults: Callable[[], dict[str, str]]) -> dict[str, Any]:
    connect_defaults = load_defaults()
    return {
        "connected": False,
        "connected_at": "",
        "last_error": "",
        "last_notice": "",
        "last_success": "",
        "health_preview": "",
        "health_columns": [],
        "health_rows": [],
        "health_row_count": 0,
        "list_preview": "",
        "list_columns": [],
        "list_rows": [],
        "list_row_count": 0,
        "chat_vs_options": [],
        "chat_list_preview": "",
        "chat_list_loaded_by_user": False,
        "list_loaded_by_user": False,
        "selected_vs_name": "",
        "bookrag_governance_vs_name": "",
        "last_created_vs_name": "",
        "actual_params": {},
        "connect_steps": [],
        "selected_connection_id": None,
        "selected_connection_name": "",
        "params": connect_defaults,
        **empty_management_state(),
    }


def refresh_disconnected_connect_defaults(state: dict, load_defaults: Callable[[], dict[str, str]]) -> None:
    if state.get("connected"):
        return
    params = state.setdefault("params", {})
    if not isinstance(params, dict):
        params = {}
        state["params"] = params
    raw_defaults = load_defaults()
    defaults = raw_defaults.get("params", raw_defaults) if isinstance(raw_defaults, dict) else {}
    for key in ("host", "username", "password", "ues_url", "pat_token", "unstructured_api_url", "unstructured_api_key"):
        if not str(params.get(key) or "").strip() and str(defaults.get(key) or "").strip():
            params[key] = defaults[key]

    pem_file = str(params.get("pem_file") or "").strip()
    default_pem = str(defaults.get("pem_file") or "").strip()
    if not pem_file:
        params["pem_file"] = default_pem
    elif pem_file != default_pem and not _configured_path_exists(pem_file, PROJECT_DIR):
        params["pem_file"] = default_pem


def new_session_scope(username: str, default_evs_state_fn: Callable[[], dict], default_create_values_fn: Callable[[], dict]) -> dict:
    return {
        "username": username.strip(),
        "role": "viewer",
        "evs_state": default_evs_state_fn(),
        "create_form_values": default_create_values_fn(),
        "last_create_operation": None,
        "document_uploads": [],
        "document_upload_notices": [],
        "chat_history": [],
    }


def apply_saved_connection_config(scope: dict[str, Any], auth_store, principal) -> bool:
    if auth_store is None or principal is None:
        return False
    loaded = False
    params = scope.setdefault("evs_state", {}).setdefault("params", {})
    try:
        saved = auth_store.get_connection_profile()
    except Exception as ex:
        scope.setdefault("evs_state", {})["last_error"] = f"Saved connection configuration could not be loaded: {ex}"
    else:
        if saved:
            params.update(saved)
            scope["evs_state"]["selected_connection_id"] = saved.get("id")
            scope["evs_state"]["selected_connection_name"] = saved.get("name", "")
            loaded = True
    try:
        unstructured = auth_store.get_unstructured_config()
    except Exception as ex:
        scope.setdefault("evs_state", {})["last_error"] = f"Unstructured IO configuration could not be loaded: {ex}"
    else:
        params["unstructured_api_url"] = str(unstructured.get("api_url") or "")
        params["unstructured_api_key"] = str(unstructured.get("api_key") or "")
        loaded = loaded or bool(params["unstructured_api_url"] or params["unstructured_api_key"])
    return loaded


def session_id_from_request(request: Request, session_cookie_name: str) -> str:
    return str(request.cookies.get(session_cookie_name, "")).strip()


def current_user(request: Request) -> str:
    sid = str(request.cookies.get("evsui_sid", "")).strip()
    auth_store = getattr(request.app.state, "auth_store", None)
    if auth_store is not None:
        principal = auth_store.get_session(sid, touch=False)
        return principal.username if principal is not None else ""
    return str(request.cookies.get("evsui_user", ""))


def activate_session_state(
    request: Request,
    app,
    session_cookie_name: str,
    default_evs_state_fn: Callable[[], dict],
    default_create_values_fn: Callable[[], dict],
) -> dict:
    sid = session_id_from_request(request, session_cookie_name)
    sessions = app.state.user_sessions
    scope = sessions.get(sid)
    if scope is None:
        auth_store = getattr(app.state, "auth_store", None)
        principal = auth_store.get_session(sid) if auth_store is not None else None
        scope = new_session_scope(
            username=principal.username if principal is not None else current_user(request),
            default_evs_state_fn=default_evs_state_fn,
            default_create_values_fn=default_create_values_fn,
        )
        if principal is not None:
            scope["role"] = principal.role
            apply_saved_connection_config(scope, auth_store, principal)
        if sid:
            sessions[sid] = scope

    activate = getattr(app.state, "activate_session_scope", None)
    if callable(activate):
        activate(scope)
        refresh_disconnected_connect_defaults(scope["evs_state"], default_evs_state_fn)
        return scope

    app.state.evs_state = scope["evs_state"]
    refresh_disconnected_connect_defaults(app.state.evs_state, default_evs_state_fn)
    app.state.create_form_values = scope["create_form_values"]
    app.state.last_create_operation = scope["last_create_operation"]
    app.state.document_uploads = scope["document_uploads"]
    app.state.document_upload_notices = scope["document_upload_notices"]
    app.state.chat_history = scope["chat_history"]
    return scope


def persist_active_session_state(request: Request, app, session_cookie_name: str) -> None:
    sid = session_id_from_request(request, session_cookie_name)
    if not sid:
        return
    scope = app.state.user_sessions.get(sid)
    if scope is None:
        return
    active_scope = getattr(app.state, "active_session_scope", None)
    if callable(active_scope) and active_scope() is scope:
        return
    scope["evs_state"] = app.state.evs_state
    scope["create_form_values"] = app.state.create_form_values
    scope["last_create_operation"] = app.state.last_create_operation
    scope["document_uploads"] = app.state.document_uploads
    scope["document_upload_notices"] = app.state.document_upload_notices
    scope["chat_history"] = app.state.chat_history


def auth_users_file_path(auth_users_file_default: Path) -> Path:
    raw = str(os.getenv("POC_AUTH_FILE", str(auth_users_file_default))).strip()
    return Path(raw).expanduser()


def load_auth_users(auth_users_file_default: Path, logger: logging.Logger) -> dict[str, str]:
    users: dict[str, str] = local_auth_users()
    path = auth_users_file_path(auth_users_file_default)
    if path.exists() and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw_users = payload.get("users", payload) if isinstance(payload, dict) else payload
            if isinstance(raw_users, dict):
                for raw_name, raw_password in raw_users.items():
                    name = str(raw_name).strip()
                    if not name:
                        continue
                    users[name] = str(raw_password)
            elif isinstance(raw_users, list):
                for item in raw_users:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("username", "")).strip()
                    if not name:
                        continue
                    users[name] = str(item.get("password", ""))
        except Exception as ex:
            logger.warning("Failed to parse auth users file '%s': %s", path, ex)
    return users


def is_logged_in(request: Request, app, session_cookie_name: str) -> bool:
    sid = session_id_from_request(request, session_cookie_name)
    if not sid:
        return False
    auth_store = getattr(app.state, "auth_store", None)
    if auth_store is not None:
        return auth_store.get_session(sid) is not None
    return sid in app.state.user_sessions and request.cookies.get("evsui_auth") == "1"


def poc_admin_credentials() -> tuple[str, str]:
    username = str(os.getenv("POC_ADMIN_USER", "")).strip()
    password = str(os.getenv("POC_ADMIN_PASSWORD", ""))
    if username or password:
        return username, password
    return local_login_defaults()


def is_poc_auth_configured(load_auth_users_fn: Callable[[], dict[str, str]]) -> bool:
    if load_auth_users_fn():
        return True
    username, password = poc_admin_credentials()
    return bool(username and password)


def is_valid_poc_login(username: str, password: str, load_auth_users_fn: Callable[[], dict[str, str]]) -> bool:
    auth_users = load_auth_users_fn()
    if not auth_users:
        expected_username, expected_password = poc_admin_credentials()
        if expected_username and expected_password:
            auth_users = {expected_username: expected_password}
    if not auth_users:
        return False
    stored_password = auth_users.get(username)
    if stored_password is None:
        return False
    return hmac.compare_digest(password, stored_password)
