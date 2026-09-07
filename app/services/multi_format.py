from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from app.core.security import redact_sensitive_text, sensitive_values
from app.services.multi_format_config import (
    BOOKRAG_COMPLETE_TABLE_CONTRACT,
    BOOKRAG_CSV_LOAD_WORKERS_DEFAULT,
    BOOKRAG_CSV_MANIFEST_FILENAME,
    BOOKRAG_CSV_MANIFEST_SCHEMA_VERSION,
    BOOKRAG_CSV_PREPARE_WORKERS_DEFAULT,
    BOOKRAG_ENTITY_TABLE_KEYS,
    BOOKRAG_GRAPH_TOGGLE_FIELD,
    BOOKRAG_LEGACY_GRAPH_TOGGLE_FIELDS,
    BOOKRAG_PARSE_MANIFEST_FILENAME,
    BOOKRAG_PARSE_MANIFEST_SCHEMA_VERSION,
    BOOKRAG_TABLE_TOGGLE_DEFAULTS,
    BOOKRAG_TABLE_TOGGLE_FIELDS,
    BOOKRAG_TABLE_TOGGLE_ORDER,
    BOOKRAG_TRANSFORM_VERSION,
    BOOKRAG_UNSTRUCTURED_WORKERS_DEFAULT,
    MULTI_FORMAT_CSV_LOAD_WORKERS_DEFAULT,
    MULTI_FORMAT_CSV_MANIFEST_FILENAME,
    MULTI_FORMAT_CSV_MANIFEST_SCHEMA_VERSION,
    MULTI_FORMAT_CSV_PREPARE_WORKERS_DEFAULT,
    MULTI_FORMAT_PARSE_MANIFEST_FILENAME,
    MULTI_FORMAT_PARSE_MANIFEST_SCHEMA_VERSION,
    MULTI_FORMAT_TRANSFORM_VERSION,
    MULTI_FORMAT_UNSTRUCTURED_WORKERS_DEFAULT,
    TERADATA_IDENTIFIER_MAX_LEN,
    UNSTRUCTURED_CHUNK_COLUMNS,
    first_defined as _first_defined,
    parse_csv_values as _parse_csv_values,
    strip_create_ingestor_params,
    strip_file_based_create_params,
    to_bool as _to_bool,
    to_int as _to_int,
)
from app.services.teradata_sql import (
    ExecuteSqlFn,
    _count_teradata_rows,
    _qualified_table_sql,
    _sanitize_teradata_text,
    _sql_typed_literal,
    _teradata_table_exists,
)
from app.services.unstructured_runtime import (
    BOOKRAG_CSV_STAGE_DIR_DEFAULT,
    BOOKRAG_PDF_IMAGE_EXTENSIONS,
    BOOKRAG_RAW_STAGE_DIR_DEFAULT,
    MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT,
    MULTI_FORMAT_RAW_STAGE_DIR_DEFAULT,
    UNSTRUCTURED_RAW_STAGE_DIR_DEFAULT,
    UNSTRUCTURED_DEBUG_DIR_DEFAULT,
    UNSTRUCTURED_FAST_UNSAFE_IMAGE_EXTENSIONS,
    UNSTRUCTURED_TERADATA_FLUSH_WAIT_INTERVAL_DEFAULT,
    UNSTRUCTURED_TERADATA_FLUSH_WAIT_SECONDS_DEFAULT,
    _env_flag,
    _load_unstructured_runtime_config,
    _load_unstructured_runtime_settings,
    _parse_langs,
    _resolve_bookrag_workflow_poll_config,
    _resolve_unstructured_request_timeout_ms,
    _resolve_multi_format_workflow_poll_config,
    _resolve_partition_strategy,
)

from app.services.bookrag_reconcile import reconcile_unstructured_elements  # noqa: F401 - legacy patch point
from app.services.bookrag_graph import build_bookrag_entities
from app.services.bookrag_integrity import validate_bookrag_dataset_relationships
from app.services.bookrag_document_relations import (
    derive_filename_document_relations,
    persist_document_relations,
)
from app.services.bookrag_document_metadata import derive_document_metadata
from app.services.bookrag_schema import (
    build_bookrag_table_targets,
    prepare_bookrag_block_table,
    prepare_bookrag_document_table,
    prepare_bookrag_document_relation_table,
    prepare_bookrag_entity_link_table,
    prepare_bookrag_entity_relation_table,
    prepare_bookrag_entity_table,
    prepare_bookrag_node_table,
    prepare_bookrag_raw_table,
    ensure_bookrag_retrieval_view,
)
from app.services.bookrag_tree import build_bookrag_nodes, elements_to_bookrag_blocks
from app.services.bookrag_storage import (
    build_bookrag_document_row,
    build_bookrag_raw_rows,
    load_bookrag_raw_stage_file,
    persist_bookrag_blocks,
    persist_bookrag_entities,
    persist_bookrag_entity_links,
    persist_bookrag_entity_relations,
    persist_bookrag_nodes,
    persist_bookrag_documents,
    persist_bookrag_raw_rows,
    load_prepared_bookrag_table_csv,
    load_prepared_unstructured_table_csv,
    prepare_unstructured_table_csv,
    prepare_bookrag_table_csv,
    validate_prepared_bookrag_table_csv,
    validate_prepared_unstructured_table_csv,
    write_bookrag_raw_stage_file,
)
from app.integrations.unstructured.gateway import (
    create_client as _create_unstructured_client,
    run_workflow_for_file as _run_unstructured_workflow_job_for_file,
    space_job_submissions as _enforce_unstructured_job_submission_spacing,
)
from app.services.unstructured_workflow_builder import (
    build_bookrag_reusable_workflow_definition as _workflow_builder_build_bookrag_reusable_workflow_definition,
    build_bookrag_workflow_partition_node as _workflow_builder_build_bookrag_workflow_partition_node,
    build_multi_format_workflow_definition as _workflow_builder_build_multi_format_workflow_definition,
)

ResolvePathFn = Callable[[str], str]
ProgressCallback = Callable[[int], None]


def _report_csv_document_progress(
    progress_callback: ProgressCallback | None,
    *,
    completed: int,
    total: int,
) -> None:
    if progress_callback is None or total <= 0:
        return
    progress_callback(10 + min(80, int(max(0, completed) * 80 / total)))


def _resolve_bookrag_unstructured_workers(file_count: int) -> int:
    configured = _to_int(
        os.getenv("BOOKRAG_UNSTRUCTURED_WORKERS", str(BOOKRAG_UNSTRUCTURED_WORKERS_DEFAULT)),
        default=BOOKRAG_UNSTRUCTURED_WORKERS_DEFAULT,
        minimum=1,
    )
    return max(1, min(configured, max(1, file_count)))


def _resolve_bookrag_csv_prepare_workers(file_count: int) -> int:
    configured = _to_int(
        os.getenv("BOOKRAG_CSV_PREPARE_WORKERS", str(BOOKRAG_CSV_PREPARE_WORKERS_DEFAULT)),
        default=BOOKRAG_CSV_PREPARE_WORKERS_DEFAULT,
        minimum=1,
    )
    return max(1, min(configured, max(1, file_count)))


def _resolve_bookrag_csv_load_workers(csv_count: int) -> int:
    configured = _to_int(
        os.getenv("BOOKRAG_CSV_LOAD_WORKERS", str(BOOKRAG_CSV_LOAD_WORKERS_DEFAULT)),
        default=BOOKRAG_CSV_LOAD_WORKERS_DEFAULT,
        minimum=1,
    )
    return max(1, min(configured, max(1, csv_count)))


def _resolve_multi_format_workers(env_name: str, default: int, task_count: int) -> int:
    configured = _to_int(os.getenv(env_name, str(default)), default=default, minimum=1)
    return max(1, min(configured, max(1, task_count)))


def _resolve_multi_format_unstructured_workers(file_count: int) -> int:
    return _resolve_multi_format_workers(
        "MULTI_FORMAT_UNSTRUCTURED_WORKERS", MULTI_FORMAT_UNSTRUCTURED_WORKERS_DEFAULT, file_count
    )


def _resolve_multi_format_csv_prepare_workers(file_count: int) -> int:
    return _resolve_multi_format_workers(
        "MULTI_FORMAT_CSV_PREPARE_WORKERS", MULTI_FORMAT_CSV_PREPARE_WORKERS_DEFAULT, file_count
    )


def _resolve_multi_format_csv_load_workers(csv_count: int) -> int:
    return _resolve_multi_format_workers(
        "MULTI_FORMAT_CSV_LOAD_WORKERS", MULTI_FORMAT_CSV_LOAD_WORKERS_DEFAULT, csv_count
    )


def _merge_bookrag_insert_stats(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "csv_files":
            files = target.setdefault(key, [])
            for path in value if isinstance(value, list) else []:
                if path not in files:
                    files.append(path)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = round(float(target.get(key, 0) or 0) + float(value), 6)
            if isinstance(value, int) and not isinstance(value, bool):
                target[key] = int(target[key])
        elif key == "native_csv_last_error":
            target[key] = value
        else:
            target.setdefault(key, value)


def _build_unstructured_table_ddl(
    qualified_table: str,
) -> str:
    column_lines = [f'  "{name}" {col_type}' for name, col_type in UNSTRUCTURED_CHUNK_COLUMNS]
    column_lines.append('  PRIMARY KEY ("id")')
    ddl_body = ",\n".join(column_lines)
    return f"""
CREATE SET TABLE {qualified_table} (
{ddl_body}
)
"""


def _resolve_bookrag_table_generation_flags(create_values: dict[str, str]) -> dict[str, bool]:
    # Core, Audit, and Graph are mandatory parts of every new BookRAG CSV contract.
    # Graph CSV files are still emitted with headers when NER produced no rows.
    return {
        "documents": True,
        "raw": True,
        "blocks": True,
        "nodes": True,
        "document_relations": True,
        "entities": True,
        "entity_links": True,
        "entity_relations": True,
    }


def _resolve_bookrag_embedding_step_flag(create_values: dict[str, str]) -> bool:
    return True


def normalize_document_files_for_create(
    create_payload: dict,
    resolve_path_hint: ResolvePathFn,
) -> tuple[dict, list[str]]:
    exec_payload = dict(create_payload)
    warnings: list[str] = []
    doc_files = exec_payload.get("document_files")
    if not doc_files:
        return exec_payload, warnings

    if isinstance(doc_files, str):
        raw_items = [doc_files]
    elif isinstance(doc_files, (list, tuple, set)):
        raw_items = [str(item).strip() for item in doc_files if str(item).strip()]
    else:
        raw_items = [str(doc_files).strip()]

    resolved_items: list[str] = []
    for raw in raw_items:
        resolved = resolve_path_hint(raw)
        resolved_items.append(resolved)
        if not Path(resolved).exists():
            warnings.append(f"Document file not found on disk: {raw}")

    # Always pass document_files as a list.
    # Some VectorStore runtimes iterate the value and will treat a single
    # string path like "C:\\..." as characters ("C", ":", "\\", ...).
    exec_payload["document_files"] = resolved_items

    return exec_payload, warnings


def _format_chunk_row_id(row_sequence: int | None) -> str:
    if row_sequence is None or row_sequence <= 0:
        return uuid.uuid4().hex
    return f"{row_sequence:012d}"


def _resolve_bookrag_image_partition_options(create_values: dict[str, str]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    runtime = _load_unstructured_runtime_settings()
    warnings: list[str] = []

    raw_extract_types = str(
        _first_defined(
            create_values.get("multi_format_bookrag_extract_image_block_types", ""),
            runtime.get("bookrag_extract_image_block_types"),
            os.getenv("BOOKRAG_EXTRACT_IMAGE_BLOCK_TYPES", ""),
        )
        or ""
    ).strip()
    extract_mode = raw_extract_types.lower()
    if extract_mode == "auto":
        extract_image_block_types = ["Image", "Table"]
        if _to_bool(create_values.get("multi_format_bookrag_enable_generative_ocr", "false")):
            extract_image_block_types.extend(
                ["Text", "NarrativeText", "Title", "ListItem", "UncategorizedText"]
            )
    else:
        extract_image_block_types = _parse_csv_values(raw_extract_types)

    raw_infer_table_structure = str(
        _first_defined(
            create_values.get("multi_format_bookrag_infer_table_structure", ""),
            runtime.get("bookrag_infer_table_structure"),
            os.getenv("BOOKRAG_INFER_TABLE_STRUCTURE", ""),
        )
        or ""
    ).strip()
    infer_table_structure = _to_bool(raw_infer_table_structure, default=False)

    coordinates = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_coordinates", "true"),
            runtime.get("bookrag_coordinates"),
            os.getenv("BOOKRAG_COORDINATES", "true"),
        ),
        default=True,
    )
    unique_element_ids = _to_bool(
        _first_defined(
            runtime.get("bookrag_unique_element_ids"),
            os.getenv("BOOKRAG_UNIQUE_ELEMENT_IDS", "true"),
        ),
        default=True,
    )
    hi_res_model_name = str(
        _first_defined(
            create_values.get("multi_format_bookrag_hi_res_model_name", ""),
            runtime.get("bookrag_hi_res_model_name"),
            os.getenv("BOOKRAG_HI_RES_MODEL_NAME", ""),
        )
        or ""
    ).strip()
    vlm_provider = str(
        _first_defined(
            create_values.get("multi_format_bookrag_vlm_provider", ""),
            runtime.get("bookrag_vlm_provider"),
            os.getenv("BOOKRAG_VLM_PROVIDER", ""),
        )
        or ""
    ).strip()
    vlm_model = str(
        _first_defined(
            create_values.get("multi_format_bookrag_vlm_model", ""),
            runtime.get("bookrag_vlm_model"),
            os.getenv("BOOKRAG_VLM_MODEL", ""),
        )
        or ""
    ).strip()
    vlm_provider_api_key = str(
        _first_defined(
            create_values.get("multi_format_bookrag_vlm_provider_api_key", ""),
            runtime.get("bookrag_vlm_provider_api_key"),
            os.getenv("BOOKRAG_VLM_PROVIDER_API_KEY", ""),
        )
        or ""
    ).strip()

    extra: dict[str, Any] = {
        "coordinates": coordinates,
        "unique_element_ids": unique_element_ids,
        "infer_table_structure": infer_table_structure,
        "vlm_provider": vlm_provider or None,
        "vlm_model": vlm_model or None,
        "vlm_provider_api_key": vlm_provider_api_key or None,
    }
    if extract_image_block_types:
        extra["extract_image_block_types"] = extract_image_block_types
    else:
        warnings.append("bookrag extract_image_block_types is off; image/table block extraction is disabled by default for faster raw ingestion.")
    if hi_res_model_name:
        extra["hi_res_model_name"] = hi_res_model_name

    summary = {
        "coordinates": coordinates,
        "extract_image_block_mode": raw_extract_types or None,
        "extract_image_block_types": extract_image_block_types,
        "unique_element_ids": unique_element_ids,
        "hi_res_model_name": hi_res_model_name or None,
        "infer_table_structure": infer_table_structure,
        "vlm_provider": vlm_provider or None,
        "vlm_model": vlm_model or None,
        "vlm_provider_api_key_configured": bool(vlm_provider_api_key),
    }
    return extra, warnings, summary


def _sanitize_teradata_identifier(raw: str, fallback: str, allow_empty: bool = False) -> str:
    candidate = re.sub(r"[^0-9A-Za-z_]", "_", str(raw or "").strip())
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if not candidate:
        return "" if allow_empty else fallback
    if candidate[0].isdigit():
        candidate = f"t_{candidate}"
    if len(candidate) <= TERADATA_IDENTIFIER_MAX_LEN:
        return candidate
    digest = hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:6]
    keep = max(1, TERADATA_IDENTIFIER_MAX_LEN - len(digest) - 1)
    return f"{candidate[:keep]}_{digest}"


def _split_object_name_hint(raw_object_name: str) -> tuple[str, str]:
    clean = str(raw_object_name or "").strip()
    if not clean:
        return "", ""
    if "." not in clean:
        return "", clean
    lhs, rhs = clean.rsplit(".", 1)
    return lhs.strip(), rhs.strip()


def _base_vector_store_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    lowered = name.lower()
    for suffix in ("_unstructured", "_unstractured"):
        if lowered.endswith(suffix):
            trimmed = name[: -len(suffix)].strip().strip("_")
            return trimmed or name
    return name


def _resolve_multi_format_table_target(
    exec_payload: dict,
    create_values: dict[str, str],
    vector_store_name: str,
) -> tuple[str, str | None, str, list[str]]:
    warnings: list[str] = []
    raw_object_names = exec_payload.get("object_names")
    object_hint = ""
    if isinstance(raw_object_names, list):
        if raw_object_names:
            object_hint = str(raw_object_names[0]).strip()
        if len(raw_object_names) > 1:
            warnings.append("multi format uses only the first object_names entry.")
    elif raw_object_names is not None:
        object_hint = str(raw_object_names).strip()

    schema_hint, _table_hint_from_object = _split_object_name_hint(object_hint)
    target_database_raw = str(exec_payload.get("target_database") or create_values.get("target_database", "")).strip()
    if target_database_raw:
        schema_hint = target_database_raw
    vs_base_name = _base_vector_store_name(vector_store_name) or vector_store_name
    table_hint = f"{vs_base_name}_unstructured"

    table_name = _sanitize_teradata_identifier(table_hint, fallback="unstructured")
    schema_name = _sanitize_teradata_identifier(schema_hint, fallback="", allow_empty=True) or None

    if table_name != table_hint:
        warnings.append(f"multi format table normalized to '{table_name}'.")
    if schema_hint and schema_name and schema_name != schema_hint:
        warnings.append(f"multi format target_database normalized to '{schema_name}'.")

    qualified = f"{schema_name}.{table_name}" if schema_name else table_name
    return table_name, schema_name, qualified, warnings


