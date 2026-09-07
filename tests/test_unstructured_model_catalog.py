from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.doc_modes import ui_fields


class UnstructuredModelCatalogTests(unittest.TestCase):
    def test_bundled_catalog_contains_current_vlm_and_ner_models(self) -> None:
        catalog = ui_fields._read_model_catalog(ui_fields._BUNDLED_MODEL_CATALOG_PATH)

        self.assertEqual(catalog["_meta"]["source_checked_at"], "2026-09-07")
        self.assertEqual(
            catalog["partitioner_vlm"]["OpenAI"],
            ["gpt-4o", "gpt-4o-mini", "gpt-5-mini", "gpt-5.2", "gpt-5.4-mini"],
        )
        self.assertIn("claude-sonnet-4-6", catalog["partitioner_vlm"]["Anthropic"])
        self.assertIn("gpt-5.4-mini", catalog["named_entity_recognition"]["OpenAI"])
        self.assertNotIn("gemini-2.0-flash-001", catalog["partitioner_vlm"]["Vertex AI"])
        self.assertNotIn("claude-sonnet-4-20250514", catalog["partitioner_vlm"]["Anthropic"])

    def test_ui_uses_bundled_catalog_without_local_override(self) -> None:
        missing = Path("missing-unstructured-model-catalog.json")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(
            ui_fields, "_LOCAL_MODEL_CATALOG_PATH", missing
        ):
            os.environ.pop("UNSTRUCTURED_MODEL_CATALOG_PATH", None)
            field_map = ui_fields._build_multi_format_field_map()

        vlm_groups = {
            group["label"]: [option["value"] for option in group["options"]]
            for group in field_map["multi_format_vlm_model"]["option_groups"]
        }
        ner_groups = {
            group["label"]: [option["value"] for option in group["options"]]
            for group in field_map["multi_format_bookrag_ner_model"]["option_groups"]
        }
        self.assertIn("gpt-5.4-mini", vlm_groups["OpenAI"])
        self.assertIn("claude-sonnet-4-6", vlm_groups["Anthropic"])
        self.assertIn("gpt-5.4-mini", ner_groups["OpenAI"])

        for field_name in (
            "multi_format_bookrag_generative_ocr_model",
            "multi_format_bookrag_image_description_model",
            "multi_format_bookrag_table_description_model",
            "multi_format_bookrag_table_to_html_model",
        ):
            groups = {
                group["label"]: [option["value"] for option in group["options"]]
                for group in field_map[field_name]["option_groups"]
            }
            self.assertIn("gpt-5.4-mini", groups["OpenAI"], field_name)
        image_subtypes = {
            option["value"]
            for option in field_map["multi_format_bookrag_image_description_subtype"]["options"]
        }
        self.assertIn("twopass_image_description", image_subtypes)

    def test_legacy_enrichment_override_merges_with_bundled_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_path = Path(tmpdir) / "models.json"
            override_path.write_text(
                json.dumps({"enrichment": {"OpenAI": ["custom-openai-model"]}}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"UNSTRUCTURED_MODEL_CATALOG_PATH": str(override_path)},
            ):
                catalog = ui_fields._load_model_catalog()

        self.assertEqual(
            catalog["named_entity_recognition"]["OpenAI"],
            ["custom-openai-model"],
        )
        self.assertIn("Anthropic", catalog["named_entity_recognition"])
        self.assertIn("gpt-5.4-mini", catalog["partitioner_vlm"]["OpenAI"])

    def test_invalid_override_falls_back_to_bundled_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_path = Path(tmpdir) / "models.json"
            override_path.write_text("not-json", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"UNSTRUCTURED_MODEL_CATALOG_PATH": str(override_path)},
            ):
                catalog = ui_fields._load_model_catalog()

        self.assertIn("gpt-5.4-mini", catalog["partitioner_vlm"]["OpenAI"])


if __name__ == "__main__":
    unittest.main()
