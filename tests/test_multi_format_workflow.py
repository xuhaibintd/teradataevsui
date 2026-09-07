from __future__ import annotations

import contextlib
import csv
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.services.doc_modes.common import DOC_PIPELINE_UI_DEFAULTS
from app.services import multi_format


class MultiFormatWorkflowDefinitionTests(unittest.TestCase):
    def _create_values(self, **overrides: str) -> dict[str, str]:
        values = dict(DOC_PIPELINE_UI_DEFAULTS)
        values.update(overrides)
        return values

    def test_bookrag_unstructured_workers_default_to_five_and_cap_at_file_count(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(multi_format._resolve_bookrag_unstructured_workers(42), 5)
            self.assertEqual(multi_format._resolve_bookrag_unstructured_workers(3), 3)
            self.assertEqual(multi_format._resolve_bookrag_csv_prepare_workers(42), 5)
            self.assertEqual(multi_format._resolve_bookrag_csv_prepare_workers(3), 3)
            self.assertEqual(multi_format._resolve_bookrag_csv_load_workers(42), 5)
            self.assertEqual(multi_format._resolve_bookrag_csv_load_workers(3), 3)

    def test_csv_document_progress_maps_completed_files_to_transform_range(self) -> None:
        updates = []

        for completed in range(1, 4):
            multi_format._report_csv_document_progress(
                updates.append,
                completed=completed,
                total=3,
            )
        multi_format._report_csv_document_progress(None, completed=3, total=3)

        self.assertEqual(updates, [36, 63, 90])

    def test_csv_generation_target_rejects_only_matching_loaded_run(self) -> None:
        loaded_run = {
            "status": "ready",
            "load_status": "ready",
            "vector_store_name": "Existing_Store",
            "target_database": "Target_DB",
        }

        with self.assertRaisesRegex(RuntimeError, "already has loaded tables"):
            multi_format._ensure_csv_generation_target_available(
                [loaded_run],
                vector_store_name="existing_store",
                target_database="target-db",
            )

        for changed_value in (
            {"load_status": "failed"},
            {"status": "failed"},
            {"vector_store_name": "another_store"},
            {"target_database": "another_db"},
        ):
            with self.subTest(changed_value=changed_value):
                multi_format._ensure_csv_generation_target_available(
                    [{**loaded_run, **changed_value}],
                    vector_store_name="existing_store",
                    target_database="target-db",
                )

    def test_each_csv_mode_rejects_a_loaded_target_from_the_other_mode(self) -> None:
        loaded_run = {
            "status": "ready",
            "load_status": "ready",
            "vector_store_name": "existing_store",
            "target_database": "target_db",
        }

        with mock.patch.object(multi_format, "list_bookrag_csv_runs", return_value=[]), mock.patch.object(
            multi_format, "list_multi_format_csv_runs", return_value=[loaded_run]
        ):
            with self.assertRaisesRegex(RuntimeError, "already has loaded tables"):
                multi_format.ensure_bookrag_csv_generation_target_available(
                    vector_store_name="existing_store",
                    target_database="target_db",
                )

        with mock.patch.object(multi_format, "list_bookrag_csv_runs", return_value=[loaded_run]), mock.patch.object(
            multi_format, "list_multi_format_csv_runs", return_value=[]
        ):
            with self.assertRaisesRegex(RuntimeError, "already has loaded tables"):
                multi_format.ensure_multi_format_csv_generation_target_available(
                    vector_store_name="existing_store",
                    target_database="target_db",
                )

    def test_document_parsing_runs_only_parallel_json_stage(self) -> None:
        payloads = {
            "one.txt": [{"type": "NarrativeText", "element_id": "one", "text": "One", "metadata": {}}],
            "two.txt": [{"type": "NarrativeText", "element_id": "two", "text": "Two", "metadata": {}}],
        }
        parse_barrier = threading.Barrier(2)

        def _run_job(client=None, *, src, **kwargs):
            parse_barrier.wait(timeout=5)
            payload = payloads[src.name]
            return payload, payload, {}, f"job-{src.stem}", "workflow-1", "BookRAG_Test"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sources = [tmp_path / "one.txt", tmp_path / "two.txt"]
            for src in sources:
                src.write_text("demo", encoding="utf-8")
            raw_stage_dir = tmp_path / "raw"
            uploads = [
                {"doc_id": f"doc-{index}", "saved_path": str(src), "filename": src.name}
                for index, src in enumerate(sources, start=1)
            ]

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict("os.environ", {"BOOKRAG_UNSTRUCTURED_WORKERS": "2"}))
                stack.enter_context(mock.patch("app.services.multi_format._load_unstructured_runtime_config", return_value=("key", "https://example.invalid")))
                stack.enter_context(mock.patch("app.services.multi_format._resolve_unstructured_request_timeout_ms", return_value=120000))
                stack.enter_context(mock.patch("app.services.multi_format._resolve_bookrag_workflow_poll_config", return_value=(30, 1)))
                stack.enter_context(mock.patch("app.services.multi_format._enforce_unstructured_job_submission_spacing", side_effect=lambda value: value))
                stack.enter_context(mock.patch("app.services.multi_format._create_unstructured_client", return_value=object()))
                stack.enter_context(mock.patch("app.services.multi_format._prepare_bookrag_raw_stage_dir", return_value=raw_stage_dir))
                stack.enter_context(mock.patch("app.services.multi_format._resolve_bookrag_image_partition_options", return_value=({}, [], {})))
                stack.enter_context(mock.patch("app.services.multi_format._build_bookrag_reusable_workflow_definition", return_value=("BookRAG_Test", [], {"workflow_name": "BookRAG_Test", "workflow_nodes": []}, [], "partition:fast")))
                stack.enter_context(mock.patch("app.services.multi_format._run_unstructured_workflow_job_for_file", side_effect=_run_job))

                summary = multi_format.run_bookrag_document_parsing(
                    create_values=self._create_values(multi_format_bookrag_strategy="fast"),
                    vector_store_name="demo",
                    uploaded_documents=uploads,
                    connection_params={},
                    resolve_path_hint=lambda value: value,
                )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["file_count"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertEqual(summary["workers"], 2)
            self.assertEqual(summary["csv_files_created"], 0)
            self.assertEqual(summary["database_writes"], 0)
            self.assertEqual([item["element_count"] for item in summary["files"]], [1, 1])
            self.assertTrue(all(Path(item["raw_json_path"]).is_file() for item in summary["files"]))
            self.assertTrue(Path(summary["manifest_path"]).is_file())
            manifest = multi_format.json.loads(Path(summary["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(manifest["parse_run_id"], summary["parse_run_id"])
            self.assertEqual(len(manifest["documents"]), 2)
            self.assertTrue(all(item["raw_json_sha256"] for item in manifest["documents"]))

    def test_existing_json_can_generate_csv_concurrently_without_reparsing_or_overwriting(self) -> None:
        transform_barrier = threading.Barrier(2)

        def _blocks(*, doc_id, raw_elements, **kwargs):
            transform_barrier.wait(timeout=5)
            return [
                {
                    "doc_id": doc_id,
                    "element_id": raw_elements[0]["element_id"],
                    "parent_id": None,
                    "category_depth": None,
                    "heading_level": None,
                    "page_number": 1,
                    "ordinal": 1,
                    "text": raw_elements[0]["text"],
                    "type": "NarrativeText",
                    "text_as_html": None,
                    "image_caption": None,
                    "image_context": None,
                }
            ]

        def _write_csv(*, table_key, table_targets, rows, csv_stage_dir, **kwargs):
            csv_stage_dir.mkdir(parents=True, exist_ok=True)
            path = csv_stage_dir / f"{table_targets[table_key]}.csv"
            path.write_text("header\nvalue\n", encoding="utf-8")
            return str(path)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            raw_root = tmp_path / "raw"
            csv_root = tmp_path / "csv"
            parse_run_id = "demo_parse_run"
            parse_dir = raw_root / parse_run_id
            parse_dir.mkdir(parents=True)
            documents = []
            for index, filename in enumerate(("one.txt", "two.txt"), start=1):
                raw_path = parse_dir / f"{Path(filename).stem}_doc-{index}.json"
                raw_path.write_text(
                    multi_format.json.dumps(
                        [{"type": "NarrativeText", "element_id": f"e-{index}", "text": filename, "metadata": {}}]
                    ),
                    encoding="utf-8",
                )
                documents.append(
                    {
                        "source_index": index - 1,
                        "doc_id": f"doc-{index}",
                        "filename": filename,
                        "source_file": f"deleted/{filename}",
                        "filetype": "text/plain",
                        "filesize_bytes": 4,
                        "job_id": f"job-{index}",
                        "workflow_id": "workflow-1",
                        "workflow_name": "BookRAG_Test",
                        "raw_json_file": raw_path.name,
                        "raw_json_sha256": multi_format._file_sha256(raw_path),
                        "element_count": 1,
                        "status": "success",
                        "error": "",
                    }
                )
            manifest = {
                "schema_version": 1,
                "artifact_type": "bookrag_parse_run",
                "parse_run_id": parse_run_id,
                "status": "ready",
                "created_at": "2026-07-15 12:00:00",
                "vector_store_name": "demo",
                "file_count": 2,
                "success_count": 2,
                "failure_count": 0,
                "ocr_languages": ["jpn"],
                "processing_profile": "partition:fast",
                "workflow_name": "BookRAG_Test",
                "documents": documents,
            }
            (parse_dir / "manifest.json").write_text(
                multi_format.json.dumps(manifest), encoding="utf-8"
            )

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(multi_format, "BOOKRAG_RAW_STAGE_DIR_DEFAULT", raw_root))
                stack.enter_context(mock.patch.object(multi_format, "BOOKRAG_CSV_STAGE_DIR_DEFAULT", csv_root))
                stack.enter_context(mock.patch.dict("os.environ", {"BOOKRAG_CSV_PREPARE_WORKERS": "2"}))
                parse_mock = stack.enter_context(
                    mock.patch("app.services.multi_format._run_unstructured_workflow_job_for_file")
                )
                stack.enter_context(mock.patch("app.services.multi_format.elements_to_bookrag_blocks", side_effect=_blocks))
                stack.enter_context(
                    mock.patch(
                        "app.services.multi_format.build_bookrag_nodes",
                        side_effect=lambda document_row, blocks: [
                            {"doc_id": document_row["doc_id"], "node_id": f"node-{document_row['doc_id']}"}
                        ],
                    )
                )
                stack.enter_context(mock.patch("app.services.multi_format.validate_bookrag_dataset_relationships"))
                stack.enter_context(mock.patch("app.services.multi_format.prepare_bookrag_table_csv", side_effect=_write_csv))

                progress_updates = []
                first = multi_format.run_bookrag_json_to_csv(
                    parse_run_id=parse_run_id,
                    create_values=self._create_values(),
                    vector_store_name="demo",
                    target_database="demo_schema",
                    progress_callback=progress_updates.append,
                )
                transform_barrier.reset()
                second = multi_format.run_bookrag_json_to_csv(
                    parse_run_id=parse_run_id,
                    create_values=self._create_values(),
                    vector_store_name="demo",
                    target_database="demo_schema",
                )

            parse_mock.assert_not_called()
            self.assertEqual(first["status"], "ready")
            self.assertEqual(progress_updates, [50, 90])
            self.assertTrue(first["created_at"])
            self.assertEqual(first["success_count"], 2)
            self.assertEqual(first["csv_files_created"], 15)
            self.assertEqual(first["csv_file_count"], 15)
            self.assertEqual(len(first["run_csv_files"]), 1)
            self.assertEqual(first["run_csv_files"][0]["table_key"], "document_relations")
            first_manifest = multi_format.json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(first_manifest["complete_table_contract"], multi_format.BOOKRAG_COMPLETE_TABLE_CONTRACT)
            self.assertTrue(all(len(item["csv_files"]) == 7 for item in first_manifest["documents"]))
            self.assertEqual(first["database_writes"], 0)
            self.assertEqual(first["vector_store_name"], "demo")
            self.assertEqual(first["target_database"], "demo_schema")
            self.assertEqual(first["table_targets"], multi_format.build_bookrag_table_targets("demo"))
            self.assertNotEqual(first["csv_run_id"], second["csv_run_id"])
            self.assertNotEqual(first["csv_stage_dir"], second["csv_stage_dir"])
            self.assertTrue(Path(first["manifest_path"]).is_file())
            self.assertTrue(Path(second["manifest_path"]).is_file())

    def test_ready_csv_manifest_loads_all_tables_concurrently_before_becoming_ready(self) -> None:
        load_barrier = threading.Barrier(3)
        table_targets = multi_format.build_bookrag_table_targets("demo_vs")

        def _load_csv(*, table_key, row_count, **kwargs):
            load_barrier.wait(timeout=5)
            return row_count

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_root = Path(tmpdir) / "csv"
            csv_run_id = "demo_csv_run"
            run_dir = csv_root / csv_run_id
            run_dir.mkdir(parents=True)
            csv_items = []
            expected_counts = {
                "documents": 1,
                "blocks": 2,
                "nodes": 3,
                "document_relations": 0,
                "entities": 0,
                "entity_links": 0,
                "entity_relations": 0,
            }
            for table_key, row_count in {
                key: value for key, value in expected_counts.items() if key != "document_relations"
            }.items():
                csv_path = run_dir / f"{table_key}.csv"
                csv_path.write_text("header\n" + ("value\n" if row_count else ""), encoding="utf-8")
                csv_items.append(
                    {
                        "table_key": table_key,
                        "row_count": row_count,
                        "csv_file": csv_path.name,
                        "csv_sha256": multi_format._file_sha256(csv_path),
                    }
                )
            relation_csv_path = run_dir / "document_relations.csv"
            relation_csv_path.write_text("header\n", encoding="utf-8")
            run_csv_files = [
                {
                    "table_key": "document_relations",
                    "row_count": 0,
                    "csv_file": relation_csv_path.name,
                    "csv_sha256": multi_format._file_sha256(relation_csv_path),
                }
            ]
            manifest = {
                "schema_version": 1,
                "artifact_type": "bookrag_csv_run",
                "csv_run_id": csv_run_id,
                "source_parse_run_id": "parse-1",
                "status": "ready",
                "created_at": "2026-07-15 12:00:00",
                "complete_table_contract": multi_format.BOOKRAG_COMPLETE_TABLE_CONTRACT,
                "vector_store_name": "demo_vs",
                "target_database": "demo_schema",
                "table_targets": table_targets,
                "qualified_table_targets": {
                    key: f"demo_schema.{value}" for key, value in table_targets.items()
                },
                "table_generation": {
                    "documents": True,
                    "raw": False,
                    "blocks": True,
                    "nodes": True,
                    "document_relations": True,
                    "entities": True,
                    "entity_links": True,
                    "entity_relations": True,
                },
                "load_status": "not_started",
                "vector_store_status": "not_started",
                "run_csv_files": run_csv_files,
                "documents": [
                    {
                        "source_index": 0,
                        "doc_id": "doc-1",
                        "filename": "one.pdf",
                        "status": "success",
                        "csv_files": csv_items,
                    }
                ],
            }
            (run_dir / "manifest.json").write_text(
                multi_format.json.dumps(manifest), encoding="utf-8"
            )

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(multi_format, "BOOKRAG_CSV_STAGE_DIR_DEFAULT", csv_root))
                stack.enter_context(mock.patch.dict("os.environ", {"BOOKRAG_CSV_LOAD_WORKERS": "3"}))
                for function_name in (
                    "prepare_bookrag_document_table",
                    "prepare_bookrag_block_table",
                    "prepare_bookrag_node_table",
                    "prepare_bookrag_document_relation_table",
                    "prepare_bookrag_entity_table",
                    "prepare_bookrag_entity_link_table",
                    "prepare_bookrag_entity_relation_table",
                ):
                    stack.enter_context(mock.patch(f"app.services.multi_format.{function_name}", return_value=[]))
                stack.enter_context(
                    mock.patch("app.services.multi_format.load_prepared_bookrag_table_csv", side_effect=_load_csv)
                )
                stack.enter_context(
                    mock.patch("app.services.multi_format.validate_prepared_bookrag_table_csv")
                )
                stack.enter_context(
                    mock.patch(
                        "app.services.multi_format._count_teradata_rows",
                        side_effect=lambda schema_name, table_name, execute_sql_fn: next(
                            count for key, count in expected_counts.items() if table_targets[key] == table_name
                        ),
                    )
                )

                execute_mock = mock.Mock()
                summary = multi_format.run_bookrag_csv_load(
                    csv_run_id=csv_run_id,
                    execute_sql_fn=execute_mock,
                    connection_profile_id=7,
                    connection_target_fingerprint="fixture-target",
                )
                ready_summary = multi_format.get_ready_bookrag_csv_load_summary(csv_run_id=csv_run_id, connection_profile_id=7, connection_target_fingerprint="fixture-target")

                failed_manifest = multi_format.json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                failed_manifest["load_status"] = "failed"
                failed_manifest["load_error"] = "simulated partial load"
                (run_dir / "manifest.json").write_text(
                    multi_format.json.dumps(failed_manifest), encoding="utf-8"
                )
                with mock.patch("app.services.multi_format._teradata_table_exists", return_value=True):
                    retry_summary = multi_format.run_bookrag_csv_load(
                        csv_run_id=csv_run_id,
                        execute_sql_fn=execute_mock,
                        connection_profile_id=7,
                        connection_target_fingerprint="fixture-target",
                    )

            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["workers"], 3)
            self.assertEqual(summary["inserted_rows"], 6)
            self.assertEqual(summary["node_table"], f"demo_schema.{table_targets['nodes']}")
            self.assertEqual(ready_summary["node_table"], summary["node_table"])
            self.assertTrue(ready_summary["already_loaded"])
            self.assertTrue(any("partial target table" in warning for warning in retry_summary["warnings"]))
            saved_manifest = multi_format.json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["load_status"], "ready")
            self.assertEqual(saved_manifest["connection_profile_id"], 7)
            self.assertEqual(saved_manifest["connection_target_fingerprint"], "fixture-target")
            self.assertEqual(saved_manifest["vector_store_status"], "not_started")
            self.assertEqual(saved_manifest["load_retry_count"], 1)
            self.assertEqual(len(saved_manifest["load_recovered_tables"]), 7)

    def test_bookrag_table_groups_keep_core_indivisible_and_graph_all_or_nothing(self) -> None:
        flags = multi_format._resolve_bookrag_table_generation_flags(
            self._create_values(
                multi_format_bookrag_generate_documents="false",
                multi_format_bookrag_generate_blocks="false",
                multi_format_bookrag_generate_nodes="false",
                multi_format_bookrag_generate_raw="false",
                multi_format_bookrag_generate_graph="true",
            )
        )

        self.assertTrue(flags["documents"])
        self.assertTrue(flags["blocks"])
        self.assertTrue(flags["nodes"])
        self.assertTrue(flags["raw"])
        self.assertTrue(flags["entities"])
        self.assertTrue(flags["entity_links"])
        self.assertTrue(flags["entity_relations"])

    def test_bookrag_embedding_is_mandatory_even_for_legacy_false_value(self) -> None:
        self.assertTrue(
            multi_format._resolve_bookrag_embedding_step_flag(
                self._create_values(multi_format_bookrag_run_embedding="false")
            )
        )

    def test_bookrag_legacy_graph_toggle_enables_complete_graph_group(self) -> None:
        flags = multi_format._resolve_bookrag_table_generation_flags(
            self._create_values(
                multi_format_bookrag_generate_graph="false",
                multi_format_bookrag_generate_entity_links="true",
            )
        )

        self.assertTrue(flags["entities"])
        self.assertTrue(flags["entity_links"])
        self.assertTrue(flags["entity_relations"])

    def test_auto_defaults_keep_enrichments_disabled(self) -> None:
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=dict(DOC_PIPELINE_UI_DEFAULTS),
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Chunker'],
        )
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertEqual(partition_node['subtype'], 'vlm')
        self.assertTrue(partition_node['settings']['is_dynamic'])
        self.assertNotIn('strategy', partition_node['settings'])
        self.assertNotIn('provider', partition_node['settings'])
        self.assertNotIn('model', partition_node['settings'])
        self.assertEqual(processing_profile, 'partition:vlm:auto,chunk:chunk_by_character')


    def test_hi_res_defaults_do_not_force_table_structure_or_block_extraction(self) -> None:
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=self._create_values(multi_format_strategy='hi_res'),
            src=Path('sample.pdf'),
            partition_strategy='hi_res',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertEqual(partition_node['subtype'], 'unstructured_api')
        self.assertEqual(partition_node['settings']['strategy'], 'hi_res')
        self.assertNotIn('infer_table_structure', partition_node['settings'])
        self.assertNotIn('pdf_infer_table_structure', partition_node['settings'])
        self.assertNotIn('extract_image_block_types', partition_node['settings'])
        self.assertEqual(processing_profile, 'partition:unstructured_api:hi_res,chunk:chunk_by_character')

    def test_chunk_by_title_adds_title_only_settings(self) -> None:
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=self._create_values(
                multi_format_strategy='hi_res',
                multi_format_chunk_strategy='chunk_by_title',
                multi_format_chunk_new_after_n_chars='500',
                multi_format_chunk_combine_text_under_n_chars='200',
                multi_format_chunk_multipage_sections='false',
            ),
            src=Path('sample.pdf'),
            partition_strategy='hi_res',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        chunk_node = request_parameters['workflow_nodes'][-1]
        self.assertEqual(chunk_node['subtype'], 'chunk_by_title')
        self.assertEqual(chunk_node['settings']['max_characters'], 600)
        self.assertEqual(chunk_node['settings']['new_after_n_chars'], 500)
        self.assertEqual(chunk_node['settings']['overlap'], 80)
        self.assertEqual(chunk_node['settings']['combine_text_under_n_chars'], 200)
        self.assertFalse(chunk_node['settings']['multipage_sections'])
        self.assertEqual(processing_profile, 'partition:unstructured_api:hi_res,chunk:chunk_by_title')

    def test_chunk_by_similarity_uses_similarity_threshold(self) -> None:
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=self._create_values(
                multi_format_chunk_strategy='chunk_by_similarity',
                multi_format_chunk_similarity_threshold='0.7',
            ),
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        chunk_node = request_parameters['workflow_nodes'][-1]
        self.assertEqual(chunk_node['subtype'], 'chunk_by_similarity')
        self.assertEqual(chunk_node['settings']['max_characters'], 600)
        self.assertEqual(chunk_node['settings']['similarity_threshold'], 0.7)
        self.assertNotIn('overlap', chunk_node['settings'])
        self.assertEqual(processing_profile, 'partition:vlm:auto,chunk:chunk_by_similarity')

    def test_chunk_by_page_uses_page_chunk_settings(self) -> None:
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=self._create_values(
                multi_format_chunk_strategy='chunk_by_page',
                multi_format_chunk_new_after_n_chars='450',
            ),
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        chunk_node = request_parameters['workflow_nodes'][-1]
        self.assertEqual(chunk_node['subtype'], 'chunk_by_page')
        self.assertEqual(chunk_node['settings']['max_characters'], 600)
        self.assertEqual(chunk_node['settings']['new_after_n_chars'], 450)
        self.assertEqual(chunk_node['settings']['overlap'], 80)
        self.assertTrue(chunk_node['settings']['overlap_all'])
        self.assertNotIn('combine_text_under_n_chars', chunk_node['settings'])
        self.assertNotIn('similarity_threshold', chunk_node['settings'])
        self.assertEqual(processing_profile, 'partition:vlm:auto,chunk:chunk_by_page')

    def test_auto_route_uses_auto_partition_and_enrichment_chain(self) -> None:
        create_values = self._create_values(
            multi_format_strategy='auto',
            multi_format_enable_image_description='true',
            multi_format_enable_table_to_html='true',
            multi_format_enable_table_description='true',
            multi_format_enable_generative_ocr='true',
            multi_format_vlm_provider='openai',
            multi_format_vlm_model='gpt-4o',
        )
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=create_values,
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Image Description', 'Table to HTML', 'Table Description', 'Generative OCR', 'Chunker'],
        )
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertEqual(partition_node['subtype'], 'vlm')
        self.assertTrue(partition_node['settings']['is_dynamic'])
        self.assertNotIn('strategy', partition_node['settings'])
        self.assertEqual(partition_node['settings']['provider'], 'openai')
        self.assertEqual(partition_node['settings']['model'], 'gpt-4o')
        enrichment_nodes = request_parameters['workflow_nodes'][1:-1]
        self.assertEqual(enrichment_nodes[0]['settings'], {'provider_type': 'openai', 'model': 'gpt-5-mini'})
        self.assertEqual(enrichment_nodes[1]['settings'], {})
        self.assertEqual(enrichment_nodes[2]['settings'], {'provider_type': 'openai', 'model': 'gpt-5-mini'})
        self.assertEqual(enrichment_nodes[3]['settings'], {'provider_type': 'openai', 'model': 'gpt-5-mini'})
        self.assertTrue(processing_profile.startswith('partition:vlm:auto'))

    def test_hi_res_route_builds_partition_enrich_chunk_chain(self) -> None:
        create_values = self._create_values(
            multi_format_strategy='hi_res',
            multi_format_infer_table_structure='true',
            multi_format_enable_image_description='true',
            multi_format_enable_table_to_html='true',
            multi_format_enable_table_description='true',
            multi_format_enable_generative_ocr='true',
        )
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=create_values,
            src=Path('sample.pdf'),
            partition_strategy='hi_res',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Image Description', 'Table to HTML', 'Table Description', 'Generative OCR', 'Chunker'],
        )
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertEqual(
            partition_node['settings']['extract_image_block_types'],
            ['Table', 'Image', 'Text', 'NarrativeText', 'Title', 'ListItem', 'UncategorizedText'],
        )
        self.assertTrue(partition_node['settings']['infer_table_structure'])
        self.assertTrue(partition_node['settings']['pdf_infer_table_structure'])
        self.assertTrue(processing_profile.endswith('chunk:chunk_by_character'))

    def test_fast_route_skips_enrichment_nodes(self) -> None:
        create_values = self._create_values(
            multi_format_strategy='fast',
            multi_format_enable_image_description='true',
            multi_format_enable_table_to_html='true',
            multi_format_enable_table_description='true',
            multi_format_enable_generative_ocr='true',
        )
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=create_values,
            src=Path('sample.pdf'),
            partition_strategy='fast',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Chunker'],
        )
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertEqual(partition_node['subtype'], 'unstructured_api')
        self.assertEqual(partition_node['settings']['strategy'], 'fast')
        self.assertEqual(partition_node['settings']['ocr_languages'], ['eng'])
        self.assertEqual(processing_profile, 'partition:unstructured_api:fast,chunk:chunk_by_character')

    def test_vlm_route_skips_enrichment_nodes(self) -> None:
        create_values = self._create_values(
            multi_format_strategy='vlm',
            multi_format_enable_image_description='true',
            multi_format_enable_table_to_html='true',
            multi_format_enable_table_description='true',
            multi_format_enable_generative_ocr='true',
        )
        request_parameters, warnings, _processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=create_values,
            src=Path('sample.pdf'),
            partition_strategy='vlm',
            languages=[],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Chunker'],
        )

    def test_bookrag_reusable_workflow_builds_expected_node_order(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_workflow_name='BookRAG Raw Prod',
            multi_format_bookrag_enable_image_description='true',
            multi_format_bookrag_enable_table_to_html='true',
            multi_format_bookrag_enable_table_description='true',
            multi_format_bookrag_enable_generative_ocr='true',
            multi_format_bookrag_enable_ner='true',
            multi_format_bookrag_image_description_subtype='openai_image_description',
            multi_format_bookrag_table_to_html_subtype='openai_table2html',
            multi_format_bookrag_table_description_subtype='openai_table_description',
            multi_format_bookrag_generative_ocr_subtype='openai_ocr',
            multi_format_bookrag_image_description_provider_type='openai',
            multi_format_bookrag_image_description_model='gpt-4o-mini',
            multi_format_bookrag_table_to_html_provider_type='openai',
            multi_format_bookrag_table_to_html_model='gpt-4o-mini',
            multi_format_bookrag_table_description_provider_type='openai',
            multi_format_bookrag_table_description_model='gpt-4o-mini',
            multi_format_bookrag_generative_ocr_provider_type='openai',
            multi_format_bookrag_generative_ocr_model='gpt-4o-mini',
            multi_format_bookrag_ner_subtype='openai_ner',
            multi_format_bookrag_ner_provider_type='openai',
            multi_format_bookrag_ner_model='gpt-4o-mini',
        )
        workflow_name, workflow_nodes, request_parameters, warnings, processing_profile = multi_format._build_bookrag_reusable_workflow_definition(
            create_values=create_values,
            partition_strategy='hi_res',
            languages=['eng'],
            image_partition_parameters={
                'infer_table_structure': True,
                'extract_image_block_types': ['Image', 'Table'],
                'unique_element_ids': True,
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(workflow_name, 'BookRAG_Raw_Prod')
        self.assertEqual(request_parameters['workflow_name'], 'BookRAG_Raw_Prod')
        self.assertEqual(
            [node['name'] for node in workflow_nodes],
            ['Partitioner', 'Image Description', 'Table to HTML', 'Table Description', 'Generative OCR', 'Named Entity Recognition'],
        )
        self.assertEqual(workflow_nodes[0]['settings']['strategy'], 'hi_res')
        for node in workflow_nodes[1:5]:
            self.assertEqual(node['settings']['provider_type'], 'openai')
            self.assertEqual(node['settings']['model'], 'gpt-4o-mini')
        self.assertEqual(workflow_nodes[-1]['subtype'], 'openai_ner')
        self.assertEqual(workflow_nodes[-1]['settings']['provider_type'], 'openai')
        self.assertEqual(workflow_nodes[-1]['settings']['model'], 'gpt-4o-mini')
        self.assertIn('image_description', processing_profile)
        self.assertIn('generative_ocr', processing_profile)
        self.assertIn('ner:openai_ner', processing_profile)

    def test_bookrag_auto_partition_warns_on_ignored_inputs(self) -> None:
        partition_node, request_parameters, warnings = multi_format._build_bookrag_workflow_partition_node(
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            image_partition_parameters={
                'extract_image_block_types': ['Image', 'Table'],
                'infer_table_structure': True,
                'unique_element_ids': True,
            },
        )

        self.assertEqual(partition_node['subtype'], 'vlm')
        self.assertTrue(partition_node['settings']['is_dynamic'])
        self.assertNotIn('strategy', partition_node['settings'])
        self.assertEqual(request_parameters['workflow_nodes'], [partition_node])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any('ocr_languages' in warning for warning in warnings))
        self.assertTrue(any('extract_image_block_types' in warning for warning in warnings))
        self.assertTrue(any('infer_table_structure' in warning for warning in warnings))

    def test_bookrag_auto_partition_accepts_vlm_provider_settings(self) -> None:
        partition_node, _request_parameters, warnings = multi_format._build_bookrag_workflow_partition_node(
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=[],
            image_partition_parameters={
                'vlm_provider': 'openai',
                'vlm_model': 'gpt-4o',
                'vlm_provider_api_key': 'secret-key',
                'unique_element_ids': True,
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(partition_node['subtype'], 'vlm')
        self.assertTrue(partition_node['settings']['is_dynamic'])
        self.assertNotIn('strategy', partition_node['settings'])
        self.assertEqual(partition_node['settings']['provider'], 'openai')
        self.assertEqual(partition_node['settings']['model'], 'gpt-4o')
        self.assertEqual(partition_node['settings']['provider_api_key'], 'secret-key')

    def test_bookrag_vlm_keeps_explicit_azure_openai_provider(self) -> None:
        partition_node, _request_parameters, warnings = multi_format._build_bookrag_workflow_partition_node(
            src=Path('sample.pdf'),
            partition_strategy='vlm',
            languages=[],
            image_partition_parameters={
                'vlm_provider': 'azure_openai',
                'vlm_model': 'gpt-5-mini',
                'unique_element_ids': True,
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(partition_node['settings']['provider'], 'azure_openai')
        self.assertEqual(partition_node['settings']['model'], 'gpt-5-mini')

    def test_bookrag_hi_res_partition_forwards_coordinates(self) -> None:
        partition_node, _request_parameters, warnings = multi_format._build_bookrag_workflow_partition_node(
            src=Path('sample.pdf'),
            partition_strategy='hi_res',
            languages=['eng'],
            image_partition_parameters={
                'coordinates': False,
                'unique_element_ids': True,
            },
        )

        self.assertEqual(warnings, [])
        self.assertFalse(partition_node['settings']['coordinates'])

    def test_bookrag_ner_model_mismatch_fails_before_submission(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_enable_ner='true',
            multi_format_bookrag_ner_subtype='openai_ner',
            multi_format_bookrag_ner_model='claude-sonnet-4-20250514',
        )
        with self.assertRaisesRegex(ValueError, "does not match provider_type"):
            multi_format._build_bookrag_reusable_workflow_definition(
                create_values=create_values,
                partition_strategy='hi_res',
                languages=[],
                image_partition_parameters={'unique_element_ids': True},
            )

    def test_bookrag_explicit_vlm_omits_redundant_enrichments(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_enable_image_description='true',
            multi_format_bookrag_enable_table_to_html='true',
            multi_format_bookrag_enable_table_description='true',
            multi_format_bookrag_enable_generative_ocr='true',
        )

        _workflow_name, workflow_nodes, _request_parameters, warnings, _profile = (
            multi_format._build_bookrag_reusable_workflow_definition(
                create_values=create_values,
                partition_strategy='vlm',
                languages=[],
                image_partition_parameters={'unique_element_ids': True},
            )
        )

        self.assertEqual([node['name'] for node in workflow_nodes], ['Partitioner'])
        self.assertTrue(any('redundant enrichment nodes were omitted' in warning for warning in warnings))

    def test_bookrag_explicit_vlm_keeps_ner(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_enable_ner='true',
            multi_format_bookrag_ner_subtype='openai_ner',
            multi_format_bookrag_ner_provider_type='openai',
            multi_format_bookrag_ner_model='gpt-4o-mini',
        )

        _workflow_name, workflow_nodes, _request_parameters, warnings, _profile = (
            multi_format._build_bookrag_reusable_workflow_definition(
                create_values=create_values,
                partition_strategy='vlm',
                languages=[],
                image_partition_parameters={'unique_element_ids': True},
            )
        )

        self.assertEqual([node['name'] for node in workflow_nodes], ['Partitioner', 'Named Entity Recognition'])
        self.assertEqual(workflow_nodes[-1]['settings'], {'provider_type': 'openai', 'model': 'gpt-4o-mini'})
        self.assertEqual(warnings, [])

    def test_bookrag_image_partition_options_read_bookrag_overrides(self) -> None:
        options, warnings, summary = multi_format._resolve_bookrag_image_partition_options(
            self._create_values(
                multi_format_bookrag_extract_image_block_types='Image,Table',
                multi_format_bookrag_infer_table_structure='true',
                multi_format_bookrag_hi_res_model_name='layout-v2',
                multi_format_bookrag_vlm_provider='openai',
                multi_format_bookrag_vlm_model='gpt-4o',
                multi_format_bookrag_vlm_provider_api_key='secret-key',
                multi_format_bookrag_coordinates='false',
            )
        )

        self.assertEqual(warnings, [])
        self.assertEqual(options['extract_image_block_types'], ['Image', 'Table'])
        self.assertTrue(options['infer_table_structure'])
        self.assertEqual(options['hi_res_model_name'], 'layout-v2')
        self.assertEqual(options['vlm_provider'], 'openai')
        self.assertEqual(options['vlm_model'], 'gpt-4o')
        self.assertEqual(options['vlm_provider_api_key'], 'secret-key')
        self.assertFalse(options['coordinates'])
        self.assertEqual(summary['vlm_provider'], 'openai')
        self.assertEqual(summary['vlm_model'], 'gpt-4o')
        self.assertTrue(summary['vlm_provider_api_key_configured'])

    def test_multi_format_definition_ignores_bookrag_namespaced_inputs(self) -> None:
        create_values = self._create_values(
            multi_format_strategy='auto',
            multi_format_bookrag_vlm_provider='openai',
            multi_format_bookrag_vlm_model='gpt-4o',
            multi_format_bookrag_enable_image_description='true',
        )
        request_parameters, warnings, processing_profile = multi_format._build_multi_format_workflow_definition(
            create_values=create_values,
            src=Path('sample.pdf'),
            partition_strategy='auto',
            languages=['eng'],
            chunk_size=600,
            chunk_overlap=80,
            include_orig_elements=False,
            overlap_all=True,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [node['name'] for node in request_parameters['workflow_nodes']],
            ['Partitioner', 'Chunker'],
        )
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertNotIn('provider', partition_node['settings'])
        self.assertNotIn('model', partition_node['settings'])
        self.assertEqual(processing_profile, 'partition:vlm:auto,chunk:chunk_by_character')

    def test_as_text_strips_invalid_unicode_for_teradata(self) -> None:
        raw = "A\x00B\ud800C\ufdd0D\ufffeE🆓\nF\tG"
        self.assertEqual(multi_format._as_text(raw), "ABCDE\nF\tG")

    def test_insert_chunk_rows_omits_invalid_unicode_from_sql_literals(self) -> None:
        executed: list[str] = []

        rows = [
            {
                "text": multi_format._as_text("Hello\x00World\ud800"),
                "type": "NarrativeText",
                "filename": "demo.pdf",
                "element_id": "el-1",
                "id": "000000000001",
                "table_id": None,
                "chunk_index": 1,
                "is_continuation": False,
                "num_carried_over_header_rows": None,
                "partitioner_type": "vlm",
                "image_description": multi_format._as_text("img\ufffe"),
                "table_description": None,
                "generative_ocr": None,
                "table_to_html": None,
                "filetype": "application/pdf",
                "date_processed": "2026-05-27 00:00:00",
            }
        ]

        inserted = multi_format._insert_chunk_rows(
            schema_name=None,
            table_name="demo_unstructured",
            rows=rows,
            execute_sql_fn=executed.append,
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(len(executed), 1)
        sql = executed[0]
        self.assertIn("HelloWorld", sql)
        self.assertIn("img", sql)
        self.assertNotIn("\x00", sql)
        self.assertNotIn("\ud800", sql)
        self.assertNotIn("\ufffe", sql)

    def test_bookrag_image_partition_options_ignore_multi_format_inputs(self) -> None:
        options, warnings, summary = multi_format._resolve_bookrag_image_partition_options(
            self._create_values(
                multi_format_extract_image_block_types='Image,Table',
                multi_format_infer_table_structure='true',
                multi_format_hi_res_model_name='layout-v2',
                multi_format_vlm_provider='openai',
                multi_format_vlm_model='gpt-4o',
                multi_format_vlm_provider_api_key='secret-key',
            )
        )

        self.assertEqual(options['extract_image_block_types'], ['Image', 'Table'])
        self.assertFalse(options['infer_table_structure'])
        self.assertNotIn('hi_res_model_name', options)
        self.assertIsNone(options['vlm_provider'])
        self.assertIsNone(options['vlm_model'])
        self.assertIsNone(options['vlm_provider_api_key'])
        self.assertTrue(options['coordinates'])
        self.assertEqual(warnings, [])
        self.assertIsNone(summary['hi_res_model_name'])
        self.assertIsNone(summary['vlm_provider'])
        self.assertIsNone(summary['vlm_model'])
        self.assertFalse(summary['vlm_provider_api_key_configured'])

    def test_generic_runtime_defaults_do_not_cross_between_modes(self) -> None:
        shared_runtime = {
            'infer_table_structure': 'true',
            'hi_res_model_name': 'shared-layout',
            'vlm_provider': 'openai',
            'vlm_model': 'gpt-4o',
            'vlm_provider_api_key': 'shared-secret',
            'extract_image_block_types': 'Image,Table',
            'unique_element_ids': 'false',
        }
        with mock.patch('app.services.multi_format._load_unstructured_runtime_settings', return_value=shared_runtime):
            bookrag_options, bookrag_warnings, _summary = multi_format._resolve_bookrag_image_partition_options(self._create_values())
            request_parameters, workflow_warnings, _profile = multi_format._build_multi_format_workflow_definition(
                create_values=self._create_values(multi_format_strategy='hi_res'),
                src=Path('sample.pdf'),
                partition_strategy='hi_res',
                languages=['eng'],
                chunk_size=600,
                chunk_overlap=80,
                include_orig_elements=False,
                overlap_all=True,
            )

        self.assertEqual(workflow_warnings, [])
        partition_node = request_parameters['workflow_nodes'][0]
        self.assertNotIn('infer_table_structure', partition_node['settings'])
        self.assertNotIn('hi_res_model_name', partition_node['settings'])
        self.assertNotIn('provider', partition_node['settings'])
        self.assertNotIn('model', partition_node['settings'])
        self.assertEqual(bookrag_options['extract_image_block_types'], ['Image', 'Table'])
        self.assertFalse(bookrag_options['infer_table_structure'])
        self.assertTrue(bookrag_options['unique_element_ids'])
        self.assertIsNone(bookrag_options['vlm_provider'])
        self.assertIsNone(bookrag_options['vlm_model'])
        self.assertIsNone(bookrag_options['vlm_provider_api_key'])
        self.assertEqual(bookrag_warnings, [])

    def test_chunk_rows_use_sequence_ids_and_map_selected_metadata(self) -> None:
        row = multi_format._element_to_chunk_row(
            {
                'id': 'unstructured-element-7',
                'element_id': 'unstructured-element-7',
                'type': 'TableChunk',
                'text': 'chunk text',
                'metadata': {
                    'filename': 'demo.pdf',
                    'table_id': 'table-1',
                    'page_number': 3,
                    'chunk_index': 3,
                    'is_continuation': True,
                    'num_carried_over_header_rows': 2,
                    'partitioner_type': 'hi_res_partition',
                    'image_description': 'image summary',
                    'table_description': 'table summary',
                    'generative_ocr': 'ocr text',
                    'text_as_html': '<table><tr><td>x</td></tr></table>',
                },
            },
            src=Path(__file__),
            content_type='application/pdf',
            row_sequence=7,
        )

        self.assertIsNotNone(row)
        self.assertEqual(row['id'], '000000000007')
        self.assertEqual(row['element_id'], 'unstructured-element-7')
        self.assertEqual(row['filename'], 'demo.pdf')
        self.assertEqual(row['table_id'], 'table-1')
        self.assertEqual(row['page_number'], 3)
        self.assertEqual(row['chunk_index'], 3)
        self.assertTrue(row['is_continuation'])
        self.assertEqual(row['num_carried_over_header_rows'], 2)
        self.assertEqual(row['partitioner_type'], 'hi_res_partition')
        self.assertEqual(row['image_description'], 'image summary')
        self.assertEqual(row['table_description'], 'table summary')
        self.assertEqual(row['generative_ocr'], 'ocr text')
        self.assertEqual(row['text_as_html'], '<table><tr><td>x</td></tr></table>')
        self.assertEqual(row['table_to_html'], '<table><tr><td>x</td></tr></table>')
        self.assertNotIn('record_id', row)
        self.assertNotIn('parent_id', row)

    def test_chunk_row_keeps_page_and_does_not_treat_narrative_html_as_table_html(self) -> None:
        row = multi_format._element_to_chunk_row(
            {
                'element_id': 'element-1',
                'type': 'CompositeElement',
                'text': 'body text',
                'metadata': {
                    'filename': 'demo.pdf',
                    'page_number': 11,
                    'text_as_html': '<p>body text</p>',
                    'table_to_html': '<table><tr><td>not a table element</td></tr></table>',
                },
            },
            src=Path('fallback.pdf'),
            content_type='application/pdf',
            row_sequence=8,
        )

        self.assertIsNotNone(row)
        self.assertEqual(row['page_number'], 11)
        self.assertEqual(row['text_as_html'], '<p>body text</p>')
        self.assertIsNone(row['table_to_html'])

    def test_chunk_row_filename_uses_unstructured_metadata_filename(self) -> None:
        src = Path('A S 茜町（異動2019.01.30）.pdf')
        row = multi_format._element_to_chunk_row(
            {
                'element_id': 'element-1',
                'type': 'NarrativeText',
                'text': 'body text',
                'metadata': {
                    'filename': 'AS茜町異動20190130-c4dcc7ff.pdf',
                    'filetype': 'application/pdf',
                },
            },
            src=src,
            content_type='application/pdf',
            row_sequence=1,
        )

        self.assertIsNotNone(row)
        self.assertEqual(row['filename'], 'AS茜町異動20190130-c4dcc7ff.pdf')

    def test_bookrag_document_and_root_node_keep_source_file_name(self) -> None:
        from app.services.bookrag_storage import build_bookrag_document_row
        from app.services.bookrag_tree import build_bookrag_nodes

        src = Path('A S 茜町（異動2019.01.30）.pdf')
        document_row = build_bookrag_document_row(
            doc_id='doc-1',
            vector_store_name='demo',
            workflow_id='workflow-1',
            workflow_name='BookRAG_Test',
            job_id='job-1',
            processing_profile='partition:vlm:vlm',
            filename=src.name,
            source_file='raw_stage/AS_20190130_doc-1.json',
            filetype='application/pdf',
            filesize_bytes=123,
            page_count=2,
            language_hint='jpn',
            created_at='2026-07-14 03:10:00',
        )
        nodes = build_bookrag_nodes(document_row, [])

        self.assertEqual(document_row['filename'], src.name)
        self.assertEqual(document_row['page_count'], 2)
        self.assertEqual(document_row['language_hint'], 'jpn')
        self.assertEqual(document_row['created_at'], '2026-07-14 03:10:00')
        self.assertEqual(nodes[0]['title'], src.name)
        self.assertEqual(nodes[0]['path'], src.name)



    def test_bookrag_pipeline_bypasses_reconcile_for_entities(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_generate_entities='true',
            multi_format_bookrag_generate_entity_links='true',
            multi_format_bookrag_generate_entity_relations='true',
        )
        raw_payload = [
            {
                'type': 'NarrativeText',
                'element_id': 'raw-1',
                'text': 'Raw payload without entities.',
                'metadata': {
                    'page_number': 1,
                    'category_depth': 1,
                },
            },
        ]
        reconciled_payload = [
            {
                'type': 'NarrativeText',
                'element_id': 'recon-1',
                'text': 'Reconciled payload with entities.',
                'metadata': {
                    'page_number': 1,
                    'category_depth': 1,
                    'entities': {
                        'items': [
                            {'entity': 'Demo Corp', 'type': 'ORGANIZATION'},
                        ],
                        'relationships': [
                            {'from': 'Demo Corp', 'relationship': 'published_in', 'to': '2026-04-18'},
                        ],
                    },
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            src = tmp_path / 'sample.txt'
            src.write_text('demo', encoding='utf-8')
            debug_dir = tmp_path / 'debug'
            raw_stage_dir = tmp_path / 'raw_stage'
            csv_stage_dir = tmp_path / 'csv_stage'

            captured: dict[str, object] = {}

            def _persist_nodes(*, nodes, **kwargs):
                captured['nodes'] = nodes
                return len(nodes)

            def _persist_entities(*, entities, **kwargs):
                captured['entities'] = entities
                return len(entities)

            def _persist_entity_links(*, entity_links, **kwargs):
                captured['entity_links'] = entity_links
                return len(entity_links)

            def _persist_entity_relations(*, entity_relations, **kwargs):
                captured['entity_relations'] = entity_relations
                return len(entity_relations)

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_block_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_node_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_link_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_raw_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format._load_unstructured_runtime_config', return_value=('key', 'https://example.invalid')))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_unstructured_request_timeout_ms', return_value=120000))
                stack.enter_context(mock.patch('app.services.multi_format._create_unstructured_client', return_value=object()))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_unstructured_debug_dir', return_value=debug_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_raw_stage_dir', return_value=raw_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_csv_stage_dir', return_value=csv_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_bookrag_workflow_poll_config', return_value=(30, 1)))
                stack.enter_context(mock.patch('app.services.multi_format._enforce_unstructured_job_submission_spacing', side_effect=lambda value: value))
                stack.enter_context(mock.patch('app.services.multi_format._build_bookrag_reusable_workflow_definition', return_value=('BookRAG_Test', [{'name': 'Partitioner'}], {'workflow_name': 'BookRAG_Test', 'workflow_nodes': [{'name': 'Partitioner'}]}, [], 'partition:vlm:vlm')))
                stack.enter_context(mock.patch('app.services.multi_format._run_unstructured_workflow_job_for_file', return_value=(raw_payload, raw_payload, {'workflow_name': 'BookRAG_Test'}, 'job-1', 'workflow-1', 'BookRAG_Test')))
                reconcile_mock = stack.enter_context(mock.patch('app.services.multi_format.reconcile_unstructured_elements', return_value=reconciled_payload))
                stack.enter_context(mock.patch('app.services.multi_format._write_unstructured_debug_file', return_value=''))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_documents', return_value=1))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_raw_rows', return_value=1))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_blocks', return_value=1))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_nodes', side_effect=_persist_nodes))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entities', side_effect=_persist_entities))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entity_links', side_effect=_persist_entity_links))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entity_relations', side_effect=_persist_entity_relations))
                stack.enter_context(mock.patch('app.services.multi_format._count_teradata_rows', return_value=1))
                _, summary = multi_format._apply_bookrag_tree_pipeline(
                    exec_payload={
                        'document_files': [str(src)],
                        'nv_ingestor': 'legacy-file-ingestor',
                        'custom_ingestor': 'legacy-custom-ingestor',
                        'ingest_host': 'legacy-host',
                    },
                    create_values=create_values,
                    vector_store_name='demo',
                    execute_sql_fn=mock.Mock(),
                    resolve_path_hint=lambda value: value,
                    effective_schema_name='demo_schema',
                    document_files=[str(src)],
                    partition_strategy='vlm',
                    ocr_languages=['jpn'],
                    target_warnings=[],
                )

        reconcile_mock.assert_not_called()
        self.assertEqual(summary['entity_count'], 0)
        self.assertEqual(summary['entity_link_count'], 0)
        self.assertEqual(summary['entity_relation_count'], 0)
        self.assertEqual(captured['nodes'][1]['source_element_id'], 'raw-1')
        self.assertEqual(captured.get('entities', []), [])
        self.assertEqual(captured.get('entity_links', []), [])
        self.assertEqual(captured.get('entity_relations', []), [])

    def test_bookrag_pipeline_persists_tree_outputs(self) -> None:
        create_values = self._create_values(
            multi_format_bookrag_generate_entities='true',
            multi_format_bookrag_generate_entity_links='true',
            multi_format_bookrag_generate_entity_relations='true',
        )
        raw_payload = [
            {
                'type': 'Title',
                'element_id': 'title-1',
                'text': 'Section Overview',
                'metadata': {
                    'page_number': 1,
                    'category_depth': 2,
                    'parent_id': 'section-1',
                },
            },
            {
                'type': 'NarrativeText',
                'element_id': 'text-1',
                'text': 'This is the body text.',
                'metadata': {
                    'page_number': 1,
                    'category_depth': 2,
                    'parent_id': 'section-1',
                    'entities': {
                        'items': [
                            {'entity': 'Demo Corp', 'type': 'ORGANIZATION'},
                            {'entity': '2026-04-18', 'type': 'DATE'},
                        ],
                        'relationships': [
                            {'from': 'Demo Corp', 'relationship': 'published_in', 'to': '2026-04-18'},
                        ],
                    },
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            src = tmp_path / 'sample.txt'
            src.write_text('demo', encoding='utf-8')
            raw_stage_dir = tmp_path / 'raw_stage'
            csv_stage_dir = tmp_path / 'csv_stage'
            debug_dir = tmp_path / 'debug'

            captured: dict[str, object] = {}

            def _persist_documents(*, rows, **kwargs):
                captured['document_rows'] = rows
                return len(rows)

            def _persist_raw(*, rows, **kwargs):
                captured['raw_rows'] = rows
                return len(rows)

            def _persist_blocks(*, blocks, **kwargs):
                captured['blocks'] = blocks
                return len(blocks)

            def _persist_nodes(*, nodes, **kwargs):
                captured['nodes'] = nodes
                return len(nodes)

            def _persist_entities(*, entities, **kwargs):
                captured['entities'] = entities
                return len(entities)

            def _persist_entity_links(*, entity_links, **kwargs):
                captured['entity_links'] = entity_links
                return len(entity_links)

            def _persist_entity_relations(*, entity_relations, **kwargs):
                captured['entity_relations'] = entity_relations
                return len(entity_relations)

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_block_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_node_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_link_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_raw_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format._load_unstructured_runtime_config', return_value=('key', 'https://example.invalid')))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_unstructured_request_timeout_ms', return_value=120000))
                stack.enter_context(mock.patch('app.services.multi_format._create_unstructured_client', return_value=object()))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_unstructured_debug_dir', return_value=debug_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_raw_stage_dir', return_value=raw_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_csv_stage_dir', return_value=csv_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_bookrag_workflow_poll_config', return_value=(30, 1)))
                stack.enter_context(mock.patch('app.services.multi_format._enforce_unstructured_job_submission_spacing', side_effect=lambda value: value))
                stack.enter_context(mock.patch('app.services.multi_format._build_bookrag_reusable_workflow_definition', return_value=('BookRAG_Test', [{'name': 'Partitioner'}], {'workflow_name': 'BookRAG_Test', 'workflow_nodes': [{'name': 'Partitioner'}]}, [], 'partition:vlm:vlm')))
                stack.enter_context(mock.patch('app.services.multi_format._run_unstructured_workflow_job_for_file', return_value=(raw_payload, raw_payload, {'workflow_name': 'BookRAG_Test'}, 'job-1', 'workflow-1', 'BookRAG_Test')))
                stack.enter_context(mock.patch('app.services.multi_format._write_unstructured_debug_file', return_value=''))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_documents', side_effect=_persist_documents))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_raw_rows', side_effect=_persist_raw))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_blocks', side_effect=_persist_blocks))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_nodes', side_effect=_persist_nodes))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entities', side_effect=_persist_entities))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entity_links', side_effect=_persist_entity_links))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_entity_relations', side_effect=_persist_entity_relations))
                stack.enter_context(mock.patch('app.services.multi_format._count_teradata_rows', return_value=len(raw_payload)))
                patched_payload, summary = multi_format._apply_bookrag_tree_pipeline(
                    exec_payload={
                        'document_files': [str(src)],
                        'document_manifest': [
                            {
                                'doc_id': 'upload-doc-id',
                                'filename': src.name,
                                'saved_path': str(src),
                            }
                        ],
                    },
                    create_values=create_values,
                    vector_store_name='demo',
                    execute_sql_fn=mock.Mock(),
                    resolve_path_hint=lambda value: value,
                    effective_schema_name='demo_schema',
                    document_files=[str(src)],
                    partition_strategy='vlm',
                    ocr_languages=['jpn'],
                    target_warnings=[],
                )

        self.assertEqual(summary['workflow_mode'], 'bookrag on-demand jobs selected tables debug')
        self.assertEqual(summary['raw_element_count'], 2)
        self.assertEqual(summary['block_count'], 2)
        self.assertEqual(summary['node_count'], 3)
        self.assertEqual(summary['entity_count'], 2)
        self.assertEqual(summary['entity_link_count'], 2)
        self.assertEqual(summary['entity_relation_count'], 1)
        self.assertEqual(summary['bookrag_chunking_strategy'], 'disabled_for_tree_debug')
        self.assertEqual(patched_payload['object_names'], 'demo_schema.demo_bk_bnode')
        self.assertEqual(patched_payload['data_columns'], ['content'])
        self.assertEqual(patched_payload['key_columns'], ['doc_id', 'node_id'])
        self.assertNotIn('vector_column', patched_payload)
        self.assertNotIn('document_files', patched_payload)
        self.assertNotIn('nv_ingestor', patched_payload)
        self.assertNotIn('custom_ingestor', patched_payload)
        self.assertNotIn('ingest_host', patched_payload)
        self.assertIn('unstructured_bookrag_flg', patched_payload['description'])
        self.assertEqual(summary['vectorstore_source_object'], 'demo_schema.demo_bk_bnode')
        self.assertEqual(len(captured['document_rows']), 1)
        self.assertEqual(captured['document_rows'][0]['doc_id'], 'upload-doc-id')
        self.assertEqual(captured['document_rows'][0]['source_file'], str(src))
        self.assertEqual(captured['document_rows'][0]['page_count'], 1)
        self.assertEqual(captured['document_rows'][0]['language_hint'], 'jpn')
        self.assertTrue(captured['document_rows'][0]['created_at'])
        self.assertEqual(len(captured['raw_rows']), 2)
        self.assertEqual(len(captured['blocks']), 2)
        self.assertEqual(len(captured['nodes']), 3)
        self.assertEqual(len(captured['entities']), 2)
        self.assertEqual(len(captured['entity_links']), 2)
        self.assertEqual(len(captured['entity_relations']), 1)
        self.assertEqual(summary['bookrag_csv_stage_dir'], str(csv_stage_dir))
        self.assertEqual(len(summary['bookrag_csv_stage_files']), 7)


    def test_bookrag_document_parsing_runs_in_parallel_and_finishes_before_csv_stage(self) -> None:
        create_values = self._create_values()
        raw_payload_by_name = {
            'sample1.txt': [{'type': 'NarrativeText', 'element_id': 'text-1', 'text': 'One', 'metadata': {'page_number': 1}}],
            'sample2.txt': [{'type': 'NarrativeText', 'element_id': 'text-2', 'text': 'Two', 'metadata': {'page_number': 1}}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sources = [tmp_path / 'sample1.txt', tmp_path / 'sample2.txt']
            for src in sources:
                src.write_text('demo', encoding='utf-8')
            persist_mocks: list[mock.Mock] = []
            parse_barrier = threading.Barrier(2)
            stage_lock = threading.Lock()
            jobs_finished = 0
            csv_stage_observations: list[int] = []

            def _run_job(client=None, *, src, **kwargs):
                nonlocal jobs_finished
                parse_barrier.wait(timeout=5)
                payload = raw_payload_by_name[src.name]
                with stage_lock:
                    jobs_finished += 1
                return payload, payload, {}, f'job-{src.stem}', 'workflow-1', 'BookRAG_Test'

            def _stop_at_csv_stage(**kwargs):
                with stage_lock:
                    csv_stage_observations.append(jobs_finished)
                raise RuntimeError('stop after isolated document parsing test')

            with contextlib.ExitStack() as stack:
                for patch_target in (
                    'prepare_bookrag_document_table',
                    'prepare_bookrag_raw_table',
                    'prepare_bookrag_block_table',
                    'prepare_bookrag_node_table',
                    'prepare_bookrag_document_relation_table',
                    'prepare_bookrag_entity_table',
                    'prepare_bookrag_entity_link_table',
                    'prepare_bookrag_entity_relation_table',
                ):
                    stack.enter_context(mock.patch(f'app.services.multi_format.{patch_target}', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format._load_unstructured_runtime_config', return_value=('key', 'https://example.invalid')))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_unstructured_request_timeout_ms', return_value=120000))
                stack.enter_context(mock.patch('app.services.multi_format._create_unstructured_client', return_value=object()))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_unstructured_debug_dir', return_value=tmp_path / 'debug'))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_raw_stage_dir', return_value=tmp_path / 'raw'))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_csv_stage_dir', return_value=tmp_path / 'csv'))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_bookrag_workflow_poll_config', return_value=(30, 1)))
                stack.enter_context(mock.patch('app.services.multi_format._enforce_unstructured_job_submission_spacing', side_effect=lambda value: value))
                stack.enter_context(mock.patch('app.services.multi_format._build_bookrag_reusable_workflow_definition', return_value=('BookRAG_Test', [], {'workflow_name': 'BookRAG_Test', 'workflow_nodes': []}, [], 'partition:fast')))
                stack.enter_context(mock.patch('app.services.multi_format._run_unstructured_workflow_job_for_file', side_effect=_run_job))
                stack.enter_context(mock.patch('app.services.multi_format._write_unstructured_debug_file', return_value=''))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_table_csv', side_effect=_stop_at_csv_stage))
                for patch_target in (
                    'persist_bookrag_documents',
                    'persist_bookrag_raw_rows',
                    'persist_bookrag_blocks',
                    'persist_bookrag_nodes',
                ):
                    persist_mocks.append(
                        stack.enter_context(mock.patch(f'app.services.multi_format.{patch_target}'))
                    )

                with self.assertRaisesRegex(RuntimeError, 'parallel JSON-to-CSV processing failed'):
                    multi_format._apply_bookrag_tree_pipeline(
                        exec_payload={'document_files': [str(src) for src in sources]},
                        create_values=create_values,
                        vector_store_name='demo',
                        execute_sql_fn=mock.Mock(),
                        resolve_path_hint=lambda value: value,
                        effective_schema_name='demo_schema',
                        document_files=[str(src) for src in sources],
                        partition_strategy='fast',
                        ocr_languages=['eng'],
                        target_warnings=[],
                    )

            for persist_mock in persist_mocks:
                persist_mock.assert_not_called()
            self.assertEqual(jobs_finished, 2)
            self.assertTrue(csv_stage_observations)
            self.assertEqual(set(csv_stage_observations), {2})

    def test_bookrag_csv_prepare_and_load_stages_run_concurrently_after_barriers(self) -> None:
        create_values = self._create_values()
        raw_payload_by_name = {
            'sample1.txt': [
                {
                    'type': 'NarrativeText',
                    'element_id': 'text-1',
                    'text': 'First document text.',
                    'metadata': {'page_number': 1},
                },
            ],
            'sample2.txt': [
                {
                    'type': 'NarrativeText',
                    'element_id': 'text-2',
                    'text': 'Second document text.',
                    'metadata': {'page_number': 1},
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sources = [tmp_path / 'sample1.txt', tmp_path / 'sample2.txt']
            for src in sources:
                src.write_text('demo', encoding='utf-8')
            raw_stage_dir = tmp_path / 'raw_stage'
            csv_stage_dir = tmp_path / 'csv_stage'
            debug_dir = tmp_path / 'debug'
            document_batch_sizes: list[int] = []
            document_csv_dirs: list[Path] = []
            raw_batch_sizes: list[int] = []
            block_batch_sizes: list[int] = []
            node_batch_sizes: list[int] = []
            document_relation_rows: list[dict] = []
            stage_lock = threading.Lock()
            csv_prepare_barrier = threading.Barrier(2)
            csv_load_barrier = threading.Barrier(2)
            jobs_finished = 0
            csv_prepared = 0
            loads_started = 0
            stage_violations: list[str] = []
            real_prepare_csv = multi_format.prepare_bookrag_table_csv

            def _run_job(client=None, *, src, **kwargs):
                nonlocal jobs_finished
                payload = raw_payload_by_name[src.name]
                with stage_lock:
                    jobs_finished += 1
                return payload, payload, {'workflow_name': 'BookRAG_Test'}, f'job-{src.stem}', 'workflow-1', 'BookRAG_Test'

            def _prepare_csv(*, table_key, **kwargs):
                nonlocal csv_prepared
                with stage_lock:
                    if jobs_finished != len(sources):
                        stage_violations.append('CSV preparation started before every JSON completed.')
                if table_key == 'documents':
                    csv_prepare_barrier.wait(timeout=2)
                csv_path = real_prepare_csv(table_key=table_key, **kwargs)
                with stage_lock:
                    csv_prepared += 1
                return csv_path

            def _before_csv_load() -> None:
                nonlocal loads_started
                with stage_lock:
                    if csv_prepared != len(sources) * 7:
                        stage_violations.append('CSV loading started before every CSV completed.')
                    load_index = loads_started
                    loads_started += 1
                if load_index < 2:
                    csv_load_barrier.wait(timeout=2)

            def _persist_documents(*, rows, csv_stage_dir=None, **kwargs):
                _before_csv_load()
                document_batch_sizes.append(len(rows))
                document_csv_dirs.append(Path(csv_stage_dir))
                return len(rows)

            def _persist_raw(*, rows, **kwargs):
                _before_csv_load()
                raw_batch_sizes.append(len(rows))
                return len(rows)

            def _persist_blocks(*, blocks, **kwargs):
                _before_csv_load()
                block_batch_sizes.append(len(blocks))
                return len(blocks)

            def _persist_nodes(*, nodes, **kwargs):
                _before_csv_load()
                node_batch_sizes.append(len(nodes))
                return len(nodes)

            def _suggest_relations(documents):
                newer, older = documents
                return [
                    {
                        "from_doc_id": newer["doc_id"],
                        "from_filename": newer["filename"],
                        "relation_type": "next_issue_of",
                        "to_doc_id": older["doc_id"],
                        "to_filename": older["filename"],
                        "relation_description": "Filename rule created an initial relationship.",
                        "source_type": "rule",
                    }
                ]

            def _persist_relations(*, relations, **kwargs):
                document_relation_rows.extend(relations)
                return len(relations)

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict('os.environ', {'BOOKRAG_UNSTRUCTURED_WORKERS': '2'}))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_block_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_node_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_document_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_raw_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_link_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_entity_relation_table', return_value=[]))
                stack.enter_context(mock.patch('app.services.multi_format._load_unstructured_runtime_config', return_value=('key', 'https://example.invalid')))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_unstructured_request_timeout_ms', return_value=120000))
                stack.enter_context(mock.patch('app.services.multi_format._create_unstructured_client', return_value=object()))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_unstructured_debug_dir', return_value=debug_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_raw_stage_dir', return_value=raw_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._prepare_bookrag_csv_stage_dir', return_value=csv_stage_dir))
                stack.enter_context(mock.patch('app.services.multi_format._resolve_bookrag_workflow_poll_config', return_value=(30, 1)))
                stack.enter_context(mock.patch('app.services.multi_format._enforce_unstructured_job_submission_spacing', side_effect=lambda value: value))
                stack.enter_context(mock.patch('app.services.multi_format._build_bookrag_reusable_workflow_definition', return_value=('BookRAG_Test', [{'name': 'Partitioner'}], {'workflow_name': 'BookRAG_Test', 'workflow_nodes': [{'name': 'Partitioner'}]}, [], 'partition:vlm:vlm')))
                stack.enter_context(mock.patch('app.services.multi_format._run_unstructured_workflow_job_for_file', side_effect=_run_job))
                stack.enter_context(mock.patch('app.services.multi_format._write_unstructured_debug_file', return_value=''))
                stack.enter_context(mock.patch('app.services.multi_format.prepare_bookrag_table_csv', side_effect=_prepare_csv))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_documents', side_effect=_persist_documents))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_raw_rows', side_effect=_persist_raw))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_blocks', side_effect=_persist_blocks))
                stack.enter_context(mock.patch('app.services.multi_format.persist_bookrag_nodes', side_effect=_persist_nodes))
                stack.enter_context(mock.patch('app.services.multi_format.derive_filename_document_relations', side_effect=_suggest_relations))
                stack.enter_context(mock.patch('app.services.multi_format.persist_document_relations', side_effect=_persist_relations))
                stack.enter_context(mock.patch('app.services.multi_format._count_teradata_rows', return_value=2))

                _, summary = multi_format._apply_bookrag_tree_pipeline(
                    exec_payload={'document_files': [str(src) for src in sources]},
                    create_values=create_values,
                    vector_store_name='demo',
                    execute_sql_fn=mock.Mock(),
                    resolve_path_hint=lambda value: value,
                    effective_schema_name='demo_schema',
                    document_files=[str(src) for src in sources],
                    partition_strategy='vlm',
                    ocr_languages=['jpn'],
                    target_warnings=[],
                )

        self.assertEqual(summary['document_count'], 2)
        self.assertEqual(
            summary['bookrag_flush_config'],
            {'mode': 'three_stage_parallel', 'csv_layout': 'per_file_per_table'},
        )
        self.assertEqual(summary['bookrag_flush_count'], 2)
        self.assertEqual(summary['bookrag_unstructured_workers'], 2)
        self.assertEqual(summary['bookrag_csv_prepare_workers'], 2)
        self.assertEqual(summary['bookrag_csv_load_workers'], 5)
        self.assertEqual(jobs_finished, 2)
        self.assertEqual(csv_prepared, 14)
        self.assertEqual(loads_started, 8)
        self.assertEqual(stage_violations, [])
        self.assertEqual(len(set(document_csv_dirs)), 2)
        self.assertTrue(all(path.parent == csv_stage_dir for path in document_csv_dirs))
        self.assertEqual([batch['reason'] for batch in summary['bookrag_flush_batches']], ['file_ready', 'file_ready'])
        self.assertEqual(document_batch_sizes, [1, 1])
        self.assertEqual(raw_batch_sizes, [1, 1])
        self.assertEqual(block_batch_sizes, [1, 1])
        self.assertEqual(node_batch_sizes, [2, 2])
        self.assertEqual(summary['document_relation_count'], 1)
        self.assertEqual(summary['document_relation_rule_count'], 1)
        self.assertEqual(len(document_relation_rows), 1)
        self.assertNotIn('is_active', document_relation_rows[0])
        self.assertNotIn('confirmed', document_relation_rows[0])


