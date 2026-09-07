from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import Request, UploadFile

from app.auth_store import AuthStore
from app.core.runtime_manager import TeradataRuntimeManager
from app.core.security import redact_sensitive_text
from app.core.settings import Settings

from app.runtime import (
    AUTH_USERS_FILE_DEFAULT,
    DEFAULT_CHAT_VS_NAME,
    DEFAULT_PAT_TOKEN,
    DOCUMENT_UPLOAD_DIR,
    PEM_UPLOAD_DIR,
    PROJECT_DIR,
    SESSION_COOKIE_NAME,
    VS_BASICS_DIR,
)
from app.services.create_config import (
    build_create_ui_sections,
    build_text_core_ui_fields,
    default_create_values,
)
from app.services.artifact_lifecycle import ArtifactLifecycle
from app.services.doc_modes.constants import DOC_PIPELINE_OPTIONS
from app.services.doc_modes.ui_fields import build_multi_format_bookrag_ui_fields, build_multi_format_ui_fields
from app.services.bookrag_section_rules import BOOKRAG_SECTION_RULES_PATH, load_bookrag_section_rules
from app.services.multi_format import (
    list_bookrag_csv_runs,
    list_bookrag_parse_runs,
    list_multi_format_csv_runs,
    list_multi_format_parse_runs,
)
from app.services.unstructured_json_inspector import build_unstructured_json_inspector_context
from app.session_state import (
    activate_session_state,
    apply_saved_connection_config,
    current_user,
    default_evs_state,
    is_logged_in,
    is_poc_auth_configured,
    load_auth_users,
    load_connect_defaults,
    new_session_scope,
    poc_admin_credentials,
    persist_active_session_state,
    SessionAwareState,
    session_id_from_request,
)
from app.teradata_runtime import (
    TERADATA_IMPORT_ERROR,
    VSManager,
    VectorStore,
    create_context,
    remove_context,
    set_auth_token,
)
from app.utils.table_state import (
    apply_chat_list_output_to_state,
    apply_list_output_to_state,
    build_file_meta,
    chunk_table_sql_for_vs,
    clear_chat_list_result,
    clear_destroy_result,
    clear_health_result,
    clear_list_result,
    destroy_output_indicates_failure,
    find_list_row_for_vs,
    find_vs_row_by_name,
    format_preview,
    is_content_based_vs_row,
    normalize_header_key,
    row_value_by_header,
    table_from_result,
)
from app.utils.uploads import (
    collect_upload_files,
    latest_uploaded_pem_relative,
    normalize_pem_filename_for_auth,
    resolve_path_hint,
    save_document_uploads,
)

logger = logging.getLogger("evsui.connect")
logger.setLevel(logging.INFO)

JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
LATIN_RE = re.compile(r"[A-Za-z]")
HAN_RE = re.compile(r"[\u4e00-\u9fff]")

def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_vectorstore_already_exists_error(raw_error: str) -> bool:
    text = str(raw_error or "").lower()
    if "already exists" not in text and "already exist" not in text:
        return False
    vectorstore_markers = (
        "vector store",
        "vectorstore",
        "vector-store",
    )
    return any(marker in text for marker in vectorstore_markers)


def _normalized_vs_name(value: str) -> str:
    return normalize_header_key(str(value or "").strip())


def _looks_like_vs_name_key(key: str) -> bool:
    normalized = normalize_header_key(key)
    return normalized in {"vsname", "vectorstorename", "vector_store_name", "name"} or (
        ("vs" in normalized or ("vector" in normalized and "store" in normalized)) and "name" in normalized
    )


def _looks_like_vs_record(value) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    normalized_keys = [normalize_header_key(key) for key in value.keys()]
    if any(_looks_like_vs_name_key(key) for key in value.keys()):
        return True
    return "name" in normalized_keys and any(
        marker in key
        for key in normalized_keys
        for marker in ("status", "database", "schema", "permission", "owner", "creator", "username", "type")
    )