def _teradata_column_exists(
    schema_name: str | None,
    table_name: str,
    column_name: str,
    execute_sql_fn: ExecuteSqlFn | None,
) -> bool:
    if execute_sql_fn is None:
        raise RuntimeError("teradataml.execute_sql is unavailable.")
    qualified_table = _qualified_table_sql(schema_name, table_name)
    quoted_column = column_name.replace('"', '""')
    try:
        execute_sql_fn(f'SELECT TOP 1 "{quoted_column}" FROM {qualified_table}')
        return True
    except Exception as ex:
        msg = str(ex).lower()
        if "3810" in msg or ("column" in msg and ("does not exist" in msg or "not found" in msg)):
            return False
        raise


def _drop_teradata_column_if_exists(
    schema_name: str | None,
    table_name: str,
    column_name: str,
    execute_sql_fn: ExecuteSqlFn | None,
) -> bool:
    if execute_sql_fn is None:
        raise RuntimeError("teradataml.execute_sql is unavailable.")
    if not _teradata_column_exists(
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
        execute_sql_fn=execute_sql_fn,
    ):
        return False
    qualified_table = _qualified_table_sql(schema_name, table_name)
    quoted_column = column_name.replace('"', '""')
    execute_sql_fn(f'ALTER TABLE {qualified_table} DROP "{quoted_column}"')
    return True


def _ensure_unstructured_unicode_columns(
    schema_name: str | None,
    table_name: str,
    execute_sql_fn: ExecuteSqlFn | None,
) -> list[str]:
    if execute_sql_fn is None:
        raise RuntimeError("teradataml.execute_sql is unavailable.")
    qualified_table = _qualified_table_sql(schema_name, table_name)
    warnings: list[str] = []
    for column_name, column_type in UNSTRUCTURED_CHUNK_COLUMNS:
        if "CHARACTER SET UNICODE" not in column_type.upper():
            continue
        if not _teradata_column_exists(
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            execute_sql_fn=execute_sql_fn,
        ):
            continue
        quoted_column = column_name.replace('"', '""')
        try:
            execute_sql_fn(f'ALTER TABLE {qualified_table} MODIFY "{quoted_column}" {column_type}')
        except Exception as ex:
            raise RuntimeError(
                f'Existing Multi-Format target table {qualified_table} has a non-UNICODE-compatible column "{column_name}". '
                "Drop/recreate that table or use a new vector store name, then rerun Multi-Format preprocessing."
            ) from ex
        warnings.append(f'Ensured Multi-Format column "{column_name}" uses CHARACTER SET UNICODE.')
    return warnings


def _ensure_unstructured_teradata_table(
    schema_name: str | None,
    table_name: str,
    execute_sql_fn: ExecuteSqlFn | None,
    clear_rows: bool = True,
) -> list[str]:
    if execute_sql_fn is None:
        raise RuntimeError("teradataml.execute_sql is unavailable.")

    warnings: list[str] = []
    qualified_table = _qualified_table_sql(schema_name, table_name)
    table_exists = _teradata_table_exists(qualified_table, execute_sql_fn=execute_sql_fn)
    table_created = False

    if table_exists and clear_rows:
        try:
            execute_sql_fn(f"DROP TABLE {qualified_table}")
            warnings.append(f"Recreated Multi-Format target table {qualified_table} to apply the latest UNICODE schema.")
            table_exists = False
        except Exception as ex:
            raise RuntimeError(
                f"Failed to recreate existing Multi-Format target table {qualified_table}. "
                "Drop that table manually or use a new vector store name, then rerun Multi-Format preprocessing."
            ) from ex

    if not table_exists:
        create_sql = _build_unstructured_table_ddl(
            qualified_table=qualified_table,
        )
        execute_sql_fn(create_sql)
        table_created = True
    else:
        # Remove legacy column from previous schema versions.
        try:
            _drop_teradata_column_if_exists(
                schema_name=schema_name,
                table_name=table_name,
                column_name="languages",
                execute_sql_fn=execute_sql_fn,
            )
        except Exception as ex:
            warnings.append(f'Failed to drop legacy column "languages": {ex}')
        warnings.extend(
            _ensure_unstructured_unicode_columns(
                schema_name=schema_name,
                table_name=table_name,
                execute_sql_fn=execute_sql_fn,
            )
        )

    if clear_rows and not table_created:
        try:
            execute_sql_fn(f"DELETE FROM {qualified_table}")
        except Exception as ex:
            warnings.append(f"Failed to clear destination table before run: {ex}")
    return warnings


