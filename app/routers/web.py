from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import redact_sensitive_text
from app.routers.auth import router as auth_router
from app.routers.jobs import (
    queue_workflow_job,
    render_job_progress,
    router as jobs_router,
)
from app.routers.system_admin import router as system_admin_router
from app.services.bookrag_section_rules import (
    BOOKRAG_SECTION_RULES_PATH,
    save_bookrag_section_rules,
)
from app.services.unstructured_json_inspector import build_unstructured_json_inspector_context
from app.services.workflow_jobs import (
    BOOKRAG_CSV_GENERATE_JOB,
    BOOKRAG_CSV_LOAD_JOB,
    BOOKRAG_PARSE_JOB,
    MULTI_FORMAT_CSV_GENERATE_JOB,
    MULTI_FORMAT_CSV_LOAD_JOB,
    MULTI_FORMAT_PARSE_JOB,
    VECTOR_STORE_CREATE_JOB,
)
from app.services.bookrag_document_relations import (
    BOOKRAG_DOCUMENT_RELATION_TYPES,
    delete_document_relation,
    document_relation_table_exists,
    ensure_document_relation_table,
    fetch_bookrag_documents,
    fetch_document_relations,
    save_document_relation,
    validate_document_relations,
)
from app.services.bookrag_document_metadata import (
    BOOKRAG_DOCUMENT_ROLES,
    BOOKRAG_DOCUMENT_SERIES,
    BOOKRAG_METADATA_STATUSES,
    BOOKRAG_PUBLICATION_DATE_PRECISIONS,
    backfill_document_metadata,
    fetch_document_metadata,
    save_document_metadata,
    validate_document_metadata_import_rows,
)
from app.services.bookrag_schema import ensure_bookrag_retrieval_view
from app.services.create_config import CREATE_FIELD_MAX_LEN, default_create_values
from app.services.doc_modes.constants import collect_doc_pipeline_ui_values
from app.services.multi_format import (
    ensure_bookrag_csv_generation_target_available,
    ensure_multi_format_csv_generation_target_available,
    list_bookrag_csv_runs,
    list_multi_format_csv_runs,
)
from app.services.vector_management import (
    clear_management_results,
    clear_selected_resource,
    disconnect_sessions,
    load_sessions,
    refresh_management,
    remove_resource,
    select_resource,
)
from app.teradata_runtime import (
    Collection,
    CollectionManager,
    TERADATA_IMPORT_ERROR,
    VSManager,
    VectorStore,
    create_context,
    execute_sql,
    set_auth_token,
)
from app.web_support import (
    _activate_session_state,
    _append_connect_step,
    _apply_chat_list_output_to_state,
    _apply_list_output_to_state,
    _build_file_meta,
    _build_evs_reply,
    _build_home_context,
    _cleanup_context,
    _cleanup_result_detail,
    _cleanup_result_status,
    _clear_chat_list_result,
    _clear_destroy_result,
    _clear_health_result,
    _clear_list_result,
    _collect_upload_files,
    _current_user,
    _default_evs_state,
    _derive_base_url,
    _ensure_connected_runtime_for_session,
    _format_preview,
    _is_logged_in,
    _is_vectorstore_already_exists_error,
    _mask_token,
    _new_connect_step,
    _normalize_pem_filename_for_auth,
    _now_ts,
    _persist_active_session_state,
    _render_connect_panel,
    _resolve_path_hint,
    _save_document_uploads,
    _session_id_from_request,
    _table_from_result,
    _verify_vectorstore_exists,
)
from app.workflows.chat_flow import handle_chat_reset, handle_chat_send
from app.workflows.create_flow import handle_upload_and_prepare_create
from app.workflows.destroy_flow import destroy_failure_message, handle_destroy_selected

_BUSINESS_WRITE_PATHS = {
    "/ui/evs/destroy",
    "/ui/admin/bookrag-section-rules",
}
_ADMIN_ONLY_PATHS = {
    "/ui/evs/sessions",
    "/ui/evs/sessions/disconnect",
}
_GOVERNANCE_PREFIXES = (
    "/ui/admin/document-governance",
    "/ui/admin/document-metadata",
    "/ui/admin/document-relations",
)


async def _authorize_business_action(request: Request) -> None:
    """Enforce role and session connection requirements for browser actions."""
    path = request.url.path
    if not path.startswith("/ui/") or path.startswith("/ui/jobs/"):
        return
    principal = _session_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    scope = _activate_session_state(request, request.app)
    scope["role"] = principal.role
    is_governance = path.startswith(_GOVERNANCE_PREFIXES)
    writes_shared_data = request.method == "POST" and (
        path.startswith("/ui/create/") or path in _BUSINESS_WRITE_PATHS or is_governance
    )
    if writes_shared_data and principal.role not in {"admin", "operator"}:
        raise HTTPException(status_code=403, detail="This action requires an operator or administrator.")
    if request.method == "POST" and path in _ADMIN_ONLY_PATHS and principal.role != "admin":
        raise HTTPException(status_code=403, detail="This action requires an administrator.")
    if is_governance and (request.method == "POST" or path.endswith("/export")):
        if not scope["evs_state"].get("connected"):
            raise HTTPException(status_code=409, detail="Connect to a database before managing documents.")


async def _close_request_uploads(request: Request):
    """Close manually parsed multipart uploads on success and every error path."""
    try:
        yield
    finally:
        await request.close()


router = APIRouter(dependencies=[Depends(_close_request_uploads), Depends(_authorize_business_action)])
router.include_router(auth_router)
router.include_router(system_admin_router)
router.include_router(jobs_router)


def _session_principal(request: Request):
    auth_store = getattr(request.app.state, "auth_store", None)
    if auth_store is None:
        return None
    return auth_store.get_session(_session_id_from_request(request))


def _can_manage_governance(app) -> bool:
    active_scope = getattr(app.state, "active_session_scope", None)
    scope = active_scope() if callable(active_scope) else None
    return (scope or {}).get("role") in {"admin", "operator"}


def _render_connection_state(request: Request):
    """Refresh dependent panels when connection gates change in an HTMX request."""
    _persist_active_session_state(request, request.app)
    if getattr(request, "headers", {}).get("HX-Request", "").lower() != "true":
        return _render_connect_panel(request, request.app)
    context = _build_home_context(request, request.app)
    context["is_htmx"] = True
    return request.app.state.templates.TemplateResponse(
        request, "partials/evs_reset_response.html", context,
    )


def _validate_document_parse_inputs(documents: list[dict]) -> None:
    if not documents:
        raise HTTPException(status_code=422, detail="Upload at least one document before parsing.")


def _register_document_artifacts(request: Request, uploaded_items: list[dict]) -> None:
    lifecycle = getattr(request.app.state, "artifact_lifecycle", None)
    settings = getattr(request.app.state, "settings", None)
    if lifecycle is None:
        return
    principal = _session_principal(request)
    retention_days = int(getattr(settings, "artifact_retention_days", 30))
    for item in uploaded_items:
        try:
            lifecycle.register_file(
                Path(_resolve_path_hint(str(item.get("saved_path") or ""))),
                kind="document-upload",
                retention_days=retention_days,
                owner_user_id=principal.user_id if principal is not None else None,
                metadata={"doc_id": item.get("doc_id"), "filename": item.get("filename")},
            )
        except (FileNotFoundError, ValueError) as ex:
            request.app.state.document_upload_notices.append(
                f"Artifact registration skipped for {item.get('filename') or 'upload'}: {ex}"
            )


def _admin_checkbox_checked(form_data, field_name: str) -> bool:
    value = form_data.get(field_name)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _admin_csv_values(form_data, field_name: str) -> list[str]:
    raw_value = str(form_data.get(field_name) or "").strip()
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _build_bookrag_admin_context(rules_payload: dict, status: dict | None = None) -> dict:
    return {
        "bookrag_section_rules": rules_payload,
        "bookrag_section_rules_path": str(BOOKRAG_SECTION_RULES_PATH),
        "bookrag_section_rules_status": status,
        "json_inspector": build_unstructured_json_inspector_context(),
    }