def _find_vs_record_in_payload(payload, vector_store_name: str, path: str = "root", depth: int = 0):
    if depth > 8:
        return None
    target = _normalized_vs_name(vector_store_name)
    if not target:
        return None

    if isinstance(payload, dict):
        if _looks_like_vs_record(payload):
            for key, value in payload.items():
                if _looks_like_vs_name_key(key) and _normalized_vs_name(value) == target:
                    return path, payload
            if "name" in payload and _normalized_vs_name(payload.get("name")) == target:
                return path, payload
        for key, value in payload.items():
            match = _find_vs_record_in_payload(value, vector_store_name, f"{path}.{key}", depth + 1)
            if match:
                return match
        return None

    if isinstance(payload, (list, tuple)):
        for idx, item in enumerate(payload):
            match = _find_vs_record_in_payload(item, vector_store_name, f"{path}[{idx}]", depth + 1)
            if match:
                return match
    return None


def _format_vs_match_detail(headers: list[str], matched_row: list[str]) -> str:
    owner = _row_value_by_header(headers, matched_row, ("username", "user", "owner", "creator", "createdby"))
    database = _row_value_by_header(headers, matched_row, ("database", "schema", "targetdatabase"))
    permission = _row_value_by_header(headers, matched_row, ("permission", "role", "access"))
    detail_parts = []
    if owner:
        detail_parts.append(f"owner='{owner}'")
    if database:
        detail_parts.append(f"database='{database}'")
    if permission:
        detail_parts.append(f"permission='{permission}'")
    if detail_parts:
        return ", ".join(detail_parts)
    row_payload = dict(zip(headers[1:], matched_row[1:])) if headers and matched_row else {"row": matched_row}
    return f"row={_format_preview(row_payload, max_chars=300)}"


def _vectorstore_status_missing_error(raw_error: str) -> bool:
    text = str(raw_error or "").lower()
    return any(
        marker in text
        for marker in (
            "not found",
            "does not exist",
            "doesn't exist",
            "unknown vector store",
            "no such vector store",
            "response code: 404",
            "responsecode:404",
            "404",
            "3807",
        )
    )


def _verify_vectorstore_exists(vector_store_name: str, *, allow_status_fallback: bool = False) -> tuple[bool, str, str]:
    target = str(vector_store_name or "").strip()
    if not target:
        return False, "", "empty vector store name"

    list_errors: list[str] = []
    if VSManager is not None:
        list_fn = getattr(VSManager, "list", None)
        if callable(list_fn):
            list_attempts = (
                ("VSManager.list(return_type='json')", lambda: list_fn(return_type="json")),
                ("VSManager.list()", lambda: list_fn()),
            )
            for label, invoke in list_attempts:
                try:
                    list_output = invoke()
                except Exception as ex:
                    list_errors.append(f"{label}: {ex}")
                    continue

                headers, rows = _table_from_result(list_output)
                matched_row = _find_vs_row_by_name(headers, rows, target)
                if matched_row:
                    detail = _format_vs_match_detail(headers, matched_row)
                    return True, f"{label} confirmed '{target}' exists ({detail}).", ""

                nested_match = _find_vs_record_in_payload(list_output, target)
                if nested_match:
                    match_path, match_payload = nested_match
                    detail = _format_preview(match_payload, max_chars=300)
                    return True, f"{label} confirmed '{target}' exists via nested payload at {match_path} ({detail}).", ""

                list_errors.append(f"{label}: no exact match for '{target}'")
        else:
            list_errors.append("VSManager.list() is not callable")
    else:
        list_errors.append("VSManager runtime is unavailable")

    detail = "; ".join(error for error in list_errors if error)
    if not allow_status_fallback:
        return False, f"No list probe found '{target}'.", detail

    if VectorStore is not None:
        try:
            vector_store = VectorStore(target)
            status_fn = getattr(vector_store, "status", None)
            if callable(status_fn):
                status_output = status_fn()
                preview = _format_preview(status_output, max_chars=300).strip()
                headers, rows = _table_from_result(status_output)
                preview_low = preview.lower()
                if status_output is None or ((not rows) and preview_low in {"", "none", "null", "unknown"}):
                    list_errors.append("VectorStore.status(): empty or unknown response")
                elif _vectorstore_status_missing_error(preview):
                    detail = "; ".join(list_errors) if list_errors else "not found"
                    return False, f"No existence probe found '{target}'.", detail
                else:
                    return True, f"VectorStore.status() responded for '{target}' ({preview}).", ""
            else:
                list_errors.append("VectorStore.status() is not callable")
        except Exception as ex:
            if _vectorstore_status_missing_error(ex):
                detail = "; ".join(list_errors) if list_errors else "not found"
                return False, f"No existence probe found '{target}'.", detail
            list_errors.append(f"VectorStore.status(): {ex}")
    else:
        list_errors.append("VectorStore runtime is unavailable")

    detail = "; ".join(error for error in list_errors if error)
    return False, f"No existence probe found '{target}'.", detail


