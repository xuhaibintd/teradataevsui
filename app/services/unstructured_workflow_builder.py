from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.integrations.unstructured.contracts import validate_workflow_nodes


def _to_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    return default


def _to_int(raw: Any, *, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _to_float(raw: Any, *, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(str(raw).strip())
    except Exception:
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _parse_csv_values(raw: Any) -> list[str]:
    return [chunk.strip() for chunk in str(raw or "").split(",") if chunk.strip()]


def _first_defined(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _infer_provider_from_model_name(raw_model: str) -> str:
    model = str(raw_model or "").strip().lower()
    if not model:
        return ""
    if model.startswith("gpt-"):
        return "openai"
    if model.startswith("gemini-"):
        return "vertexai"
    if model.startswith("us."):
        return "bedrock"
    if model.startswith("claude-"):
        return "anthropic"
    return ""


def _model_provider_conflicts(explicit_provider: str, inferred_provider: str) -> bool:
    explicit = str(explicit_provider or "").strip().lower()
    inferred = str(inferred_provider or "").strip().lower()
    if not explicit or not inferred:
        return False
    if inferred == "openai" and explicit in {"openai", "azure_openai"}:
        return False
    return explicit != inferred


_TWOPASS_PROMPTER_SUBTYPES = {"twopass_image_description", "twopass_table2html"}
_PROMPTER_SUBTYPE_PROVIDER = {
    "openai_image_description": "openai",
    "anthropic_image_description": "anthropic",
    "bedrock_image_description": "bedrock",
    "vertexai_image_description": "vertexai",
    "openai_table_description": "openai",
    "anthropic_table_description": "anthropic",
    "bedrock_table_description": "bedrock",
    "vertexai_table_description": "vertexai",
    "openai_table2html": "openai",
    "anthropic_table2html": "anthropic",
    "openai_ocr": "openai",
    "anthropic_ocr": "anthropic",
    "bedrock_ocr": "bedrock",
    "openai_ner": "openai",
    "anthropic_ner": "anthropic",
}
_DEFAULT_PROMPTER_MODELS = {
    "openai": "gpt-5-mini",
    "azure_openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-4-6",
    "bedrock": "us.amazon.nova-lite-v1:0",
    "vertexai": "gemini-2.5-flash",
}
_GENERATIVE_OCR_TEXT_TYPES = ("Text", "NarrativeText", "Title", "ListItem", "UncategorizedText")


def _resolve_prompter_settings(*, subtype: str, provider_type: str, model: str) -> dict[str, Any]:
    subtype = str(subtype or "").strip()
    if subtype in _TWOPASS_PROMPTER_SUBTYPES:
        return {}

    expected_provider = _PROMPTER_SUBTYPE_PROVIDER.get(subtype, "")
    provider_type = str(provider_type or expected_provider).strip().lower()
    if expected_provider and provider_type not in {expected_provider, "azure_openai" if expected_provider == "openai" else expected_provider}:
        raise ValueError(
            f"Unstructured subtype '{subtype}' does not match provider_type '{provider_type}'."
        )

    model = str(model or "").strip()
    inferred_provider = _infer_provider_from_model_name(model)
    if inferred_provider and _model_provider_conflicts(provider_type, inferred_provider):
        raise ValueError(
            f"Unstructured model '{model}' does not match provider_type '{provider_type}' for subtype '{subtype}'."
        )
    if not model:
        model = _DEFAULT_PROMPTER_MODELS.get(provider_type, "")
    if not provider_type or not model:
        raise ValueError(f"Unstructured subtype '{subtype}' requires provider_type and model.")
    return {"provider_type": provider_type, "model": model}


def _partition_route_label(partition_node: dict[str, Any]) -> str:
    settings = partition_node.get("settings") if isinstance(partition_node.get("settings"), dict) else {}
    if partition_node.get("subtype") == "vlm":
        return "auto" if settings.get("is_dynamic") is True else "vlm"
    return str(settings.get("strategy") or "unknown")


def _normalize_bookrag_workflow_name(raw_name: str | None) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return "bookrag_raw_prod"
    return re.sub(r"\s+", "_", name)


def _normalize_multi_format_workflow_name(raw_name: str | None) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return "multi_format_prod"
    return re.sub(r"\s+", "_", name)


def _resolve_multi_format_accuracy_options(
    create_values: dict[str, str],
    *,
    runtime: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    warnings: list[str] = []

    infer_table_structure = _to_bool(
        _first_defined(
            create_values.get("multi_format_infer_table_structure", ""),
            runtime.get("multi_format_infer_table_structure"),
            os.getenv("MULTI_FORMAT_INFER_TABLE_STRUCTURE", "false"),
        ),
        default=False,
    )
    hi_res_model_name = str(
        _first_defined(
            create_values.get("multi_format_hi_res_model_name", ""),
            runtime.get("multi_format_hi_res_model_name"),
            os.getenv("MULTI_FORMAT_HI_RES_MODEL_NAME", ""),
        )
        or ""
    ).strip()
    vlm_provider = str(
        _first_defined(
            create_values.get("multi_format_vlm_provider", ""),
            runtime.get("multi_format_vlm_provider"),
            os.getenv("MULTI_FORMAT_VLM_PROVIDER", ""),
        )
        or ""
    ).strip()
    vlm_model = str(
        _first_defined(
            create_values.get("multi_format_vlm_model", ""),
            runtime.get("multi_format_vlm_model"),
            os.getenv("MULTI_FORMAT_VLM_MODEL", ""),
        )
        or ""
    ).strip()
    vlm_provider_api_key = str(
        _first_defined(
            create_values.get("multi_format_vlm_provider_api_key", ""),
            runtime.get("multi_format_vlm_provider_api_key"),
            os.getenv("MULTI_FORMAT_VLM_PROVIDER_API_KEY", ""),
        )
        or ""
    ).strip()

    enable_generative_ocr = _to_bool(
        _first_defined(
            create_values.get("multi_format_enable_generative_ocr", ""),
            runtime.get("multi_format_enable_generative_ocr"),
            os.getenv("MULTI_FORMAT_ENABLE_GENERATIVE_OCR", "false"),
        ),
        default=False,
    )
    enable_table_to_html = _to_bool(
        _first_defined(
            create_values.get("multi_format_enable_table_to_html", ""),
            runtime.get("multi_format_enable_table_to_html"),
            os.getenv("MULTI_FORMAT_ENABLE_TABLE_TO_HTML", "false"),
        ),
        default=False,
    )
    enable_table_description = _to_bool(
        _first_defined(
            create_values.get("multi_format_enable_table_description", ""),
            runtime.get("multi_format_enable_table_description"),
            os.getenv("MULTI_FORMAT_ENABLE_TABLE_DESCRIPTION", "false"),
        ),
        default=False,
    )
    enable_image_description = _to_bool(
        _first_defined(
            create_values.get("multi_format_enable_image_description", ""),
            runtime.get("multi_format_enable_image_description"),
            os.getenv("MULTI_FORMAT_ENABLE_IMAGE_DESCRIPTION", "false"),
        ),
        default=False,
    )

    raw_extract_types = str(
        _first_defined(
            create_values.get("multi_format_extract_image_block_types", ""),
            runtime.get("multi_format_extract_image_block_types"),
            os.getenv("MULTI_FORMAT_EXTRACT_IMAGE_BLOCK_TYPES", "auto"),
        )
        or "auto"
    ).strip()
    if raw_extract_types.lower() == "auto":
        extract_image_block_types: list[str] = []
        if enable_table_to_html or enable_table_description:
            extract_image_block_types.append("Table")
        if enable_image_description:
            extract_image_block_types.append("Image")
        if enable_generative_ocr:
            extract_image_block_types.extend(_GENERATIVE_OCR_TEXT_TYPES)
    else:
        extract_image_block_types = _parse_csv_values(raw_extract_types)
    normalized_extract_types: list[str] = []
    for item in extract_image_block_types:
        value = str(item or "").strip()
        if value and value not in normalized_extract_types:
            normalized_extract_types.append(value)

    partition_options: dict[str, Any] = {
        "infer_table_structure": infer_table_structure,
        "extract_image_block_types": normalized_extract_types,
        "hi_res_model_name": hi_res_model_name or None,
        "vlm_provider": vlm_provider or None,
        "vlm_model": vlm_model or None,
        "vlm_provider_api_key": vlm_provider_api_key or None,
        "unique_element_ids": True,
    }

    def _provider_settings(prefix: str, *, enabled: bool, default_subtype: str, default_provider: str, default_model: str) -> tuple[str, dict[str, Any]]:
        subtype = str(
            create_values.get(f"multi_format_{prefix}_subtype", "")
            or runtime.get(f"multi_format_{prefix}_subtype")
            or os.getenv(f"MULTI_FORMAT_{prefix.upper()}_SUBTYPE", default_subtype)
        ).strip() or default_subtype
        provider_type = str(
            create_values.get(f"multi_format_{prefix}_provider_type", "")
            or runtime.get(f"multi_format_{prefix}_provider_type")
            or os.getenv(f"MULTI_FORMAT_{prefix.upper()}_PROVIDER_TYPE", "")
            or ""
        ).strip()
        model = str(
            create_values.get(f"multi_format_{prefix}_model", "")
            or runtime.get(f"multi_format_{prefix}_model")
            or os.getenv(f"MULTI_FORMAT_{prefix.upper()}_MODEL", "")
            or ""
        ).strip()
        if not enabled:
            return subtype, {}
        return subtype, _resolve_prompter_settings(
            subtype=subtype,
            provider_type=provider_type or default_provider,
            model=model or default_model,
        )

    enrichment_options = {
        "enable_generative_ocr": enable_generative_ocr,
        "enable_table_to_html": enable_table_to_html,
        "enable_table_description": enable_table_description,
        "enable_image_description": enable_image_description,
    }
    subtype, settings = _provider_settings(
        "generative_ocr",
        enabled=enable_generative_ocr,
        default_subtype="openai_ocr",
        default_provider="openai",
        default_model="gpt-5-mini",
    )
    enrichment_options["generative_ocr_subtype"] = subtype
    enrichment_options["generative_ocr_settings"] = settings
    subtype, settings = _provider_settings(
        "table_to_html",
        enabled=enable_table_to_html,
        default_subtype="twopass_table2html",
        default_provider="",
        default_model="",
    )
    enrichment_options["table_to_html_subtype"] = subtype
    enrichment_options["table_to_html_settings"] = settings
    subtype, settings = _provider_settings(
        "table_description",
        enabled=enable_table_description,
        default_subtype="openai_table_description",
        default_provider="openai",
        default_model="gpt-5-mini",
    )
    enrichment_options["table_description_subtype"] = subtype
    enrichment_options["table_description_settings"] = settings
    subtype, settings = _provider_settings(
        "image_description",
        enabled=enable_image_description,
        default_subtype="openai_image_description",
        default_provider="openai",
        default_model="gpt-5-mini",
    )
    enrichment_options["image_description_subtype"] = subtype
    enrichment_options["image_description_settings"] = settings

    return partition_options, enrichment_options, warnings


def _build_multi_format_workflow_partition_node(
    *,
    src: Path,
    partition_strategy: str,
    languages: list[str],
    partition_options: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    partition_options = partition_options or {}
    extract_image_block_types = partition_options.get("extract_image_block_types") or []
    hi_res_model_name = str(partition_options.get("hi_res_model_name") or "").strip()
    infer_table_structure = bool(partition_options.get("infer_table_structure"))
    unique_element_ids = bool(partition_options.get("unique_element_ids", True))
    vlm_provider = str(partition_options.get("vlm_provider") or "").strip()
    vlm_model = str(partition_options.get("vlm_model") or "").strip()
    vlm_provider_api_key = str(partition_options.get("vlm_provider_api_key") or "").strip()
    requested_strategy = (partition_strategy or "auto").strip().lower() or "auto"
    inferred_vlm_provider = _infer_provider_from_model_name(vlm_model)
    if inferred_vlm_provider:
        if not vlm_provider:
            vlm_provider = inferred_vlm_provider
            warnings.append(
                f"multi format VLM provider inferred as '{vlm_provider}' from model '{vlm_model}'."
            )
        elif _model_provider_conflicts(vlm_provider, inferred_vlm_provider):
            warnings.append(
                f"multi format VLM provider '{vlm_provider}' does not match model '{vlm_model}'; overriding provider to '{inferred_vlm_provider}'."
            )
            vlm_provider = inferred_vlm_provider

    if requested_strategy == "auto":
        settings: dict[str, Any] = {
            "output_format": "application/json",
            "format_html": False,
            "unique_element_ids": unique_element_ids,
            "is_dynamic": True,
            "allow_fast": True,
        }
        if vlm_provider:
            settings["provider"] = vlm_provider
        if vlm_model:
            settings["model"] = vlm_model
        if vlm_provider_api_key:
            settings["provider_api_key"] = vlm_provider_api_key
        return {
            "name": "Partitioner",
            "type": "partition",
            "subtype": "vlm",
            "settings": settings,
        }, warnings

    if requested_strategy == "vlm":
        settings = {
            "output_format": "application/json",
            "format_html": False,
            "unique_element_ids": unique_element_ids,
            "is_dynamic": False,
            "allow_fast": False,
        }
        if vlm_provider:
            settings["provider"] = vlm_provider
        if vlm_model:
            settings["model"] = vlm_model
        if vlm_provider_api_key:
            settings["provider_api_key"] = vlm_provider_api_key
        return {
            "name": "Partitioner",
            "type": "partition",
            "subtype": "vlm",
            "settings": settings,
        }, warnings

    settings = {
        "strategy": requested_strategy,
        "include_page_breaks": False,
        "unique_element_ids": unique_element_ids,
    }
    if languages:
        settings["ocr_languages"] = languages
    if extract_image_block_types:
        settings["extract_image_block_types"] = extract_image_block_types
    if requested_strategy == "hi_res" and infer_table_structure:
        settings["pdf_infer_table_structure"] = True
        settings["infer_table_structure"] = True
    if hi_res_model_name and requested_strategy == "hi_res":
        settings["hi_res_model_name"] = hi_res_model_name
    return {
        "name": "Partitioner",
        "type": "partition",
        "subtype": "unstructured_api",
        "settings": settings,
    }, warnings


def _build_multi_format_enrichment_nodes(*, enrichment_options: dict[str, Any], partition_strategy: str) -> tuple[list[dict[str, Any]], list[str]]:
    requested_strategy = (partition_strategy or "auto").strip().lower() or "auto"
    if requested_strategy not in {"auto", "hi_res"}:
        return [], []

    nodes: list[dict[str, Any]] = []
    if enrichment_options.get("enable_image_description"):
        nodes.append({
            "name": "Image Description",
            "type": "prompter",
            "subtype": str(enrichment_options.get("image_description_subtype") or "openai_image_description"),
            "settings": dict(enrichment_options.get("image_description_settings") or {}),
        })
    if enrichment_options.get("enable_table_to_html"):
        nodes.append({
            "name": "Table to HTML",
            "type": "prompter",
            "subtype": str(enrichment_options.get("table_to_html_subtype") or "twopass_table2html"),
            "settings": dict(enrichment_options.get("table_to_html_settings") or {}),
        })
    if enrichment_options.get("enable_table_description"):
        nodes.append({
            "name": "Table Description",
            "type": "prompter",
            "subtype": str(enrichment_options.get("table_description_subtype") or "openai_table_description"),
            "settings": dict(enrichment_options.get("table_description_settings") or {}),
        })
    if enrichment_options.get("enable_generative_ocr"):
        nodes.append({
            "name": "Generative OCR",
            "type": "prompter",
            "subtype": str(enrichment_options.get("generative_ocr_subtype") or "openai_ocr"),
            "settings": dict(enrichment_options.get("generative_ocr_settings") or {}),
        })

    return nodes, []


def build_bookrag_workflow_partition_node(
    *,
    src: Path,
    partition_strategy: str,
    languages: list[str],
    image_partition_parameters: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    warnings: list[str] = []
    image_partition_parameters = image_partition_parameters or {}
    extract_image_block_types = image_partition_parameters.get("extract_image_block_types") or []
    normalized_extract_types: list[str] = []
    for item in extract_image_block_types:
        value = str(item or "").strip()
        if not value:
            continue
        normalized = "Image" if value.lower() == "image" else "Table" if value.lower() == "table" else value
        if normalized not in normalized_extract_types:
            normalized_extract_types.append(normalized)

    hi_res_model_name = str(image_partition_parameters.get("hi_res_model_name") or "").strip()
    infer_table_structure = bool(image_partition_parameters.get("infer_table_structure"))
    requested_strategy = (partition_strategy or "auto").strip().lower() or "auto"
    unique_element_ids = bool(image_partition_parameters.get("unique_element_ids", True))
    coordinates = bool(image_partition_parameters.get("coordinates", True))
    vlm_provider = str(image_partition_parameters.get("vlm_provider") or "").strip()
    vlm_model = str(image_partition_parameters.get("vlm_model") or "").strip()
    vlm_provider_api_key = str(image_partition_parameters.get("vlm_provider_api_key") or "").strip()
    inferred_vlm_provider = _infer_provider_from_model_name(vlm_model)
    if inferred_vlm_provider:
        if not vlm_provider:
            vlm_provider = inferred_vlm_provider
            warnings.append(
                f"bookrag VLM provider inferred as '{vlm_provider}' from model '{vlm_model}'."
            )
        elif _model_provider_conflicts(vlm_provider, inferred_vlm_provider):
            warnings.append(
                f"bookrag VLM provider '{vlm_provider}' does not match model '{vlm_model}'; overriding provider to '{inferred_vlm_provider}'."
            )
            vlm_provider = inferred_vlm_provider

    if requested_strategy == "auto":
        if languages:
            warnings.append(
                f"bookrag ocr_languages for {src.name} are ignored when workflow strategy='auto'; Unstructured auto routing controls OCR internally."
            )
        if normalized_extract_types:
            warnings.append(
                f"bookrag extract_image_block_types for {src.name} are ignored when workflow strategy='auto'; downstream enrichment nodes handle matching elements automatically."
            )
        if infer_table_structure:
            warnings.append(
                f"bookrag infer_table_structure for {src.name} is ignored when workflow strategy='auto'; use the Table to HTML enrichment node instead."
            )
        settings: dict[str, Any] = {
            "output_format": "application/json",
            "format_html": False,
            "unique_element_ids": unique_element_ids,
            "is_dynamic": True,
            "allow_fast": True,
        }
        if vlm_provider:
            settings["provider"] = vlm_provider
        if vlm_model:
            settings["model"] = vlm_model
        if vlm_provider_api_key:
            settings["provider_api_key"] = vlm_provider_api_key
        workflow_node = {
            "name": "Partitioner",
            "type": "partition",
            "subtype": "vlm",
            "settings": settings,
        }
    elif requested_strategy == "vlm":
        settings = {
            "output_format": "application/json",
            "format_html": False,
            "unique_element_ids": unique_element_ids,
            "is_dynamic": False,
            "allow_fast": False,
        }
        if vlm_provider:
            settings["provider"] = vlm_provider
        if vlm_model:
            settings["model"] = vlm_model
        if vlm_provider_api_key:
            settings["provider_api_key"] = vlm_provider_api_key
        workflow_node = {
            "name": "Partitioner",
            "type": "partition",
            "subtype": "vlm",
            "settings": settings,
        }
    else:
        settings = {
            "strategy": requested_strategy,
            "include_page_breaks": False,
            "unique_element_ids": unique_element_ids,
        }
        if requested_strategy == "hi_res":
            settings["coordinates"] = coordinates
        if languages:
            settings["ocr_languages"] = languages
        if normalized_extract_types:
            settings["extract_image_block_types"] = normalized_extract_types
        if requested_strategy == "hi_res" and infer_table_structure:
            settings["pdf_infer_table_structure"] = True
            settings["infer_table_structure"] = True
        if hi_res_model_name and requested_strategy == "hi_res":
            settings["hi_res_model_name"] = hi_res_model_name
        if requested_strategy not in {"hi_res", "vlm"} and infer_table_structure:
            warnings.append(
                f"bookrag infer_table_structure was requested for {src.name} but is only enabled when strategy='hi_res' or 'vlm'."
            )
        workflow_node = {
            "name": "Partitioner",
            "type": "partition",
            "subtype": "unstructured_api",
            "settings": settings,
        }

    request_parameters = {
        "source_file": str(src),
        "workflow_type": "custom",
        "workflow_nodes": [workflow_node],
    }
    validate_workflow_nodes(request_parameters["workflow_nodes"])
    return workflow_node, request_parameters, warnings


def build_bookrag_reusable_workflow_definition(
    *,
    create_values: dict[str, str],
    partition_strategy: str,
    languages: list[str],
    image_partition_parameters: dict[str, Any] | None,
    runtime: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, Any], list[str], str]:
    warnings: list[str] = []
    image_partition_parameters = image_partition_parameters or {}

    workflow_name = _normalize_bookrag_workflow_name(
        _first_defined(
            create_values.get("multi_format_bookrag_workflow_name"),
            runtime.get("bookrag_workflow_name"),
            os.getenv("BOOKRAG_WORKFLOW_NAME"),
            "bookrag_raw_prod",
        )
    )

    enable_image_description = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_enable_image_description", ""),
            runtime.get("bookrag_enable_image_description"),
            os.getenv("BOOKRAG_ENABLE_IMAGE_DESCRIPTION", "false"),
        ),
        default=False,
    )
    enable_table_to_html = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_enable_table_to_html", ""),
            runtime.get("bookrag_enable_table_to_html"),
            os.getenv("BOOKRAG_ENABLE_TABLE_TO_HTML", "false"),
        ),
        default=False,
    )
    enable_table_description = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_enable_table_description", ""),
            runtime.get("bookrag_enable_table_description"),
            os.getenv("BOOKRAG_ENABLE_TABLE_DESCRIPTION", "false"),
        ),
        default=False,
    )
    enable_generative_ocr = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_enable_generative_ocr", ""),
            runtime.get("bookrag_enable_generative_ocr"),
            os.getenv("BOOKRAG_ENABLE_GENERATIVE_OCR", "false"),
        ),
        default=False,
    )
    enable_ner = _to_bool(
        _first_defined(
            create_values.get("multi_format_bookrag_enable_ner", ""),
            runtime.get("bookrag_enable_ner"),
            os.getenv("BOOKRAG_ENABLE_NER", "false"),
        ),
        default=False,
    )

    image_subtype = str(
        _first_defined(
            create_values.get("multi_format_bookrag_image_description_subtype", ""),
            runtime.get("bookrag_image_description_subtype"),
            os.getenv("BOOKRAG_IMAGE_DESCRIPTION_SUBTYPE", "openai_image_description"),
        )
        or "openai_image_description"
    ).strip() or "openai_image_description"
    table_to_html_subtype = str(
        _first_defined(
            create_values.get("multi_format_bookrag_table_to_html_subtype", ""),
            runtime.get("bookrag_table_to_html_subtype"),
            os.getenv("BOOKRAG_TABLE_TO_HTML_SUBTYPE", "openai_table2html"),
        )
        or "openai_table2html"
    ).strip() or "openai_table2html"
    table_description_subtype = str(
        _first_defined(
            create_values.get("multi_format_bookrag_table_description_subtype", ""),
            runtime.get("bookrag_table_description_subtype"),
            os.getenv("BOOKRAG_TABLE_DESCRIPTION_SUBTYPE", "openai_table_description"),
        )
        or "openai_table_description"
    ).strip() or "openai_table_description"
    generative_ocr_subtype = str(
        _first_defined(
            create_values.get("multi_format_bookrag_generative_ocr_subtype", ""),
            runtime.get("bookrag_generative_ocr_subtype"),
            os.getenv("BOOKRAG_GENERATIVE_OCR_SUBTYPE", "openai_ocr"),
        )
        or "openai_ocr"
    ).strip() or "openai_ocr"
    ner_subtype = str(
        _first_defined(
            create_values.get("multi_format_bookrag_ner_subtype", ""),
            runtime.get("bookrag_ner_subtype"),
            os.getenv("BOOKRAG_NER_SUBTYPE", "openai_ner"),
        )
        or "openai_ner"
    ).strip() or "openai_ner"
    def _bookrag_prompter_settings(prefix: str, subtype: str) -> dict[str, Any]:
        provider_type = str(
            _first_defined(
                create_values.get(f"multi_format_bookrag_{prefix}_provider_type", ""),
                runtime.get(f"bookrag_{prefix}_provider_type"),
                os.getenv(f"BOOKRAG_{prefix.upper()}_PROVIDER_TYPE", ""),
            )
            or ""
        ).strip()
        model = str(
            _first_defined(
                create_values.get(f"multi_format_bookrag_{prefix}_model", ""),
                runtime.get(f"bookrag_{prefix}_model"),
                os.getenv(f"BOOKRAG_{prefix.upper()}_MODEL", ""),
            )
            or ""
        ).strip()
        return _resolve_prompter_settings(subtype=subtype, provider_type=provider_type, model=model)

    partition_node, _, partition_warnings = build_bookrag_workflow_partition_node(
        src=Path("bookrag_document"),
        partition_strategy=partition_strategy or "auto",
        languages=languages,
        image_partition_parameters=image_partition_parameters,
    )
    warnings.extend(partition_warnings)
    if _partition_route_label(partition_node) == "vlm":
        redundant_enrichments = [
            label
            for enabled, label in (
                (enable_image_description, "image description"),
                (enable_table_to_html, "table to HTML"),
                (enable_table_description, "table description"),
                (enable_generative_ocr, "generative OCR"),
            )
            if enabled
        ]
        if redundant_enrichments:
            warnings.append(
                "bookrag explicit VLM partition already performs "
                + ", ".join(redundant_enrichments)
                + "; redundant enrichment nodes were omitted."
            )
        enable_image_description = False
        enable_table_to_html = False
        enable_table_description = False
        enable_generative_ocr = False

    workflow_nodes: list[dict[str, Any]] = [partition_node]
    partition_strategy_label = _partition_route_label(partition_node)
    partition_subtype_label = partition_node.get('subtype', '') or 'unknown'
    profile_parts = [f"partition:{partition_subtype_label}:{partition_strategy_label}"]
    if enable_image_description:
        workflow_nodes.append({
            "name": "Image Description",
            "type": "prompter",
            "subtype": image_subtype,
            "settings": _bookrag_prompter_settings("image_description", image_subtype),
        })
        profile_parts.append("image_description")
    if enable_table_to_html:
        workflow_nodes.append({
            "name": "Table to HTML",
            "type": "prompter",
            "subtype": table_to_html_subtype,
            "settings": _bookrag_prompter_settings("table_to_html", table_to_html_subtype),
        })
        profile_parts.append("table_to_html")
    if enable_table_description:
        workflow_nodes.append({
            "name": "Table Description",
            "type": "prompter",
            "subtype": table_description_subtype,
            "settings": _bookrag_prompter_settings("table_description", table_description_subtype),
        })
        profile_parts.append("table_description")
    if enable_generative_ocr:
        workflow_nodes.append({
            "name": "Generative OCR",
            "type": "prompter",
            "subtype": generative_ocr_subtype,
            "settings": _bookrag_prompter_settings("generative_ocr", generative_ocr_subtype),
        })
        profile_parts.append("generative_ocr")
    if enable_ner:
        workflow_nodes.append({
            "name": "Named Entity Recognition",
            "type": "prompter",
            "subtype": ner_subtype,
            "settings": _bookrag_prompter_settings("ner", ner_subtype),
        })
        profile_parts.append(f"ner:{ner_subtype}")

    request_parameters = {
        "workflow_type": "custom",
        "workflow_name": workflow_name,
        "workflow_nodes": workflow_nodes,
    }
    validate_workflow_nodes(workflow_nodes)
    return workflow_name, workflow_nodes, request_parameters, warnings, ",".join(profile_parts)