def _document_relation_schema_name(app) -> str | None:
    create_values = getattr(app.state, "create_form_values", {}) or {}
    state = getattr(app.state, "evs_state", {}) or {}
    params = state.get("actual_params") or state.get("params") or {}
    return str(create_values.get("target_database") or params.get("username") or "").strip() or None


def _bookrag_admin_vector_store_options(app) -> list[str]:
    """Include locally loaded BookRAG runs before their Vector Store becomes Ready."""
    state = app.state.evs_state
    schema_name = _document_relation_schema_name(app)
    local_run_names: list[str] = []
    for run in list_bookrag_csv_runs():
        if str(run.get("load_status") or "").strip().lower() != "ready":
            continue
        run_schema = str(run.get("target_database") or "").strip()
        if schema_name and run_schema and run_schema.lower() != schema_name.lower():
            continue
        name = str(run.get("vector_store_name") or "").strip()
        if name:
            local_run_names.append(name)
    return list(dict.fromkeys(
        str(item).strip()
        for item in (
            list(state.get("chat_vs_options") or [])
            + local_run_names
            + [
                state.get("bookrag_governance_vs_name"),
                state.get("last_created_vs_name"),
                state.get("selected_vs_name"),
            ]
        )
        if str(item or "").strip()
    ))


def _document_relation_admin_context(
    app,
    *,
    vector_store_name: str = "",
    status: dict | None = None,
    auto_refresh: bool = False,
) -> dict:
    state = app.state.evs_state
    options = _bookrag_admin_vector_store_options(app)
    selected = str(
        vector_store_name
        or state.get("bookrag_governance_vs_name")
        or state.get("last_created_vs_name")
        or state.get("selected_vs_name")
        or ""
    ).strip()
    context = {
        "vector_store_options": options,
        "selected_vector_store": selected,
        "documents": [],
        "relations": [],
        "relation_types": list(BOOKRAG_DOCUMENT_RELATION_TYPES),
        "table_initialized": False,
        "status": status,
        "source": "database",
        "auto_refresh": auto_refresh,
        "can_manage_governance": _can_manage_governance(app),
    }
    if not selected:
        return context
    if not state.get("connected"):
        context["status"] = status or {
            "kind": "warn", "title": "Not Connected", "detail": "Connect to a database before loading documents."
        }
        return context
    try:
        schema_name = _document_relation_schema_name(app)
        context["documents"] = fetch_bookrag_documents(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        context["table_initialized"] = document_relation_table_exists(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        if context["table_initialized"]:
            context["relations"] = fetch_document_relations(
                vector_store_name=selected,
                schema_name=schema_name,
                execute_sql_fn=execute_sql,
            )
    except Exception as ex:
        if status is None:
            context["status"] = {
                "kind": "error",
                "title": "Document Relationship Load Failed",
                "detail": str(ex),
            }
    return context


def _document_metadata_admin_context(
    app,
    *,
    vector_store_name: str = "",
    status: dict | None = None,
    auto_refresh: bool = False,
) -> dict:
    state = app.state.evs_state
    options = _bookrag_admin_vector_store_options(app)
    selected = str(
        vector_store_name
        or state.get("bookrag_governance_vs_name")
        or state.get("last_created_vs_name")
        or state.get("selected_vs_name")
        or ""
    ).strip()
    context = {
        "vector_store_options": options,
        "selected_vector_store": selected,
        "documents": [],
        "series_options": BOOKRAG_DOCUMENT_SERIES,
        "role_options": BOOKRAG_DOCUMENT_ROLES,
        "precision_options": BOOKRAG_PUBLICATION_DATE_PRECISIONS,
        "status_options": BOOKRAG_METADATA_STATUSES,
        "status": status,
        "auto_refresh": auto_refresh,
        "can_manage_governance": _can_manage_governance(app),
    }
    if not selected:
        return context
    if not state.get("connected"):
        context["status"] = status or {
            "kind": "warn", "title": "Not Connected", "detail": "Connect to a database before loading documents."
        }
        return context
    try:
        schema_name = _document_relation_schema_name(app)
        context["documents"] = fetch_document_metadata(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
    except Exception as ex:
        if status is None:
            context["status"] = {
                "kind": "error",
                "title": "Document Metadata Load Failed",
                "detail": str(ex),
            }
    return context


def _document_governance_admin_context(
    app,
    *,
    vector_store_name: str = "",
    status: dict | None = None,
) -> dict:
    """Build both governance sections from one shared Vector Store selection."""
    state = app.state.evs_state
    selected = str(
        vector_store_name
        or state.get("bookrag_governance_vs_name")
        or state.get("last_created_vs_name")
        or state.get("selected_vs_name")
        or ""
    ).strip()
    options = _bookrag_admin_vector_store_options(app)
    return {
        "document_governance_admin": {
            "vector_store_options": options,
            "selected_vector_store": selected,
            "status": status,
        },
        "document_metadata_admin": _document_metadata_admin_context(
            app,
            vector_store_name=selected,
        ),
        "document_relation_admin": _document_relation_admin_context(
            app,
            vector_store_name=selected,
        ),
    }


def _refresh_document_relation_vector_store_options(request: Request) -> dict[str, str]:
    state = request.app.state.evs_state
    if VSManager is None or not callable(getattr(VSManager, "list", None)):
        return {
            "kind": "error",
            "title": "Vector Store Refresh Failed",
            "detail": f"VSManager.list() is unavailable. {TERADATA_IMPORT_ERROR}".strip(),
        }

    try:
        _ensure_connected_runtime_for_session(
            request,
            request.app,
            allow_saved_params=True,
        )
        previous_selected = str(state.get("selected_vs_name") or "").strip()
        list_output = VSManager.list()
        visible_rows, total_rows, _username_filter = _apply_chat_list_output_to_state(state, list_output)
        state["selected_vs_name"] = previous_selected
        total_label = str(total_rows) if total_rows is not None else str(visible_rows)
        return {
            "kind": "ok",
            "title": "Vector Stores Refreshed",
            "detail": f"Loaded {visible_rows} selectable Vector Store(s) from {total_label} row(s).",
        }
    except Exception as ex:
        return {
            "kind": "error",
            "title": "Vector Store Refresh Failed",
            "detail": str(ex),
        }

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not _is_logged_in(request, request.app):
        return RedirectResponse(url="/login", status_code=303)
    _activate_session_state(request, request.app)
    context = _build_home_context(request, request.app)
    return request.app.state.templates.TemplateResponse(request, "index.html", context)


@router.post("/ui/evs/connect", response_class=HTMLResponse)
async def evs_connect(request: Request, connection_id: int | None = Form(default=None)):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    state.pop("_sdk_auth_data", None)
    clear_management_results(state)
    steps: list[dict[str, str]] = []
    actual_params: dict = {}
    try:
        saved_config = request.app.state.auth_store.get_connection_profile(connection_id)
        if saved_config is None:
            raise ValueError("Select a valid database connection.")
    except Exception as ex:
        state["connected"] = False
        state["last_success"] = ""
        state["last_error"] = f"System connection configuration could not be loaded: {ex}"
        state["connect_steps"] = [_new_connect_step("Load System Configuration", "error", str(ex))]
        return _render_connection_state(request)

    resolved_pem_path = str(saved_config.get("pem_file") or "").strip()
    if resolved_pem_path:
        resolved_existing_pem = _resolve_path_hint(resolved_pem_path)
        if Path(resolved_existing_pem).is_file():
            steps.append(_new_connect_step("PEM File", "ok", f"Using configured PEM path: {resolved_pem_path}"))
        else:
            steps.append(_new_connect_step("PEM File", "warn", f"Configured PEM path not found: {resolved_pem_path}"))
    else:
        steps.append(_new_connect_step("PEM File", "warn", "No PEM file is configured."))

    params = {
        "host": str(saved_config.get("host") or "").strip(),
        "username": str(saved_config.get("username") or "").strip(),
        "password": str(saved_config.get("password") or ""),
        "ues_url": str(saved_config.get("ues_url") or "").strip(),
        "pat_token": str(saved_config.get("pat_token") or "").strip(),
        "pem_file": resolved_pem_path,
    }
    previous_params = state.get("params") or {}
    params["unstructured_api_url"] = str(previous_params.get("unstructured_api_url") or "").strip()
    params["unstructured_api_key"] = str(previous_params.get("unstructured_api_key") or "").strip()
    if params["pat_token"]:
        steps.append(_new_connect_step("PAT Token", "ok", f"Using configured token: {_mask_token(params['pat_token'])}"))
    else:
        steps.append(_new_connect_step("PAT Token", "warn", "PAT token is not configured. An administrator must save it."))
    state["params"] = params
    state["selected_connection_id"] = saved_config.get("id")
    state["selected_connection_name"] = saved_config.get("name", "")
    steps.append(
        _new_connect_step(
            "Input Capture",
            "ok",
            f"host={params['host']}, username={params['username']}, ues_url={params['ues_url']}",
        )
    )

    missing = []
    if not params["host"]:
        missing.append("host")
    if not params["username"]:
        missing.append("username")
    if not params["password"]:
        missing.append("password")
    if not params["pat_token"]:
        missing.append("pat_token")
    if not params["ues_url"]:
        missing.append("ues_url")
    if not params["pem_file"]:
        missing.append("pem_file")

    if missing:
        steps.append(_new_connect_step("Validate Required Fields", "error", f"Missing required fields: {', '.join(missing)}"))
        state["connected"] = False
        state["connected_at"] = ""
        state["last_success"] = ""
        state["last_error"] = f"Missing required fields: {', '.join(missing)}"
        _clear_health_result(state)
        _clear_list_result(state)
        _clear_chat_list_result(state)
        state["selected_vs_name"] = ""
        _clear_destroy_result(state)
        state["actual_params"] = actual_params
        state["connect_steps"] = steps
    elif not (create_context and set_auth_token and VSManager):
        steps.append(
            _new_connect_step(
                "Dependency Check",
                "error",
                f"teradataml/teradatagenai import failed: {TERADATA_IMPORT_ERROR}",
            )
        )
        state["connected"] = False
        state["connected_at"] = ""
        state["last_success"] = ""
        state["last_error"] = (
            "teradataml/teradatagenai is not available. "
            "Install them first. "
            f"Import error: {TERADATA_IMPORT_ERROR}"
        )
        _clear_health_result(state)
        _clear_list_result(state)
        _clear_chat_list_result(state)
        state["selected_vs_name"] = ""
        _clear_destroy_result(state)
        state["actual_params"] = actual_params
        state["connect_steps"] = steps
    else:
        steps.append(_new_connect_step("Validate Required Fields", "ok", "All required fields are present."))
        derived_base_url = _derive_base_url(params["ues_url"])
        steps.append(_new_connect_step("Derive Base URL", "ok", f"base_url = {derived_base_url}"))
        resolved_pem_for_auth = _resolve_path_hint(params["pem_file"])
        normalized_pem_for_auth = _normalize_pem_filename_for_auth(resolved_pem_for_auth) if resolved_pem_for_auth else ""
        pem_meta = _build_file_meta(params["pem_file"])
        warnings: list[str] = []
        if params["pem_file"] and not Path(resolved_pem_for_auth).is_file():
            warnings.append("PEM path not found on disk; authentication will use provided raw value.")
            steps.append(
                _new_connect_step(
                    "Resolve PEM Path",
                    "warn",
                    f"PEM file not found on disk, using raw value: {params['pem_file']}",
                )
            )
        elif resolved_pem_for_auth:
            steps.append(_new_connect_step("Resolve PEM Path", "ok", f"Resolved PEM path: {resolved_pem_for_auth}"))
        else:
            steps.append(_new_connect_step("Resolve PEM Path", "warn", "No PEM path resolved."))
        if normalized_pem_for_auth and normalized_pem_for_auth != resolved_pem_for_auth:
            steps.append(
                _new_connect_step(
                    "Normalize PEM Filename",
                    "ok",
                    f"Auth will use normalized filename path: {normalized_pem_for_auth}",
                )
            )
        elif normalized_pem_for_auth:
            steps.append(
                _new_connect_step(
                    "Normalize PEM Filename",
                    "ok",
                    f"Filename already valid for auth: {normalized_pem_for_auth}",
                )
            )
        try:
            cleanup_before = _cleanup_context()
            steps.append(
                _new_connect_step(
                    "Cleanup Previous Session",
                    _cleanup_result_status(cleanup_before),
                    _cleanup_result_detail(cleanup_before),
                )
            )
            create_context(host=params["host"], username=params["username"], password=params["password"])
            steps.append(_new_connect_step("create_context", "ok", "Database context created successfully."))

            auth_kwargs = {"base_url": derived_base_url, "pat_token": params["pat_token"]}
            if normalized_pem_for_auth:
                auth_kwargs["pem_file"] = normalized_pem_for_auth
            elif resolved_pem_for_auth:
                auth_kwargs["pem_file"] = resolved_pem_for_auth
            elif params["pem_file"]:
                auth_kwargs["pem_file"] = params["pem_file"]
            actual_params = {
                "create_context": {
                    "host": params["host"],
                    "username": params["username"],
                    "password_length": len(params["password"] or ""),
                },
                "set_auth_token": {
                    "base_url": derived_base_url,
                    "pat_token": _mask_token(params["pat_token"]),
                    "pem_file": Path(str(auth_kwargs.get("pem_file") or "")).name,
                    "pem_meta": pem_meta,
                },
                "pem_resolution": {
                    "input": Path(str(params["pem_file"] or "")).name,
                    "resolved": Path(str(resolved_pem_for_auth or "")).name,
                    "normalized": Path(str(normalized_pem_for_auth or "")).name,
                },
            }
            state["_sdk_auth_data"] = set_auth_token(**auth_kwargs)
            steps.append(_new_connect_step("set_auth_token", "ok", "VS authentication token set successfully with selected PEM."))

            state["connected"] = True
            state["connected_at"] = _now_ts()
            state["last_error"] = " | ".join(warnings) if warnings else ""
            state["last_success"] = "Step 1 completed. Database connection and VS authentication succeeded."
            _clear_health_result(state)
            _clear_list_result(state)
            _clear_chat_list_result(state)
            state["selected_vs_name"] = ""
            _clear_destroy_result(state)
            clear_management_results(state)
            state["actual_params"] = actual_params
            runtime_manager = getattr(request.app.state, "teradata_runtime_manager", None)
            if runtime_manager is not None:
                runtime_manager.mark_active(
                    f"session:{_session_id_from_request(request) or 'anonymous'}:"
                    f"connection:{state.get('selected_connection_id') or 'default'}"
                )
            steps.append(_new_connect_step("VSManager.list()", "info", "Skipped on connect. Click 'Run List' manually."))
            state["connect_steps"] = steps
        except Exception as ex:
            cleanup_after_fail = _cleanup_context()
            safe_error = redact_sensitive_text(
                ex,
                secrets=(params.get("password"), params.get("pat_token")),
            )
            steps.append(_new_connect_step("Execution Failed", "error", safe_error))
            steps.append(
                _new_connect_step(
                    "Rollback Cleanup",
                    _cleanup_result_status(cleanup_after_fail),
                    _cleanup_result_detail(cleanup_after_fail),
                )
            )
            state["connected"] = False
            state.pop("_sdk_auth_data", None)
            state["connected_at"] = ""
            state["last_success"] = ""
            state["last_error"] = f"Connection/auth failed: {safe_error}"
            _clear_health_result(state)
            _clear_list_result(state)
            _clear_chat_list_result(state)
            state["selected_vs_name"] = ""
            _clear_destroy_result(state)
            state["actual_params"] = actual_params
            state["connect_steps"] = steps
            runtime_manager = getattr(request.app.state, "teradata_runtime_manager", None)
            if runtime_manager is not None:
                runtime_manager.invalidate()

    return _render_connection_state(request)


@router.post("/ui/evs/reset", response_class=HTMLResponse)
async def evs_reset(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected_connection_id = request.app.state.evs_state.get("selected_connection_id")
    cleanup_result = _cleanup_context()
    runtime_manager = getattr(request.app.state, "teradata_runtime_manager", None)
    if runtime_manager is not None:
        runtime_manager.invalidate()
    reset_state = _default_evs_state()
    principal = _session_principal(request)
    if principal is not None:
        saved = request.app.state.auth_store.get_connection_profile(selected_connection_id)
        if saved is None:
            saved = request.app.state.auth_store.get_connection_profile()
        if saved:
            reset_state["params"].update(saved)
            reset_state["selected_connection_id"] = saved.get("id")
            reset_state["selected_connection_name"] = saved.get("name", "")
    reset_state["last_success"] = "Disconnected and reset completed."
    reset_state["connect_steps"] = [
        _new_connect_step(
            "Reset / Disconnect",
            _cleanup_result_status(cleanup_result),
            f"Reset endpoint called. {_cleanup_result_detail(cleanup_result)}",
        )
    ]
    request.app.state.evs_state = reset_state
    request.app.state.create_form_values = default_create_values()
    request.app.state.last_create_operation = None
    request.app.state.document_uploads = []
    request.app.state.document_upload_notices = []
    request.app.state.chat_history = []
    _persist_active_session_state(request, request.app)
    return _render_connection_state(request)


@router.post("/ui/create/upload-documents", response_class=HTMLResponse)
async def upload_documents_for_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)

    form = await request.form()
    files = _collect_upload_files(form, field_name="files")

    saved: list[dict] = []
    notices: list[str] = []
    upload_error = ""
    if not files:
        upload_error = "No files selected."
    else:
        saved, notices = await _save_document_uploads(files)
        if not saved:
            upload_error = "No valid files found in selection."

    if saved:
        request.app.state.document_uploads = saved
    request.app.state.document_upload_notices = notices
    _register_document_artifacts(request, saved)

    _persist_active_session_state(request, request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/selected_documents.html",
        {
            "document_uploads": request.app.state.document_uploads,
            "document_upload_error": upload_error,
            "document_upload_notices": notices,
        },
    )


@router.post("/ui/create/parse-documents", response_class=HTMLResponse)
async def parse_documents_for_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    uploaded_documents = [dict(item) for item in request.app.state.document_uploads]
    create_values = default_create_values()
    create_values.update(collect_doc_pipeline_ui_values(form, field_max_len=CREATE_FIELD_MAX_LEN))
    vector_store_name = str(form.get("vector_store_name") or "").strip()
    _validate_document_parse_inputs(uploaded_documents)
    job = queue_workflow_job(
        request,
        kind=BOOKRAG_PARSE_JOB,
        payload={
            "create_values": create_values,
            "vector_store_name": vector_store_name,
            "uploaded_documents": uploaded_documents,
        },
    )
    return render_job_progress(request, job)


@router.post("/ui/create/generate-csv", response_class=HTMLResponse)
async def generate_csv_for_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    parse_run_id = str(
        form.get("bookrag_parse_run_id_current")
        or form.get("bookrag_parse_run_id")
        or ""
    ).strip()
    create_values = default_create_values()
    create_values.update(collect_doc_pipeline_ui_values(form, field_max_len=CREATE_FIELD_MAX_LEN))
    vector_store_name = str(form.get("bookrag_csv_vector_store_name") or "").strip()
    evs_state = getattr(request.app.state, "evs_state", {}) or {}
    connection_params = dict(evs_state.get("params") or {})
    target_database = str(
        form.get("bookrag_csv_target_database")
        or form.get("target_database")
        or connection_params.get("username")
        or ""
    ).strip()

    try:
        ensure_bookrag_csv_generation_target_available(
            vector_store_name=vector_store_name,
            target_database=target_database,
        )
    except RuntimeError as ex:
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/bookrag_csv_generation_result.html",
            {
                "bookrag_csv_generation": None,
                "bookrag_csv_generation_error": str(ex),
            },
        )

    job = queue_workflow_job(
        request,
        kind=BOOKRAG_CSV_GENERATE_JOB,
        payload={
            "parse_run_id": parse_run_id,
            "create_values": create_values,
            "vector_store_name": vector_store_name,
            "target_database": target_database,
        },
    )
    return render_job_progress(request, job)


@router.post("/ui/create/load-csv-tables", response_class=HTMLResponse)
async def load_csv_tables(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    csv_run_id = str(
        form.get("bookrag_csv_run_id_current")
        or form.get("bookrag_csv_run_id")
        or ""
    ).strip()
    summary = None
    error = ""
    try:
        if not request.app.state.evs_state.get("connected"):
            raise RuntimeError("Connect/authenticate in Step 1 before loading CSV files.")
        if execute_sql is None:
            raise RuntimeError(f"Teradata SQL runtime is unavailable: {TERADATA_IMPORT_ERROR}")

        csv_run = next(
            (item for item in list_bookrag_csv_runs() if item["csv_run_id"] == csv_run_id),
            None,
        )
        if csv_run is None:
            raise RuntimeError("Select a CSV generation run in ready status.")

        job = queue_workflow_job(
            request,
            kind=BOOKRAG_CSV_LOAD_JOB,
            payload={"csv_run_id": csv_run_id},
        )
        return render_job_progress(request, job)
    except Exception as ex:
        error = str(ex)
        request.app.state.evs_state["last_success"] = ""
        request.app.state.evs_state["last_error"] = error

    return request.app.state.templates.TemplateResponse(
        request,
        "partials/bookrag_csv_load_result.html",
        {
            "bookrag_csv_load": None,
            "bookrag_csv_load_error": error,
            "bookrag_loaded_csv_runs": [],
            "bookrag_selected_loaded_csv_run_id": "",
            "bookrag_vector_name_oob": False,
            "create_values": getattr(request.app.state, "create_form_values", {}),
            "evs": request.app.state.evs_state,
        },
    )


@router.post("/ui/create/multi-format/parse-documents", response_class=HTMLResponse)
async def parse_multi_format_documents_for_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    create_values = default_create_values()
    create_values.update(collect_doc_pipeline_ui_values(form, field_max_len=CREATE_FIELD_MAX_LEN))
    vector_store_name = str(form.get("vector_store_name") or "").strip()
    uploaded_documents = [dict(item) for item in request.app.state.document_uploads]
    _validate_document_parse_inputs(uploaded_documents)
    job = queue_workflow_job(
        request,
        kind=MULTI_FORMAT_PARSE_JOB,
        payload={
            "create_values": create_values,
            "vector_store_name": vector_store_name,
            "uploaded_documents": uploaded_documents,
        },
    )
    return render_job_progress(request, job)


@router.post("/ui/create/multi-format/generate-csv", response_class=HTMLResponse)
async def generate_multi_format_csv_for_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    parse_run_id = str(
        form.get("multi_format_parse_run_id_current")
        or form.get("multi_format_parse_run_id")
        or ""
    ).strip()
    evs_state = getattr(request.app.state, "evs_state", {}) or {}
    connection_params = dict(evs_state.get("params") or {})
    vector_store_name = str(form.get("multi_format_csv_vector_store_name") or "").strip()
    target_database = str(
        form.get("multi_format_csv_target_database")
        or form.get("target_database")
        or connection_params.get("username")
        or ""
    ).strip()
    try:
        ensure_multi_format_csv_generation_target_available(
            vector_store_name=vector_store_name,
            target_database=target_database,
        )
    except RuntimeError as ex:
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/multi_format_csv_generation_result.html",
            {
                "multi_format_csv_generation": None,
                "multi_format_csv_generation_error": str(ex),
            },
        )
    job = queue_workflow_job(
        request,
        kind=MULTI_FORMAT_CSV_GENERATE_JOB,
        payload={
            "parse_run_id": parse_run_id,
            "vector_store_name": vector_store_name,
            "target_database": target_database,
        },
    )
    return render_job_progress(request, job)


@router.post("/ui/create/multi-format/load-csv-table", response_class=HTMLResponse)
async def load_multi_format_csv_table(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    form = await request.form()
    csv_run_id = str(
        form.get("multi_format_csv_run_id_current")
        or form.get("multi_format_csv_run_id")
        or ""
    ).strip()
    summary = None
    error = ""
    try:
        if not request.app.state.evs_state.get("connected"):
            raise RuntimeError("Connect/authenticate in Step 1 before loading CSV files.")
        if execute_sql is None:
            raise RuntimeError(f"Teradata SQL runtime is unavailable: {TERADATA_IMPORT_ERROR}")
        if not any(item["csv_run_id"] == csv_run_id for item in list_multi_format_csv_runs()):
            raise RuntimeError("Select a Multi-Format CSV generation run in ready status.")
        job = queue_workflow_job(
            request,
            kind=MULTI_FORMAT_CSV_LOAD_JOB,
            payload={"csv_run_id": csv_run_id},
        )
        return render_job_progress(request, job)
    except Exception as ex:
        error = str(ex)
        request.app.state.evs_state["last_success"] = ""
        request.app.state.evs_state["last_error"] = error
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/multi_format_csv_load_result.html",
        {
            "multi_format_csv_load": None,
            "multi_format_csv_load_error": error,
            "multi_format_loaded_csv_runs": [],
            "multi_format_selected_loaded_csv_run_id": "",
            "multi_format_vector_name_oob": False,
            "create_values": getattr(request.app.state, "create_form_values", {}),
            "evs": request.app.state.evs_state,
        },
    )


@router.post("/ui/evs/health", response_class=HTMLResponse)
async def evs_run_health(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    if not state["connected"]:
        _clear_health_result(state)
        state["health_preview"] = "Connect in Step 1 first."
        state["last_error"] = "Run blocked: connection is not established."
        _append_connect_step(state, "VSManager.health()", "warn", "Blocked: Step 1 is not connected.")
        return _render_connect_panel(request, request.app)
    if VSManager is None:
        _clear_health_result(state)
        state["health_preview"] = f"Cannot run: {TERADATA_IMPORT_ERROR}"
        state["last_error"] = "VS runtime is unavailable."
        _append_connect_step(state, "VSManager.health()", "error", f"Runtime unavailable: {TERADATA_IMPORT_ERROR}")
        return _render_connect_panel(request, request.app)
    health_fn = getattr(VSManager, "health", None)
    if not callable(health_fn):
        _clear_health_result(state)
        state["health_preview"] = "Cannot run: VSManager.health is not callable."
        state["last_error"] = "VSManager.health() is not callable."
        _append_connect_step(state, "VSManager.health()", "error", "VSManager.health is missing or not callable.")
        return _render_connect_panel(request, request.app)
    try:
        health_output = health_fn()
        headers, rows_data = _table_from_result(health_output)
        state["health_columns"] = headers
        state["health_rows"] = rows_data
        state["health_row_count"] = len(rows_data)
        state["health_preview"] = _format_preview(health_output, max_chars=None)
        state["last_error"] = ""
        state["last_success"] = "VSManager.health() completed."
        _append_connect_step(state, "VSManager.health()", "ok", "Called successfully.")
    except Exception as ex:
        _clear_health_result(state)
        state["health_preview"] = f"Error: {ex}"
        state["last_error"] = f"VSManager.health() failed: {ex}"
        _append_connect_step(state, "VSManager.health()", "error", f"Execution failed: {ex}")
    return _render_connect_panel(request, request.app)


def _redact_management_messages(state: dict) -> None:
    params = state.get("params") or {}
    secrets = (params.get("password"), params.get("pat_token"))
    for key in (
        "management_health_detail",
        "resource_detail_preview",
        "resource_status_preview",
        "resource_file_preview",
        "resource_permission_preview",
        "session_preview",
    ):
        if state.get(key):
            state[key] = redact_sensitive_text(state[key], secrets=secrets)
    state["management_warnings"] = [
        redact_sensitive_text(message, secrets=secrets)
        for message in state.get("management_warnings") or []
    ]


@router.post("/ui/evs/refresh", response_class=HTMLResponse)
async def evs_refresh_management(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    _clear_destroy_result(state)
    if not state.get("connected"):
        clear_management_results(state)
        state["last_success"] = ""
        state["last_error"] = "Connect to a database before refreshing management data."
        return _render_connect_panel(request, request.app)

    refresh_management(
        state,
        auth_data=state.get("_sdk_auth_data"),
        vs_manager_class=VSManager,
        collection_manager_class=CollectionManager,
        connection_username=str((state.get("params") or {}).get("username") or ""),
        table_from_result=_table_from_result,
        format_preview=_format_preview,
        now=_now_ts,
        vector_store_class=VectorStore,
        collection_class=Collection,
    )
    _redact_management_messages(state)
    resource_count = len(state.get("resource_rows") or [])
    warning_count = len(state.get("management_warnings") or [])
    state["last_error"] = ""
    state["last_success"] = (
        f"Management data refreshed. {resource_count} resource(s) loaded"
        + (f" with {warning_count} warning(s)." if warning_count else ".")
    )
    _append_connect_step(
        state,
        "EVS management refresh",
        "warn" if warning_count else "ok",
        f"Loaded {resource_count} resource name(s) for the active database connection; warnings={warning_count}.",
    )
    return _render_connect_panel(request, request.app)


@router.post("/ui/evs/list", response_class=HTMLResponse)
async def evs_run_list(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    _clear_destroy_result(state)
    if not state["connected"]:
        _clear_list_result(state)
        state["list_preview"] = "Connect in Step 1 first."
        state["last_error"] = "Run blocked: connection is not established."
        _append_connect_step(state, "VSManager.list()", "warn", "Blocked: Step 1 is not connected.")
        return _render_connect_panel(request, request.app)
    if VSManager is None:
        _clear_list_result(state)
        state["list_preview"] = f"Cannot run: {TERADATA_IMPORT_ERROR}"
        state["last_error"] = "VS runtime is unavailable."
        _append_connect_step(state, "VSManager.list()", "error", f"Runtime unavailable: {TERADATA_IMPORT_ERROR}")
        return _render_connect_panel(request, request.app)
    list_fn = getattr(VSManager, "list", None)
    if not callable(list_fn):
        _clear_list_result(state)
        state["list_preview"] = "Cannot run: VSManager.list is not callable."
        state["last_error"] = "VSManager.list() is not callable."
        _append_connect_step(state, "VSManager.list()", "error", "VSManager.list is missing or not callable.")
        return _render_connect_panel(request, request.app)
    try:
        list_output = list_fn()
        visible_rows, total_rows, username_filter = _apply_list_output_to_state(
            state,
            list_output,
            sync_chat_options=False,
        )
        state["list_loaded_by_user"] = True
        if total_rows is not None:
            if username_filter:
                _append_connect_step(
                    state,
                    "VSManager.list()",
                    "ok",
                    f"Called successfully. rows={visible_rows}/{total_rows} (filtered by username='{username_filter}').",
                )
            else:
                _append_connect_step(state, "VSManager.list()", "ok", f"Called successfully. rows={total_rows}.")
        else:
            _append_connect_step(state, "VSManager.list()", "ok", "Called successfully.")
        state["last_error"] = ""
        state["last_success"] = "VSManager.list() completed."
    except Exception as ex:
        _clear_list_result(state)
        state["list_preview"] = f"Error: {ex}"
        state["last_error"] = f"VSManager.list() failed: {ex}"
        _append_connect_step(state, "VSManager.list()", "error", f"Execution failed: {ex}")
    return _render_connect_panel(request, request.app)


@router.post("/ui/chat/vs-list", response_class=HTMLResponse)
async def chat_run_list(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)

    state = request.app.state.evs_state
    if not state["connected"]:
        _clear_chat_list_result(state)
        state["chat_list_preview"] = "Connect in Step 1 first."
        _persist_active_session_state(request, request.app)
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/chat_vector_store_list.html",
            {"evs": state, "is_oob": False},
        )

    if VSManager is None:
        _clear_chat_list_result(state)
        state["chat_list_preview"] = f"Cannot run: {TERADATA_IMPORT_ERROR}"
        _persist_active_session_state(request, request.app)
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/chat_vector_store_list.html",
            {"evs": state, "is_oob": False},
        )

    list_fn = getattr(VSManager, "list", None)
    if not callable(list_fn):
        _clear_chat_list_result(state)
        state["chat_list_preview"] = "Cannot run: VSManager.list is not callable."
        _persist_active_session_state(request, request.app)
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/chat_vector_store_list.html",
            {"evs": state, "is_oob": False},
        )

    try:
        list_output = list_fn()
        visible_rows, total_rows, username_filter = _apply_chat_list_output_to_state(state, list_output)
        if total_rows is not None:
            if username_filter:
                _append_connect_step(
                    state,
                    "Step 3 VSManager.list()",
                    "ok",
                    f"Called successfully. rows={visible_rows}/{total_rows} (filtered by username='{username_filter}').",
                )
            else:
                _append_connect_step(state, "Step 3 VSManager.list()", "ok", f"Called successfully. rows={total_rows}.")
        else:
            _append_connect_step(state, "Step 3 VSManager.list()", "ok", "Called successfully.")
        state["last_error"] = ""
        state["last_success"] = "Step 3 Run List completed."
    except Exception as ex:
        _clear_chat_list_result(state)
        state["chat_list_preview"] = f"Error: {ex}"
        state["last_error"] = f"Step 3 Run List failed: {ex}"
        _append_connect_step(state, "Step 3 VSManager.list()", "error", f"Execution failed: {ex}")

    _persist_active_session_state(request, request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/chat_vector_store_list.html",
        {"evs": state, "is_oob": False},
    )


@router.post("/ui/evs/select", response_class=HTMLResponse)
async def evs_select_from_list(
    request: Request,
    vs_name: str = Form(default=""),
    resource_kind: str = Form(default="v1"),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)

    state = request.app.state.evs_state
    selected_name = (vs_name or str(request.query_params.get("vs_name", ""))).strip()
    selected_kind = (resource_kind or str(request.query_params.get("resource_kind", "v1"))).strip().lower()
    if selected_kind not in {"v1", "v2"}:
        return HTMLResponse("Invalid resource API generation.", status_code=422)
    if selected_name:
        if not select_resource(state, kind=selected_kind, name=selected_name):
            return HTMLResponse("The selected resource is not available for this database connection.", status_code=404)
    return _render_connect_panel(request, request.app, include_top_status=False)


@router.post("/ui/evs/sessions", response_class=HTMLResponse)
async def evs_load_sessions(request: Request):
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    if not state.get("connected"):
        state["last_success"] = ""
        state["last_error"] = "Connect to a database before loading active sessions."
        return _render_connect_panel(request, request.app)
    try:
        load_sessions(
            state,
            auth_data=state.get("_sdk_auth_data"),
            vs_manager_class=VSManager,
            collection_manager_class=CollectionManager,
            format_preview=_format_preview,
            now=_now_ts,
        )
        state["last_error"] = ""
        state["last_success"] = f"Loaded {len(state.get('session_rows') or [])} active session(s)."
    except Exception as ex:
        state["sessions_loaded"] = True
        state["session_rows"] = []
        state["session_preview"] = f"Unable to load active sessions: {ex}"
        state["last_success"] = ""
        state["last_error"] = state["session_preview"]
    _redact_management_messages(state)
    return _render_connect_panel(request, request.app)


@router.post("/ui/evs/sessions/disconnect", response_class=HTMLResponse)
async def evs_disconnect_sessions(request: Request, username: list[str] = Form(default=[])):
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    if not state.get("connected"):
        state["last_success"] = ""
        state["last_error"] = "Connect to a database before disconnecting sessions."
        return _render_connect_panel(request, request.app)
    try:
        disconnect_sessions(
            usernames=username,
            auth_data=state.get("_sdk_auth_data"),
            collection_manager_class=CollectionManager,
        )
        load_sessions(
            state,
            auth_data=state.get("_sdk_auth_data"),
            vs_manager_class=VSManager,
            collection_manager_class=CollectionManager,
            format_preview=_format_preview,
            now=_now_ts,
        )
        disconnected = ", ".join(sorted(set(username)))
        state["last_error"] = ""
        state["last_success"] = f"Disconnected EVS session(s) for: {disconnected}."
    except Exception as ex:
        state["last_success"] = ""
        state["last_error"] = f"Session disconnect failed: {ex}"
    _redact_management_messages(state)
    return _render_connect_panel(request, request.app)


@router.post("/ui/evs/destroy", response_class=HTMLResponse)
async def evs_destroy_selected(
    request: Request,
    vs_name: str = Form(default=""),
    resource_kind: str = Form(default="v1"),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    selected_kind = str(resource_kind or state.get("selected_resource_kind") or "v1").strip().lower()
    selected_name = str(vs_name or state.get("selected_resource_name") or "").strip()
    if selected_name and selected_kind in {"v1", "v2"}:
        select_resource(state, kind=selected_kind, name=selected_name)
    if selected_kind == "v2":
        principal = _session_principal(request)
        if principal is None or principal.role != "admin":
            return HTMLResponse("Collection deletion requires an administrator.", status_code=403)
        if not selected_name:
            return HTMLResponse("Select a Collection to delete.", status_code=422)
        if Collection is None:
            return HTMLResponse("The collection service is unavailable.", status_code=503)
        try:
            try:
                collection = Collection(name=selected_name, auth_data=state.get("_sdk_auth_data"))
            except TypeError:
                collection = Collection(name=selected_name)
            result = collection.destroy()
            state["last_error"] = ""
            state["last_success"] = f"Deletion requested for Collection '{selected_name}'. Refresh to track status."
            clear_selected_resource(state)
            state["selected_vs_name"] = ""
            remove_resource(state, kind="v2", name=selected_name)
            _append_connect_step(
                state,
                "Collection.destroy()",
                "ok",
                f"Deletion request accepted for '{selected_name}': {_format_preview(result, max_chars=200)}",
            )
        except Exception as ex:
            safe_error = redact_sensitive_text(
                ex,
                secrets=((state.get("params") or {}).get("password"), (state.get("params") or {}).get("pat_token")),
            )
            state["last_success"] = ""
            state["last_error"] = destroy_failure_message(selected_name, safe_error)
        return _render_connect_panel(request, request.app)
    if selected_kind != "v1":
        return HTMLResponse("Invalid resource API generation.", status_code=422)
    return await handle_destroy_selected(
        request,
        state,
        selected_name,
        vector_store_cls=VectorStore,
        vs_manager=VSManager,
        execute_sql_fn=execute_sql,
        teradata_import_error=TERADATA_IMPORT_ERROR,
        render_connect_panel=lambda req: _render_connect_panel(req, request.app),
        append_connect_step=_append_connect_step,
        after_destroy=lambda name: remove_resource(state, kind="v1", name=name),
    )


@router.post("/ui/create/upload", response_class=HTMLResponse)
async def upload_and_prepare_create(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"

    def enqueue_create(command: dict):
        job = queue_workflow_job(request, kind=VECTOR_STORE_CREATE_JOB, payload=command)
        request.app.state.evs_state["last_error"] = ""
        request.app.state.evs_state["last_success"] = ""
        request.app.state.evs_state["last_notice"] = (
            f"Vector Store creation queued as job {job['id']}."
        )
        return render_job_progress(request, job)

    response = await handle_upload_and_prepare_create(
        request,
        request.app,
        request.app.state.templates,
        vector_store_cls=VectorStore,
        execute_sql_fn=execute_sql,
        save_document_uploads_fn=_save_document_uploads,
        collect_upload_files_fn=_collect_upload_files,
        resolve_path_hint_fn=_resolve_path_hint,
        now_ts=_now_ts,
        is_htmx=is_htmx,
        is_vectorstore_already_exists_error_fn=_is_vectorstore_already_exists_error,
        verify_vectorstore_exists_fn=_verify_vectorstore_exists,
        append_connect_step=_append_connect_step,
        enqueue_create_fn=enqueue_create,
    )
    _register_document_artifacts(request, list(request.app.state.document_uploads or []))
    _persist_active_session_state(request, request.app)
    return response


@router.post("/ui/chat", response_class=HTMLResponse)
async def chat_send(
    request: Request,
    message: str = Form(...),
    validation_target: str = Form(default="vectorstore.ask"),
    selected_vs_name: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    response = await handle_chat_send(
        request,
        request.app,
        request.app.state.templates,
        message=message,
        validation_target=validation_target,
        selected_vs_name=selected_vs_name,
        build_evs_reply=_build_evs_reply,
    )
    _persist_active_session_state(request, request.app)
    return response


@router.post("/ui/chat/reset", response_class=HTMLResponse)
async def chat_reset(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    response = await handle_chat_reset(request, request.app, request.app.state.templates)
    _persist_active_session_state(request, request.app)
    return response


@router.post("/ui/admin/bookrag-section-rules", response_class=HTMLResponse)
async def update_bookrag_section_rules_panel(request: Request):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)

    form_data = await request.form()
    chapter_pattern_count = max(0, int(str(form_data.get("chapter_pattern_count") or "0").strip() or "0"))
    chapter_patterns: list[dict[str, object]] = []
    for index in range(chapter_pattern_count):
        pattern = str(form_data.get(f"chapter_pattern_{index}") or "").strip()
        name = str(form_data.get(f"chapter_name_{index}") or "").strip()
        if not pattern and not name:
            continue
        chapter_patterns.append(
            {
                "name": name or f"rule_{index + 1}",
                "pattern": pattern,
                "level": max(1, int(str(form_data.get(f"chapter_level_{index}") or "1").strip() or "1")),
                "family": str(form_data.get(f"chapter_family_{index}") or "chapter").strip() or "chapter",
                "enabled": _admin_checkbox_checked(form_data, f"chapter_enabled_{index}"),
                "priority": int(str(form_data.get(f"chapter_priority_{index}") or ((index + 1) * 10)).strip() or ((index + 1) * 10)),
            }
        )

    submitted_rules = {
        "version": 1,
        "updated_at": "",
        "profiles": {
            "jp": {
                "chapter_patterns": chapter_patterns,
                "numeric_pattern": str(form_data.get("numeric_pattern") or "").strip(),
                "enum_heading_pattern": str(form_data.get("enum_heading_pattern") or "").strip(),
                "alpha_section_pattern": str(form_data.get("alpha_section_pattern") or "").strip(),
                "bracket_section_pattern": str(form_data.get("bracket_section_pattern") or "").strip(),
                "note_pattern": str(form_data.get("note_pattern") or "").strip(),
                "table_html_pattern": str(form_data.get("table_html_pattern") or "").strip(),
                "heading_tag_pattern": str(form_data.get("heading_tag_pattern") or "").strip(),
                "header_footer_types": _admin_csv_values(form_data, "header_footer_types"),
                "major_section_families": _admin_csv_values(form_data, "major_section_families"),
                "group_section_families": _admin_csv_values(form_data, "group_section_families"),
                "enum_section_families": _admin_csv_values(form_data, "enum_section_families"),
                "fullwidth_numeric_source": str(form_data.get("fullwidth_numeric_source") or "").strip(),
                "fullwidth_numeric_target": str(form_data.get("fullwidth_numeric_target") or "").strip(),
            }
        },
    }

    try:
        saved_rules = save_bookrag_section_rules(submitted_rules)
        status = {
            "kind": "ok",
            "title": "Rules Saved",
            "detail": f"Saved BookRAG section rules to {BOOKRAG_SECTION_RULES_PATH}.",
        }
        context = _build_bookrag_admin_context(saved_rules, status=status)
        context["evs"] = request.app.state.evs_state
    except Exception as ex:
        status = {
            "kind": "error",
            "title": "Save Failed",
            "detail": str(ex),
        }
        context = _build_bookrag_admin_context(submitted_rules, status=status)
        context["evs"] = request.app.state.evs_state

    return request.app.state.templates.TemplateResponse(
        request,
        "partials/bookrag_admin_panel.html",
        context,
    )



@router.get("/ui/admin/document-governance", response_class=HTMLResponse)
async def load_document_governance_admin(
    request: Request,
    vector_store_name: str = "",
    refresh: bool = False,
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    state = request.app.state.evs_state
    selected = vector_store_name.strip()
    if selected:
        state["bookrag_governance_vs_name"] = selected
    status = _refresh_document_relation_vector_store_options(request) if refresh else None
    if selected or refresh:
        _persist_active_session_state(request, request.app)
    context = _document_governance_admin_context(
        request.app,
        vector_store_name=selected,
        status=status,
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_governance_admin.html",
        context,
    )


@router.get("/ui/admin/document-metadata", response_class=HTMLResponse)
async def load_document_metadata_admin(
    request: Request,
    vector_store_name: str = "",
    refresh: bool = False,
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    if selected:
        request.app.state.evs_state["selected_vs_name"] = selected
    status = _refresh_document_relation_vector_store_options(request) if refresh else None
    if refresh:
        _persist_active_session_state(request, request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_metadata_admin.html",
        {"document_metadata_admin": _document_metadata_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.post("/ui/admin/document-metadata/autofill", response_class=HTMLResponse)
async def autofill_document_metadata_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        updated = backfill_document_metadata(
            vector_store_name=selected,
            schema_name=_document_relation_schema_name(request.app),
            execute_sql_fn=execute_sql,
            username=_current_user(request) or "metadata-rule",
        )
        status = {
            "kind": "ok",
            "title": "Metadata Auto-fill Complete",
            "detail": f"Updated {updated} document(s); manual dates were preserved.",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "Metadata Auto-fill Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_metadata_admin.html",
        {"document_metadata_admin": _document_metadata_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.post("/ui/admin/document-metadata/save", response_class=HTMLResponse)
async def save_document_metadata_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
    doc_id: str = Form(default=""),
    publication_date: str = Form(default=""),
    publication_date_precision: str = Form(default=""),
    document_series: str = Form(default=""),
    document_role: str = Form(default=""),
    logical_document_key: str = Form(default=""),
    revision_no: str = Form(default="1"),
    metadata_status: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        saved = save_document_metadata(
            vector_store_name=selected,
            schema_name=_document_relation_schema_name(request.app),
            doc_id=doc_id,
            values={
                "publication_date": publication_date,
                "publication_date_precision": publication_date_precision,
                "document_series": document_series,
                "document_role": document_role,
                "logical_document_key": logical_document_key,
                "revision_no": revision_no,
                "metadata_status": metadata_status,
            },
            execute_sql_fn=execute_sql,
            username=_current_user(request),
        )
        status = {
            "kind": "ok",
            "title": "Document Metadata Saved",
            "detail": f"Saved metadata for {saved.get('filename') or saved['doc_id']}.",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "Document Metadata Save Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_metadata_admin.html",
        {"document_metadata_admin": _document_metadata_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.get("/ui/admin/document-metadata/export")
async def export_document_metadata_admin(request: Request, vector_store_name: str = ""):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    rows = fetch_document_metadata(
        vector_store_name=selected,
        schema_name=_document_relation_schema_name(request.app),
        execute_sql_fn=execute_sql,
    )
    fieldnames = [
        "doc_id",
        "filename",
        "publication_date",
        "publication_date_source",
        "publication_date_precision",
        "document_series",
        "document_role",
        "logical_document_key",
        "revision_no",
        "metadata_status",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{selected}_bdoc_metadata.csv"'},
    )


@router.post("/ui/admin/document-metadata/import", response_class=HTMLResponse)
async def import_document_metadata_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
    metadata_csv: UploadFile = File(default=None),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        if metadata_csv is None or not metadata_csv.filename:
            raise ValueError("Select a document metadata CSV file.")
        raw = await metadata_csv.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Metadata CSV exceeds 2 MiB.")
        payload = raw.decode("utf-8-sig")
        imported = [dict(row) for row in csv.DictReader(io.StringIO(payload))]
        if not imported:
            raise ValueError("CSV must contain a header and at least one document row.")
        schema_name = _document_relation_schema_name(request.app)
        documents = fetch_document_metadata(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        normalized_rows = validate_document_metadata_import_rows(imported, documents)
        for row in normalized_rows:
            save_document_metadata(
                vector_store_name=selected,
                schema_name=schema_name,
                doc_id=row["doc_id"],
                values=row,
                execute_sql_fn=execute_sql,
                username=_current_user(request),
                ensure_view=False,
            )
        ensure_bookrag_retrieval_view(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        status = {
            "kind": "ok",
            "title": "Metadata CSV Imported",
            "detail": f"Validated and saved {len(imported)} document row(s).",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "Metadata CSV Import Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_metadata_admin.html",
        {"document_metadata_admin": _document_metadata_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.get("/ui/admin/document-relations", response_class=HTMLResponse)
async def load_document_relations_admin(
    request: Request,
    vector_store_name: str = "",
    refresh: bool = False,
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    if selected:
        request.app.state.evs_state["selected_vs_name"] = selected
    status = _refresh_document_relation_vector_store_options(request) if refresh else None
    if refresh:
        _persist_active_session_state(request, request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_relation_admin.html",
        {"document_relation_admin": _document_relation_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.post("/ui/admin/document-relations/initialize", response_class=HTMLResponse)
async def initialize_document_relations_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        schema_name = _document_relation_schema_name(request.app)
        documents = fetch_bookrag_documents(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        if not documents:
            raise RuntimeError("bdoc contains no documents; bdrel was not initialized.")
        created = ensure_document_relation_table(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        fetch_document_metadata(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        ensure_bookrag_retrieval_view(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        status = {
            "kind": "ok",
            "title": "bdrel Ready",
            "detail": "Created the document relationship table." if created else "The document relationship table already exists.",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "bdrel Initialization Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_relation_admin.html",
        {"document_relation_admin": _document_relation_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.post("/ui/admin/document-relations/save", response_class=HTMLResponse)
async def save_document_relations_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
    from_doc_id: str = Form(default=""),
    relation_type: str = Form(default=""),
    to_doc_id: str = Form(default=""),
    relation_description: str = Form(default=""),
    original_from_doc_id: str = Form(default=""),
    original_relation_type: str = Form(default=""),
    original_to_doc_id: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        schema_name = _document_relation_schema_name(request.app)
        documents = fetch_bookrag_documents(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        saved = save_document_relation(
            vector_store_name=selected,
            schema_name=schema_name,
            relation={
                "from_doc_id": from_doc_id,
                "relation_type": relation_type,
                "to_doc_id": to_doc_id,
                "relation_description": relation_description,
                "source_type": "human",
            },
            documents=documents,
            execute_sql_fn=execute_sql,
            username=_current_user(request),
            original_key=(
                original_from_doc_id.strip(),
                original_relation_type.strip(),
                original_to_doc_id.strip(),
            )
            if all((original_from_doc_id.strip(), original_relation_type.strip(), original_to_doc_id.strip()))
            else None,
        )
        fetch_document_metadata(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        ensure_bookrag_retrieval_view(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        status = {
            "kind": "ok",
            "title": "Relationship Saved",
            "detail": f"Saved {saved['relation_type']} between the selected documents.",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "Relationship Save Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_relation_admin.html",
        {"document_relation_admin": _document_relation_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.post("/ui/admin/document-relations/delete", response_class=HTMLResponse)
async def delete_document_relations_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
    from_doc_id: str = Form(default=""),
    relation_type: str = Form(default=""),
    to_doc_id: str = Form(default=""),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        delete_document_relation(
            vector_store_name=selected,
            schema_name=_document_relation_schema_name(request.app),
            from_doc_id=from_doc_id.strip(),
            relation_type=relation_type.strip(),
            to_doc_id=to_doc_id.strip(),
            execute_sql_fn=execute_sql,
        )
        fetch_document_metadata(
            vector_store_name=selected,
            schema_name=_document_relation_schema_name(request.app),
            execute_sql_fn=execute_sql,
        )
        ensure_bookrag_retrieval_view(
            vector_store_name=selected,
            schema_name=_document_relation_schema_name(request.app),
            execute_sql_fn=execute_sql,
        )
        status = {"kind": "ok", "title": "Relationship Deleted", "detail": "The relationship was deleted."}
    except Exception as ex:
        status = {"kind": "error", "title": "Relationship Delete Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_relation_admin.html",
        {"document_relation_admin": _document_relation_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.get("/ui/admin/document-relations/export")
async def export_document_relations_admin(request: Request, vector_store_name: str = ""):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    rows = fetch_document_relations(
        vector_store_name=selected,
        schema_name=_document_relation_schema_name(request.app),
        execute_sql_fn=execute_sql,
    )
    fieldnames = [
        "from_doc_id",
        "from_filename",
        "relation_type",
        "to_doc_id",
        "to_filename",
        "relation_description",
        "source_type",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{selected}_bdrel.csv"'},
    )


@router.post("/ui/admin/document-relations/import", response_class=HTMLResponse)
async def import_document_relations_admin(
    request: Request,
    vector_store_name: str = Form(default=""),
    relation_csv: UploadFile = File(default=None),
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    selected = vector_store_name.strip()
    try:
        if relation_csv is None or not relation_csv.filename:
            raise ValueError("Select a document relationship CSV file.")
        raw = await relation_csv.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Relationship CSV exceeds 2 MiB.")
        payload = raw.decode("utf-8-sig")
        imported = [dict(row) for row in csv.DictReader(io.StringIO(payload))]
        if not imported:
            raise ValueError("CSV must contain a header and at least one relationship row.")
        schema_name = _document_relation_schema_name(request.app)
        documents = fetch_bookrag_documents(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        filename_map: dict[str, str] = {}
        duplicate_filenames: set[str] = set()
        for document in documents:
            filename = str(document["filename"])
            if filename in filename_map:
                duplicate_filenames.add(filename)
            filename_map[filename] = str(document["doc_id"])
        for row in imported:
            if not str(row.get("from_doc_id") or "").strip():
                filename = str(row.get("from_filename") or "").strip()
                if filename in duplicate_filenames or filename not in filename_map:
                    raise ValueError(f"Source filename is missing or not unique: {filename!r}.")
                row["from_doc_id"] = filename_map[filename]
            if not str(row.get("to_doc_id") or "").strip():
                filename = str(row.get("to_filename") or "").strip()
                if filename in duplicate_filenames or filename not in filename_map:
                    raise ValueError(f"Target filename is missing or not unique: {filename!r}.")
                row["to_doc_id"] = filename_map[filename]
            row["source_type"] = "import"
        normalized_rows = validate_document_relations(imported, documents)
        for row in normalized_rows:
            save_document_relation(
                vector_store_name=selected,
                schema_name=schema_name,
                relation=row,
                documents=documents,
                execute_sql_fn=execute_sql,
                username=_current_user(request),
            )
        fetch_document_metadata(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        ensure_bookrag_retrieval_view(
            vector_store_name=selected,
            schema_name=schema_name,
            execute_sql_fn=execute_sql,
        )
        status = {
            "kind": "ok",
            "title": "Relationship CSV Imported",
            "detail": f"Validated and saved {len(normalized_rows)} relationship row(s).",
        }
    except Exception as ex:
        status = {"kind": "error", "title": "Relationship CSV Import Failed", "detail": str(ex)}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/document_relation_admin.html",
        {"document_relation_admin": _document_relation_admin_context(
            request.app,
            vector_store_name=selected,
            status=status,
        )},
    )


@router.get("/ui/admin/json-inspector", response_class=HTMLResponse)
async def inspect_unstructured_json_file(
    request: Request,
    json_file: str = "",
):
    if not _is_logged_in(request, request.app):
        return HTMLResponse("Unauthorized", status_code=401)
    _activate_session_state(request, request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/unstructured_json_inspector_response.html",
        {"json_inspector": build_unstructured_json_inspector_context(json_file.strip())},
    )