_normalize_header_key = normalize_header_key
_find_list_row_for_vs = find_list_row_for_vs
_find_vs_row_by_name = find_vs_row_by_name
_destroy_output_indicates_failure = destroy_output_indicates_failure
_row_value_by_header = row_value_by_header
_is_content_based_vs_row = is_content_based_vs_row
_chunk_table_sql_for_vs = chunk_table_sql_for_vs
_table_from_result = table_from_result


def _derive_base_url(ues_url: str) -> str:
    src = ues_url.strip()
    # strip off the trailing /open-analytics
    if src.endswith("/open-analytics"):
        return src[:-15]
    return src


def _latest_uploaded_pem_relative() -> str:
    return latest_uploaded_pem_relative(PEM_UPLOAD_DIR, PROJECT_DIR)


def _collect_upload_files(form_data, field_name: str = "files") -> list[UploadFile]:
    return collect_upload_files(form_data, field_name)


async def _save_document_uploads(files: list[UploadFile]) -> tuple[list[dict], list[str]]:
    settings = Settings.from_env(project_dir=PROJECT_DIR)
    return await save_document_uploads(
        files,
        DOCUMENT_UPLOAD_DIR,
        PROJECT_DIR,
        _now_ts,
        max_upload_bytes=settings.max_upload_bytes,
    )


def _resolve_path_hint(path_hint: str) -> str:
    return resolve_path_hint(path_hint, PROJECT_DIR, VS_BASICS_DIR)


def _normalize_pem_filename_for_auth(resolved_pem_path: str) -> str:
    return normalize_pem_filename_for_auth(resolved_pem_path)


def _cleanup_context() -> dict[str, str]:
    result = {
        "vs_disconnect": "skipped (VSManager unavailable)",
        "remove_context": "skipped (remove_context unavailable)",
    }
    if VSManager is not None:
        try:
            VSManager.disconnect(raise_error=False)
            result["vs_disconnect"] = "called"
        except Exception:
            result["vs_disconnect"] = "error"
    if remove_context is not None:
        try:
            remove_context()
            result["remove_context"] = "called"
        except Exception:
            result["remove_context"] = "error"
    return result


def _ensure_connected_runtime_for_session(
    request: Request,
    app,
    *,
    allow_saved_params: bool = False,
) -> None:
    _activate_session_state(request, app)
    state = app.state.evs_state
    was_connected = bool(state.get("connected"))
    if not was_connected and not allow_saved_params:
        raise RuntimeError("Step 1 is not connected for the active session.")
    if create_context is None or set_auth_token is None:
        raise RuntimeError(f"teradataml/teradatagenai runtime is unavailable: {TERADATA_IMPORT_ERROR}")

    params = dict(state.get("params") or {})
    host = str(params.get("host") or "").strip()
    username = str(params.get("username") or "").strip()
    password = params.get("password") or ""
    ues_url = str(params.get("ues_url") or "").strip()
    pat_token = str(params.get("pat_token") or "").strip()
    pem_hint = str(params.get("pem_file") or "").strip()
    if not all([host, username, password, ues_url, pat_token]):
        raise RuntimeError("Stored Step 1 connection parameters are incomplete for runtime reactivation.")

    base_url = _derive_base_url(ues_url)
    resolved_pem_for_auth = _resolve_path_hint(pem_hint)
    normalized_pem_for_auth = _normalize_pem_filename_for_auth(resolved_pem_for_auth) if resolved_pem_for_auth else ""
    auth_kwargs = {"base_url": base_url, "pat_token": pat_token}
    if normalized_pem_for_auth:
        auth_kwargs["pem_file"] = normalized_pem_for_auth
    elif resolved_pem_for_auth:
        auth_kwargs["pem_file"] = resolved_pem_for_auth
    elif pem_hint:
        auth_kwargs["pem_file"] = pem_hint
    manager = getattr(app.state, "teradata_runtime_manager", None)
    if manager is None:
        _cleanup_context()
        create_context(host=host, username=username, password=password)
        auth_data = set_auth_token(**auth_kwargs)
    else:
        profile_id = state.get("selected_connection_id") or "default"
        session_id = _session_id_from_request(request) or "anonymous"
        auth_data = manager.reactivate(
            identity=f"session:{session_id}:connection:{profile_id}",
            cleanup=_cleanup_context,
            connect=lambda: create_context(host=host, username=username, password=password),
            authenticate=lambda: set_auth_token(**auth_kwargs),
        )
    state["_sdk_auth_data"] = auth_data
    if not was_connected:
        state["connected"] = True
        state["connected_at"] = _now_ts()
        state["last_error"] = ""
        state["last_success"] = "Connection restored from saved parameters."