class MultiFormatStagedPipelineTests(unittest.TestCase):
    def test_multi_format_vector_store_status_is_persisted_in_csv_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_root = Path(tmpdir)
            run_id = "multi-format-csv-run"
            run_dir = csv_root / run_id
            run_dir.mkdir(parents=True)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                multi_format.json.dumps(
                    {
                        "schema_version": multi_format.MULTI_FORMAT_CSV_MANIFEST_SCHEMA_VERSION,
                        "artifact_type": "multi_format_csv_run",
                        "csv_run_id": run_id,
                        "status": "ready",
                        "load_status": "ready",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(multi_format, "MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT", csv_root):
                multi_format.update_multi_format_csv_vector_store_status(
                    csv_run_id=run_id,
                    status="creating",
                    create_payload={"object_names": "demo_unstructured"},
                )
                multi_format.update_multi_format_csv_vector_store_status(
                    csv_run_id=run_id,
                    status="failed",
                    error="index row count mismatch",
                )

            saved = multi_format.json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["vector_store_status"], "failed")
            self.assertEqual(saved["vector_store_error"], "index row count mismatch")
            self.assertEqual(
                saved["vector_store_create_payload"]["object_names"],
                "demo_unstructured",
            )
            self.assertTrue(saved["vector_store_updated_at"])

    def test_raw_json_runs_are_shared_across_bookrag_and_multi_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            shared_root = tmp_path / "shared"
            legacy_multi_root = tmp_path / "legacy-multi"

            def _write_run(root: Path, run_id: str, artifact_type: str) -> None:
                run_dir = root / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "manifest.json").write_text(
                    multi_format.json.dumps(
                        {
                            "schema_version": 1,
                            "artifact_type": artifact_type,
                            "parse_run_id": run_id,
                            "status": "ready",
                            "created_at": "2026-07-16 00:00:00",
                            "vector_store_name": "demo",
                            "file_count": 1,
                            "documents": [],
                        }
                    ),
                    encoding="utf-8",
                )

            _write_run(shared_root, "bookrag-run", "bookrag_parse_run")
            _write_run(legacy_multi_root, "multi-run", "multi_format_parse_run")
            with mock.patch.object(
                multi_format, "UNSTRUCTURED_RAW_STAGE_DIR_DEFAULT", shared_root
            ), mock.patch.object(
                multi_format, "BOOKRAG_RAW_STAGE_DIR_DEFAULT", shared_root
            ), mock.patch.object(
                multi_format, "MULTI_FORMAT_RAW_STAGE_DIR_DEFAULT", legacy_multi_root
            ):
                bookrag_runs = multi_format.list_bookrag_parse_runs()
                multi_runs = multi_format.list_multi_format_parse_runs()
                multi_from_bookrag = multi_format._resolve_multi_format_parse_manifest("bookrag-run")
                bookrag_from_multi = multi_format._resolve_bookrag_parse_manifest("multi-run")

            self.assertEqual(bookrag_runs, multi_runs)
            self.assertEqual(
                {run["parse_run_id"] for run in multi_runs}, {"bookrag-run", "multi-run"}
            )
            self.assertEqual(multi_from_bookrag[1]["artifact_type"], "bookrag_parse_run")
            self.assertEqual(bookrag_from_multi[1]["artifact_type"], "multi_format_parse_run")

    def test_document_parsing_uses_standard_multi_format_workflow_and_writes_json_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            src = tmp_path / "sample.pdf"
            src.write_text("demo", encoding="utf-8")
            raw_dir = tmp_path / "raw-run"
            raw_dir.mkdir()
            payload = [
                {"type": "CompositeElement", "element_id": "chunk-1", "text": "chunk", "metadata": {}}
            ]
            create_values = dict(DOC_PIPELINE_UI_DEFAULTS)
            create_values.update(
                {
                    "multi_format_strategy": "hi_res",
                    "multi_format_chunk_size": "700",
                    "multi_format_chunk_overlap": "70",
                }
            )
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(multi_format, "_prepare_multi_format_raw_stage_dir", return_value=raw_dir))
                stack.enter_context(mock.patch.object(multi_format, "_load_unstructured_runtime_config", return_value=("key", "url")))
                stack.enter_context(mock.patch.object(multi_format, "_resolve_unstructured_request_timeout_ms", return_value=1000))
                stack.enter_context(mock.patch.object(multi_format, "_resolve_multi_format_workflow_poll_config", return_value=(30, 1)))
                stack.enter_context(mock.patch.object(multi_format, "_load_unstructured_runtime_settings", return_value={}))
                stack.enter_context(mock.patch.object(multi_format, "_create_unstructured_client", return_value=object()))
                stack.enter_context(mock.patch.object(multi_format, "_enforce_unstructured_job_submission_spacing", side_effect=lambda value: value))
                stack.enter_context(
                    mock.patch.object(
                        multi_format,
                        "_multi_format_partition_options_for_file",
                        return_value=("hi_res", ["eng"], False, [], False),
                    )
                )
                workflow_builder = stack.enter_context(
                    mock.patch.object(
                        multi_format,
                        "_workflow_builder_build_multi_format_workflow_definition",
                        return_value=(
                            {"workflow_name": "Multi_Format", "workflow_nodes": [{"name": "Partitioner"}, {"name": "Chunker"}]},
                            [],
                            "partition:hi_res,chunk:chunk_by_character",
                        ),
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        multi_format,
                        "_run_unstructured_workflow_job_for_file",
                        return_value=(payload, payload, {}, "job-1", "workflow-1", "Multi_Format"),
                    )
                )
                csv_mock = stack.enter_context(mock.patch.object(multi_format, "prepare_unstructured_table_csv"))
                load_mock = stack.enter_context(mock.patch.object(multi_format, "load_prepared_unstructured_table_csv"))
                summary = multi_format.run_multi_format_document_parsing(
                    create_values=create_values,
                    vector_store_name="demo",
                    uploaded_documents=[{"doc_id": "doc-1", "saved_path": str(src)}],
                    connection_params={},
                    resolve_path_hint=lambda value: value,
                )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["csv_files_created"], 0)
            self.assertEqual(summary["database_writes"], 0)
            self.assertTrue(Path(summary["files"][0]["raw_json_path"]).is_file())
            manifest = multi_format.json.loads(Path(summary["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifact_type"], "multi_format_parse_run")
            self.assertEqual(manifest["chunk_size"], 700)
            workflow_builder.assert_called_once()
            self.assertEqual(workflow_builder.call_args.kwargs["chunk_size"], 700)
            csv_mock.assert_not_called()
            load_mock.assert_not_called()

    def test_bookrag_json_to_csv_keeps_standard_unstructured_mapping_and_loads_one_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            raw_root = tmp_path / "raw"
            csv_root = tmp_path / "csv"
            parse_run_id = "multi_parse_run"
            parse_dir = raw_root / parse_run_id
            parse_dir.mkdir(parents=True)
            payloads = [
                [
                    {"type": "NarrativeText", "element_id": "one", "text": "One", "metadata": {"filename": "one.pdf"}},
                    {"type": "Image", "element_id": "empty", "text": "", "metadata": {}},
                ],
                [
                    {"type": "TableChunk", "element_id": "two", "text": "Two", "metadata": {"filename": "two.pdf", "text_as_html": "<table></table>"}},
                ],
            ]
            filenames = [
                "doc-0.pdf",
                "⑥GMAP不定期レポート_" + ("非常に長い日本語タイトル" * 20) + ".pdf",
            ]
            documents = []
            for index, payload in enumerate(payloads):
                json_path = parse_dir / f"doc-{index}.json"
                json_path.write_text(multi_format.json.dumps(payload), encoding="utf-8")
                documents.append(
                    {
                        "source_index": index,
                        "doc_id": f"doc-{index}",
                        "filename": filenames[index],
                        "filetype": "application/pdf",
                        "raw_json_file": json_path.name,
                        "raw_json_sha256": multi_format._file_sha256(json_path),
                        "element_count": len(payload),
                        "status": "success",
                    }
                )
            manifest = {
                "schema_version": multi_format.BOOKRAG_PARSE_MANIFEST_SCHEMA_VERSION,
                "artifact_type": "bookrag_parse_run",
                "parse_run_id": parse_run_id,
                "status": "ready",
                "created_at": "2026-07-16 00:00:00",
                "file_count": len(documents),
                "documents": documents,
            }
            (parse_dir / "manifest.json").write_text(
                multi_format.json.dumps(manifest), encoding="utf-8"
            )

            with mock.patch.object(multi_format, "UNSTRUCTURED_RAW_STAGE_DIR_DEFAULT", raw_root), mock.patch.object(
                multi_format, "BOOKRAG_RAW_STAGE_DIR_DEFAULT", raw_root
            ), mock.patch.object(
                multi_format, "MULTI_FORMAT_CSV_STAGE_DIR_DEFAULT", csv_root
            ):
                progress_updates = []
                generated = multi_format.run_multi_format_json_to_csv(
                    parse_run_id=parse_run_id,
                    vector_store_name="demo",
                    target_database="demo_schema",
                    progress_callback=progress_updates.append,
                )

                self.assertEqual(generated["status"], "ready")
                self.assertEqual(progress_updates, [50, 90])
                self.assertEqual(generated["qualified_table"], "demo_schema.demo_unstructured")
                self.assertEqual(generated["csv_file_count"], 2)
                self.assertEqual(generated["row_count"], 2)
                ids = []
                for result in generated["files"]:
                    generated_csv_path = Path(generated["csv_stage_dir"]) / result["csv_file"]
                    self.assertLessEqual(len(generated_csv_path.parent.name), 26)
                    with generated_csv_path.open(
                        "r", encoding="utf-8-sig", newline=""
                    ) as handle:
                        rows = list(csv.DictReader(handle))
                    ids.extend(row["id"] for row in rows)
                    self.assertEqual(list(rows[0]), [name for name, _ in multi_format.UNSTRUCTURED_CHUNK_COLUMNS])
                self.assertEqual(ids, ["000000000001", "000000000003"])

                with mock.patch.object(
                    multi_format, "_ensure_unstructured_teradata_table", return_value=[]
                ) as ensure_table, mock.patch.object(
                    multi_format,
                    "load_prepared_unstructured_table_csv",
                    side_effect=lambda **kwargs: kwargs["row_count"],
                ) as load_csv, mock.patch.object(
                    multi_format, "_count_teradata_rows", return_value=2
                ):
                    loaded = multi_format.run_multi_format_csv_load(
                        csv_run_id=generated["csv_run_id"], execute_sql_fn=mock.Mock(), connection_profile_id=7,
                        connection_target_fingerprint="fixture-target",
                    )
                    saved_manifest = multi_format.json.loads(Path(generated["manifest_path"]).read_text(encoding="utf-8"))
                    self.assertEqual(saved_manifest["connection_profile_id"], 7)
                    self.assertEqual(loaded["connection_profile_id"], 7)
                    self.assertEqual(saved_manifest["connection_target_fingerprint"], "fixture-target")

            self.assertEqual(loaded["persisted_row_count"], 2)
            self.assertEqual(loaded["qualified_table"], "demo_schema.demo_unstructured")
            ensure_table.assert_called_once_with(
                schema_name="demo_schema",
                table_name="demo_unstructured",
                execute_sql_fn=mock.ANY,
                clear_rows=True,
            )
            self.assertEqual(load_csv.call_count, 2)


if __name__ == '__main__':
    unittest.main()
