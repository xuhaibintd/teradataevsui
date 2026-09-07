from __future__ import annotations

from app.core.form_fields import validate_create_field

DOC_PIPELINE_UI_DEFAULTS = {
    "multi_format_strategy": "auto",
    "multi_format_chunk_strategy": "chunk_by_character",
    "multi_format_chunk_size": "600",
    "multi_format_chunk_overlap": "80",
    "multi_format_chunk_new_after_n_chars": "600",
    "multi_format_chunk_combine_text_under_n_chars": "600",
    "multi_format_chunk_multipage_sections": "true",
    "multi_format_chunk_similarity_threshold": "0.5",
    "multi_format_ocr_languages": "",
    "multi_format_vlm_provider": "",
    "multi_format_vlm_model": "",
    "multi_format_vlm_provider_api_key": "",
    "multi_format_hi_res_model_name": "",
    "multi_format_infer_table_structure": "false",
    "multi_format_extract_image_block_types": "auto",
    "multi_format_enable_generative_ocr": "false",
    "multi_format_enable_table_to_html": "false",
    "multi_format_enable_table_description": "false",
    "multi_format_enable_image_description": "false",
    "multi_format_generative_ocr_subtype": "openai_ocr",
    "multi_format_generative_ocr_provider_type": "openai",
    "multi_format_generative_ocr_model": "gpt-5-mini",
    "multi_format_table_to_html_subtype": "twopass_table2html",
    "multi_format_table_to_html_provider_type": "openai",
    "multi_format_table_to_html_model": "gpt-5-mini",
    "multi_format_table_description_subtype": "openai_table_description",
    "multi_format_table_description_provider_type": "openai",
    "multi_format_table_description_model": "gpt-5-mini",
    "multi_format_image_description_subtype": "openai_image_description",
    "multi_format_image_description_provider_type": "openai",
    "multi_format_image_description_model": "gpt-5-mini",
    "multi_format_bookrag_strategy": "auto",
    "multi_format_bookrag_ocr_languages": "",
    "multi_format_bookrag_vlm_provider": "",
    "multi_format_bookrag_vlm_model": "",
    "multi_format_bookrag_vlm_provider_api_key": "",
    "multi_format_bookrag_hi_res_model_name": "",
    "multi_format_bookrag_infer_table_structure": "false",
    "multi_format_bookrag_enable_generative_ocr": "false",
    "multi_format_bookrag_enable_table_to_html": "false",
    "multi_format_bookrag_enable_table_description": "false",
    "multi_format_bookrag_enable_image_description": "false",
    "multi_format_bookrag_enable_ner": "false",
    "multi_format_bookrag_generative_ocr_subtype": "openai_ocr",
    "multi_format_bookrag_generative_ocr_provider_type": "openai",
    "multi_format_bookrag_generative_ocr_model": "gpt-5-mini",
    "multi_format_bookrag_table_to_html_subtype": "twopass_table2html",
    "multi_format_bookrag_table_to_html_provider_type": "openai",
    "multi_format_bookrag_table_to_html_model": "gpt-5-mini",
    "multi_format_bookrag_table_description_subtype": "openai_table_description",
    "multi_format_bookrag_table_description_provider_type": "openai",
    "multi_format_bookrag_table_description_model": "gpt-5-mini",
    "multi_format_bookrag_image_description_subtype": "openai_image_description",
    "multi_format_bookrag_image_description_provider_type": "openai",
    "multi_format_bookrag_image_description_model": "gpt-5-mini",
    "multi_format_bookrag_ner_subtype": "openai_ner",
    "multi_format_bookrag_ner_provider_type": "openai",
    "multi_format_bookrag_ner_model": "gpt-5-mini",
    "multi_format_keep_tables": "",
    "multi_format_extract_images": "",
    "multi_format_bookrag_chunk_size": "1200",
    "multi_format_bookrag_chunk_overlap": "120",
    "multi_format_bookrag_new_after_n_chars": "1000",
    "multi_format_bookrag_combine_under_n_chars": "600",
    "multi_format_bookrag_multipage_sections": "true",
    "multi_format_bookrag_coordinates": "true",
    "multi_format_bookrag_extract_image_block_types": "auto",
    "multi_format_bookrag_generate_raw": "true",
    "multi_format_bookrag_generate_graph": "true",
    "multi_format_bookrag_run_embedding": "true",
    "bookrag_loaded_csv_run_id": "",
    "multi_format_loaded_csv_run_id": "",
}

DOC_PIPELINE_CHECKBOX_FIELDS: set[str] = set()
DOC_PIPELINE_UNTRUNCATED_FIELDS = {"bookrag_loaded_csv_run_id", "multi_format_loaded_csv_run_id"}

DOC_PIPELINE_OPTIONS = [
    {"value": "text_core", "label": "Text PDF Only"},
    {"value": "multi_format", "label": "Multi-Format"},
    {"value": "multi_format_bookrag", "label": "Multi-Format BookRAG"},
]
DOC_PIPELINE_MODE_VALUES = {item["value"] for item in DOC_PIPELINE_OPTIONS}

def normalize_doc_pipeline_mode(value: str) -> str:
    mode = str(value or "text_core").strip().lower() or "text_core"
    if mode not in DOC_PIPELINE_MODE_VALUES:
        return "text_core"
    return mode


def collect_doc_pipeline_ui_values(form, *, field_max_len: int) -> dict[str, str]:
    values: dict[str, str] = {}
    for ui_field, default_value in DOC_PIPELINE_UI_DEFAULTS.items():
        if ui_field in DOC_PIPELINE_CHECKBOX_FIELDS:
            getlist = getattr(form, "getlist", None)
            raw_values = getlist(ui_field) if callable(getlist) else ([form.get(ui_field)] if ui_field in form else [])
            values[ui_field] = "true" if any(str(item).strip().lower() == "true" for item in raw_values) else "false"
            continue
        ui_raw = str(form.get(ui_field, default_value)).strip()
        values[ui_field] = validate_create_field(ui_field, ui_raw, default=field_max_len)
    return values