def _ensure_external_api_runtime(app) -> None:
    manager = getattr(app.state, "teradata_runtime_manager", None)
    if manager is None:
        return
    if create_context is None or set_auth_token is None:
        raise RuntimeError(f"teradataml/teradatagenai runtime is unavailable: {TERADATA_IMPORT_ERROR}")
    saved = app.state.auth_store.get_connection_profile()
    if not saved:
        raise RuntimeError("No default database connection is configured for the external API.")
    host = str(saved.get("host") or "").strip()
    username = str(saved.get("username") or "").strip()
    password = str(saved.get("password") or "")
    ues_url = str(saved.get("ues_url") or "").strip()
    pat_token = str(saved.get("pat_token") or "").strip()
    if not all([host, username, password, ues_url, pat_token]):
        raise RuntimeError("The default database connection is incomplete for external API use.")
    pem_hint = str(saved.get("pem_file") or "").strip()
    resolved_pem = _resolve_path_hint(pem_hint)
    normalized_pem = _normalize_pem_filename_for_auth(resolved_pem) if resolved_pem else ""
    auth_kwargs = {"base_url": _derive_base_url(ues_url), "pat_token": pat_token}
    if normalized_pem or resolved_pem or pem_hint:
        auth_kwargs["pem_file"] = normalized_pem or resolved_pem or pem_hint
    manager.reactivate(
        identity=f"external-api:connection:{saved.get('id') or 'default'}",
        cleanup=_cleanup_context,
        connect=lambda: create_context(host=host, username=username, password=password),
        authenticate=lambda: set_auth_token(**auth_kwargs),
    )


def _cleanup_result_status(cleanup_result: dict[str, str]) -> str:
    if cleanup_result.get("vs_disconnect") == "error" or cleanup_result.get("remove_context") == "error":
        return "warn"
    return "ok"


def _cleanup_result_detail(cleanup_result: dict[str, str]) -> str:
    return (
        f"VSManager.disconnect(): {cleanup_result.get('vs_disconnect', 'skipped')}; "
        f"remove_context(): {cleanup_result.get('remove_context', 'skipped')}."
    )


_format_preview = format_preview
_apply_list_output_to_state = apply_list_output_to_state
_apply_chat_list_output_to_state = apply_chat_list_output_to_state
_clear_list_result = clear_list_result
_clear_chat_list_result = clear_chat_list_result
_clear_health_result = clear_health_result
_clear_destroy_result = clear_destroy_result


def _build_file_meta(path_hint: str) -> dict[str, str | int | bool]:
    return build_file_meta(path_hint, _resolve_path_hint)


def _new_connect_step(step: str, status: str, detail: str) -> dict[str, str]:
    status_lower = status.lower()
    safe_detail = redact_sensitive_text(detail)
    message = f"[{step}] {safe_detail}"
    if status_lower == "error":
        logger.error(message)
    elif status_lower == "warn":
        logger.warning(message)
    else:
        logger.info(message)
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "step": step,
        "status": status,
        "detail": safe_detail,
    }


def _append_connect_step(state: dict, step: str, status: str, detail: str, limit: int = 120) -> None:
    steps = list(state.get("connect_steps", []))
    steps.append(_new_connect_step(step, status, detail))
    state["connect_steps"] = steps[-limit:]