def _resolve_multi_format_chunk_options(
    create_values: dict[str, str],
    *,
    runtime: dict[str, Any],
    chunk_size: int,
    chunk_overlap: int,
) -> dict[str, Any]:
    strategy = str(
        _first_defined(
            create_values.get("multi_format_chunk_strategy", ""),
            runtime.get("multi_format_chunk_strategy"),
            os.getenv("MULTI_FORMAT_CHUNK_STRATEGY", "chunk_by_character"),
        )
        or "chunk_by_character"
    ).strip().lower()
    if strategy not in {"chunk_by_character", "chunk_by_title", "chunk_by_page", "chunk_by_similarity"}:
        strategy = "chunk_by_character"

    new_after_n_chars = _to_int(
        _first_defined(
            create_values.get("multi_format_chunk_new_after_n_chars", ""),
            runtime.get("multi_format_chunk_new_after_n_chars"),
            os.getenv("MULTI_FORMAT_CHUNK_NEW_AFTER_N_CHARS", str(chunk_size)),
        ),
        default=chunk_size,
        minimum=1,
        maximum=chunk_size,
    )
    combine_text_under_n_chars = _to_int(
        _first_defined(
            create_values.get("multi_format_chunk_combine_text_under_n_chars", ""),
            runtime.get("multi_format_chunk_combine_text_under_n_chars"),
            os.getenv("MULTI_FORMAT_CHUNK_COMBINE_TEXT_UNDER_N_CHARS", str(min(chunk_size, 600))),
        ),
        default=min(chunk_size, 600),
        minimum=0,
        maximum=chunk_size,
    )
    multipage_sections = _to_bool(
        _first_defined(
            create_values.get("multi_format_chunk_multipage_sections", ""),
            runtime.get("multi_format_chunk_multipage_sections"),
            os.getenv("MULTI_FORMAT_CHUNK_MULTIPAGE_SECTIONS", "true"),
        ),
        default=True,
    )
    similarity_threshold = _to_float(
        _first_defined(
            create_values.get("multi_format_chunk_similarity_threshold", ""),
            runtime.get("multi_format_chunk_similarity_threshold"),
            os.getenv("MULTI_FORMAT_CHUNK_SIMILARITY_THRESHOLD", "0.5"),
        ),
        default=0.5,
        minimum=0.0,
        maximum=1.0,
    )

    return {
        "strategy": strategy,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "new_after_n_chars": new_after_n_chars,
        "combine_text_under_n_chars": combine_text_under_n_chars,
        "multipage_sections": multipage_sections,
        "similarity_threshold": similarity_threshold,
    }


