from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .constants import DOC_PIPELINE_UI_DEFAULTS


_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_BUNDLED_MODEL_CATALOG_PATH = _CONFIG_DIR / "unstructured_models.example.json"
_LOCAL_MODEL_CATALOG_PATH = _CONFIG_DIR / "unstructured_models.json"


def _merge_wrapper_class(existing: str | None, extra: str | None) -> str:
    parts: list[str] = []
    for value in (existing, extra):
        if not value:
            continue
        parts.extend(chunk for chunk in str(value).split() if chunk)
    return " ".join(dict.fromkeys(parts))


def _read_model_catalog(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_model_catalog(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_model_catalog(current, value)
        else:
            merged[key] = value
    return merged


def _expand_legacy_model_sections(catalog: dict[str, Any]) -> dict[str, Any]:
    expanded = dict(catalog)
    legacy_enrichment = catalog.get("enrichment")
    if isinstance(legacy_enrichment, dict):
        for key in ("generative_ocr", "image_description", "named_entity_recognition", "table_description"):
            expanded.setdefault(key, legacy_enrichment)
    return expanded


def _load_model_catalog() -> dict[str, Any]:
    bundled = _read_model_catalog(_BUNDLED_MODEL_CATALOG_PATH)
    raw_path = os.getenv("UNSTRUCTURED_MODEL_CATALOG_PATH", "").strip()
    override_path = Path(raw_path) if raw_path else _LOCAL_MODEL_CATALOG_PATH
    override = _expand_legacy_model_sections(_read_model_catalog(override_path))
    return _merge_model_catalog(bundled, override)


def _normalize_model_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(model_id).strip() for model_id in raw if str(model_id).strip()]


def _model_options(model_ids: list[str]) -> list[dict[str, str]]:
    return [{"value": model_id, "label": model_id} for model_id in model_ids]


def _model_option_groups(catalog: dict[str, Any], key: str) -> list[dict[str, object]]:
    section = catalog.get(key)
    if not isinstance(section, dict):
        return []
    groups: list[dict[str, object]] = []
    for raw_label, raw_models in section.items():
        label = str(raw_label).strip()
        models = _normalize_model_ids(raw_models)
        if label and models:
            groups.append({"label": label, "options": _model_options(models)})
    return groups


def _build_multi_format_field_map() -> dict[str, dict[str, object]]:
    defaults = DOC_PIPELINE_UI_DEFAULTS
    model_catalog = _load_model_catalog()
    vlm_model_option_groups = _model_option_groups(model_catalog, "partitioner_vlm")
    generative_ocr_model_groups = _model_option_groups(model_catalog, "generative_ocr")
    image_description_model_groups = _model_option_groups(model_catalog, "image_description")
    ner_model_groups = _model_option_groups(model_catalog, "named_entity_recognition")
    table_description_model_groups = _model_option_groups(model_catalog, "table_description")
    table_to_html_model_groups = _model_option_groups(model_catalog, "table_to_html")

    def _select_field(
        name: str,
        label: str,
        default_key: str,
        *,
        options: list[dict[str, str]] | None = None,
        option_groups: list[dict[str, object]] | None = None,
        help_text: str = "",
        wrapper_attrs: dict[str, str] | None = None,
        input_attrs: dict[str, str] | None = None,
        wrapper_class: str | None = None,
    ) -> dict[str, object]:
        field: dict[str, object] = {
            "name": name,
            "label": label,
            "help": help_text,
            "kind": "select",
            "default": str(defaults.get(default_key, "")),
        }
        if options is not None:
            field["options"] = options
        if option_groups is not None:
            field["option_groups"] = option_groups
        if wrapper_attrs:
            field["wrapper_attrs"] = wrapper_attrs
        if input_attrs:
            field["input_attrs"] = input_attrs
        if wrapper_class:
            field["wrapper_class"] = _merge_wrapper_class(field.get("wrapper_class"), wrapper_class)
        return field

    def _text_field(
        name: str,
        label: str,
        default_key: str,
        *,
        kind: str = "text",
        help_text: str = "",
        placeholder: str = "",
        wrapper_attrs: dict[str, str] | None = None,
        input_attrs: dict[str, str] | None = None,
        wrapper_class: str | None = None,
    ) -> dict[str, object]:
        field: dict[str, object] = {
            "name": name,
            "label": label,
            "help": help_text,
            "kind": kind,
            "default": str(defaults.get(default_key, "")),
            "placeholder": placeholder,
        }
        if wrapper_attrs:
            field["wrapper_attrs"] = wrapper_attrs
        if input_attrs:
            field["input_attrs"] = input_attrs
        if wrapper_class:
            field["wrapper_class"] = _merge_wrapper_class(field.get("wrapper_class"), wrapper_class)
        return field

    partition_hi_res_attrs = {"data-partition-routes": "hi_res"}
    partition_vlm_attrs = {"data-partition-routes": "auto vlm"}
    enrichment_route_attrs = {"data-partition-routes": "auto hi_res"}
    bookrag_partition_hi_res_attrs = {"data-bookrag-partition-routes": "hi_res"}
    bookrag_partition_vlm_attrs = {"data-bookrag-partition-routes": "auto vlm"}
    bookrag_enrichment_route_attrs = {"data-bookrag-partition-routes": "auto hi_res"}
    bookrag_ner_model_groups = [group for group in ner_model_groups if group["label"] in {"Anthropic", "Azure OpenAI", "OpenAI"}]
    chunk_title_attrs = {"data-chunk-strategies": "chunk_by_title"}
    chunk_similarity_attrs = {"data-chunk-strategies": "chunk_by_similarity"}
    chunk_sequential_attrs = {"data-chunk-strategies": "chunk_by_character chunk_by_title chunk_by_page"}

    return {
        "multi_format_strategy": _select_field(
            "multi_format_strategy",
            "Partition Route",
            "multi_format_strategy",
            options=[
                {"value": "", "label": "(select route)"},
                {"value": "auto", "label": "Auto"},
                {"value": "hi_res", "label": "High Res"},
                {"value": "vlm", "label": "VLM"},
                {"value": "fast", "label": "Fast"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_ocr_languages": _select_field(
            "multi_format_ocr_languages",
            "OCR Languages",
            "multi_format_ocr_languages",
            help_text="Optional language hint.",
            wrapper_attrs=partition_hi_res_attrs,
            options=[
                {"value": "", "label": "(auto detect / route default)"},
                {"value": "jpn", "label": "Japanese"},
                {"value": "eng", "label": "English"},
                {"value": "jpn,eng", "label": "Japanese + English"},
                {"value": "chi_sim", "label": "Chinese (Simplified)"},
                {"value": "chi_sim,eng", "label": "Chinese (Simplified) + English"},
                {"value": "chi_tra", "label": "Chinese (Traditional)"},
                {"value": "chi_tra,eng", "label": "Chinese (Traditional) + English"},
                {"value": "kor", "label": "Korean"},
                {"value": "kor,eng", "label": "Korean + English"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_vlm_provider": _select_field(
            "multi_format_vlm_provider",
            "VLM Provider",
            "multi_format_vlm_provider",
            help_text="Used when Auto routes to VLM, or when VLM is selected directly.",
            wrapper_attrs=partition_vlm_attrs,
            input_attrs={"data-provider-model-key": "vlm"},
            options=[
                {"value": "", "label": "(platform default)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_vlm_model": _select_field(
            "multi_format_vlm_model",
            "VLM Model",
            "multi_format_vlm_model",
            help_text="Optional explicit VLM model.",
            wrapper_attrs=partition_vlm_attrs,
            input_attrs={"data-provider-model-target": "vlm"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=vlm_model_option_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_vlm_provider_api_key": _text_field(
            "multi_format_vlm_provider_api_key",
            "VLM Provider API Key",
            "multi_format_vlm_provider_api_key",
            help_text="Optional non-default key.",
            placeholder="optional",
            wrapper_attrs=partition_vlm_attrs,
            wrapper_class="field doc-field-long",
        ),
        "multi_format_hi_res_model_name": _text_field(
            "multi_format_hi_res_model_name",
            "High Res Model",
            "multi_format_hi_res_model_name",
            help_text="Optional explicit Hi Res model.",
            placeholder="optional",
            wrapper_attrs=partition_hi_res_attrs,
            wrapper_class="field doc-field-long",
        ),
        "multi_format_infer_table_structure": _select_field(
            "multi_format_infer_table_structure",
            "Infer Table Structure",
            "multi_format_infer_table_structure",
            help_text="Enable table structure extraction.",
            wrapper_attrs=partition_hi_res_attrs,
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_extract_image_block_types": _select_field(
            "multi_format_extract_image_block_types",
            "Enrichment Block Extraction",
            "multi_format_extract_image_block_types",
            help_text="Extract blocks required by downstream enrichments.",
            wrapper_attrs=partition_hi_res_attrs,
            options=[
                {"value": "auto", "label": "auto"},
                {"value": "", "label": "(off)"},
                {"value": "Image", "label": "Image"},
                {"value": "Table", "label": "Table"},
                {"value": "Image,Table", "label": "Image + Table"},
                {"value": "Text,NarrativeText,Title,ListItem,UncategorizedText", "label": "OCR text blocks"},
                {"value": "Image,Table,Text,NarrativeText,Title,ListItem,UncategorizedText", "label": "Image + Table + OCR text"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_enable_generative_ocr": _select_field(
            "multi_format_enable_generative_ocr",
            "Enabled",
            "multi_format_enable_generative_ocr",
            help_text="Auto/High Res only.",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "generative_ocr"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_enable_table_to_html": _select_field(
            "multi_format_enable_table_to_html",
            "Enabled",
            "multi_format_enable_table_to_html",
            help_text="Auto/High Res only.",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "table_to_html"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_enable_table_description": _select_field(
            "multi_format_enable_table_description",
            "Enabled",
            "multi_format_enable_table_description",
            help_text="Auto/High Res only.",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "table_description"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_enable_image_description": _select_field(
            "multi_format_enable_image_description",
            "Enabled",
            "multi_format_enable_image_description",
            help_text="Auto/High Res only.",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "image_description"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_generative_ocr_subtype": _select_field(
            "multi_format_generative_ocr_subtype",
            "Subtype",
            "multi_format_generative_ocr_subtype",
            wrapper_attrs=enrichment_route_attrs,
            options=[
                {"value": "anthropic_ocr", "label": "anthropic_ocr"},
                {"value": "bedrock_ocr", "label": "bedrock_ocr"},
                {"value": "openai_ocr", "label": "openai_ocr"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_generative_ocr_provider_type": _select_field(
            "multi_format_generative_ocr_provider_type",
            "Provider",
            "multi_format_generative_ocr_provider_type",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "generative_ocr"},
            options=[
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_generative_ocr_model": _select_field(
            "multi_format_generative_ocr_model",
            "Model",
            "multi_format_generative_ocr_model",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "generative_ocr"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=generative_ocr_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_table_to_html_subtype": _select_field(
            "multi_format_table_to_html_subtype",
            "Subtype",
            "multi_format_table_to_html_subtype",
            wrapper_attrs=enrichment_route_attrs,
            options=[
                {"value": "twopass_table2html", "label": "twopass_table2html"},
                {"value": "anthropic_table2html", "label": "anthropic_table2html"},
                {"value": "openai_table2html", "label": "openai_table2html"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_table_to_html_provider_type": _select_field(
            "multi_format_table_to_html_provider_type",
            "Provider",
            "multi_format_table_to_html_provider_type",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "table_to_html"},
            options=[
                {"value": "", "label": "(not used for twopass)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_table_to_html_model": _select_field(
            "multi_format_table_to_html_model",
            "Model",
            "multi_format_table_to_html_model",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "table_to_html"},
            options=[{"value": "", "label": "(not used for twopass)"}],
            option_groups=table_to_html_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_table_description_subtype": _select_field(
            "multi_format_table_description_subtype",
            "Subtype",
            "multi_format_table_description_subtype",
            wrapper_attrs=enrichment_route_attrs,
            options=[
                {"value": "anthropic_table_description", "label": "anthropic_table_description"},
                {"value": "bedrock_table_description", "label": "bedrock_table_description"},
                {"value": "openai_table_description", "label": "openai_table_description"},
                {"value": "vertexai_table_description", "label": "vertexai_table_description"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_table_description_provider_type": _select_field(
            "multi_format_table_description_provider_type",
            "Provider",
            "multi_format_table_description_provider_type",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "table_description"},
            options=[
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_table_description_model": _select_field(
            "multi_format_table_description_model",
            "Model",
            "multi_format_table_description_model",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "table_description"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=table_description_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_image_description_subtype": _select_field(
            "multi_format_image_description_subtype",
            "Subtype",
            "multi_format_image_description_subtype",
            wrapper_attrs=enrichment_route_attrs,
            options=[
                {"value": "twopass_image_description", "label": "twopass_image_description"},
                {"value": "anthropic_image_description", "label": "anthropic_image_description"},
                {"value": "bedrock_image_description", "label": "bedrock_image_description"},
                {"value": "openai_image_description", "label": "openai_image_description"},
                {"value": "vertexai_image_description", "label": "vertexai_image_description"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_image_description_provider_type": _select_field(
            "multi_format_image_description_provider_type",
            "Provider",
            "multi_format_image_description_provider_type",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "image_description"},
            options=[
                {"value": "", "label": "(not used for twopass)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_image_description_model": _select_field(
            "multi_format_image_description_model",
            "Model",
            "multi_format_image_description_model",
            wrapper_attrs=enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "image_description"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=image_description_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_chunk_strategy": _select_field(
            "multi_format_chunk_strategy",
            "Chunk Strategy",
            "multi_format_chunk_strategy",
            help_text="Workflow chunker subtype.",
            options=[
                {"value": "chunk_by_character", "label": "By Character"},
                {"value": "chunk_by_title", "label": "By Title"},
                {"value": "chunk_by_page", "label": "By Page"},
                {"value": "chunk_by_similarity", "label": "By Similarity"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_size": _text_field(
            "multi_format_chunk_size",
            "Chunk Size",
            "multi_format_chunk_size",
            kind="number",
            help_text="Max characters per chunk.",
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_overlap": _text_field(
            "multi_format_chunk_overlap",
            "Chunk Overlap",
            "multi_format_chunk_overlap",
            kind="number",
            help_text="Character overlap between neighboring chunks.",
            wrapper_attrs=chunk_sequential_attrs,
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_new_after_n_chars": _text_field(
            "multi_format_chunk_new_after_n_chars",
            "New After N Chars",
            "multi_format_chunk_new_after_n_chars",
            kind="number",
            help_text="Start a new chunk after this size threshold.",
            wrapper_attrs=chunk_sequential_attrs,
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_combine_text_under_n_chars": _text_field(
            "multi_format_chunk_combine_text_under_n_chars",
            "Combine Text Under N Chars",
            "multi_format_chunk_combine_text_under_n_chars",
            kind="number",
            help_text="Only used by title-based chunking.",
            wrapper_attrs=chunk_title_attrs,
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_multipage_sections": _select_field(
            "multi_format_chunk_multipage_sections",
            "Multipage Sections",
            "multi_format_chunk_multipage_sections",
            help_text="Allow title-based sections to continue across pages.",
            wrapper_attrs=chunk_title_attrs,
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_chunk_similarity_threshold": _text_field(
            "multi_format_chunk_similarity_threshold",
            "Similarity Threshold",
            "multi_format_chunk_similarity_threshold",
            kind="number",
            help_text="Similarity cutoff for semantic chunk grouping.",
            wrapper_attrs=chunk_similarity_attrs,
            input_attrs={"step": "0.05", "min": "0.0", "max": "1.0"},
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_strategy": _select_field(
            "multi_format_bookrag_strategy",
            "Partition Route",
            "multi_format_bookrag_strategy",
            options=[
                {"value": "", "label": "(select route)"},
                {"value": "auto", "label": "Auto"},
                {"value": "hi_res", "label": "High Res"},
                {"value": "vlm", "label": "VLM"},
                {"value": "fast", "label": "Fast"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_ocr_languages": _select_field(
            "multi_format_bookrag_ocr_languages",
            "OCR Languages",
            "multi_format_bookrag_ocr_languages",
            help_text="Optional language hint.",
            wrapper_attrs=bookrag_partition_hi_res_attrs,
            options=[
                {"value": "", "label": "(auto detect / route default)"},
                {"value": "jpn", "label": "Japanese"},
                {"value": "eng", "label": "English"},
                {"value": "jpn,eng", "label": "Japanese + English"},
                {"value": "chi_sim", "label": "Chinese (Simplified)"},
                {"value": "chi_sim,eng", "label": "Chinese (Simplified) + English"},
                {"value": "chi_tra", "label": "Chinese (Traditional)"},
                {"value": "chi_tra,eng", "label": "Chinese (Traditional) + English"},
                {"value": "kor", "label": "Korean"},
                {"value": "kor,eng", "label": "Korean + English"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_vlm_provider": _select_field(
            "multi_format_bookrag_vlm_provider",
            "VLM Provider",
            "multi_format_bookrag_vlm_provider",
            help_text="Used when Auto routes to VLM, or when VLM is selected directly.",
            wrapper_attrs=bookrag_partition_vlm_attrs,
            input_attrs={"data-provider-model-key": "bookrag_vlm"},
            options=[
                {"value": "", "label": "(platform default)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_vlm_model": _select_field(
            "multi_format_bookrag_vlm_model",
            "VLM Model",
            "multi_format_bookrag_vlm_model",
            help_text="Optional explicit VLM model.",
            wrapper_attrs=bookrag_partition_vlm_attrs,
            input_attrs={"data-provider-model-target": "bookrag_vlm"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=vlm_model_option_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_vlm_provider_api_key": _text_field(
            "multi_format_bookrag_vlm_provider_api_key",
            "VLM Provider API Key",
            "multi_format_bookrag_vlm_provider_api_key",
            help_text="Optional non-default key.",
            placeholder="optional",
            wrapper_attrs=bookrag_partition_vlm_attrs,
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_hi_res_model_name": _text_field(
            "multi_format_bookrag_hi_res_model_name",
            "High Res Model",
            "multi_format_bookrag_hi_res_model_name",
            help_text="Optional explicit Hi Res model.",
            placeholder="optional",
            wrapper_attrs=bookrag_partition_hi_res_attrs,
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_infer_table_structure": _select_field(
            "multi_format_bookrag_infer_table_structure",
            "Infer Table Structure",
            "multi_format_bookrag_infer_table_structure",
            help_text="Enable table structure extraction.",
            wrapper_attrs=bookrag_partition_hi_res_attrs,
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_extract_image_block_types": _select_field(
            "multi_format_bookrag_extract_image_block_types",
            "Enrichment Block Extraction",
            "multi_format_bookrag_extract_image_block_types",
            help_text="Extract blocks required by downstream enrichments.",
            wrapper_attrs=bookrag_partition_hi_res_attrs,
            options=[
                {"value": "auto", "label": "auto"},
                {"value": "", "label": "(off)"},
                {"value": "Image", "label": "Image"},
                {"value": "Table", "label": "Table"},
                {"value": "Image,Table", "label": "Image + Table"},
                {"value": "Text,NarrativeText,Title,ListItem,UncategorizedText", "label": "OCR text blocks"},
                {"value": "Image,Table,Text,NarrativeText,Title,ListItem,UncategorizedText", "label": "Image + Table + OCR text"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_enable_generative_ocr": _select_field(
            "multi_format_bookrag_enable_generative_ocr",
            "Enabled",
            "multi_format_bookrag_enable_generative_ocr",
            help_text="Auto/High Res only.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "bookrag_generative_ocr"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_enable_table_to_html": _select_field(
            "multi_format_bookrag_enable_table_to_html",
            "Enabled",
            "multi_format_bookrag_enable_table_to_html",
            help_text="Auto/High Res only.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "bookrag_table_to_html"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_enable_table_description": _select_field(
            "multi_format_bookrag_enable_table_description",
            "Enabled",
            "multi_format_bookrag_enable_table_description",
            help_text="Auto/High Res only.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "bookrag_table_description"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_enable_image_description": _select_field(
            "multi_format_bookrag_enable_image_description",
            "Enabled",
            "multi_format_bookrag_enable_image_description",
            help_text="Auto/High Res only.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-enrichment-toggle": "bookrag_image_description"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_enable_ner": _select_field(
            "multi_format_bookrag_enable_ner",
            "Enabled",
            "multi_format_bookrag_enable_ner",
            help_text="Runs named entity recognition on parsed content.",
            input_attrs={"data-enrichment-toggle": "bookrag_ner"},
            options=[
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            wrapper_class="field doc-field-short",
        ),
        "multi_format_bookrag_generative_ocr_subtype": _select_field(
            "multi_format_bookrag_generative_ocr_subtype",
            "Subtype",
            "multi_format_bookrag_generative_ocr_subtype",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            options=[
                {"value": "anthropic_ocr", "label": "anthropic_ocr"},
                {"value": "bedrock_ocr", "label": "bedrock_ocr"},
                {"value": "openai_ocr", "label": "openai_ocr"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_generative_ocr_provider_type": _select_field(
            "multi_format_bookrag_generative_ocr_provider_type",
            "Provider",
            "multi_format_bookrag_generative_ocr_provider_type",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "bookrag_generative_ocr"},
            options=[
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_generative_ocr_model": _select_field(
            "multi_format_bookrag_generative_ocr_model",
            "Model",
            "multi_format_bookrag_generative_ocr_model",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "bookrag_generative_ocr"},
            options=[],
            option_groups=generative_ocr_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_table_to_html_subtype": _select_field(
            "multi_format_bookrag_table_to_html_subtype",
            "Subtype",
            "multi_format_bookrag_table_to_html_subtype",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            options=[
                {"value": "twopass_table2html", "label": "twopass_table2html"},
                {"value": "anthropic_table2html", "label": "anthropic_table2html"},
                {"value": "openai_table2html", "label": "openai_table2html"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_table_to_html_provider_type": _select_field(
            "multi_format_bookrag_table_to_html_provider_type",
            "Provider",
            "multi_format_bookrag_table_to_html_provider_type",
            help_text="Ignored for two-pass.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "bookrag_table_to_html"},
            options=[
                {"value": "", "label": "(not used for twopass)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_table_to_html_model": _select_field(
            "multi_format_bookrag_table_to_html_model",
            "Model",
            "multi_format_bookrag_table_to_html_model",
            help_text="Ignored for two-pass.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "bookrag_table_to_html"},
            options=[{"value": "", "label": "(not used for twopass)"}],
            option_groups=table_to_html_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_table_description_subtype": _select_field(
            "multi_format_bookrag_table_description_subtype",
            "Subtype",
            "multi_format_bookrag_table_description_subtype",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            options=[
                {"value": "anthropic_table_description", "label": "anthropic_table_description"},
                {"value": "bedrock_table_description", "label": "bedrock_table_description"},
                {"value": "openai_table_description", "label": "openai_table_description"},
                {"value": "vertexai_table_description", "label": "vertexai_table_description"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_table_description_provider_type": _select_field(
            "multi_format_bookrag_table_description_provider_type",
            "Provider",
            "multi_format_bookrag_table_description_provider_type",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "bookrag_table_description"},
            options=[
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_table_description_model": _select_field(
            "multi_format_bookrag_table_description_model",
            "Model",
            "multi_format_bookrag_table_description_model",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "bookrag_table_description"},
            options=[],
            option_groups=table_description_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_image_description_subtype": _select_field(
            "multi_format_bookrag_image_description_subtype",
            "Subtype",
            "multi_format_bookrag_image_description_subtype",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            options=[
                {"value": "twopass_image_description", "label": "twopass_image_description"},
                {"value": "anthropic_image_description", "label": "anthropic_image_description"},
                {"value": "bedrock_image_description", "label": "bedrock_image_description"},
                {"value": "openai_image_description", "label": "openai_image_description"},
                {"value": "vertexai_image_description", "label": "vertexai_image_description"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_image_description_provider_type": _select_field(
            "multi_format_bookrag_image_description_provider_type",
            "Provider",
            "multi_format_bookrag_image_description_provider_type",
            help_text="Ignored for two-pass.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-key": "bookrag_image_description"},
            options=[
                {"value": "", "label": "(not used for twopass)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "bedrock", "label": "Bedrock"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
                {"value": "vertexai", "label": "Vertex AI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_image_description_model": _select_field(
            "multi_format_bookrag_image_description_model",
            "Model",
            "multi_format_bookrag_image_description_model",
            help_text="Ignored for two-pass.",
            wrapper_attrs=bookrag_enrichment_route_attrs,
            input_attrs={"data-provider-model-target": "bookrag_image_description"},
            options=[],
            option_groups=image_description_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_ner_subtype": _select_field(
            "multi_format_bookrag_ner_subtype",
            "Subtype",
            "multi_format_bookrag_ner_subtype",
            options=[
                {"value": "openai_ner", "label": "openai_ner"},
                {"value": "anthropic_ner", "label": "anthropic_ner"},
            ],
            wrapper_class="field doc-field-long",
        ),
        "multi_format_bookrag_ner_provider_type": _select_field(
            "multi_format_bookrag_ner_provider_type",
            "Provider",
            "multi_format_bookrag_ner_provider_type",
            input_attrs={"data-provider-model-key": "bookrag_ner"},
            options=[
                {"value": "", "label": "(infer from subtype/model)"},
                {"value": "anthropic", "label": "Anthropic"},
                {"value": "openai", "label": "OpenAI"},
                {"value": "azure_openai", "label": "Azure OpenAI"},
            ],
            wrapper_class="field doc-field-medium",
        ),
        "multi_format_bookrag_ner_model": _select_field(
            "multi_format_bookrag_ner_model",
            "Model",
            "multi_format_bookrag_ner_model",
            input_attrs={"data-provider-model-target": "bookrag_ner"},
            options=[{"value": "", "label": "(platform default)"}],
            option_groups=bookrag_ner_model_groups,
            wrapper_class="field doc-field-xxl",
        ),
        "multi_format_bookrag_chunk_size": {
            "name": "multi_format_bookrag_chunk_size",
            "label": "bookrag_chunk_size",
            "kind": "number",
            "default": str(defaults.get("multi_format_bookrag_chunk_size", "1200")),
            "placeholder": "",
            "wrapper_class": "field doc-field-short",
        },
        "multi_format_bookrag_chunk_overlap": {
            "name": "multi_format_bookrag_chunk_overlap",
            "label": "bookrag_chunk_overlap",
            "kind": "number",
            "default": str(defaults.get("multi_format_bookrag_chunk_overlap", "120")),
            "placeholder": "",
            "wrapper_class": "field doc-field-short",
        },
        "multi_format_bookrag_new_after_n_chars": {
            "name": "multi_format_bookrag_new_after_n_chars",
            "label": "bookrag_new_after_n_chars",
            "kind": "number",
            "default": str(defaults.get("multi_format_bookrag_new_after_n_chars", "1000")),
            "placeholder": "",
            "wrapper_class": "field doc-field-short",
        },
        "multi_format_bookrag_combine_under_n_chars": {
            "name": "multi_format_bookrag_combine_under_n_chars",
            "label": "bookrag_combine_under_n_chars",
            "kind": "number",
            "default": str(defaults.get("multi_format_bookrag_combine_under_n_chars", "600")),
            "placeholder": "",
            "wrapper_class": "field doc-field-short",
        },
        "multi_format_bookrag_multipage_sections": {
            "name": "multi_format_bookrag_multipage_sections",
            "label": "bookrag_multipage_sections",
            "kind": "select",
            "default": str(defaults.get("multi_format_bookrag_multipage_sections", "true")),
            "options": [
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            "wrapper_class": "field doc-field-short",
        },
        "multi_format_bookrag_coordinates": {
            "name": "multi_format_bookrag_coordinates",
            "label": "Coordinates",
            "kind": "select",
            "default": str(defaults.get("multi_format_bookrag_coordinates", "true")),
            "help": "Include element coordinates for High Res.",
            "wrapper_attrs": bookrag_partition_hi_res_attrs,
            "options": [
                {"value": "true", "label": "true"},
                {"value": "false", "label": "false"},
            ],
            "wrapper_class": "field doc-field-short",
        },
    }


def _build_multi_format_fields(*names: str) -> list[dict[str, object]]:
    field_map = _build_multi_format_field_map()
    return [dict(field_map[name]) for name in names]


def build_multi_format_ui_fields() -> list[dict[str, object]]:
    return _build_multi_format_fields(
        "multi_format_strategy",
        "multi_format_ocr_languages",
        "multi_format_vlm_provider",
        "multi_format_vlm_model",
        "multi_format_vlm_provider_api_key",
        "multi_format_hi_res_model_name",
        "multi_format_infer_table_structure",
        "multi_format_extract_image_block_types",
        "multi_format_enable_generative_ocr",
        "multi_format_generative_ocr_subtype",
        "multi_format_generative_ocr_provider_type",
        "multi_format_generative_ocr_model",
        "multi_format_enable_table_to_html",
        "multi_format_table_to_html_subtype",
        "multi_format_table_to_html_provider_type",
        "multi_format_table_to_html_model",
        "multi_format_enable_table_description",
        "multi_format_table_description_subtype",
        "multi_format_table_description_provider_type",
        "multi_format_table_description_model",
        "multi_format_enable_image_description",
        "multi_format_image_description_subtype",
        "multi_format_image_description_provider_type",
        "multi_format_image_description_model",
        "multi_format_chunk_strategy",
        "multi_format_chunk_size",
        "multi_format_chunk_overlap",
        "multi_format_chunk_new_after_n_chars",
        "multi_format_chunk_combine_text_under_n_chars",
        "multi_format_chunk_multipage_sections",
        "multi_format_chunk_similarity_threshold",
    )


def build_multi_format_bookrag_ui_fields() -> list[dict[str, object]]:
    return _build_multi_format_fields(
        "multi_format_bookrag_strategy",
        "multi_format_bookrag_ocr_languages",
        "multi_format_bookrag_vlm_provider",
        "multi_format_bookrag_vlm_model",
        "multi_format_bookrag_vlm_provider_api_key",
        "multi_format_bookrag_hi_res_model_name",
        "multi_format_bookrag_infer_table_structure",
        "multi_format_bookrag_extract_image_block_types",
        "multi_format_bookrag_enable_generative_ocr",
        "multi_format_bookrag_generative_ocr_subtype",
        "multi_format_bookrag_generative_ocr_provider_type",
        "multi_format_bookrag_generative_ocr_model",
        "multi_format_bookrag_enable_table_to_html",
        "multi_format_bookrag_table_to_html_subtype",
        "multi_format_bookrag_table_to_html_provider_type",
        "multi_format_bookrag_table_to_html_model",
        "multi_format_bookrag_enable_table_description",
        "multi_format_bookrag_table_description_subtype",
        "multi_format_bookrag_table_description_provider_type",
        "multi_format_bookrag_table_description_model",
        "multi_format_bookrag_enable_image_description",
        "multi_format_bookrag_image_description_subtype",
        "multi_format_bookrag_image_description_provider_type",
        "multi_format_bookrag_image_description_model",
        "multi_format_bookrag_enable_ner",
        "multi_format_bookrag_ner_subtype",
        "multi_format_bookrag_ner_provider_type",
        "multi_format_bookrag_ner_model",
        "multi_format_bookrag_coordinates",
    )