def _mask_token(token: str) -> str:
    value = token.strip()
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _load_connect_defaults() -> dict[str, str]:
    return load_connect_defaults(_latest_uploaded_pem_relative, VS_BASICS_DIR, DEFAULT_PAT_TOKEN)


def _load_session_connect_defaults() -> dict[str, str]:
    defaults = _load_connect_defaults()
    for key in (
        "host",
        "username",
        "password",
        "ues_url",
        "pat_token",
        "pem_file",
        "unstructured_api_url",
        "unstructured_api_key",
    ):
        defaults[key] = ""
    return defaults


def _default_evs_state() -> dict:
    return default_evs_state(_load_session_connect_defaults)


def _new_session_scope(username: str = "") -> dict:
    return new_session_scope(username, _default_evs_state, default_create_values)


def _apply_saved_connection_config(scope: dict, app, principal) -> bool:
    return apply_saved_connection_config(scope, app.state.auth_store, principal)


def _session_id_from_request(request: Request) -> str:
    return session_id_from_request(request, SESSION_COOKIE_NAME)


def _activate_session_state(request: Request, app) -> dict:
    scope = activate_session_state(
        request,
        app,
        SESSION_COOKIE_NAME,
        _default_evs_state,
        default_create_values,
    )
    principal = app.state.auth_store.get_session(_session_id_from_request(request), touch=False)
    if principal is not None and not scope.get("evs_state", {}).get("connected"):
        _apply_saved_connection_config(scope, app, principal)
        app.state.activate_session_scope(scope)
    return scope


def _persist_active_session_state(request: Request, app) -> None:
    persist_active_session_state(request, app, SESSION_COOKIE_NAME)


def _load_auth_users() -> dict[str, str]:
    return load_auth_users(AUTH_USERS_FILE_DEFAULT, logger)


def _active_vector_store_name(app) -> str:
    state = app.state.evs_state or {}
    selected = str(state.get("selected_vs_name", "")).strip()
    return selected or DEFAULT_CHAT_VS_NAME


def _detect_message_language(text: str) -> str:
    # Product language priority: Japanese first, then English.
    if JP_KANA_RE.search(text):
        return "ja"
    if HAN_RE.search(text):
        return "ja"
    if LATIN_RE.search(text):
        return "en"
    return "en"


def _ask_prompt_for_language(lang: str) -> str:
    if lang == "ja":
        return (
            "Answer only with evidence from retrieved documents. "
            "Respond in Japanese only. "
            "If evidence is missing, explicitly state that in Japanese."
        )
    return (
        "Answer only with evidence from retrieved documents. "
        "Use exactly the same language as the user's question. "
        "If evidence is missing, state that clearly in the same language."
    )


def _build_evs_reply(message: str, validation_target: str, vector_store_name: str = "") -> str:
    if VectorStore is None:
        return "Validation failed: VectorStore runtime is unavailable."

    question = message.strip()
    try:
        lang = _detect_message_language(question)
    except re.error:
        lang = "en"
    ask_prompt = _ask_prompt_for_language(lang)
    target = validation_target.strip().lower()
    vs_name = str(vector_store_name or "").strip()
    if not vs_name:
        return "Validation failed: no vector store selected. Click 'Run List' and choose one."

    try:
        vector_store = VectorStore(vs_name)
    except Exception as ex:
        return f"Validation failed: cannot open VectorStore('{vs_name}'): {ex}"

    try:
        if target == "vectorstore.similarity_search":
            try:
                result = vector_store.similarity_search(question=question)
            except TypeError:
                result = vector_store.similarity_search(question)
            return _format_preview(result, max_chars=None)

        try:
            result = vector_store.ask(question=question, prompt=ask_prompt)
        except TypeError:
            try:
                result = vector_store.ask(question, ask_prompt)
            except TypeError:
                try:
                    result = vector_store.ask(question=question)
                except TypeError:
                    result = vector_store.ask(question)
        return _format_preview(result, max_chars=None)
    except Exception as ex:
        method_name = "similarity_search" if target == "vectorstore.similarity_search" else "ask"
        return f"{method_name} failed on '{vs_name}': {ex}"