def _build_multi_format_workflow_chunk_node(
    *,
    chunk_options: dict[str, Any],
    include_orig_elements: bool,
    overlap_all: bool,
) -> dict[str, Any]:
    strategy = str(chunk_options.get("strategy") or "chunk_by_character").strip().lower() or "chunk_by_character"
    settings: dict[str, Any] = {
        "unstructured_api_url": None,
        "unstructured_api_key": None,
        "include_orig_elements": include_orig_elements,
        "max_characters": int(chunk_options.get("chunk_size") or 600),
    }

    if strategy in {"chunk_by_character", "chunk_by_title", "chunk_by_page"}:
        settings["new_after_n_chars"] = int(chunk_options.get("new_after_n_chars") or settings["max_characters"])
        settings["overlap"] = int(chunk_options.get("chunk_overlap") or 0)
        settings["overlap_all"] = overlap_all
    if strategy == "chunk_by_title":
        settings["combine_text_under_n_chars"] = int(chunk_options.get("combine_text_under_n_chars") or 0)
        settings["multipage_sections"] = bool(chunk_options.get("multipage_sections", True))
    if strategy == "chunk_by_similarity":
        settings["similarity_threshold"] = float(chunk_options.get("similarity_threshold") or 0.5)

    return {
        "name": "Chunker",
        "type": "chunk",
        "subtype": strategy,
        "settings": settings,
    }


