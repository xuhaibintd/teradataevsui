from __future__ import annotations

import unittest

from app.integrations.unstructured.contracts import validate_workflow_nodes


class UnstructuredWorkflowContractTests(unittest.TestCase):
    def test_explicit_vlm_rejects_redundant_enrichment(self) -> None:
        nodes = [
            {
                "name": "Partitioner",
                "type": "partition",
                "subtype": "vlm",
                "settings": {"is_dynamic": False},
            },
            {
                "name": "Image Description",
                "type": "prompter",
                "subtype": "openai_image_description",
                "settings": {"provider_type": "openai", "model": "gpt-5-mini"},
            },
        ]

        with self.assertRaisesRegex(ValueError, "does not accept"):
            validate_workflow_nodes(nodes)

    def test_auto_vlm_allows_separate_enrichment_nodes(self) -> None:
        nodes = [
            {
                "name": "Partitioner",
                "type": "partition",
                "subtype": "vlm",
                "settings": {"is_dynamic": True},
            },
            {
                "name": "Image Description",
                "type": "prompter",
                "subtype": "openai_image_description",
                "settings": {"provider_type": "openai", "model": "gpt-5-mini"},
            },
        ]

        validate_workflow_nodes(nodes)

    def test_explicit_vlm_allows_ner(self) -> None:
        validate_workflow_nodes([
            {
                "name": "Partitioner",
                "type": "partition",
                "subtype": "vlm",
                "settings": {"is_dynamic": False},
            },
            {
                "name": "Named Entity Recognition",
                "type": "prompter",
                "subtype": "openai_ner",
                "settings": {"provider_type": "openai", "model": "gpt-5-mini"},
            },
        ])

    def test_vlm_rejects_legacy_strategy_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "use is_dynamic"):
            validate_workflow_nodes([
                {
                    "name": "Partitioner",
                    "type": "partition",
                    "subtype": "vlm",
                    "settings": {"strategy": "auto", "is_dynamic": True},
                }
            ])

    def test_model_backed_prompter_requires_provider_and_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires provider_type and model"):
            validate_workflow_nodes([
                {
                    "name": "Partitioner",
                    "type": "partition",
                    "subtype": "unstructured_api",
                    "settings": {"strategy": "hi_res"},
                },
                {
                    "name": "Generative OCR",
                    "type": "prompter",
                    "subtype": "openai_ocr",
                    "settings": {},
                },
            ])

    def test_twopass_prompter_omits_provider_and_model(self) -> None:
        validate_workflow_nodes([
            {
                "name": "Partitioner",
                "type": "partition",
                "subtype": "unstructured_api",
                "settings": {"strategy": "hi_res"},
            },
            {
                "name": "Table to HTML",
                "type": "prompter",
                "subtype": "twopass_table2html",
                "settings": {},
            },
        ])

    def test_partition_must_be_first(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be first"):
            validate_workflow_nodes([
                {"name": "Chunker", "type": "chunk", "subtype": "chunk_by_title", "settings": {}},
                {
                    "name": "Partitioner",
                    "type": "partition",
                    "subtype": "unstructured_api",
                    "settings": {"strategy": "hi_res"},
                },
            ])

    def test_ner_must_follow_chunker(self) -> None:
        with self.assertRaisesRegex(ValueError, "must follow"):
            validate_workflow_nodes([
                {
                    "name": "Partitioner",
                    "type": "partition",
                    "subtype": "unstructured_api",
                    "settings": {"strategy": "hi_res"},
                },
                {
                    "name": "Named Entity Recognition",
                    "type": "prompter",
                    "subtype": "openai_ner",
                    "settings": {"provider_type": "openai", "model": "gpt-5-mini"},
                },
                {"name": "Chunker", "type": "chunk", "subtype": "chunk_by_title", "settings": {}},
            ])


if __name__ == "__main__":
    unittest.main()