def _build_bookrag_chat_reply(evidence: dict | None, vector_store_name: str) -> str:
    vs_name = str(vector_store_name or "").strip()
    evidence_text = str((evidence or {}).get("evidence_text") or "").strip()
    if evidence_text:
        return f"BookRAG evidence for '{vs_name}':\n\n{evidence_text}"

    similarity_row_count = 0
    package_count = 0
    try:
        similarity_row_count = int((evidence or {}).get("similarity_row_count") or 0)
    except Exception:
        similarity_row_count = 0
    try:
        package_count = int((evidence or {}).get("package_count") or 0)
    except Exception:
        package_count = 0
    headers = (evidence or {}).get("similarity_headers") or []
    header_preview = ", ".join(str(item).strip() for item in headers[:6] if str(item).strip())
    similarity_preview = str((evidence or {}).get("similarity_preview") or "").strip()

    if vs_name:
        message = f"No BookRAG evidence found for '{vs_name}'."
    else:
        message = "No BookRAG evidence found."

    diagnostics: list[str] = []
    if similarity_row_count:
        diagnostics.append(f"similarity_rows={similarity_row_count}")
    if package_count:
        diagnostics.append(f"packages={package_count}")
    if header_preview:
        diagnostics.append(f"headers=[{header_preview}]")
    if similarity_preview:
        diagnostics.append(f"preview={similarity_preview}")
    if diagnostics:
        message += " " + " ".join(diagnostics) + "."
    return message


def _current_user(request: Request) -> str:
    return current_user(request)


def _build_home_context(request: Request, app) -> dict:
    _activate_session_state(request, app)
    state = app.state.evs_state

    username = _current_user(request)
    principal = app.state.auth_store.get_session(_session_id_from_request(request), touch=False)
    bookrag_section_rules = load_bookrag_section_rules()
    bookrag_csv_runs = list_bookrag_csv_runs()
    bookrag_loaded_csv_runs = [
        run
        for run in bookrag_csv_runs
        if run.get("load_status") == "ready" and run.get("vector_store_status") != "ready"
    ]
    multi_format_csv_runs = list_multi_format_csv_runs()
    multi_format_loaded_csv_runs = [
        run
        for run in multi_format_csv_runs
        if run.get("load_status") == "ready" and run.get("vector_store_status") != "ready"
    ]
    return {
        "load_app_scripts": True,
        "messages": app.state.chat_history,
        "evs": state,
        "create_ui_sections": build_create_ui_sections(),
        "text_core_ui_fields": build_text_core_ui_fields(),
        "multi_format_ui_fields": build_multi_format_ui_fields(),
        "multi_format_bookrag_ui_fields": build_multi_format_bookrag_ui_fields(),
        "create_values": app.state.create_form_values,
        "doc_pipeline_options": DOC_PIPELINE_OPTIONS,
        "create_result": app.state.last_create_operation,
        "document_uploads": app.state.document_uploads,
        "bookrag_parse_runs": list_bookrag_parse_runs(),
        "bookrag_csv_runs": bookrag_csv_runs,
        "bookrag_loaded_csv_runs": bookrag_loaded_csv_runs,
        "multi_format_parse_runs": list_multi_format_parse_runs(),
        "multi_format_csv_runs": multi_format_csv_runs,
        "multi_format_loaded_csv_runs": multi_format_loaded_csv_runs,
        "document_governance_admin": {
            "vector_store_options": list(state.get("chat_vs_options") or []),
            "selected_vector_store": str(
                state.get("bookrag_governance_vs_name")
                or state.get("last_created_vs_name")
                or state.get("selected_vs_name")
                or ""
            ).strip(),
            "status": None,
        },
        "document_relation_admin": {
            "can_manage_governance": principal is not None and principal.role in {"admin", "operator"},
            "vector_store_options": list(state.get("chat_vs_options") or []),
            "selected_vector_store": str(
                state.get("bookrag_governance_vs_name")
                or state.get("last_created_vs_name")
                or state.get("selected_vs_name")
                or ""
            ).strip(),
            "documents": [],
            "relations": [],
            "relation_types": [
                "summary_of",
                "next_issue_of",
                "updates",
                "supplement_to",
                "follow_up_to",
                "references",
                "related_to",
            ],
            "table_initialized": False,
            "status": None,
            "source": "database",
            # Loading the home page must not close and recreate the shared
            # Teradata runtime. Users can refresh the list explicitly from the
            # administration panel when they need it.
            "auto_refresh": False,
        },
        "document_metadata_admin": {
            "can_manage_governance": principal is not None and principal.role in {"admin", "operator"},
            "vector_store_options": list(state.get("chat_vs_options") or []),
            "selected_vector_store": str(
                state.get("bookrag_governance_vs_name")
                or state.get("last_created_vs_name")
                or state.get("selected_vs_name")
                or ""
            ).strip(),
            "documents": [],
            "series_options": ["main", "summary", "monthly", "spot", "topics", "other"],
            "role_options": ["comprehensive", "update", "theme", "performance", "other"],
            "precision_options": ["day", "month"],
            "status_options": ["confirmed", "review", "missing"],
            "status": None,
            "auto_refresh": False,
        },
        "document_upload_error": "",
        "document_upload_notices": app.state.document_upload_notices,
        "bookrag_section_rules": bookrag_section_rules,
        "bookrag_section_rules_path": str(BOOKRAG_SECTION_RULES_PATH),
        "bookrag_section_rules_status": None,
        "json_inspector": build_unstructured_json_inspector_context(),
        "connection_profiles": app.state.auth_store.list_connection_profiles(),
        "logged_in": _is_logged_in(request, app),
        "username": username,
        "user_role": principal.role if principal is not None else "viewer",
    }