def build_multi_format_workflow_definition(
    *,
    create_values: dict[str, str],
    src: Path,
    partition_strategy: str,
    languages: list[str],
    chunk_size: int,
    chunk_overlap: int,
    include_orig_elements: bool,
    overlap_all: bool,
    runtime: dict[str, Any],
) -> tuple[dict[str, Any], list[str], str]:
    warnings: list[str] = []

    workflow_name = _normalize_multi_format_workflow_name(
        create_values.get("multi_format_workflow_name")
        or runtime.get("multi_format_workflow_name")
        or os.getenv("MULTI_FORMAT_WORKFLOW_NAME")
        or "multi_format_prod"
    )

    partition_options, enrichment_options, option_warnings = _resolve_multi_format_accuracy_options(
        create_values,
        runtime=runtime,
    )
    warnings.extend(option_warnings)

    partition_node, partition_warnings = _build_multi_format_workflow_partition_node(
        src=src,
        partition_strategy=partition_strategy or "auto",
        languages=languages,
        partition_options=partition_options,
    )
    warnings.extend(partition_warnings)

    enrichment_nodes, enrichment_warnings = _build_multi_format_enrichment_nodes(
        enrichment_options=enrichment_options,
        partition_strategy=partition_strategy or "auto",
    )
    warnings.extend(enrichment_warnings)

    chunk_options = _resolve_multi_format_chunk_options(
        create_values,
        runtime=runtime,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    chunker_node = _build_multi_format_workflow_chunk_node(
        chunk_options=chunk_options,
        include_orig_elements=include_orig_elements,
        overlap_all=overlap_all,
    )
    workflow_nodes = [partition_node, *enrichment_nodes, chunker_node]
    validate_workflow_nodes(workflow_nodes)
    request_parameters = {
        "workflow_type": "custom",
        "workflow_name": workflow_name,
        "workflow_nodes": workflow_nodes,
    }
    partition_strategy_label = _partition_route_label(partition_node)
    partition_subtype_label = partition_node.get('subtype', '') or 'unknown'
    profile_parts = [f"partition:{partition_subtype_label}:{partition_strategy_label}"]
    for node in enrichment_nodes:
        node_name = str(node.get('name') or '').strip().lower().replace(' ', '_') or 'enrichment'
        node_subtype = str(node.get('subtype') or '').strip() or 'unknown'
        profile_parts.append(f"{node_name}:{node_subtype}")
    profile_parts.append(f"chunk:{chunker_node['subtype']}")
    processing_profile = ",".join(profile_parts)
    return request_parameters, warnings, processing_profile