def _wait_for_table_rows(
    schema_name: str | None,
    table_name: str,
    execute_sql_fn: ExecuteSqlFn | None,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> int:
    started = time.time()
    last_count = 0
    while True:
        current = _count_teradata_rows(
            schema_name=schema_name,
            table_name=table_name,
            execute_sql_fn=execute_sql_fn,
        )
        if current is not None:
            last_count = current
            if current > 0:
                return current
        if time.time() - started >= timeout_seconds:
            return last_count
        time.sleep(max(1, poll_interval_seconds))


_strip_file_based_create_params = strip_file_based_create_params


def _now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_stem(src: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]", "_", src.stem.strip())
    return stem or "document"


def _document_csv_stage_dir(
    csv_stage_dir: Path,
    *,
    source_index: int,
    doc_id: str,
    filename: str,
) -> Path:
    """Build a fixed-length per-document directory safe for Windows CSV paths."""
    identity = f"{source_index}\0{doc_id}\0{filename}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return csv_stage_dir / f"doc_{max(0, source_index):04d}_{digest}"


def _prepare_unstructured_debug_dir(vector_store_name: str) -> Path | None:
    if not _env_flag("UNSTRUCTURED_WRITE_DEBUG_JSON", True):
        return None
    vs_name = _sanitize_teradata_identifier(vector_store_name, fallback="bookrag")
    run_id = time.strftime("%Y%m%d_%H%M%S")
    debug_dir = UNSTRUCTURED_DEBUG_DIR_DEFAULT / f"{vs_name}_{run_id}"
    debug_dir.mkdir(parents=True, exist_ok=True)
    return debug_dir


def _prepare_bookrag_raw_stage_dir(vector_store_name: str) -> Path:
    vs_name = _sanitize_teradata_identifier(vector_store_name, fallback="bookrag")
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_stage_dir = BOOKRAG_RAW_STAGE_DIR_DEFAULT / f"{vs_name}_{run_id}"
    raw_stage_dir.mkdir(parents=True, exist_ok=True)
    return raw_stage_dir


def _prepare_bookrag_csv_stage_dir(vector_store_name: str) -> Path:
    vs_name = _sanitize_teradata_identifier(vector_store_name, fallback="bookrag")
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    csv_stage_dir = BOOKRAG_CSV_STAGE_DIR_DEFAULT / f"{vs_name}_{run_id}"
    csv_stage_dir.mkdir(parents=True, exist_ok=True)
    return csv_stage_dir


def _prepare_multi_format_raw_stage_dir(run_label: str) -> Path:
    safe_label = _sanitize_teradata_identifier(run_label, fallback="multi_format")
    run_id = f"{safe_label}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_stage_dir = UNSTRUCTURED_RAW_STAGE_DIR_DEFAULT / run_id
    raw_stage_dir.mkdir(parents=True, exist_ok=False)
    return raw_stage_dir


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _bookrag_config_snapshot(create_values: dict[str, str]) -> dict[str, str]:
    blocked_fragments = ("api_key", "password", "secret", "token")
    return {
        str(key): str(value)
        for key, value in create_values.items()
        if str(key).startswith("multi_format_bookrag_")
        and not any(fragment in str(key).lower() for fragment in blocked_fragments)
    }


def _multi_format_config_snapshot(create_values: dict[str, str]) -> dict[str, str]:
    blocked_fragments = ("api_key", "password", "secret", "token")
    return {
        str(key): str(value)
        for key, value in create_values.items()
        if str(key).startswith("multi_format_")
        and not str(key).startswith("multi_format_bookrag_")
        and not any(fragment in str(key).lower() for fragment in blocked_fragments)
    }


SHARED_PARSE_ARTIFACT_TYPES = {"bookrag_parse_run", "multi_format_parse_run"}


def _shared_parse_roots() -> list[Path]:
    """Return the common raw root plus the legacy Multi-Format root, without duplicates."""
    roots: list[Path] = []
    seen: set[str] = set()
    for root in (
        UNSTRUCTURED_RAW_STAGE_DIR_DEFAULT,
        BOOKRAG_RAW_STAGE_DIR_DEFAULT,
        MULTI_FORMAT_RAW_STAGE_DIR_DEFAULT,
    ):
        resolved = root.resolve()
        normalized = str(resolved).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        roots.append(resolved)
    return roots


def _validate_shared_parse_manifest(
    manifest_path: Path,
    manifest: Any,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid shared JSON parsing manifest object: {manifest_path}")
    artifact_type = str(manifest.get("artifact_type") or "")
    if artifact_type not in SHARED_PARSE_ARTIFACT_TYPES:
        raise RuntimeError(f"Unsupported shared JSON parsing manifest type: {manifest_path}")
    expected_schema_version = (
        BOOKRAG_PARSE_MANIFEST_SCHEMA_VERSION
        if artifact_type == "bookrag_parse_run"
        else MULTI_FORMAT_PARSE_MANIFEST_SCHEMA_VERSION
    )
    if _to_int(manifest.get("schema_version"), default=0) != expected_schema_version:
        raise RuntimeError(f"Unsupported shared JSON parsing manifest schema version: {manifest_path}")
    parse_run_id = str(manifest.get("parse_run_id") or "").strip()
    if not parse_run_id or parse_run_id != manifest_path.parent.name:
        raise RuntimeError(f"Shared JSON parsing manifest run ID does not match its directory: {manifest_path}")
    if expected_run_id is not None and parse_run_id != expected_run_id:
        raise RuntimeError(f"Shared JSON parsing manifest does not match the selected run: {manifest_path}")
    return manifest


def _resolve_shared_parse_manifest(parse_run_id: str) -> tuple[Path, dict[str, Any]]:
    normalized_run_id = str(parse_run_id or "").strip()
    if not normalized_run_id or Path(normalized_run_id).name != normalized_run_id:
        raise RuntimeError("Select a valid shared JSON parsing run.")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for root in _shared_parse_roots():
        run_dir = (root / normalized_run_id).resolve()
        if run_dir.parent != root:
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as ex:
            raise RuntimeError(f"Invalid shared JSON parsing manifest: {manifest_path}: {ex}") from ex
        matches.append(
            (
                manifest_path,
                _validate_shared_parse_manifest(
                    manifest_path, manifest, expected_run_id=normalized_run_id
                ),
            )
        )
    if not matches:
        raise RuntimeError(f"Shared JSON parsing manifest was not found: {normalized_run_id}")
    if len(matches) > 1:
        paths = ", ".join(str(path) for path, _ in matches)
        raise RuntimeError(f"Shared JSON parsing run ID is ambiguous across stage roots: {paths}")
    return matches[0]


def list_shared_parse_runs(*, include_incomplete: bool = False) -> list[dict[str, Any]]:
    """List raw JSON runs reusable by either CSV transformation."""
    runs: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for root in _shared_parse_roots():
        if not root.exists():
            continue
        for manifest_path in root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = _validate_shared_parse_manifest(manifest_path, manifest)
            except Exception:
                continue
            status = str(manifest.get("status") or "")
            if not include_incomplete and status != "ready":
                continue
            parse_run_id = str(manifest["parse_run_id"])
            if parse_run_id in seen_run_ids:
                continue
            seen_run_ids.add(parse_run_id)
            runs.append(
                {
                    "parse_run_id": parse_run_id,
                    "created_at": str(manifest.get("created_at") or ""),
                    "vector_store_name": str(manifest.get("vector_store_name") or ""),
                    "file_count": _to_int(manifest.get("file_count"), default=0),
                    "status": status,
                    "artifact_type": str(manifest.get("artifact_type") or ""),
                    "manifest_path": str(manifest_path),
                }
            )
    runs.sort(key=lambda item: (item["created_at"], item["parse_run_id"]), reverse=True)
    return runs


def _resolve_multi_format_manifest(
    run_id: str,
    *,
    root: Path,
    artifact_type: str,
    schema_version: int,
    run_id_key: str,
    invalid_message: str,
) -> tuple[Path, dict[str, Any]]:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id or Path(normalized_run_id).name != normalized_run_id:
        raise RuntimeError(invalid_message)
    resolved_root = root.resolve()
    run_dir = (resolved_root / normalized_run_id).resolve()
    if run_dir.parent != resolved_root:
        raise RuntimeError(f"{invalid_message} Run directory is outside its stage root.")
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Multi-Format run manifest was not found: {normalized_run_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as ex:
        raise RuntimeError(f"Invalid Multi-Format run manifest: {manifest_path}: {ex}") from ex
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid Multi-Format run manifest object: {manifest_path}")
    if manifest.get("artifact_type") != artifact_type:
        raise RuntimeError(f"Unsupported Multi-Format run manifest type: {manifest_path}")
    if _to_int(manifest.get("schema_version"), default=0) != schema_version:
        raise RuntimeError(f"Unsupported Multi-Format run manifest schema version: {manifest_path}")
    if str(manifest.get(run_id_key) or "") != normalized_run_id:
        raise RuntimeError(f"Multi-Format manifest run ID does not match its directory: {manifest_path}")
    return manifest_path, manifest


def _resolve_multi_format_parse_manifest(parse_run_id: str) -> tuple[Path, dict[str, Any]]:
    return _resolve_shared_parse_manifest(parse_run_id)


def _resolve_multi_format_csv_manifest(csv_run_id: str) -> tuple[Path, dict[str, Any]]:
    return _resolve_multi_format_manifest(
        csv_run_id,
        root=MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT,
        artifact_type="multi_format_csv_run",
        schema_version=MULTI_FORMAT_CSV_MANIFEST_SCHEMA_VERSION,
        run_id_key="csv_run_id",
        invalid_message="Select a valid Multi-Format CSV generation run.",
    )


def _list_multi_format_manifests(
    *,
    root: Path,
    artifact_type: str,
    run_id_key: str,
    include_incomplete: bool,
) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict) or manifest.get("artifact_type") != artifact_type:
            continue
        status = str(manifest.get("status") or "")
        if not include_incomplete and status != "ready":
            continue
        runs.append(
            {
                run_id_key: str(manifest.get(run_id_key) or manifest_path.parent.name),
                "created_at": str(manifest.get("created_at") or ""),
                "vector_store_name": str(manifest.get("vector_store_name") or ""),
                "target_database": str(manifest.get("target_database") or ""),
                "file_count": _to_int(manifest.get("file_count"), default=0),
                "csv_file_count": _to_int(manifest.get("csv_file_count"), default=0),
                "status": status,
                "load_status": str(manifest.get("load_status") or "not_started"),
                "vector_store_status": str(manifest.get("vector_store_status") or "not_started"),
                "manifest_path": str(manifest_path),
            }
        )
    runs.sort(key=lambda item: (item["created_at"], item[run_id_key]), reverse=True)
    return runs


def list_multi_format_parse_runs(*, include_incomplete: bool = False) -> list[dict[str, Any]]:
    return list_shared_parse_runs(include_incomplete=include_incomplete)


def list_multi_format_csv_runs(*, include_incomplete: bool = False) -> list[dict[str, Any]]:
    return _list_multi_format_manifests(
        root=MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT,
        artifact_type="multi_format_csv_run",
        run_id_key="csv_run_id",
        include_incomplete=include_incomplete,
    )


def _resolve_bookrag_parse_manifest(parse_run_id: str) -> tuple[Path, dict[str, Any]]:
    return _resolve_shared_parse_manifest(parse_run_id)


def list_bookrag_parse_runs(*, include_incomplete: bool = False) -> list[dict[str, Any]]:
    """List raw JSON runs reusable by BookRAG or standard Multi-Format."""
    return list_shared_parse_runs(include_incomplete=include_incomplete)


def _resolve_bookrag_csv_manifest(csv_run_id: str) -> tuple[Path, dict[str, Any]]:
    normalized_run_id = str(csv_run_id or "").strip()
    if not normalized_run_id or Path(normalized_run_id).name != normalized_run_id:
        raise RuntimeError("Select a valid CSV generation run.")
    csv_root = BOOKRAG_CSV_STAGE_DIR_DEFAULT.resolve()
    run_dir = (csv_root / normalized_run_id).resolve()
    if run_dir.parent != csv_root:
        raise RuntimeError("CSV generation run is outside the CSV stage directory.")
    manifest_path = run_dir / BOOKRAG_CSV_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RuntimeError(f"CSV generation manifest was not found: {normalized_run_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as ex:
        raise RuntimeError(f"Invalid CSV generation manifest: {manifest_path}: {ex}") from ex
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid CSV generation manifest object: {manifest_path}")
    if manifest.get("artifact_type") != "bookrag_csv_run":
        raise RuntimeError(f"Unsupported CSV generation manifest type: {manifest_path}")
    if _to_int(manifest.get("schema_version"), default=0) != BOOKRAG_CSV_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported CSV generation manifest schema version: {manifest_path}")
    if str(manifest.get("csv_run_id") or "") != normalized_run_id:
        raise RuntimeError(f"CSV generation manifest run ID does not match its directory: {manifest_path}")
    return manifest_path, manifest


def list_bookrag_csv_runs(*, include_incomplete: bool = False) -> list[dict[str, Any]]:
    """List locally stored CSV generation manifests available for database loading."""
    root = BOOKRAG_CSV_STAGE_DIR_DEFAULT
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for manifest_path in root.glob(f"*/{BOOKRAG_CSV_MANIFEST_FILENAME}"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("complete_table_contract") != BOOKRAG_COMPLETE_TABLE_CONTRACT:
            continue
        status = str(manifest.get("status") or "")
        if not include_incomplete and status != "ready":
            continue
        runs.append(
            {
                "csv_run_id": str(manifest.get("csv_run_id") or manifest_path.parent.name),
                "created_at": str(manifest.get("created_at") or ""),
                "vector_store_name": str(manifest.get("vector_store_name") or ""),
                "target_database": str(manifest.get("target_database") or ""),
                "file_count": _to_int(manifest.get("file_count"), default=0),
                "csv_file_count": _to_int(manifest.get("csv_file_count"), default=0),
                "status": status,
                "load_status": str(manifest.get("load_status") or "not_started"),
                "vector_store_status": str(manifest.get("vector_store_status") or "not_started"),
                "manifest_path": str(manifest_path),
            }
        )
    runs.sort(key=lambda item: (item["created_at"], item["csv_run_id"]), reverse=True)
    return runs


def _ensure_csv_generation_target_available(
    runs: list[dict[str, Any]],
    *,
    vector_store_name: str,
    target_database: str,
) -> None:
    requested_name = str(vector_store_name or "").strip()
    requested_database = _sanitize_teradata_identifier(
        str(target_database or "").strip(), fallback="", allow_empty=True
    )
    if not requested_name or not requested_database:
        return
    for run in runs:
        if str(run.get("status") or "") != "ready" or str(run.get("load_status") or "") != "ready":
            continue
        if str(run.get("vector_store_name") or "").strip().casefold() != requested_name.casefold():
            continue
        if str(run.get("target_database") or "").strip().casefold() != requested_database.casefold():
            continue
        raise RuntimeError(
            f"Target Vector Store '{requested_name}' already has loaded tables in database "
            f"'{requested_database}'. Use a different Target Vector Store Name; CSV generation was not started."
        )


def ensure_bookrag_csv_generation_target_available(*, vector_store_name: str, target_database: str) -> None:
    _ensure_csv_generation_target_available(
        [*list_bookrag_csv_runs(), *list_multi_format_csv_runs()],
        vector_store_name=vector_store_name,
        target_database=target_database,
    )


def ensure_multi_format_csv_generation_target_available(*, vector_store_name: str, target_database: str) -> None:
    _ensure_csv_generation_target_available(
        [*list_bookrag_csv_runs(), *list_multi_format_csv_runs()],
        vector_store_name=vector_store_name,
        target_database=target_database,
    )


def _find_failed_bookrag_csv_runs_for_target(
    *,
    vector_store_name: str,
    target_database: str,
) -> list[str]:
    """Identify failed local runs whose partial tables are superseded by a new run."""
    root = BOOKRAG_CSV_STAGE_DIR_DEFAULT
    if not root.exists():
        return []
    run_ids: list[str] = []
    for manifest_path in root.glob(f"*/{BOOKRAG_CSV_MANIFEST_FILENAME}"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        if str(manifest.get("load_status") or "") != "failed":
            continue
        if str(manifest.get("vector_store_status") or "not_started") in {"creating", "ready"}:
            continue
        if str(manifest.get("vector_store_name") or "").strip() != vector_store_name:
            continue
        if str(manifest.get("target_database") or "").strip().lower() != target_database.lower():
            continue
        run_id = str(manifest.get("csv_run_id") or manifest_path.parent.name).strip()
        if run_id:
            run_ids.append(run_id)
    return sorted(set(run_ids))


def _write_unstructured_debug_file(
    debug_dir: Path | None,
    src: Path,
    raw_elements: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    request_parameters: dict[str, Any],
    extra_payload: dict[str, Any] | None = None,
) -> str | None:
    if debug_dir is None:
        return None
    payload = {
        "source_file": str(src),
        "saved_at": _now_ts(),
        "raw_element_count": len(raw_elements),
        "row_count": len(rows),
        "request_parameters": _json_safe_value(request_parameters),
        "raw_elements": raw_elements,
        "table_rows": rows,
    }
    if extra_payload:
        payload.update(_json_safe_value(extra_payload))
    out_path = debug_dir / f"{_safe_stem(src)}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out_path)


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    return str(value)

def _workflow_debug_payload(
    request_parameters: dict[str, Any],
    *,
    processing_profile: str,
    workflow_id: str = "",
    workflow_name: str = "",
    job_id: str = "",
    workflow_kind: str = "",
) -> dict[str, Any]:
    workflow_nodes = list(request_parameters.get("workflow_nodes") or [])
    return {
        "workflow_kind": workflow_kind or "workflow",
        "processing_profile": processing_profile,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name or str(request_parameters.get("workflow_name") or "").strip(),
        "job_id": job_id or str(request_parameters.get("job_id") or "").strip(),
        "workflow_node_count": len(workflow_nodes),
        "workflow_nodes": _json_safe_value(workflow_nodes),
    }


def _bookrag_partition_options_for_file(
    src: Path,
    default_strategy: str,
    default_languages: list[str],
    include_orig_elements: bool,
) -> tuple[str, list[str], bool]:
    suffix = src.suffix.lower()
    if suffix in (BOOKRAG_PDF_IMAGE_EXTENSIONS - {".pdf"}):
        return "hi_res", ["jpn"], include_orig_elements
    if suffix == ".pdf" and _looks_like_scanned_pdf(src):
        return "hi_res", ["jpn"], include_orig_elements
    return default_strategy or "auto", list(default_languages or []), include_orig_elements


def _looks_like_scanned_pdf(src: Path) -> bool:
    if src.suffix.lower() != ".pdf":
        return False
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(src))
        sampled_pages = reader.pages[: min(3, len(reader.pages))]
        total_text_len = 0
        image_only_pages = 0
        for page in sampled_pages:
            text = (page.extract_text() or "").strip()
            total_text_len += len(text)
            resources = page.get("/Resources")
            xobjects = resources.get("/XObject") if resources else None
            has_image = False
            if xobjects:
                for key in xobjects.keys():
                    try:
                        obj = xobjects[key].get_object()
                    except Exception:
                        continue
                    if str(obj.get("/Subtype")) == "/Image":
                        has_image = True
                        break
            if has_image and len(text) < 24:
                image_only_pages += 1
        if not sampled_pages:
            return False
        return image_only_pages == len(sampled_pages) or total_text_len < 24
    except Exception:
        return False


def _multi_format_partition_options_for_file(
    src: Path,
    default_strategy: str,
    default_languages: list[str],
    include_orig_elements: bool,
) -> tuple[str, list[str], bool, list[str], bool]:
    warnings: list[str] = []
    resolved_strategy = default_strategy
    resolved_languages = list(default_languages)
    suffix = src.suffix.lower()
    scan_ocr_fallback_applied = False

    is_scan_like = suffix in (BOOKRAG_PDF_IMAGE_EXTENSIONS - {".pdf"})
    if suffix == ".pdf":
        is_scan_like = _looks_like_scanned_pdf(src)

    if is_scan_like and resolved_strategy == "fast":
        resolved_strategy = "auto"
        warnings.append(
            f"multi format strategy 'fast' is not recommended for scan-style document {src.name}; fallback to 'auto'."
        )
        scan_ocr_fallback_applied = True

    if resolved_strategy == "fast" and suffix in UNSTRUCTURED_FAST_UNSAFE_IMAGE_EXTENSIONS:
        resolved_strategy = "auto"
        warnings.append(
            f"multi format strategy 'fast' is not supported for image file {src.name}; fallback to 'auto'."
        )
        scan_ocr_fallback_applied = True

    if is_scan_like and resolved_strategy == "hi_res" and not resolved_languages:
        resolved_languages = ["jpn"]
        warnings.append(
            f"multi format auto-assigned OCR language 'jpn' for hi_res scan-style document {src.name}."
        )

    return resolved_strategy, resolved_languages, include_orig_elements, warnings, scan_ocr_fallback_applied


def _as_text(value: Any, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = _sanitize_teradata_text(text)
    text = text.strip()
    if not text:
        return None
    if max_len is not None and len(text) > max_len:
        return text[:max_len]
    return text


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _element_to_chunk_row(
    element: dict[str, Any],
    src: Path,
    content_type: str,
    *,
    row_sequence: int | None = None,
) -> dict[str, Any] | None:
    metadata = element.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    text = _as_text(element.get("text"), max_len=32000)
    if not text:
        return None

    row_id = _format_chunk_row_id(row_sequence)
    element_id = _as_text(element.get("element_id") or element.get("id"), max_len=64)
    element_type = _as_text(element.get("type"), max_len=50)
    filetype = _as_text(metadata.get("filetype"), max_len=50) or _as_text(content_type, max_len=50)
    text_as_html = _as_text(metadata.get("text_as_html"), max_len=32000)
    table_to_html = None
    if element_type in {"Table", "TableChunk"}:
        table_to_html = _as_text(metadata.get("table_to_html"), max_len=32000) or text_as_html

    row = {
        "text": text,
        "type": element_type,
        "filename": _as_text(metadata.get("filename"), max_len=255) or _as_text(src.name, max_len=255),
        "element_id": element_id,
        "id": row_id,
        "table_id": _as_text(metadata.get("table_id"), max_len=128),
        "page_number": _as_int(metadata.get("page_number")),
        "chunk_index": _as_int(metadata.get("chunk_index")),
        "is_continuation": bool(metadata.get("is_continuation")) if metadata.get("is_continuation") is not None else None,
        "num_carried_over_header_rows": _as_int(metadata.get("num_carried_over_header_rows")),
        "partitioner_type": _as_text(metadata.get("partitioner_type"), max_len=100),
        "image_description": _as_text(metadata.get("image_description"), max_len=32000),
        "table_description": _as_text(metadata.get("table_description"), max_len=32000),
        "generative_ocr": _as_text(metadata.get("generative_ocr"), max_len=32000),
        "text_as_html": text_as_html,
        "table_to_html": table_to_html,
        "filetype": filetype,
        "date_processed": _now_ts(),
    }
    return row


def _build_bookrag_workflow_partition_node(
    *,
    src: Path,
    partition_strategy: str,
    languages: list[str],
    image_partition_parameters: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    return _workflow_builder_build_bookrag_workflow_partition_node(
        src=src,
        partition_strategy=partition_strategy,
        languages=languages,
        image_partition_parameters=image_partition_parameters,
    )


def _build_bookrag_reusable_workflow_definition(
    *,
    create_values: dict[str, str],
    partition_strategy: str,
    languages: list[str],
    image_partition_parameters: dict[str, Any] | None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any], list[str], str]:
    return _workflow_builder_build_bookrag_reusable_workflow_definition(
        create_values=create_values,
        partition_strategy=partition_strategy,
        languages=languages,
        image_partition_parameters=image_partition_parameters,
        runtime=_load_unstructured_runtime_settings(),
    )


def _build_multi_format_workflow_definition(
    *,
    create_values: dict[str, str],
    src: Path,
    partition_strategy: str,
    languages: list[str],
    chunk_size: int,
    chunk_overlap: int,
    include_orig_elements: bool,
    overlap_all: bool,
) -> tuple[dict[str, Any], list[str], str]:
    return _workflow_builder_build_multi_format_workflow_definition(
        create_values=create_values,
        src=src,
        partition_strategy=partition_strategy,
        languages=languages,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        include_orig_elements=include_orig_elements,
        overlap_all=overlap_all,
        runtime=_load_unstructured_runtime_settings(),
    )


def _insert_chunk_rows(
    schema_name: str | None,
    table_name: str,
    rows: list[dict[str, Any]],
    execute_sql_fn: ExecuteSqlFn | None,
) -> int:
    if execute_sql_fn is None:
        raise RuntimeError("teradataml.execute_sql is unavailable.")
    if not rows:
        return 0
    qualified_table = _qualified_table_sql(schema_name, table_name)
    columns = [name for name, _ in UNSTRUCTURED_CHUNK_COLUMNS]
    quoted_cols = ", ".join(f'"{name}"' for name in columns)
    inserted = 0
    for row in rows:
        values_sql = ", ".join(_sql_typed_literal(row.get(name), column_type) for name, column_type in UNSTRUCTURED_CHUNK_COLUMNS)
        execute_sql_fn(f"INSERT INTO {qualified_table} ({quoted_cols}) VALUES ({values_sql})")
        inserted += 1
    return inserted


def _build_bookrag_rows_from_raw_elements(
    *,
    doc_id: str,
    filename: str,
    source_file: str,
    filetype: str,
    filesize_bytes: int,
    vector_store_name: str,
    workflow_id: str,
    workflow_name: str,
    job_id: str,
    processing_profile: str,
    language_hint: str | None,
    created_at: str,
    raw_elements: list[dict[str, Any]],
    graph_enabled: bool,
) -> dict[str, Any]:
    """Apply the shared BookRAG JSON-to-table-row algorithm without writing CSV or a database."""
    raw_rows = build_bookrag_raw_rows(doc_id=doc_id, elements=raw_elements)
    blocks = elements_to_bookrag_blocks(
        doc_id=doc_id,
        src=Path(filename),
        content_type=filetype,
        raw_elements=raw_elements,
    )
    page_count = max((_as_int(block.get("page_number")) or 0 for block in blocks), default=0)
    document_row = build_bookrag_document_row(
        doc_id=doc_id,
        vector_store_name=vector_store_name,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        job_id=job_id,
        processing_profile=processing_profile,
        filename=filename,
        source_file=source_file,
        filetype=filetype,
        filesize_bytes=filesize_bytes,
        page_count=page_count,
        language_hint=language_hint,
        created_at=created_at,
        metadata=derive_document_metadata(
            filename=filename,
            content_values=(
                element.get("text")
                for element in raw_elements
                if isinstance(element, dict)
            ),
        ),
    )
    nodes = build_bookrag_nodes(document_row, blocks)
    entities: list[dict[str, Any]] = []
    entity_links: list[dict[str, Any]] = []
    entity_relations: list[dict[str, Any]] = []
    if graph_enabled:
        entities, entity_links, entity_relations = build_bookrag_entities(document_row, raw_elements, nodes)
    validate_bookrag_dataset_relationships(
        document_row=document_row,
        raw_rows=raw_rows,
        blocks=blocks,
        nodes=nodes,
        entities=entities,
        entity_links=entity_links,
        entity_relations=entity_relations,
        graph_enabled=graph_enabled,
    )
    return {
        "document_row": document_row,
        "raw_rows": raw_rows,
        "blocks": blocks,
        "nodes": nodes,
        "entities": entities,
        "entity_links": entity_links,
        "entity_relations": entity_relations,
    }


def run_multi_format_document_parsing(
    *,
    create_values: dict[str, str],
    vector_store_name: str,
    uploaded_documents: list[dict[str, Any]],
    connection_params: dict[str, Any] | None,
    resolve_path_hint: Callable[[str], str],
) -> dict[str, Any]:
    """Run the existing standard Multi-Format workflow and persist only reusable JSON."""
    if not uploaded_documents:
        raise RuntimeError("Upload at least one document before parsing documents.")

    partition_strategy = _resolve_partition_strategy(create_values.get("multi_format_strategy", "auto"))
    if partition_strategy == "ocr_only":
        raise RuntimeError(
            "Multi-Format does not expose 'ocr_only' as a supported workflow route. Use hi_res or vlm instead."
        )
    ocr_languages = _parse_langs(create_values.get("multi_format_ocr_languages", ""))
    chunk_size = _to_int(
        create_values.get("multi_format_chunk_size", "600"), default=600, minimum=100, maximum=8000
    )
    chunk_overlap = _to_int(
        create_values.get("multi_format_chunk_overlap", "80"), default=80, minimum=0, maximum=2000
    )
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size // 5)

    api_key, api_url = _load_unstructured_runtime_config(connection_params)
    request_timeout_ms = _resolve_unstructured_request_timeout_ms()
    timeout_seconds, poll_interval_seconds = _resolve_multi_format_workflow_poll_config()
    raw_stage_dir = _prepare_multi_format_raw_stage_dir(vector_store_name or "multi_format_parse")

    source_items: list[tuple[int, Path, str]] = []
    for index, item in enumerate(uploaded_documents):
        saved_path = str(item.get("saved_path") or "").strip()
        doc_id = str(item.get("doc_id") or "").strip()[:64]
        if not saved_path:
            raise RuntimeError(f"Uploaded document has no saved path: index={index}")
        src = Path(resolve_path_hint(saved_path))
        if not src.exists() or not src.is_file():
            raise RuntimeError(f"Uploaded document is missing: {saved_path}")
        source_items.append((index, src, doc_id or uuid.uuid4().hex))

    workers = _resolve_multi_format_unstructured_workers(len(source_items))
    runtime_settings = _load_unstructured_runtime_settings()
    error_secrets = [api_key, *sensitive_values(create_values), *sensitive_values(connection_params), *sensitive_values(runtime_settings)]
    submission_lock = Lock()
    last_job_submitted_at: float | None = None

    def _parse_one(index: int, src: Path, doc_id: str) -> dict[str, Any]:
        nonlocal last_job_submitted_at
        started_at = time.perf_counter()
        (
            file_partition_strategy,
            file_ocr_languages,
            include_orig_elements,
            file_warnings,
            scan_ocr_fallback_applied,
        ) = _multi_format_partition_options_for_file(
            src,
            default_strategy=partition_strategy,
            default_languages=ocr_languages,
            include_orig_elements=False,
        )
        request_parameters, workflow_warnings, processing_profile = (
            _workflow_builder_build_multi_format_workflow_definition(
                create_values=create_values,
                src=src,
                partition_strategy=file_partition_strategy,
                languages=file_ocr_languages,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                include_orig_elements=include_orig_elements,
                overlap_all=True,
                runtime=runtime_settings,
            )
        )
        with submission_lock:
            last_job_submitted_at = _enforce_unstructured_job_submission_spacing(last_job_submitted_at)
        client = _create_unstructured_client(api_key=api_key, api_url=api_url, timeout_ms=request_timeout_ms)
        raw_payload, _, _, job_id, workflow_id, workflow_name = _run_unstructured_workflow_job_for_file(
            client,
            request_parameters=request_parameters,
            src=src,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            api_key=api_key,
            api_url=api_url,
        )
        raw_stage_file = write_bookrag_raw_stage_file(raw_stage_dir, src.name, doc_id, raw_payload)
        elements = load_bookrag_raw_stage_file(raw_stage_file)
        return {
            "source_index": index,
            "doc_id": doc_id,
            "filename": src.name,
            "source_file": str(src),
            "filetype": mimetypes.guess_type(src.name)[0] or src.suffix.lower().lstrip("."),
            "filesize_bytes": src.stat().st_size,
            "job_id": job_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name or str(request_parameters.get("workflow_name") or ""),
            "processing_profile": processing_profile,
            "partition_strategy": file_partition_strategy,
            "ocr_languages": file_ocr_languages,
            "scan_ocr_fallback_applied": scan_ocr_fallback_applied,
            "warnings": file_warnings + workflow_warnings,
            "raw_json_path": str(raw_stage_file),
            "element_count": len(elements),
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
            "status": "success",
            "error": "",
        }

    started_at = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multi-format-document-parse") as executor:
        future_map = {
            executor.submit(_parse_one, index, src, doc_id): (index, src, doc_id)
            for index, src, doc_id in source_items
        }
        for future in as_completed(future_map):
            index, src, doc_id = future_map[future]
            try:
                results.append(future.result())
            except Exception as ex:
                results.append(
                    {
                        "source_index": index,
                        "doc_id": doc_id,
                        "filename": src.name,
                        "source_file": str(src),
                        "filetype": mimetypes.guess_type(src.name)[0] or src.suffix.lower().lstrip("."),
                        "filesize_bytes": src.stat().st_size,
                        "job_id": "",
                        "workflow_id": "",
                        "workflow_name": "",
                        "processing_profile": "",
                        "partition_strategy": partition_strategy,
                        "ocr_languages": ocr_languages,
                        "scan_ocr_fallback_applied": False,
                        "warnings": [],
                        "raw_json_path": "",
                        "element_count": 0,
                        "elapsed_seconds": 0.0,
                        "status": "failed",
                        "error": _sanitize_teradata_text(redact_sensitive_text(ex, secrets=error_secrets))[:2000],
                    }
                )
    results.sort(key=lambda item: int(item["source_index"]))
    success_count = sum(1 for item in results if item["status"] == "success")
    failure_count = len(results) - success_count
    parse_run_id = raw_stage_dir.name
    documents: list[dict[str, Any]] = []
    for item in results:
        raw_json_path = Path(str(item.get("raw_json_path") or ""))
        raw_json_file = raw_json_path.name if raw_json_path.is_file() else ""
        documents.append(
            {
                key: item.get(key)
                for key in (
                    "source_index", "doc_id", "filename", "source_file", "filetype", "filesize_bytes",
                    "job_id", "workflow_id", "workflow_name", "processing_profile", "partition_strategy",
                    "ocr_languages", "scan_ocr_fallback_applied", "warnings", "element_count", "status", "error",
                )
            }
            | {
                "raw_json_file": raw_json_file,
                "raw_json_sha256": _file_sha256(raw_json_path) if raw_json_file else "",
            }
        )
    manifest_path = raw_stage_dir / MULTI_FORMAT_PARSE_MANIFEST_FILENAME
    manifest = {
        "schema_version": MULTI_FORMAT_PARSE_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "multi_format_parse_run",
        "parse_run_id": parse_run_id,
        "status": "ready" if failure_count == 0 else "failed",
        "created_at": _now_ts(),
        "vector_store_name": vector_store_name,
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "partition_strategy": partition_strategy,
        "ocr_languages": ocr_languages,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "create_values": _multi_format_config_snapshot(create_values),
        "documents": documents,
    }
    _write_json_atomic(manifest_path, manifest)
    return {
        "status": "ok" if failure_count == 0 else ("partial" if success_count else "error"),
        "parse_run_id": parse_run_id,
        "manifest_path": str(manifest_path),
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "workers": workers,
        "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
        "raw_stage_dir": str(raw_stage_dir),
        "partition_strategy": partition_strategy,
        "ocr_languages": ocr_languages,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "warnings": [warning for item in results for warning in (item.get("warnings") or [])],
        "files": results,
        "csv_files_created": 0,
        "database_writes": 0,
    }


def run_multi_format_json_to_csv(
    *,
    parse_run_id: str,
    vector_store_name: str,
    target_database: str,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Map stored JSON through the existing chunk-row mapping into unstructured CSV files."""
    parse_manifest_path, parse_manifest = _resolve_multi_format_parse_manifest(parse_run_id)
    if str(parse_manifest.get("status") or "") != "ready":
        raise RuntimeError("CSV generation requires a Multi-Format parsing run in ready status.")
    documents = parse_manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("Multi-Format parsing manifest contains no reusable JSON documents.")
    if any(not isinstance(item, dict) or item.get("status") != "success" for item in documents):
        raise RuntimeError("CSV generation requires every JSON document to be successful.")

    effective_vector_store_name = str(vector_store_name or "").strip()
    if not effective_vector_store_name:
        raise RuntimeError("Target Vector Store Name is required before generating CSV files.")
    effective_target_database = _sanitize_teradata_identifier(
        str(target_database or "").strip(), fallback="", allow_empty=True
    )
    if not effective_target_database:
        raise RuntimeError("Target Database is required before generating CSV files.")
    ensure_multi_format_csv_generation_target_available(
        vector_store_name=effective_vector_store_name,
        target_database=effective_target_database,
    )
    table_name, _, qualified_table, target_warnings = _resolve_multi_format_table_target(
        {"target_database": effective_target_database},
        {"target_database": effective_target_database},
        effective_vector_store_name,
    )
    qualified_table = f"{effective_target_database}.{table_name}"

    csv_run_id = (
        f"{_sanitize_teradata_identifier(parse_run_id, fallback='multi_format')}_csv_"
        f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    csv_stage_dir = MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT / csv_run_id
    csv_stage_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = csv_stage_dir / MULTI_FORMAT_CSV_MANIFEST_FILENAME
    parse_run_dir = parse_manifest_path.parent.resolve()
    running_manifest = {
        "schema_version": MULTI_FORMAT_CSV_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "multi_format_csv_run",
        "csv_run_id": csv_run_id,
        "source_parse_run_id": parse_run_id,
        "status": "running",
        "created_at": _now_ts(),
        "vector_store_name": effective_vector_store_name,
        "target_database": effective_target_database,
        "table_name": table_name,
        "qualified_table": qualified_table,
        "transform_version": MULTI_FORMAT_TRANSFORM_VERSION,
        "load_status": "not_started",
        "vector_store_status": "not_started",
        "documents": [],
    }
    _write_json_atomic(manifest_path, running_manifest)

    sequence_offsets: dict[int, int] = {}
    next_offset = 0
    for document in sorted(documents, key=lambda item: int(item.get("source_index") or 0)):
        source_index = int(document.get("source_index") or 0)
        sequence_offsets[source_index] = next_offset
        next_offset += max(0, int(document.get("element_count") or 0))

    def _transform_one(item: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        source_index = int(item.get("source_index") or 0)
        filename = str(item.get("filename") or "").strip()
        doc_id = str(item.get("doc_id") or "").strip()
        raw_json_file = str(item.get("raw_json_file") or "").strip()
        if not filename or not doc_id or not raw_json_file or Path(raw_json_file).name != raw_json_file:
            raise RuntimeError(f"Invalid Multi-Format document metadata at source_index={source_index}.")
        raw_json_path = (parse_run_dir / raw_json_file).resolve()
        if raw_json_path.parent != parse_run_dir or not raw_json_path.is_file():
            raise RuntimeError(f"Raw JSON file was not found for {filename}: {raw_json_file}")
        expected_sha256 = str(item.get("raw_json_sha256") or "").strip()
        if not expected_sha256 or _file_sha256(raw_json_path) != expected_sha256:
            raise RuntimeError(f"Raw JSON checksum mismatch for {filename}.")
        raw_elements = load_bookrag_raw_stage_file(raw_json_path)
        content_type = str(item.get("filetype") or "").strip() or (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        rows: list[dict[str, Any]] = []
        base_sequence = sequence_offsets[source_index]
        for ordinal, element in enumerate(raw_elements, start=1):
            row = _element_to_chunk_row(
                element,
                src=Path(filename),
                content_type=content_type,
                row_sequence=base_sequence + ordinal,
            )
            if row:
                rows.append(row)
        file_stage_dir = _document_csv_stage_dir(
            csv_stage_dir,
            source_index=source_index,
            doc_id=doc_id,
            filename=filename,
        )
        csv_path = prepare_unstructured_table_csv(
            table_name=table_name,
            rows=rows,
            columns=UNSTRUCTURED_CHUNK_COLUMNS,
            csv_stage_dir=file_stage_dir,
        )
        csv_file = str(Path(csv_path).relative_to(csv_stage_dir))
        return {
            "source_index": source_index,
            "doc_id": doc_id,
            "filename": filename,
            "raw_json_file": raw_json_file,
            "status": "success",
            "row_count": len(rows),
            "csv_file": csv_file,
            "csv_sha256": _file_sha256(Path(csv_path)),
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started), 6),
            "error": "",
        }

    started_at = time.perf_counter()
    workers = _resolve_multi_format_csv_prepare_workers(len(documents))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multi-format-json-to-csv") as executor:
        future_map = {executor.submit(_transform_one, item): item for item in documents}
        for future in as_completed(future_map):
            item = future_map[future]
            try:
                results.append(future.result())
            except Exception as ex:
                results.append(
                    {
                        "source_index": int(item.get("source_index") or 0),
                        "doc_id": str(item.get("doc_id") or ""),
                        "filename": str(item.get("filename") or ""),
                        "raw_json_file": str(item.get("raw_json_file") or ""),
                        "status": "failed",
                        "row_count": 0,
                        "csv_file": "",
                        "csv_sha256": "",
                        "elapsed_seconds": 0.0,
                        "error": _sanitize_teradata_text(str(ex))[:2000],
                    }
                )
            _report_csv_document_progress(
                progress_callback,
                completed=len(results),
                total=len(documents),
            )
    results.sort(key=lambda item: int(item["source_index"]))
    success_count = sum(1 for item in results if item["status"] == "success")
    failure_count = len(results) - success_count
    csv_file_count = success_count
    total_rows = sum(int(item.get("row_count") or 0) for item in results)
    final_status = "ready" if failure_count == 0 and total_rows > 0 else "failed"
    final_manifest = {
        **running_manifest,
        "status": final_status,
        "completed_at": _now_ts(),
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "csv_file_count": csv_file_count,
        "row_count": total_rows,
        "warnings": target_warnings,
        "run_error": "" if total_rows > 0 else "Generated CSV files contain no loadable unstructured rows.",
        "documents": results,
    }
    _write_json_atomic(manifest_path, final_manifest)
    return {
        "status": final_status,
        "created_at": running_manifest["created_at"],
        "parse_run_id": parse_run_id,
        "csv_run_id": csv_run_id,
        "manifest_path": str(manifest_path),
        "csv_stage_dir": str(csv_stage_dir),
        "vector_store_name": effective_vector_store_name,
        "target_database": effective_target_database,
        "table_name": table_name,
        "qualified_table": qualified_table,
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "csv_files_created": csv_file_count,
        "csv_file_count": csv_file_count,
        "row_count": total_rows,
        "run_error": final_manifest["run_error"],
        "workers": workers,
        "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
        "transform_version": MULTI_FORMAT_TRANSFORM_VERSION,
        "database_writes": 0,
        "warnings": target_warnings,
        "files": results,
    }


def _validate_load_connection(
    manifest: dict[str, Any], connection_profile_id: int | None, connection_target_fingerprint: str | None,
) -> None:
    if connection_profile_id is None:
        return
    if not connection_target_fingerprint:
        raise RuntimeError("The selected database connection target fingerprint is unavailable.")
    stored_profile = manifest.get("connection_profile_id")
    stored_target = str(manifest.get("connection_target_fingerprint") or "")
    if stored_profile is not None:
        if int(stored_profile) != int(connection_profile_id):
            raise RuntimeError(
                "This CSV run is bound to a different database connection. Select its original connection "
                "or generate a new CSV run for the selected connection. No target tables were changed."
            )
    if stored_target and stored_target != connection_target_fingerprint:
        raise RuntimeError(
            "This database connection now points to a different target than the saved CSV load. "
            "Generate a new CSV run for the current target. No target tables were changed."
        )
    if (stored_profile is None or not stored_target) and str(manifest.get("load_status") or "not_started") != "not_started":
        raise RuntimeError(
            "This legacy CSV load has no verified database connection target. Inspect its target tables "
            "and generate a new CSV run for the selected connection before loading or creating a Vector Store. "
            "The saved load result cannot verify this connection."
        )


def run_multi_format_csv_load(
    *,
    csv_run_id: str,
    execute_sql_fn: ExecuteSqlFn | None,
    connection_profile_id: int | None = None,
    connection_target_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Load a ready standard Multi-Format CSV run into one unstructured table."""
    if execute_sql_fn is None:
        raise RuntimeError("Teradata SQL execution is unavailable.")
    manifest_path, manifest = _resolve_multi_format_csv_manifest(csv_run_id)
    if str(manifest.get("status") or "") != "ready":
        raise RuntimeError("Database loading requires a Multi-Format CSV run in ready status.")
    _validate_load_connection(manifest, connection_profile_id, connection_target_fingerprint)
    if str(manifest.get("load_status") or "not_started") == "ready":
        summary = manifest.get("load_summary")
        if not isinstance(summary, dict):
            raise RuntimeError("Multi-Format CSV manifest has no completed load summary.")
        return {**summary, "already_loaded": True}
    if str(manifest.get("load_status") or "") == "loading":
        raise RuntimeError(
            "This Multi-Format CSV run is already marked as loading. If its operation was interrupted, "
            "inspect the target table and confirm no loader is active before explicitly starting a new CSV load run. "
            "Automatic retry did not clear or overwrite any target table."
        )

    target_database = _sanitize_teradata_identifier(
        str(manifest.get("target_database") or "").strip(), fallback="", allow_empty=True
    )
    table_name = _sanitize_teradata_identifier(
        str(manifest.get("table_name") or "").strip(), fallback="", allow_empty=True
    )
    vector_store_name = str(manifest.get("vector_store_name") or "").strip()
    if not target_database or not table_name or not vector_store_name:
        raise RuntimeError("Multi-Format CSV manifest is missing its target table.")
    expected_table_name, _, _, _ = _resolve_multi_format_table_target(
        {"target_database": target_database},
        {"target_database": target_database},
        vector_store_name,
    )
    if table_name != expected_table_name:
        raise RuntimeError("Multi-Format CSV manifest table does not match its Vector Store name.")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("Multi-Format CSV manifest contains no document outputs.")

    csv_run_dir = manifest_path.parent.resolve()
    load_tasks: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    expected_rows = 0
    for document in documents:
        if not isinstance(document, dict) or document.get("status") != "success":
            raise RuntimeError("CSV loading requires every Multi-Format document output to be successful.")
        relative_csv = str(document.get("csv_file") or "").strip()
        csv_path = (csv_run_dir / relative_csv).resolve()
        if csv_run_dir not in csv_path.parents or not csv_path.is_file():
            raise RuntimeError(f"Multi-Format CSV file is outside its run or missing: {relative_csv}")
        if str(csv_path) in seen_files:
            raise RuntimeError(f"Multi-Format CSV manifest contains a duplicate file: {relative_csv}")
        seen_files.add(str(csv_path))
        expected_sha256 = str(document.get("csv_sha256") or "").strip()
        if not expected_sha256 or _file_sha256(csv_path) != expected_sha256:
            raise RuntimeError(f"Multi-Format CSV checksum mismatch: {relative_csv}")
        validate_prepared_unstructured_table_csv(
            csv_path=str(csv_path), columns=UNSTRUCTURED_CHUNK_COLUMNS
        )
        row_count = _to_int(document.get("row_count"), default=0, minimum=0)
        expected_rows += row_count
        if row_count > 0:
            load_tasks.append({"csv_path": str(csv_path), "row_count": row_count})
    if expected_rows <= 0 or not load_tasks:
        raise RuntimeError("Multi-Format CSV run contains no loadable unstructured rows.")

    manifest["load_status"] = "loading"
    if connection_profile_id is not None:
        manifest["connection_profile_id"] = int(connection_profile_id)
        manifest["connection_target_fingerprint"] = connection_target_fingerprint
    manifest["load_started_at"] = _now_ts()
    manifest["load_error"] = ""
    _write_json_atomic(manifest_path, manifest)
    started_at = time.perf_counter()
    try:
        warnings = _ensure_unstructured_teradata_table(
            schema_name=target_database,
            table_name=table_name,
            execute_sql_fn=execute_sql_fn,
            clear_rows=True,
        )
        workers = _resolve_multi_format_csv_load_workers(len(load_tasks))

        def _load_one(task: dict[str, Any]) -> dict[str, Any]:
            inserted = load_prepared_unstructured_table_csv(
                schema_name=target_database,
                table_name=table_name,
                csv_path=task["csv_path"],
                row_count=task["row_count"],
                columns=UNSTRUCTURED_CHUNK_COLUMNS,
            )
            return {**task, "inserted_rows": inserted}

        load_results: list[dict[str, Any]] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multi-format-csv-load") as executor:
            future_map = {executor.submit(_load_one, task): task for task in load_tasks}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    load_results.append(future.result())
                except Exception as ex:
                    failures.append(f"csv={task['csv_path']}: {ex}")
        if failures:
            raise RuntimeError("Parallel Multi-Format CSV loading failed: " + " | ".join(failures))
        persisted_rows = _count_teradata_rows(
            schema_name=target_database,
            table_name=table_name,
            execute_sql_fn=execute_sql_fn,
        )
        if persisted_rows is None:
            raise RuntimeError("Could not verify the loaded Multi-Format row count.")
        if persisted_rows != expected_rows:
            raise RuntimeError(
                f"Loaded Multi-Format row count mismatch: expected={expected_rows}, actual={persisted_rows}"
            )
        summary = {
            "status": "ready",
            "csv_run_id": csv_run_id,
            "connection_profile_id": connection_profile_id,
            "connection_target_fingerprint": connection_target_fingerprint,
            "vector_store_name": vector_store_name,
            "target_database": target_database,
            "table_name": table_name,
            "qualified_table": f"{target_database}.{table_name}",
            "task_count": len(load_tasks),
            "csv_file_count": len(seen_files),
            "workers": workers,
            "inserted_rows": sum(int(item["inserted_rows"]) for item in load_results),
            "expected_row_count": expected_rows,
            "persisted_row_count": persisted_rows,
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
            "warnings": warnings,
            "already_loaded": False,
        }
        manifest["load_status"] = "ready"
        manifest["load_completed_at"] = _now_ts()
        manifest["load_summary"] = summary
        _write_json_atomic(manifest_path, manifest)
        return summary
    except Exception as ex:
        manifest["load_status"] = "failed"
        manifest["load_completed_at"] = _now_ts()
        manifest["load_error"] = _sanitize_teradata_text(str(ex))[:4000]
        _write_json_atomic(manifest_path, manifest)
        raise


def get_ready_multi_format_csv_load_summary(
    *, csv_run_id: str, connection_profile_id: int | None = None,
    connection_target_fingerprint: str | None = None,
) -> dict[str, Any]:
    _, manifest = _resolve_multi_format_csv_manifest(csv_run_id)
    if str(manifest.get("status") or "") != "ready":
        raise RuntimeError("Vector Store creation requires a ready Multi-Format CSV run.")
    if str(manifest.get("load_status") or "not_started") != "ready":
        raise RuntimeError("Load the Multi-Format CSV run before creating the Vector Store.")
    _validate_load_connection(manifest, connection_profile_id, connection_target_fingerprint)
    summary = manifest.get("load_summary")
    if not isinstance(summary, dict):
        raise RuntimeError("Multi-Format CSV manifest contains no completed load summary.")
    return {**summary, "already_loaded": True}


def update_multi_format_csv_vector_store_status(
    *,
    csv_run_id: str,
    status: str,
    error: str = "",
    create_payload: dict[str, Any] | None = None,
) -> None:
    if status not in {"creating", "ready", "failed"}:
        raise RuntimeError(f"Unsupported Vector Store manifest status: {status}")
    manifest_path, manifest = _resolve_multi_format_csv_manifest(csv_run_id)
    manifest["vector_store_status"] = status
    manifest["vector_store_updated_at"] = _now_ts()
    manifest["vector_store_error"] = _sanitize_teradata_text(error)[:4000]
    if create_payload is not None:
        manifest["vector_store_create_payload"] = _json_safe_value(create_payload)
    _write_json_atomic(manifest_path, manifest)


def run_bookrag_document_parsing(
    *,
    create_values: dict[str, str],
    vector_store_name: str,
    uploaded_documents: list[dict[str, Any]],
    connection_params: dict[str, Any] | None,
    resolve_path_hint: Callable[[str], str],
) -> dict[str, Any]:
    """Run only the concurrent Unstructured-to-JSON phase for uploaded documents."""
    if not uploaded_documents:
        raise RuntimeError("Upload at least one document before parsing documents.")

    partition_strategy = _resolve_partition_strategy(
        create_values.get("multi_format_bookrag_strategy", "auto")
    )
    ocr_languages = _parse_langs(create_values.get("multi_format_bookrag_ocr_languages", ""))
    image_parameters, image_warnings, image_summary = _resolve_bookrag_image_partition_options(create_values)
    workflow_name, workflow_nodes, request_parameters, workflow_warnings, processing_profile = (
        _build_bookrag_reusable_workflow_definition(
            create_values=create_values,
            partition_strategy=partition_strategy,
            languages=ocr_languages,
            image_partition_parameters=image_parameters,
        )
    )
    api_key, api_url = _load_unstructured_runtime_config(connection_params)
    request_timeout_ms = _resolve_unstructured_request_timeout_ms()
    timeout_seconds, poll_interval_seconds = _resolve_bookrag_workflow_poll_config()
    run_label = f"{vector_store_name or 'bookrag'}_parse_{uuid.uuid4().hex[:8]}"
    raw_stage_dir = _prepare_bookrag_raw_stage_dir(run_label)

    source_items: list[tuple[int, Path, str]] = []
    for index, item in enumerate(uploaded_documents):
        saved_path = str(item.get("saved_path") or "").strip()
        doc_id = str(item.get("doc_id") or "").strip()[:64]
        if not saved_path:
            raise RuntimeError(f"Uploaded document has no saved path: index={index}")
        src = Path(resolve_path_hint(saved_path))
        if not src.exists() or not src.is_file():
            raise RuntimeError(f"Uploaded document is missing: {saved_path}")
        source_items.append((index, src, doc_id or uuid.uuid4().hex))

    workers = _resolve_bookrag_unstructured_workers(len(source_items))
    error_secrets = [api_key, *sensitive_values(create_values), *sensitive_values(connection_params), *sensitive_values(request_parameters)]
    submission_lock = Lock()
    last_job_submitted_at: float | None = None

    def _parse_one(index: int, src: Path, doc_id: str) -> dict[str, Any]:
        nonlocal last_job_submitted_at
        started_at = time.perf_counter()
        with submission_lock:
            last_job_submitted_at = _enforce_unstructured_job_submission_spacing(last_job_submitted_at)
        client = _create_unstructured_client(api_key=api_key, api_url=api_url, timeout_ms=request_timeout_ms)
        raw_payload, _, _, job_id, workflow_id, workflow_name_for_job = _run_unstructured_workflow_job_for_file(
            client,
            request_parameters=request_parameters,
            src=src,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            api_key=api_key,
            api_url=api_url,
        )
        raw_stage_file = write_bookrag_raw_stage_file(raw_stage_dir, src.name, doc_id, raw_payload)
        elements = load_bookrag_raw_stage_file(raw_stage_file)
        return {
            "source_index": index,
            "doc_id": doc_id,
            "filename": src.name,
            "source_file": str(src),
            "filetype": mimetypes.guess_type(src.name)[0] or src.suffix.lower().lstrip("."),
            "filesize_bytes": src.stat().st_size,
            "job_id": job_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name_for_job or workflow_name,
            "raw_json_path": str(raw_stage_file),
            "element_count": len(elements),
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
            "status": "success",
            "error": "",
        }

    started_at = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bookrag-document-parse") as executor:
        future_map = {
            executor.submit(_parse_one, index, src, doc_id): (index, src, doc_id)
            for index, src, doc_id in source_items
        }
        for completed_future in as_completed(future_map):
            index, src, doc_id = future_map[completed_future]
            try:
                results.append(completed_future.result())
            except Exception as ex:
                results.append(
                    {
                        "source_index": index,
                        "doc_id": doc_id,
                        "filename": src.name,
                        "source_file": str(src),
                        "filetype": mimetypes.guess_type(src.name)[0] or src.suffix.lower().lstrip("."),
                        "filesize_bytes": src.stat().st_size,
                        "job_id": "",
                        "workflow_id": "",
                        "workflow_name": workflow_name,
                        "raw_json_path": "",
                        "element_count": 0,
                        "elapsed_seconds": 0.0,
                        "status": "failed",
                        "error": _sanitize_teradata_text(redact_sensitive_text(ex, secrets=error_secrets))[:2000],
                    }
                )
    results.sort(key=lambda item: int(item["source_index"]))
    success_count = sum(1 for item in results if item["status"] == "success")
    failure_count = len(results) - success_count
    parse_run_id = raw_stage_dir.name
    created_at = _now_ts()
    manifest_documents: list[dict[str, Any]] = []
    for item in results:
        raw_json_path = Path(str(item.get("raw_json_path") or ""))
        raw_json_file = raw_json_path.name if raw_json_path.is_file() else ""
        manifest_documents.append(
            {
                "source_index": int(item["source_index"]),
                "doc_id": str(item["doc_id"]),
                "filename": str(item["filename"]),
                "source_file": str(item["source_file"]),
                "filetype": str(item.get("filetype") or ""),
                "filesize_bytes": int(item.get("filesize_bytes") or 0),
                "job_id": str(item.get("job_id") or ""),
                "workflow_id": str(item.get("workflow_id") or ""),
                "workflow_name": str(item.get("workflow_name") or ""),
                "raw_json_file": raw_json_file,
                "raw_json_sha256": _file_sha256(raw_json_path) if raw_json_file else "",
                "element_count": int(item.get("element_count") or 0),
                "status": str(item["status"]),
                "error": str(item.get("error") or ""),
            }
        )
    manifest_path = raw_stage_dir / BOOKRAG_PARSE_MANIFEST_FILENAME
    manifest = {
        "schema_version": BOOKRAG_PARSE_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "bookrag_parse_run",
        "parse_run_id": parse_run_id,
        "status": "ready" if failure_count == 0 else "failed",
        "created_at": created_at,
        "vector_store_name": vector_store_name,
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "partition_strategy": partition_strategy,
        "ocr_languages": ocr_languages,
        "processing_profile": processing_profile,
        "workflow_name": workflow_name,
        "image_partition_parameters": image_summary,
        "create_values": _bookrag_config_snapshot(create_values),
        "documents": manifest_documents,
    }
    _write_json_atomic(manifest_path, manifest)
    summary = {
        "status": "ok" if failure_count == 0 else ("partial" if success_count else "error"),
        "parse_run_id": parse_run_id,
        "manifest_path": str(manifest_path),
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "workers": workers,
        "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
        "raw_stage_dir": str(raw_stage_dir),
        "partition_strategy": partition_strategy,
        "ocr_languages": ocr_languages,
        "processing_profile": processing_profile,
        "workflow_name": workflow_name,
        "workflow_node_count": len(workflow_nodes),
        "image_partition_parameters": image_summary,
        "warnings": image_warnings + workflow_warnings,
        "files": results,
        "csv_files_created": 0,
        "database_writes": 0,
    }
    return summary


def run_bookrag_json_to_csv(
    *,
    parse_run_id: str,
    create_values: dict[str, str],
    vector_store_name: str = "",
    target_database: str = "",
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Regenerate per-document/per-table CSV files from a reusable raw JSON parsing run."""
    parse_manifest_path, parse_manifest = _resolve_bookrag_parse_manifest(parse_run_id)
    if str(parse_manifest.get("status") or "") != "ready":
        raise RuntimeError("CSV generation requires a document parsing run in ready status.")
    documents = parse_manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("Document parsing manifest contains no reusable JSON documents.")
    if any(not isinstance(item, dict) or item.get("status") != "success" for item in documents):
        raise RuntimeError("CSV generation requires every JSON document in the parsing run to be successful.")

    effective_vector_store_name = str(vector_store_name or "").strip()
    if not effective_vector_store_name:
        raise RuntimeError("Target Vector Store Name is required before generating CSV files.")
    effective_target_database = _sanitize_teradata_identifier(
        str(target_database or "").strip(), fallback="", allow_empty=True
    )
    if not effective_target_database:
        raise RuntimeError("Target Database is required before generating CSV files.")
    ensure_bookrag_csv_generation_target_available(
        vector_store_name=effective_vector_store_name,
        target_database=effective_target_database,
    )
    table_targets = build_bookrag_table_targets(effective_vector_store_name)
    qualified_table_targets = {
        table_key: f"{effective_target_database}.{table_name}"
        for table_key, table_name in table_targets.items()
    }
    table_generation = _resolve_bookrag_table_generation_flags(create_values)
    selected_entity_tables = any(table_generation[key] for key in BOOKRAG_ENTITY_TABLE_KEYS)
    supersedes_failed_csv_run_ids = _find_failed_bookrag_csv_runs_for_target(
        vector_store_name=effective_vector_store_name,
        target_database=effective_target_database,
    )
    processing_profile = str(parse_manifest.get("processing_profile") or "")
    ocr_languages = [str(value) for value in (parse_manifest.get("ocr_languages") or [])]
    parse_run_dir = parse_manifest_path.parent.resolve()
    csv_run_id = (
        f"{_sanitize_teradata_identifier(parse_run_id, fallback='bookrag')}_csv_"
        f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    csv_stage_dir = BOOKRAG_CSV_STAGE_DIR_DEFAULT / csv_run_id
    csv_stage_dir.mkdir(parents=True, exist_ok=False)
    csv_manifest_path = csv_stage_dir / "manifest.json"
    started_at = time.perf_counter()

    running_manifest = {
        "schema_version": BOOKRAG_CSV_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "bookrag_csv_run",
        "csv_run_id": csv_run_id,
        "source_parse_run_id": parse_run_id,
        "source_parse_manifest": str(parse_manifest_path),
        "status": "running",
        "created_at": _now_ts(),
        "vector_store_name": effective_vector_store_name,
        "target_database": effective_target_database,
        "transform_version": BOOKRAG_TRANSFORM_VERSION,
        "complete_table_contract": BOOKRAG_COMPLETE_TABLE_CONTRACT,
        "table_generation": table_generation,
        "table_targets": table_targets,
        "qualified_table_targets": qualified_table_targets,
        "load_status": "not_started",
        "vector_store_status": "not_started",
        "supersedes_failed_csv_run_ids": supersedes_failed_csv_run_ids,
        "run_csv_files": [],
        "documents": [],
    }
    _write_json_atomic(csv_manifest_path, running_manifest)

    def _transform_document(item: dict[str, Any]) -> dict[str, Any]:
        source_index = int(item.get("source_index") or 0)
        doc_id = str(item.get("doc_id") or "").strip()
        filename = str(item.get("filename") or "").strip()
        raw_json_file = str(item.get("raw_json_file") or "").strip()
        if not doc_id or not filename or not raw_json_file or Path(raw_json_file).name != raw_json_file:
            raise RuntimeError(f"Invalid document metadata at source_index={source_index}.")
        raw_json_path = (parse_run_dir / raw_json_file).resolve()
        if raw_json_path.parent != parse_run_dir or not raw_json_path.is_file():
            raise RuntimeError(f"Raw JSON file was not found for {filename}: {raw_json_file}")
        expected_sha256 = str(item.get("raw_json_sha256") or "").strip()
        if expected_sha256 and _file_sha256(raw_json_path) != expected_sha256:
            raise RuntimeError(f"Raw JSON checksum mismatch for {filename}.")

        transform_started = time.perf_counter()
        raw_elements = load_bookrag_raw_stage_file(raw_json_path)
        if not raw_elements:
            raise RuntimeError(f"Raw JSON contains no elements for {filename}.")
        filetype = str(item.get("filetype") or "").strip() or (
            mimetypes.guess_type(filename)[0] or Path(filename).suffix.lower().lstrip(".")
        )
        table_rows_by_name = _build_bookrag_rows_from_raw_elements(
            doc_id=doc_id,
            filename=filename,
            source_file=str(item.get("source_file") or filename),
            filetype=filetype,
            filesize_bytes=int(item.get("filesize_bytes") or 0),
            vector_store_name=effective_vector_store_name,
            workflow_id=str(item.get("workflow_id") or ""),
            workflow_name=str(item.get("workflow_name") or parse_manifest.get("workflow_name") or ""),
            job_id=str(item.get("job_id") or ""),
            processing_profile=processing_profile,
            language_hint=",".join(ocr_languages) or None,
            created_at=str(parse_manifest.get("created_at") or _now_ts()),
            raw_elements=raw_elements,
            graph_enabled=selected_entity_tables,
        )

        table_rows = {
            "documents": [table_rows_by_name["document_row"]],
            "raw": table_rows_by_name["raw_rows"],
            "blocks": table_rows_by_name["blocks"],
            "nodes": table_rows_by_name["nodes"],
            "entities": table_rows_by_name["entities"],
            "entity_links": table_rows_by_name["entity_links"],
            "entity_relations": table_rows_by_name["entity_relations"],
        }
        file_csv_stage_dir = _document_csv_stage_dir(
            csv_stage_dir,
            source_index=source_index,
            doc_id=doc_id,
            filename=filename,
        )
        csv_files: list[dict[str, Any]] = []
        for table_key, rows in table_rows.items():
            if not table_generation[table_key]:
                continue
            csv_path = prepare_bookrag_table_csv(
                table_key=table_key,
                table_targets=table_targets,
                rows=rows,
                csv_stage_dir=file_csv_stage_dir,
            )
            if csv_path:
                csv_files.append(
                    {
                        "table_key": table_key,
                        "row_count": len(rows),
                        "csv_file": str(Path(csv_path).relative_to(csv_stage_dir)),
                        "csv_sha256": _file_sha256(Path(csv_path)),
                    }
                )
        return {
            "source_index": source_index,
            "doc_id": doc_id,
            "filename": filename,
            "raw_json_file": raw_json_file,
            "status": "success",
            "csv_files": csv_files,
            "elapsed_seconds": round(max(0.0, time.perf_counter() - transform_started), 6),
            "error": "",
        }

    workers = _resolve_bookrag_csv_prepare_workers(len(documents))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bookrag-json-to-csv") as executor:
        future_map = {executor.submit(_transform_document, item): item for item in documents}
        for future in as_completed(future_map):
            item = future_map[future]
            try:
                results.append(future.result())
            except Exception as ex:
                results.append(
                    {
                        "source_index": int(item.get("source_index") or 0),
                        "doc_id": str(item.get("doc_id") or ""),
                        "filename": str(item.get("filename") or ""),
                        "raw_json_file": str(item.get("raw_json_file") or ""),
                        "status": "failed",
                        "csv_files": [],
                        "elapsed_seconds": 0.0,
                        "error": _sanitize_teradata_text(str(ex))[:2000],
                    }
                )
            _report_csv_document_progress(
                progress_callback,
                completed=len(results),
                total=len(documents),
            )
    results.sort(key=lambda value: int(value["source_index"]))
    success_count = sum(1 for item in results if item["status"] == "success")
    failure_count = len(results) - success_count
    run_csv_files: list[dict[str, Any]] = []
    run_error = ""
    document_relation_count = 0
    if failure_count == 0:
        try:
            relation_documents = [
                {"doc_id": item["doc_id"], "filename": item["filename"]}
                for item in results
            ]
            relation_rows = derive_filename_document_relations(relation_documents)
            relation_timestamp = _now_ts()
            relation_rows = [
                {
                    **row,
                    "created_by": None,
                    "created_at": relation_timestamp,
                    "updated_by": None,
                    "updated_at": relation_timestamp,
                }
                for row in relation_rows
            ]
            relation_csv_path = prepare_bookrag_table_csv(
                table_key="document_relations",
                table_targets=table_targets,
                rows=relation_rows,
                csv_stage_dir=csv_stage_dir / "_run",
            )
            if not relation_csv_path:
                raise RuntimeError("Run-level document relation CSV generation produced no file.")
            document_relation_count = len(relation_rows)
            run_csv_files.append(
                {
                    "table_key": "document_relations",
                    "row_count": document_relation_count,
                    "csv_file": str(Path(relation_csv_path).relative_to(csv_stage_dir)),
                    "csv_sha256": _file_sha256(Path(relation_csv_path)),
                }
            )
        except Exception as ex:
            run_error = _sanitize_teradata_text(str(ex))[:2000]
    csv_file_count = sum(len(item["csv_files"]) for item in results) + len(run_csv_files)
    final_status = "ready" if failure_count == 0 and not run_error else "failed"
    final_manifest = {
        **running_manifest,
        "status": final_status,
        "completed_at": _now_ts(),
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "csv_file_count": csv_file_count,
        "document_relation_count": document_relation_count,
        "run_csv_files": run_csv_files,
        "run_error": run_error,
        "documents": results,
    }
    _write_json_atomic(csv_manifest_path, final_manifest)
    return {
        "status": final_status,
        "created_at": running_manifest["created_at"],
        "parse_run_id": parse_run_id,
        "csv_run_id": csv_run_id,
        "manifest_path": str(csv_manifest_path),
        "csv_stage_dir": str(csv_stage_dir),
        "vector_store_name": effective_vector_store_name,
        "target_database": effective_target_database,
        "table_targets": table_targets,
        "qualified_table_targets": qualified_table_targets,
        "file_count": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "csv_files_created": csv_file_count,
        "csv_file_count": csv_file_count,
        "document_relation_count": document_relation_count,
        "run_csv_files": run_csv_files,
        "run_error": run_error,
        "workers": workers,
        "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
        "transform_version": BOOKRAG_TRANSFORM_VERSION,
        "database_writes": 0,
        "files": results,
    }


def _cleanup_failed_bookrag_csv_load_tables(
    *,
    target_database: str,
    table_targets: dict[str, str],
    table_generation: dict[str, Any],
    execute_sql_fn: ExecuteSqlFn | None,
) -> list[str]:
    """Drop only the mapped tables owned by a failed CSV load before a full retry."""
    if execute_sql_fn is None:
        raise RuntimeError("Teradata SQL execution is unavailable for failed-load cleanup.")
    dropped: list[str] = []
    for table_key in reversed(BOOKRAG_TABLE_TOGGLE_ORDER):
        if not table_generation.get(table_key):
            continue
        table_name = table_targets.get(table_key)
        if not table_name:
            continue
        qualified_table = _qualified_table_sql(target_database, table_name)
        if not _teradata_table_exists(qualified_table, execute_sql_fn=execute_sql_fn):
            continue
        execute_sql_fn(f"DROP TABLE {qualified_table}")
        dropped.append(f"{target_database}.{table_name}")
    return dropped


def run_bookrag_csv_load(
    *,
    csv_run_id: str,
    execute_sql_fn: ExecuteSqlFn | None,
    connection_profile_id: int | None = None,
    connection_target_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Load every verified CSV in one ready generation run, without creating a Vector Store."""
    if execute_sql_fn is None:
        raise RuntimeError("Teradata SQL execution is unavailable.")
    manifest_path, manifest = _resolve_bookrag_csv_manifest(csv_run_id)
    if str(manifest.get("status") or "") != "ready":
        raise RuntimeError("Database loading requires a CSV generation run in ready status.")
    if manifest.get("complete_table_contract") != BOOKRAG_COMPLETE_TABLE_CONTRACT:
        raise RuntimeError("Regenerate CSV: this run does not contain the mandatory Graph and bdrel contract.")
    _validate_load_connection(manifest, connection_profile_id, connection_target_fingerprint)
    load_status = str(manifest.get("load_status") or "not_started")
    if load_status == "ready":
        stored_summary = manifest.get("load_summary")
        if not isinstance(stored_summary, dict):
            raise RuntimeError("CSV manifest says loading is ready but contains no load summary.")
        return {**stored_summary, "already_loaded": True}
    if load_status == "loading":
        raise RuntimeError(
            "This CSV generation run is already marked as loading. If its operation was interrupted, "
            "inspect the target tables and confirm no loader is active before explicitly starting a new CSV load run. "
            "Automatic retry did not clear or overwrite any target table."
        )
    recover_failed_load = load_status == "failed"

    vector_store_name = str(manifest.get("vector_store_name") or "").strip()
    target_database = _sanitize_teradata_identifier(
        str(manifest.get("target_database") or "").strip(), fallback="", allow_empty=True
    )
    if not vector_store_name or not target_database:
        raise RuntimeError("CSV manifest is missing its target Vector Store name or target database.")
    expected_table_targets = build_bookrag_table_targets(vector_store_name)
    manifest_table_targets = manifest.get("table_targets")
    if manifest_table_targets != expected_table_targets:
        raise RuntimeError("CSV manifest table mapping does not match its target Vector Store name.")
    table_generation = manifest.get("table_generation")
    if not isinstance(table_generation, dict):
        raise RuntimeError("CSV manifest contains no table generation configuration.")
    for mandatory_table in (
        "documents",
        "blocks",
        "nodes",
        "document_relations",
        "entities",
        "entity_links",
        "entity_relations",
    ):
        if not table_generation.get(mandatory_table):
            raise RuntimeError(f"CSV manifest disables mandatory table: {mandatory_table}")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("CSV manifest contains no document outputs.")

    csv_run_dir = manifest_path.parent.resolve()
    load_tasks: list[dict[str, Any]] = []
    expected_row_counts: dict[str, int] = {}
    seen_csv_files: set[str] = set()

    def _register_csv_item(csv_item: dict[str, Any], *, task_id: str) -> str:
        if not isinstance(csv_item, dict):
            raise RuntimeError("CSV manifest contains an invalid CSV output entry.")
        table_key = str(csv_item.get("table_key") or "").strip()
        relative_csv_file = str(csv_item.get("csv_file") or "").strip()
        row_count = _to_int(csv_item.get("row_count"), default=0, minimum=0)
        if table_key not in expected_table_targets or not table_generation.get(table_key):
            raise RuntimeError(f"CSV manifest contains an unexpected table output: {table_key}")
        csv_path = (csv_run_dir / relative_csv_file).resolve()
        if csv_run_dir not in csv_path.parents or not csv_path.is_file():
            raise RuntimeError(f"CSV file is outside its generation run or missing: {relative_csv_file}")
        normalized_csv_path = str(csv_path)
        if normalized_csv_path in seen_csv_files:
            raise RuntimeError(f"CSV manifest contains a duplicate file: {relative_csv_file}")
        seen_csv_files.add(normalized_csv_path)
        expected_sha256 = str(csv_item.get("csv_sha256") or "").strip()
        if not expected_sha256 or _file_sha256(csv_path) != expected_sha256:
            raise RuntimeError(f"CSV checksum mismatch: {relative_csv_file}")
        validate_prepared_bookrag_table_csv(table_key=table_key, csv_path=normalized_csv_path)
        expected_row_counts[table_key] = expected_row_counts.get(table_key, 0) + row_count
        if row_count > 0:
            load_tasks.append(
                {
                    "task_id": task_id,
                    "table_key": table_key,
                    "csv_path": normalized_csv_path,
                    "row_count": row_count,
                }
            )
        return table_key

    required_document_tables = {
        "documents",
        "blocks",
        "nodes",
        "entities",
        "entity_links",
        "entity_relations",
    }
    if table_generation.get("raw"):
        required_document_tables.add("raw")
    for document in documents:
        if not isinstance(document, dict) or document.get("status") != "success":
            raise RuntimeError("CSV loading requires every document output to be successful.")
        csv_files = document.get("csv_files")
        if not isinstance(csv_files, list):
            raise RuntimeError(f"CSV output list is missing for document {document.get('filename') or '?'}.")
        document_table_keys: set[str] = set()
        for csv_item in csv_files:
            task_table_key = csv_item.get("table_key", "?") if isinstance(csv_item, dict) else "?"
            table_key = _register_csv_item(
                csv_item,
                task_id=f"{document.get('source_index', 0)}:{task_table_key}",
            )
            if table_key in document_table_keys:
                raise RuntimeError(
                    f"CSV manifest contains duplicate table output for {document.get('filename') or '?'}: {table_key}"
                )
            document_table_keys.add(table_key)
        missing_document_tables = sorted(required_document_tables - document_table_keys)
        if missing_document_tables:
            raise RuntimeError(
                f"CSV output is incomplete for {document.get('filename') or '?'}: "
                f"missing {', '.join(missing_document_tables)}"
            )

    run_csv_files = manifest.get("run_csv_files")
    if not isinstance(run_csv_files, list):
        raise RuntimeError("CSV manifest contains no run-level output list.")
    run_table_keys: set[str] = set()
    for csv_item in run_csv_files:
        task_table_key = csv_item.get("table_key", "?") if isinstance(csv_item, dict) else "?"
        table_key = _register_csv_item(csv_item, task_id=f"run:{task_table_key}")
        if table_key in run_table_keys:
            raise RuntimeError(f"CSV manifest contains duplicate run-level table output: {table_key}")
        run_table_keys.add(table_key)
    if run_table_keys != {"document_relations"}:
        raise RuntimeError("CSV manifest must contain exactly one run-level document_relations CSV.")
    if not load_tasks:
        raise RuntimeError("CSV manifest contains no loadable files.")
    for required_table in ("documents", "blocks", "nodes"):
        if expected_row_counts.get(required_table, 0) <= 0:
            raise RuntimeError(f"CSV manifest contains no rows for required table: {required_table}")

    warnings: list[str] = []
    superseded_failed_run_ids = [
        str(value).strip()
        for value in (manifest.get("supersedes_failed_csv_run_ids") or [])
        if str(value).strip()
    ]
    if recover_failed_load or superseded_failed_run_ids:
        vector_store_status = str(manifest.get("vector_store_status") or "not_started")
        if vector_store_status in {"creating", "ready"}:
            raise RuntimeError(
                f"Cannot clean a failed CSV load while Vector Store status is {vector_store_status!r}."
            )
        dropped_tables = _cleanup_failed_bookrag_csv_load_tables(
            target_database=target_database,
            table_targets=expected_table_targets,
            table_generation=table_generation,
            execute_sql_fn=execute_sql_fn,
        )
        manifest["load_recovered_at"] = _now_ts()
        manifest["load_recovered_tables"] = dropped_tables
        if recover_failed_load:
            retry_count = _to_int(manifest.get("load_retry_count"), default=0, minimum=0) + 1
            manifest["load_retry_count"] = retry_count
            warnings.append(
                f"Cleaned {len(dropped_tables)} partial target table(s) from failed load before full retry #{retry_count}."
            )
        else:
            manifest["superseded_failed_csv_run_ids_cleaned"] = superseded_failed_run_ids
            manifest["supersedes_failed_csv_run_ids"] = []
            warnings.append(
                f"Cleaned {len(dropped_tables)} partial target table(s) left by "
                f"{len(superseded_failed_run_ids)} superseded failed CSV run(s)."
            )

    manifest["load_status"] = "loading"
    if connection_profile_id is not None:
        manifest["connection_profile_id"] = int(connection_profile_id)
        manifest["connection_target_fingerprint"] = connection_target_fingerprint
    manifest["load_started_at"] = _now_ts()
    manifest["load_error"] = ""
    _write_json_atomic(manifest_path, manifest)
    started_at = time.perf_counter()
    prepare_functions = {
        "documents": prepare_bookrag_document_table,
        "raw": prepare_bookrag_raw_table,
        "blocks": prepare_bookrag_block_table,
        "nodes": prepare_bookrag_node_table,
        "document_relations": prepare_bookrag_document_relation_table,
        "entities": prepare_bookrag_entity_table,
        "entity_links": prepare_bookrag_entity_link_table,
        "entity_relations": prepare_bookrag_entity_relation_table,
    }
    try:
        for table_key in BOOKRAG_TABLE_TOGGLE_ORDER:
            if not table_generation.get(table_key):
                continue
            prepare_fn = prepare_functions[table_key]
            warnings.extend(
                prepare_fn(
                    schema_name=target_database,
                    table_targets=expected_table_targets,
                    execute_sql_fn=execute_sql_fn,
                )
            )

        workers = _resolve_bookrag_csv_load_workers(len(load_tasks))

        def _load_one(task: dict[str, Any]) -> dict[str, Any]:
            task_started = time.perf_counter()
            stats: dict[str, Any] = {}
            inserted = load_prepared_bookrag_table_csv(
                schema_name=target_database,
                table_key=task["table_key"],
                table_targets=expected_table_targets,
                csv_path=task["csv_path"],
                row_count=task["row_count"],
                stats=stats,
            )
            return {
                **task,
                "inserted_rows": inserted,
                "elapsed_seconds": round(max(0.0, time.perf_counter() - task_started), 6),
                "stats": stats,
            }

        load_results: list[dict[str, Any]] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bookrag-manifest-csv-load") as executor:
            future_map = {executor.submit(_load_one, task): task for task in load_tasks}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    load_results.append(future.result())
                except Exception as ex:
                    failures.append(
                        f"task={task['task_id']}, table={task['table_key']}, csv={task['csv_path']}: {ex}"
                    )
        if failures:
            raise RuntimeError("Parallel CSV loading failed: " + " | ".join(failures))

        persisted_row_counts: dict[str, int] = {}
        for table_key, expected_count in expected_row_counts.items():
            actual_count = _count_teradata_rows(
                schema_name=target_database,
                table_name=expected_table_targets[table_key],
                execute_sql_fn=execute_sql_fn,
            )
            if actual_count is None:
                raise RuntimeError(f"Could not verify loaded row count for table: {table_key}")
            if actual_count != expected_count:
                raise RuntimeError(
                    f"Loaded row count mismatch for {table_key}: expected={expected_count}, actual={actual_count}"
                )
            persisted_row_counts[table_key] = actual_count

        retrieval_view_name = ensure_bookrag_retrieval_view(
            vector_store_name=vector_store_name,
            schema_name=target_database,
            execute_sql_fn=execute_sql_fn,
        )

        summary = {
            "status": "ready",
            "csv_run_id": csv_run_id,
            "vector_store_name": vector_store_name,
            "target_database": target_database,
            "table_targets": expected_table_targets,
            "connection_profile_id": connection_profile_id,
            "connection_target_fingerprint": connection_target_fingerprint,
            "qualified_table_targets": {
                key: f"{target_database}.{value}" for key, value in expected_table_targets.items()
            },
            "task_count": len(load_tasks),
            "csv_file_count": len(seen_csv_files),
            "empty_csv_count": len(seen_csv_files) - len(load_tasks),
            "workers": workers,
            "inserted_rows": sum(int(result["inserted_rows"]) for result in load_results),
            "expected_row_counts": expected_row_counts,
            "persisted_row_counts": persisted_row_counts,
            "node_table": f"{target_database}.{expected_table_targets['nodes']}",
            "retrieval_view": f"{target_database}.{retrieval_view_name}",
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 6),
            "warnings": warnings,
            "already_loaded": False,
        }
        manifest["load_status"] = "ready"
        manifest["load_completed_at"] = _now_ts()
        manifest["load_summary"] = summary
        _write_json_atomic(manifest_path, manifest)
        return summary
    except Exception as ex:
        manifest["load_status"] = "failed"
        manifest["load_completed_at"] = _now_ts()
        manifest["load_error"] = _sanitize_teradata_text(str(ex))[:4000]
        _write_json_atomic(manifest_path, manifest)
        raise


def get_ready_bookrag_csv_load_summary(
    *, csv_run_id: str, connection_profile_id: int | None = None,
    connection_target_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Read a verified table-load result without loading CSV or creating a Vector Store."""
    _, manifest = _resolve_bookrag_csv_manifest(csv_run_id)
    if str(manifest.get("status") or "") != "ready":
        raise RuntimeError("Vector Store creation requires a CSV generation run in ready status.")
    if str(manifest.get("load_status") or "not_started") != "ready":
        raise RuntimeError("Load the CSV run into database tables before creating the Vector Store.")
    _validate_load_connection(manifest, connection_profile_id, connection_target_fingerprint)
    summary = manifest.get("load_summary")
    if not isinstance(summary, dict):
        raise RuntimeError("CSV manifest says table loading is ready but contains no load summary.")
    required = ("vector_store_name", "target_database", "node_table")
    missing = [key for key in required if not str(summary.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"CSV table-load summary is missing required fields: {', '.join(missing)}")
    return {**summary, "already_loaded": True}


def update_bookrag_csv_vector_store_status(
    *,
    csv_run_id: str,
    status: str,
    error: str = "",
    create_payload: dict[str, Any] | None = None,
) -> None:
    if status not in {"creating", "ready", "failed"}:
        raise RuntimeError(f"Unsupported Vector Store manifest status: {status}")
    manifest_path, manifest = _resolve_bookrag_csv_manifest(csv_run_id)
    manifest["vector_store_status"] = status
    manifest["vector_store_updated_at"] = _now_ts()
    manifest["vector_store_error"] = _sanitize_teradata_text(error)[:4000]
    if create_payload is not None:
        manifest["vector_store_create_payload"] = _json_safe_value(create_payload)
    _write_json_atomic(manifest_path, manifest)


def _apply_bookrag_tree_pipeline(
    exec_payload: dict,
    create_values: dict[str, str],
    vector_store_name: str,
    *,
    execute_sql_fn: ExecuteSqlFn | None,
    resolve_path_hint: Callable[[str], str],
    effective_schema_name: str | None,
    document_files: list[str],
    partition_strategy: str,
    ocr_languages: list[str],
    target_warnings: list[str],
    connection_params: dict | None = None,
) -> tuple[dict, dict]:
    bookrag_tables = build_bookrag_table_targets(vector_store_name)
    table_generation = _resolve_bookrag_table_generation_flags(create_values)
    run_embedding_step = _resolve_bookrag_embedding_step_flag(create_values)
    if run_embedding_step and not table_generation["nodes"]:
        raise RuntimeError("BookRAG embedding step requires bnode generation to be enabled.")
    if table_generation["documents"]:
        target_warnings.extend(
            prepare_bookrag_document_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["raw"]:
        target_warnings.extend(
            prepare_bookrag_raw_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["blocks"]:
        target_warnings.extend(
            prepare_bookrag_block_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["nodes"]:
        target_warnings.extend(
            prepare_bookrag_node_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["document_relations"]:
        target_warnings.extend(
            prepare_bookrag_document_relation_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["entities"]:
        target_warnings.extend(
            prepare_bookrag_entity_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["entity_links"]:
        target_warnings.extend(
            prepare_bookrag_entity_link_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    if table_generation["entity_relations"]:
        target_warnings.extend(
            prepare_bookrag_entity_relation_table(
                schema_name=effective_schema_name,
                table_targets=bookrag_tables,
                execute_sql_fn=execute_sql_fn,
            )
        )
    selected_entity_tables = any(table_generation[key] for key in BOOKRAG_ENTITY_TABLE_KEYS)
    image_partition_parameters, image_partition_warnings, image_partition_summary = _resolve_bookrag_image_partition_options(create_values)
    target_warnings.extend(image_partition_warnings)


    api_key, api_url = _load_unstructured_runtime_config(connection_params)
    request_timeout_ms = _resolve_unstructured_request_timeout_ms()
    debug_dir = _prepare_unstructured_debug_dir(vector_store_name)
    raw_stage_dir = _prepare_bookrag_raw_stage_dir(vector_store_name)
    csv_stage_dir = _prepare_bookrag_csv_stage_dir(vector_store_name)
    partition_warnings: list[str] = []
    debug_files: list[str] = []
    raw_stage_files: list[str] = []
    job_ids: list[str] = []
    document_insert_stats: dict[str, Any] = {}
    raw_insert_stats: dict[str, Any] = {}
    block_insert_stats: dict[str, Any] = {}
    node_insert_stats: dict[str, Any] = {}
    entity_insert_stats: dict[str, Any] = {}
    entity_link_insert_stats: dict[str, Any] = {}
    entity_relation_insert_stats: dict[str, Any] = {}
    document_relation_insert_stats: dict[str, Any] = {}
    inserted_rows = 0
    raw_element_count = 0
    block_count = 0
    node_count = 0
    entity_count: int | None = 0 if selected_entity_tables else None
    entity_link_count: int | None = 0 if selected_entity_tables else None
    entity_relation_count: int | None = 0 if selected_entity_tables else None
    document_count = 0
    persisted_documents: list[dict[str, Any]] = []
    flush_config: dict[str, Any] = {
        "mode": "three_stage_parallel",
        "csv_layout": "per_file_per_table",
    }
    flush_batches: list[dict[str, Any]] = []

    qualified_tables = {
        name: (f"{effective_schema_name}.{table_name}" if effective_schema_name else table_name)
        for name, table_name in bookrag_tables.items()
    }
    persisted_bookrag_tables = {
        name: qualified_tables[name]
        for name in BOOKRAG_TABLE_TOGGLE_ORDER
        if table_generation[name]
    }

    workflow_name, workflow_nodes, request_parameters, workflow_definition_warnings, processing_profile = _build_bookrag_reusable_workflow_definition(
        create_values=create_values,
        partition_strategy=partition_strategy,
        languages=ocr_languages,
        image_partition_parameters=image_partition_parameters,
    )
    partition_warnings.extend(workflow_definition_warnings)

    manifest_by_path: dict[str, dict[str, Any]] = {}
    raw_manifest = exec_payload.get("document_manifest")
    if isinstance(raw_manifest, list):
        for item in raw_manifest:
            if not isinstance(item, dict):
                continue
            saved_path = str(item.get("saved_path") or "").strip()
            if not saved_path:
                continue
            manifest_path = Path(resolve_path_hint(saved_path))
            manifest_by_path[str(manifest_path.resolve())] = item

    source_items: list[tuple[Path, str]] = []
    for path_hint in document_files:
        resolved = resolve_path_hint(path_hint)
        src = Path(resolved)
        if not src.exists() or not src.is_file():
            raise RuntimeError(f"multi format source file is missing: {path_hint}")
        manifest_item = manifest_by_path.get(str(src.resolve())) or {}
        assigned_doc_id = str(manifest_item.get("doc_id") or "").strip()[:64]
        source_items.append((src, assigned_doc_id or uuid.uuid4().hex))

    workflow_ids: list[str] = []
    workflow_names_seen: list[str] = []
    timeout_seconds, poll_interval_seconds = _resolve_bookrag_workflow_poll_config()
    unstructured_workers = _resolve_bookrag_unstructured_workers(len(source_items))
    submission_lock = Lock()
    last_job_submitted_at: float | None = None

    def _extract_and_stage_file(src: Path, doc_id: str) -> dict[str, Any]:
        nonlocal last_job_submitted_at
        unstructured_started = time.perf_counter()
        with submission_lock:
            last_job_submitted_at = _enforce_unstructured_job_submission_spacing(last_job_submitted_at)
        file_client = _create_unstructured_client(api_key=api_key, api_url=api_url, timeout_ms=request_timeout_ms)
        raw_output_payload, _, file_request_parameters, job_id, workflow_id, workflow_name_for_job = _run_unstructured_workflow_job_for_file(
            file_client,
            request_parameters=request_parameters,
            src=src,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            api_key=api_key,
            api_url=api_url,
        )
        unstructured_seconds = max(0.0, time.perf_counter() - unstructured_started)
        json_started = time.perf_counter()
        raw_stage_file = write_bookrag_raw_stage_file(
            raw_stage_dir,
            src.name,
            doc_id,
            raw_output_payload,
        )
        json_stage_seconds = max(0.0, time.perf_counter() - json_started)
        return {
            "src": src,
            "doc_id": doc_id,
            "raw_stage_file": raw_stage_file,
            "file_request_parameters": file_request_parameters,
            "job_id": job_id,
            "workflow_id": workflow_id,
            "workflow_name_for_job": workflow_name_for_job,
            "unstructured_seconds": round(unstructured_seconds, 6),
            "json_stage_seconds": round(json_stage_seconds, 6),
        }

    extracted_files: list[dict[str, Any]] = []
    extraction_failures: list[str] = []
    with ThreadPoolExecutor(max_workers=unstructured_workers, thread_name_prefix="bookrag-unstructured") as executor:
        future_map = {
            executor.submit(_extract_and_stage_file, src, doc_id): (index, src, doc_id)
            for index, (src, doc_id) in enumerate(source_items)
        }
        for completed_future in as_completed(future_map):
            index, src, doc_id = future_map[completed_future]
            try:
                extracted = completed_future.result()
                extracted["source_index"] = index
                extracted_files.append(extracted)
            except Exception as ex:
                extraction_failures.append(f"file={src.name}, doc_id={doc_id}: {ex}")
    if extraction_failures:
        raise RuntimeError("BookRAG parallel document parsing failed: " + " | ".join(extraction_failures))
    extracted_files.sort(key=lambda item: int(item["source_index"]))

    for extracted in extracted_files:
        job_ids.append(extracted["job_id"])
        if extracted["workflow_id"]:
            workflow_ids.append(extracted["workflow_id"])
        if extracted["workflow_name_for_job"]:
            workflow_names_seen.append(extracted["workflow_name_for_job"])
        raw_stage_files.append(str(extracted["raw_stage_file"]))

    def _transform_and_prepare_csv(extracted: dict[str, Any]) -> dict[str, Any]:
        src = extracted["src"]
        doc_id = extracted["doc_id"]
        raw_stage_file = extracted["raw_stage_file"]
        workflow_id = extracted["workflow_id"]
        workflow_name_for_job = extracted["workflow_name_for_job"]
        job_id = extracted["job_id"]
        transform_started = time.perf_counter()
        staged_raw_elements = load_bookrag_raw_stage_file(raw_stage_file)
        reconciled_elements = staged_raw_elements
        filetype = mimetypes.guess_type(src.name)[0] or src.suffix.lower().lstrip(".")
        try:
            table_rows_by_name = _build_bookrag_rows_from_raw_elements(
                doc_id=doc_id,
                filename=src.name,
                source_file=str(src),
                filetype=filetype,
                filesize_bytes=src.stat().st_size,
                vector_store_name=vector_store_name,
                workflow_id=workflow_id,
                workflow_name=workflow_name_for_job or workflow_name,
                job_id=job_id,
                processing_profile=processing_profile,
                language_hint=",".join(ocr_languages) or None,
                created_at=_now_ts(),
                raw_elements=reconciled_elements,
                graph_enabled=selected_entity_tables,
            )
        except RuntimeError as ex:
            raise RuntimeError(
                f"BookRAG integrity validation failed for file={src.name}, doc_id={doc_id}: {ex}"
            ) from ex
        document_row = table_rows_by_name["document_row"]
        raw_rows = table_rows_by_name["raw_rows"]
        blocks = table_rows_by_name["blocks"]
        nodes = table_rows_by_name["nodes"]
        entities = table_rows_by_name["entities"]
        entity_links = table_rows_by_name["entity_links"]
        entity_relations = table_rows_by_name["entity_relations"]
        transform_seconds = max(0.0, time.perf_counter() - transform_started)

        debug_file = _write_unstructured_debug_file(
            debug_dir,
            src,
            reconciled_elements,
            [],
            extracted["file_request_parameters"],
            extra_payload={
                **_workflow_debug_payload(
                    extracted["file_request_parameters"],
                    processing_profile=processing_profile,
                    workflow_id=workflow_id,
                    workflow_name=workflow_name_for_job or workflow_name,
                    job_id=job_id,
                    workflow_kind="bookrag",
                ),
                "bookrag_image_partition_parameters": image_partition_summary,
                "workflow_nodes": workflow_nodes,
                "raw_element_count_before_reconcile": len(staged_raw_elements),
                "raw_element_count_after_reconcile": len(reconciled_elements),
                "block_count": len(blocks),
                "node_count": len(nodes),
                "entity_count": len(entities),
                "entity_link_count": len(entity_links),
                "entity_relation_count": len(entity_relations),
                "bookrag_table_generation": table_generation,
            },
        )
        result = {
            **extracted,
            "document_row": document_row,
            "raw_rows": raw_rows,
            "blocks": blocks,
            "nodes": nodes,
            "entities": entities,
            "entity_links": entity_links,
            "entity_relations": entity_relations,
            "debug_file": debug_file,
            "transform_seconds": round(transform_seconds, 6),
            "csv_tasks": [],
        }
        if not raw_rows:
            result["warning"] = f"No BookRAG raw elements extracted from file: {src.name}"
            return result

        file_csv_stage_dir = _document_csv_stage_dir(
            csv_stage_dir,
            source_index=int(extracted["source_index"]),
            doc_id=doc_id,
            filename=src.name,
        )
        result["file_csv_stage_dir"] = file_csv_stage_dir
        table_rows = {
            "documents": [document_row],
            "raw": raw_rows,
            "blocks": blocks,
            "nodes": nodes,
            "entities": entities,
            "entity_links": entity_links,
            "entity_relations": entity_relations,
        }
        csv_started = time.perf_counter()
        for table_key, rows in table_rows.items():
            if not table_generation[table_key]:
                continue
            task_stats: dict[str, Any] = {}
            csv_path = prepare_bookrag_table_csv(
                table_key=table_key,
                table_targets=bookrag_tables,
                rows=rows,
                csv_stage_dir=file_csv_stage_dir,
                stats=task_stats,
            )
            result["csv_tasks"].append(
                {
                    "task_id": f"{extracted['source_index']}:{table_key}",
                    "table_key": table_key,
                    "rows": rows,
                    "csv_path": csv_path,
                    "stats": task_stats,
                }
            )
        result["csv_prepare_seconds"] = round(max(0.0, time.perf_counter() - csv_started), 6)
        return result

    csv_prepare_workers = _resolve_bookrag_csv_prepare_workers(len(extracted_files))
    transformed_files: list[dict[str, Any]] = []
    transform_failures: list[str] = []
    with ThreadPoolExecutor(max_workers=csv_prepare_workers, thread_name_prefix="bookrag-csv-prepare") as executor:
        future_map = {executor.submit(_transform_and_prepare_csv, extracted): extracted for extracted in extracted_files}
        for completed_future in as_completed(future_map):
            extracted = future_map[completed_future]
            try:
                transformed_files.append(completed_future.result())
            except Exception as ex:
                transform_failures.append(
                    f"file={extracted['src'].name}, doc_id={extracted['doc_id']}: {ex}"
                )
    if transform_failures:
        raise RuntimeError("BookRAG parallel JSON-to-CSV processing failed: " + " | ".join(transform_failures))
    transformed_files.sort(key=lambda item: int(item["source_index"]))

    csv_tasks = [
        task
        for item in transformed_files
        for task in item["csv_tasks"]
        if task["rows"]
    ]
    csv_load_workers = _resolve_bookrag_csv_load_workers(len(csv_tasks))
    persist_functions = {
        "documents": (persist_bookrag_documents, "rows"),
        "raw": (persist_bookrag_raw_rows, "rows"),
        "blocks": (persist_bookrag_blocks, "blocks"),
        "nodes": (persist_bookrag_nodes, "nodes"),
        "entities": (persist_bookrag_entities, "entities"),
        "entity_links": (persist_bookrag_entity_links, "entity_links"),
        "entity_relations": (persist_bookrag_entity_relations, "entity_relations"),
    }

    def _load_prepared_csv(task: dict[str, Any]) -> dict[str, Any]:
        persist_fn, rows_argument = persist_functions[task["table_key"]]
        started = time.perf_counter()
        inserted = persist_fn(
            schema_name=effective_schema_name,
            table_targets=bookrag_tables,
            execute_sql_fn=execute_sql_fn,
            csv_stage_dir=Path(task["csv_path"]).parent,
            stats=task["stats"],
            prepared_csv_path=task["csv_path"],
            **{rows_argument: task["rows"]},
        )
        return {
            "task_id": task["task_id"],
            "inserted": inserted,
            "persist_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    load_results: dict[str, dict[str, Any]] = {}
    load_failures: list[str] = []
    if csv_tasks:
        with ThreadPoolExecutor(max_workers=csv_load_workers, thread_name_prefix="bookrag-csv-load") as executor:
            future_map = {executor.submit(_load_prepared_csv, task): task for task in csv_tasks}
            for completed_future in as_completed(future_map):
                task = future_map[completed_future]
                try:
                    result = completed_future.result()
                    load_results[result["task_id"]] = result
                except Exception as ex:
                    load_failures.append(
                        f"task={task['task_id']}, csv={task['csv_path']}, table={task['table_key']}: {ex}"
                    )
    if load_failures:
        raise RuntimeError("BookRAG parallel CSV loading failed: " + " | ".join(load_failures))

    aggregate_stats = {
        "documents": document_insert_stats,
        "raw": raw_insert_stats,
        "blocks": block_insert_stats,
        "nodes": node_insert_stats,
        "entities": entity_insert_stats,
        "entity_links": entity_link_insert_stats,
        "entity_relations": entity_relation_insert_stats,
    }
    for task in csv_tasks:
        _merge_bookrag_insert_stats(aggregate_stats[task["table_key"]], task["stats"])

    for item in transformed_files:
        if item.get("debug_file"):
            debug_files.append(item["debug_file"])
        if item.get("warning"):
            partition_warnings.append(item["warning"])
            continue
        document_row = item["document_row"]
        raw_rows = item["raw_rows"]
        blocks = item["blocks"]
        nodes = item["nodes"]
        entities = item["entities"]
        entity_links = item["entity_links"]
        entity_relations = item["entity_relations"]
        item_load_results = [
            load_results[task["task_id"]]
            for task in item["csv_tasks"]
            if task["task_id"] in load_results
        ]
        inserted_for_file = sum(int(result["inserted"]) for result in item_load_results)
        persist_seconds = sum(float(result["persist_seconds"]) for result in item_load_results)
        inserted_rows += inserted_for_file
        document_count += 1
        raw_element_count += len(raw_rows)
        block_count += len(blocks)
        node_count += len(nodes)
        if table_generation["documents"]:
            persisted_documents.append(document_row)
        if selected_entity_tables:
            entity_count = (entity_count or 0) + len(entities)
            entity_link_count = (entity_link_count or 0) + len(entity_links)
            entity_relation_count = (entity_relation_count or 0) + len(entity_relations)
        flush_batches.append(
            {
                "batch_index": len(flush_batches) + 1,
                "reason": "file_ready",
                "file_count": 1,
                "filename": item["src"].name,
                "doc_id": item["doc_id"],
                "csv_stage_dir": str(item["file_csv_stage_dir"]),
                "documents": 1,
                "raw": len(raw_rows),
                "blocks": len(blocks),
                "nodes": len(nodes),
                "entities": len(entities),
                "entity_links": len(entity_links),
                "entity_relations": len(entity_relations),
                "inserted_rows": inserted_for_file,
                "unstructured_seconds": item["unstructured_seconds"],
                "json_stage_seconds": item["json_stage_seconds"],
                "transform_seconds": item["transform_seconds"],
                "csv_prepare_seconds": item.get("csv_prepare_seconds", 0.0),
                "persist_seconds": round(persist_seconds, 6),
            }
        )

    document_relation_count = 0
    document_relation_rule_count = 0
    raw_document_relations = exec_payload.get("document_relations")
    document_relations_to_persist = [
        item
        for item in (raw_document_relations if isinstance(raw_document_relations, list) else [])
        if isinstance(item, dict)
    ]
    existing_relation_keys = {
        (
            str(item.get("from_doc_id") or "").strip(),
            str(item.get("relation_type") or "").strip(),
            str(item.get("to_doc_id") or "").strip(),
        )
        for item in document_relations_to_persist
    }
    if table_generation["document_relations"]:
        for relationship in derive_filename_document_relations(persisted_documents):
            key = (
                str(relationship.get("from_doc_id") or "").strip(),
                str(relationship.get("relation_type") or "").strip(),
                str(relationship.get("to_doc_id") or "").strip(),
            )
            if key in existing_relation_keys:
                continue
            existing_relation_keys.add(key)
            document_relations_to_persist.append(relationship)
            document_relation_rule_count += 1

    if table_generation["document_relations"] and document_relations_to_persist:
        relation_started = time.perf_counter()
        document_relation_count = persist_document_relations(
            vector_store_name=vector_store_name,
            schema_name=effective_schema_name,
            relations=document_relations_to_persist,
            documents=persisted_documents,
            execute_sql_fn=execute_sql_fn,
            username=str((connection_params or {}).get("username") or ""),
        )
        inserted_rows += document_relation_count
        document_relation_insert_stats.update(
            {
                "input_rows": document_relation_count,
                "inserted_rows": document_relation_count,
                "insert_mode": "sql",
                "insert_total_seconds": round(
                    max(0.0, time.perf_counter() - relation_started),
                    6,
                ),
            }
        )
        if document_relation_rule_count:
            partition_warnings.append(
                f"Created {document_relation_rule_count} bdrel relationship(s) from filename rules; "
                "every stored relationship is available to retrieval."
            )

    retrieval_view_name = ""
    if all(
        table_generation[table_key]
        for table_key in ("documents", "nodes", "document_relations")
    ):
        retrieval_view_name = ensure_bookrag_retrieval_view(
            vector_store_name=vector_store_name,
            schema_name=effective_schema_name,
            execute_sql_fn=execute_sql_fn,
        )

    persisted_table_row_counts: dict[str, Any] = {}
    for table_key in BOOKRAG_TABLE_TOGGLE_ORDER:
        if not table_generation[table_key]:
            continue
        row_count = _count_teradata_rows(
            schema_name=effective_schema_name,
            table_name=bookrag_tables[table_key],
            execute_sql_fn=execute_sql_fn,
        )
        if row_count is not None:
            persisted_table_row_counts[table_key] = row_count

    persisted_raw_count = persisted_table_row_counts.get("raw", raw_element_count)
    if inserted_rows <= 0:
        enabled_tables = ", ".join(table_key for table_key in BOOKRAG_TABLE_TOGGLE_ORDER if table_generation[table_key])
        raise RuntimeError(f"bookrag workflow completed but selected tables received 0 inserted rows. tables={enabled_tables}")
    if persisted_table_row_counts and max(persisted_table_row_counts.values()) <= 0:
        enabled_tables = ", ".join(persisted_bookrag_tables.values())
        raise RuntimeError(f"bookrag workflow completed but selected tables have 0 persisted rows. tables={enabled_tables}")

    patched_payload = _strip_file_based_create_params(exec_payload)
    if table_generation["nodes"]:
        patched_payload["object_names"] = qualified_tables["nodes"]
        patched_payload["data_columns"] = ["content"]
        patched_payload["key_columns"] = ["doc_id", "node_id"]
        patched_payload.pop("vector_column", None)
    description_text = str(patched_payload.get("description") or "").strip()
    description_low = description_text.lower()
    if "unstructured_bookrag_flg" not in description_low:
        patched_payload["description"] = (
            f"{description_text} unstructured_bookrag_flg".strip() if description_text else "unstructured_bookrag_flg"
        )
    else:
        patched_payload["description"] = description_text

    summary = {
        "table_name": "",
        "documents_table_name": persisted_bookrag_tables.get("documents", ""),
        "raw_table_name": persisted_bookrag_tables.get("raw", ""),
        "blocks_table_name": persisted_bookrag_tables.get("blocks", ""),
        "nodes_table_name": persisted_bookrag_tables.get("nodes", ""),
        "document_relations_table_name": persisted_bookrag_tables.get("document_relations", ""),
        "retrieval_view_name": retrieval_view_name,
        "entities_table_name": persisted_bookrag_tables.get("entities", ""),
        "entity_links_table_name": persisted_bookrag_tables.get("entity_links", ""),
        "entity_relations_table_name": persisted_bookrag_tables.get("entity_relations", ""),
        "vectorstore_source_object": persisted_bookrag_tables.get("nodes", ""),
        "vectorstore_data_columns": ["content"] if table_generation["nodes"] else [],
        "vectorstore_key_columns": ["doc_id", "node_id"] if table_generation["nodes"] else [],
        "skip_vectorstore_create": not run_embedding_step,
        "run_embedding_step": run_embedding_step,
        "raw_element_count": persisted_raw_count,
        "block_count": block_count,
        "node_count": node_count,
        "entity_count": entity_count,
        "entity_link_count": entity_link_count,
        "entity_relation_count": entity_relation_count,
        "document_count": document_count,
        "document_relation_count": document_relation_count,
        "document_relation_rule_count": document_relation_rule_count,
        "job_id": job_ids[-1] if job_ids else "",
        "job_ids": job_ids,
        "workflow_id": workflow_ids[-1] if workflow_ids else "",
        "workflow_name": workflow_names_seen[-1] if workflow_names_seen else workflow_name,
        "destination_id": "",
        "warnings": target_warnings + partition_warnings,
        "workflow_mode": "bookrag on-demand jobs selected tables debug",
        "inserted_rows": inserted_rows,
        "debug_dir": str(debug_dir) if debug_dir else "",
        "debug_files": debug_files,
        "bookrag_raw_stage_dir": str(raw_stage_dir),
        "bookrag_raw_stage_files": raw_stage_files,
        "bookrag_csv_stage_dir": str(csv_stage_dir),
        "bookrag_csv_stage_files": sorted(str(path) for path in csv_stage_dir.rglob("*.csv")),
        "effective_partition_strategy": partition_strategy,
        "effective_ocr_languages": ocr_languages,
        "include_orig_elements": False,
        "file_mode": "on-demand-jobs",
        "bookrag_tables": persisted_bookrag_tables,
        "bookrag_table_generation": table_generation,
        "bookrag_flush_config": flush_config,
        "bookrag_unstructured_workers": unstructured_workers,
        "bookrag_csv_prepare_workers": csv_prepare_workers,
        "bookrag_csv_load_workers": csv_load_workers,
        "bookrag_flush_count": len(flush_batches),
        "bookrag_flush_batches": flush_batches,
        "bookrag_persisted_table_row_counts": persisted_table_row_counts,
        "bookrag_profile": processing_profile,
        "bookrag_document_insert_stats": document_insert_stats,
        "bookrag_raw_insert_stats": raw_insert_stats,
        "bookrag_block_insert_stats": block_insert_stats,
        "bookrag_node_insert_stats": node_insert_stats,
        "bookrag_entity_insert_stats": entity_insert_stats,
        "bookrag_entity_link_insert_stats": entity_link_insert_stats,
        "bookrag_entity_relation_insert_stats": entity_relation_insert_stats,
        "bookrag_document_relation_insert_stats": document_relation_insert_stats,
        "bookrag_insert_stats": raw_insert_stats,
        "bookrag_image_partition_parameters": image_partition_summary,
        "bookrag_chunking_strategy": "disabled_for_tree_debug",
    }
    return patched_payload, summary


def apply_multi_format_pipeline(
    exec_payload: dict,
    create_values: dict[str, str],
    vector_store_name: str,
    connection_params: dict | None = None,
    *,
    execute_sql_fn: ExecuteSqlFn | None,
    resolve_path_hint: Callable[[str], str],
    pipeline_mode: str = "multi_format",
) -> tuple[dict, dict]:
    connection_params = connection_params or {}

    raw_doc_files = exec_payload.get("document_files")
    if isinstance(raw_doc_files, str):
        document_files = [raw_doc_files]
    elif isinstance(raw_doc_files, (list, tuple, set)):
        document_files = [str(item).strip() for item in raw_doc_files if str(item).strip()]
    else:
        document_files = []
    if not document_files:
        raise RuntimeError("multi format requires at least one document file.")

    include_orig_elements = False
    overlap_all = True

    if pipeline_mode == "multi_format_bookrag":
        partition_strategy = _resolve_partition_strategy(create_values.get("multi_format_bookrag_strategy", "auto"))
        ocr_languages = _parse_langs(create_values.get("multi_format_bookrag_ocr_languages", ""))
    else:
        partition_strategy = _resolve_partition_strategy(create_values.get("multi_format_strategy", "auto"))
        if partition_strategy == "ocr_only":
            raise RuntimeError(
                "Multi-Format does not expose 'ocr_only' as a supported workflow route because the current Unstructured workflow docs do not document it as an official workflow partition route. Use hi_res or vlm instead."
            )
        ocr_languages = _parse_langs(create_values.get("multi_format_ocr_languages", ""))
        chunk_size = _to_int(create_values.get("multi_format_chunk_size", "600"), default=600, minimum=100, maximum=8000)
        chunk_overlap = _to_int(create_values.get("multi_format_chunk_overlap", "80"), default=80, minimum=0, maximum=2000)
        if chunk_overlap >= chunk_size:
            chunk_overlap = max(0, chunk_size // 5)

    table_name, schema_name, qualified_name, target_warnings = _resolve_multi_format_table_target(
        exec_payload,
        create_values,
        vector_store_name,
    )
    database_name = schema_name or str(create_values.get("target_database", "")).strip()
    if not database_name:
        database_name = str(connection_params.get("username", "")).strip()
        if database_name:
            target_warnings.append(f"multi format target_database not set; fallback to '{database_name}'.")
    if not database_name:
        raise RuntimeError("multi format requires target_database (or object_names with schema prefix).")

    effective_schema_name = schema_name
    if not effective_schema_name:
        effective_schema_name = _sanitize_teradata_identifier(database_name, fallback="", allow_empty=True) or None
        if effective_schema_name:
            qualified_name = f"{effective_schema_name}.{table_name}"

    if pipeline_mode == "multi_format_bookrag":
        return _apply_bookrag_tree_pipeline(
            exec_payload=exec_payload,
            create_values=create_values,
            vector_store_name=vector_store_name,
            execute_sql_fn=execute_sql_fn,
            resolve_path_hint=resolve_path_hint,
            effective_schema_name=effective_schema_name,
            document_files=document_files,
            partition_strategy=partition_strategy,
            ocr_languages=ocr_languages,
            target_warnings=target_warnings,
            connection_params=connection_params,
        )

    target_warnings.extend(
        _ensure_unstructured_teradata_table(
            schema_name=effective_schema_name,
            table_name=table_name,
            execute_sql_fn=execute_sql_fn,
            clear_rows=True,
        )
    )

    api_key, api_url = _load_unstructured_runtime_config(connection_params)
    request_timeout_ms = _resolve_unstructured_request_timeout_ms()
    client = _create_unstructured_client(api_key=api_key, api_url=api_url, timeout_ms=request_timeout_ms)
    runtime_settings = _load_unstructured_runtime_settings()
    timeout_seconds, poll_interval_seconds = _resolve_multi_format_workflow_poll_config()
    debug_dir = _prepare_unstructured_debug_dir(vector_store_name)
    inserted_rows = 0
    partition_warnings: list[str] = []
    debug_files: list[str] = []
    used_partition_strategies: set[str] = set()
    used_ocr_languages: set[tuple[str, ...]] = set()
    scan_ocr_fallback_files: list[str] = []
    job_ids: list[str] = []
    workflow_ids: list[str] = []
    workflow_names_seen: list[str] = []
    last_workflow_name = ""
    last_job_submitted_at: float | None = None
    chunk_sequence = 0
    for path_hint in document_files:
        resolved = resolve_path_hint(path_hint)
        src = Path(resolved)
        if not src.exists() or not src.is_file():
            raise RuntimeError(f"multi format source file is missing: {path_hint}")
        (
            file_partition_strategy,
            file_ocr_languages,
            file_include_orig_elements,
            file_partition_warnings,
            scan_ocr_fallback_applied,
        ) = _multi_format_partition_options_for_file(
            src,
            default_strategy=partition_strategy,
            default_languages=ocr_languages,
            include_orig_elements=include_orig_elements,
        )
        partition_warnings.extend(file_partition_warnings)
        request_parameters, workflow_definition_warnings, processing_profile = _workflow_builder_build_multi_format_workflow_definition(
            create_values=create_values,
            src=src,
            partition_strategy=file_partition_strategy,
            languages=file_ocr_languages,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            include_orig_elements=file_include_orig_elements,
            overlap_all=overlap_all,
            runtime=runtime_settings,
        )
        partition_warnings.extend(workflow_definition_warnings)
        last_workflow_name = str(request_parameters.get("workflow_name") or "").strip()
        used_partition_strategies.add(file_partition_strategy)
        used_ocr_languages.add(tuple(file_ocr_languages))
        if scan_ocr_fallback_applied:
            scan_ocr_fallback_files.append(src.name)

        last_job_submitted_at = _enforce_unstructured_job_submission_spacing(last_job_submitted_at)
        raw_output_payload, raw_elements, request_parameters_with_job, job_id, workflow_id, workflow_name_for_job = _run_unstructured_workflow_job_for_file(
            client,
            request_parameters=request_parameters,
            src=src,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            api_key=api_key,
            api_url=api_url,
        )
        job_ids.append(job_id)
        if workflow_id:
            workflow_ids.append(workflow_id)
        if workflow_name_for_job:
            workflow_names_seen.append(workflow_name_for_job)

        rows: list[dict[str, Any]] = []
        content_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        for element in raw_elements:
            if not isinstance(element, dict):
                continue
            chunk_sequence += 1
            row = _element_to_chunk_row(element, src=src, content_type=content_type, row_sequence=chunk_sequence)
            if row:
                rows.append(row)

        debug_file = _write_unstructured_debug_file(
            debug_dir,
            src,
            raw_elements,
            rows,
            request_parameters_with_job,
            extra_payload={
                **_workflow_debug_payload(
                    request_parameters_with_job,
                    processing_profile=processing_profile,
                    workflow_id=workflow_id,
                    workflow_name=workflow_name_for_job or last_workflow_name,
                    job_id=job_id,
                    workflow_kind="multi_format",
                ),
                "job_output_payload": raw_output_payload,
            },
        )
        if debug_file:
            debug_files.append(debug_file)
        if not rows:
            partition_warnings.append(f"No chunks extracted from file: {src.name}")
            continue
        inserted_rows += _insert_chunk_rows(
            schema_name=effective_schema_name,
            table_name=table_name,
            rows=rows,
            execute_sql_fn=execute_sql_fn,
        )
    flush_wait_seconds = _to_int(
        os.getenv("UNSTRUCTURED_TERADATA_FLUSH_WAIT_SECONDS", str(UNSTRUCTURED_TERADATA_FLUSH_WAIT_SECONDS_DEFAULT)),
        default=UNSTRUCTURED_TERADATA_FLUSH_WAIT_SECONDS_DEFAULT,
        minimum=1,
        maximum=300,
    )
    flush_wait_interval_seconds = _to_int(
        os.getenv("UNSTRUCTURED_TERADATA_FLUSH_WAIT_INTERVAL", str(UNSTRUCTURED_TERADATA_FLUSH_WAIT_INTERVAL_DEFAULT)),
        default=UNSTRUCTURED_TERADATA_FLUSH_WAIT_INTERVAL_DEFAULT,
        minimum=1,
        maximum=30,
    )
    chunk_count = _wait_for_table_rows(
        schema_name=effective_schema_name,
        table_name=table_name,
        execute_sql_fn=execute_sql_fn,
        timeout_seconds=flush_wait_seconds,
        poll_interval_seconds=flush_wait_interval_seconds,
    )
    if chunk_count <= 0:
        raise RuntimeError(
            "multi format partition completed but destination table has 0 rows. "
            f"table={qualified_name}"
        )

    patched_payload = _strip_file_based_create_params(exec_payload)
    patched_payload["target_database"] = effective_schema_name or database_name
    patched_payload["object_names"] = table_name
    patched_payload["data_columns"] = ["text"]
    patched_payload.setdefault("key_columns", ["id"])

    # multi format already writes chunked content into Teradata.
    # For content-based vector store creation, chunking inputs must be omitted.

    strategy_label = (
        next(iter(used_partition_strategies))
        if len(used_partition_strategies) == 1
        else ("mixed" if used_partition_strategies else "")
    )
    languages_label = ""
    if len(used_ocr_languages) == 1:
        languages_label = ",".join(next(iter(used_ocr_languages)))
    elif used_ocr_languages:
        languages_label = "mixed"
    summary = {
        "table_name": qualified_name,
        "chunk_count": chunk_count,
        "document_count": len(document_files),
        "job_id": job_ids[-1] if job_ids else "",
        "workflow_id": workflow_ids[-1] if workflow_ids else "",
        "workflow_name": workflow_names_seen[-1] if workflow_names_seen else last_workflow_name,
        "destination_id": "",
        "warnings": target_warnings + partition_warnings,
        "workflow_mode": "multi format on-demand jobs direct insert",
        "inserted_rows": inserted_rows,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "debug_dir": str(debug_dir) if debug_dir else "",
        "debug_files": debug_files,
        "effective_partition_strategy": partition_strategy,
        "effective_ocr_languages": ocr_languages,
        "effective_partition_strategy_label": strategy_label,
        "effective_ocr_languages_label": languages_label,
        "scan_ocr_fallback_files": scan_ocr_fallback_files,
        "include_orig_elements": include_orig_elements,
        "overlap_all": overlap_all,
        "file_mode": "on-demand-jobs",
    }
    return patched_payload, summary