def _render_connect_panel(request: Request, app, *, include_top_status: bool = True):
    _persist_active_session_state(request, app)
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    principal = app.state.auth_store.get_session(_session_id_from_request(request), touch=False)
    return app.state.templates.TemplateResponse(
        request,
        "partials/evs_connect_panel.html",
        {
            "evs": app.state.evs_state,
            "is_htmx": is_htmx,
            "include_top_status": include_top_status,
            "user_role": principal.role if principal is not None else "viewer",
            "connection_profiles": app.state.auth_store.list_connection_profiles(),
        },
    )


def _is_logged_in(request: Request, app) -> bool:
    return is_logged_in(request, app, SESSION_COOKIE_NAME)


def _is_poc_auth_configured(app=None) -> bool:
    if app is not None and getattr(app.state, "auth_store", None) is not None:
        return app.state.auth_store.count_users() > 0
    return is_poc_auth_configured(_load_auth_users)


def initialize_app_state(app, templates, *, settings: Settings | None = None) -> None:
    existing_state = dict(getattr(app.state, "_state", {}) or {})
    app.state = SessionAwareState(existing_state)
    app.state.templates = templates
    resolved_settings = settings or getattr(app.state, "settings", None) or Settings.from_env()
    app.state.settings = resolved_settings
    app.state.teradata_runtime_manager = TeradataRuntimeManager()
    app.state.ensure_session_runtime = _ensure_connected_runtime_for_session
    auth_store = AuthStore(
        resolved_settings.database_path,
        session_ttl_seconds=resolved_settings.session_ttl_seconds,
        pem_runtime_dir=PROJECT_DIR / "pem_runtime",
        legacy_file_root=PROJECT_DIR,
        credential_key=resolved_settings.credential_key,
        credential_key_file=resolved_settings.credential_key_file,
        allow_generated_credential_key=not resolved_settings.is_production,
    )
    auth_store.initialize()
    if resolved_settings.is_production:
        auth_store.credential_vault.cipher()
    legacy_users = _load_auth_users()
    if not legacy_users:
        fallback_username, fallback_password = poc_admin_credentials()
        if fallback_username and fallback_password:
            legacy_users[fallback_username] = fallback_password
    auth_store.bootstrap(legacy_users)
    auth_store.bootstrap_connection_config(_load_connect_defaults())
    auth_store.bootstrap_unstructured_config(_load_connect_defaults())
    auth_store.migrate_legacy_system_pem()
    auth_store.migrate_singleton_connection_profile()
    app.state.auth_store = auth_store
    app.state.job_repository = auth_store.jobs
    app.state.artifact_lifecycle = ArtifactLifecycle(
        auth_store.artifacts,
        root=PROJECT_DIR / "uploads",
    )
    app.state.user_sessions = {}
    fallback_scope = _new_session_scope()
    app.state.activate_session_scope(fallback_scope)
