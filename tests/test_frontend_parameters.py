"""Browser regressions for conditional form inputs and navigation state."""
from __future__ import annotations

import os
import json
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright is not None and os.environ.get("EVSUI_BROWSER_TESTS") == "1",
                     "Set EVSUI_BROWSER_TESTS=1 with Playwright installed")
class FrontendParameterBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.e2e_support import BrowserApplication

        cls.fixture = BrowserApplication().__enter__()
        cls.addClassCleanup(cls.fixture.__exit__, None, None, None)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        launch = {"headless": True}
        if os.environ.get("EVSUI_BROWSER_CHANNEL"):
            launch["channel"] = os.environ["EVSUI_BROWSER_CHANNEL"]
        cls.browser = cls.playwright.chromium.launch(**launch)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def login(self, username="e2e_admin"):
        response = self.context.request.post(self.fixture.base_url + "/login", form={
            "username": username, "password": "browser-password",
        })
        self.assertEqual(response.status, 200)
        self.page.goto(self.fixture.base_url)
        self.page.wait_for_function("() => Boolean(window.EVSUIApp && window.EVSUIApp.stepGate)")
        self.page.locator("button[form='evs-connect-form']").click()
        expect(self.page.locator("[data-step1-connected]")).to_have_attribute("data-step1-connected", "true")

    def create_form(self):
        self.page.locator(".menu-item[data-section='section-create']").click()
        return self.page.locator("#section-create form")

    def assert_hidden_fields_disabled(self, form):
        enabled_hidden = form.evaluate("""form => Array.from(form.querySelectorAll('input,select,textarea'))
            .filter(control => control.closest('[hidden]') && !control.disabled).map(control => control.name)""")
        self.assertEqual(enabled_hidden, [])

    def test_mode_route_enrichment_and_navigation_preserve_disabled_fields(self):
        self.login()
        form = self.create_form()
        mode = form.locator("[name='doc_pipeline_mode']")
        for value in ("text_core", "multi_format", "multi_format_bookrag", "multi_format"):
            mode.select_option(value)
            self.assert_hidden_fields_disabled(form)
        strategy = form.locator("[name='multi_format_strategy']")
        strategy.select_option("hi_res")
        enrichment = form.locator("[data-enrichment-toggle='generative_ocr']")
        enrichment.select_option("true")
        expect(form.locator("[name='multi_format_generative_ocr_subtype']")).to_be_enabled()
        expect(form.locator("[name='multi_format_generative_ocr_provider_type']")).to_have_value("openai")
        expect(form.locator("[name='multi_format_generative_ocr_model']")).to_have_value("gpt-5-mini")
        enrichment.select_option("false")
        expect(form.locator("[name='multi_format_generative_ocr_subtype']")).to_be_disabled()
        expect(form.locator("[name='multi_format_generative_ocr_model']")).to_be_disabled()
        for route in ("fast", "vlm", "auto"):
            strategy.select_option(route)
            self.assert_hidden_fields_disabled(form)
        form.locator("[name='search_algorithm']").select_option("HNSW")
        self.page.locator(".menu-item[data-section='section-chat']").click()
        self.page.locator(".menu-item[data-section='section-create']").click()
        self.assert_hidden_fields_disabled(form)
        expect(form.locator("[data-multi-format-csv-button]")).to_be_disabled()
        expect(form.locator("[data-bookrag-csv-button]")).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_long_provider_key_survives_binding_input_and_navigation(self):
        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format")
        form.locator("[name='multi_format_strategy']").select_option("vlm")
        field = form.locator("[name='multi_format_vlm_provider_api_key']")
        secret = "fixture-long-key-" + "abcdef0123456789" * 15
        field.fill(secret)
        expect(field).to_have_value(secret)
        expect(field).to_have_attribute("type", "password")
        self.page.evaluate("window.EVSUIApp.bindAll(document)")
        self.page.locator(".menu-item[data-section='section-chat']").click()
        self.page.locator(".menu-item[data-section='section-create']").click()
        expect(field).to_have_value(secret)
        self.assertEqual(field.evaluate("field => field.maxLength"), -1)
        self.assertEqual(self.errors, [])

    def test_viewer_has_no_creation_or_destroy_actions(self):
        self.login("e2e_viewer")
        expect(self.page.locator(".menu-item[data-section='section-create']")).to_have_count(0)
        expect(self.page.locator("#section-create form")).to_have_count(0)
        expect(self.page.locator("[data-destroy-btn]")).to_have_count(0)
        self.page.locator(".menu-item[data-section='section-chat']").click()
        expect(self.page.locator("#chat-message")).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_provider_model_filter_preserves_valid_selection_across_changes(self):
        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format")
        form.locator("[name='multi_format_strategy']").select_option("vlm")
        provider = form.locator("[data-provider-model-key='vlm']")
        model = form.locator("[data-provider-model-target='vlm']")
        provider.select_option("openai")
        self.assertEqual(model.locator("optgroup").evaluate_all("groups => groups.map(group => group.label)"),
                         ["OpenAI"])
        expect(model.locator("option[value='gpt-5.4-mini']")).to_have_count(1)
        selected = model.locator("optgroup option").first.get_attribute("value")
        model.select_option(selected)
        form.locator("[name='search_algorithm']").select_option("HNSW")
        expect(model).to_have_value(selected)
        provider.select_option("anthropic")
        self.assertEqual(model.locator("optgroup").evaluate_all("groups => groups.map(group => group.label)"),
                         ["Anthropic"])
        expect(model.locator("option[value='claude-sonnet-4-6']")).to_have_count(1)
        expect(model).to_have_value("")
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format_bookrag")
        expect(provider).to_be_disabled()
        expect(model).to_be_disabled()
        form.locator("[name='multi_format_bookrag_strategy']").select_option("hi_res")
        form.locator("[data-enrichment-toggle='bookrag_image_description']").select_option("true")
        bookrag_provider = form.locator("[data-provider-model-key='bookrag_image_description']")
        bookrag_model = form.locator("[data-provider-model-target='bookrag_image_description']")
        expect(bookrag_provider).to_be_enabled()
        expect(bookrag_model).to_be_enabled()
        bookrag_provider.select_option("openai")
        self.assertEqual(
            bookrag_model.locator("optgroup").evaluate_all("groups => groups.map(group => group.label)"),
            ["OpenAI"],
        )
        expect(bookrag_model.locator("option[value='gpt-5.4-mini']")).to_have_count(1)
        self.assert_hidden_fields_disabled(form)
        self.assertEqual(self.errors, [])

    def test_each_chunk_strategy_excludes_hidden_controls_from_form_payload(self):
        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format")
        strategy = form.locator("[name='multi_format_chunk_strategy']")
        choices = strategy.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
        self.assertGreater(len(choices), 1)
        for choice in choices:
            strategy.select_option(choice)
            self.assert_hidden_fields_disabled(form)
            leaked_names = form.evaluate("""form => {
                const payload = new FormData(form);
                return Array.from(form.querySelectorAll('[data-chunk-strategies][hidden] input, [data-chunk-strategies][hidden] select'))
                    .filter(control => payload.has(control.name)).map(control => control.name);
            }""")
            self.assertEqual(leaked_names, [], choice)
        self.assertEqual(self.errors, [])

    def test_json_filters_clear_stale_detail_and_restore_selection(self):
        self.login()
        # Load the production viewer module against a small deterministic element list.
        self.page.evaluate("""() => {
            const viewer = document.createElement('section');
            viewer.dataset.jsonViewer = '';
            viewer.id = 'filter-regression-viewer';
            viewer.innerHTML = `<input data-json-viewer-filter><select data-json-viewer-type><option value="">All</option><option>Title</option></select>
                <button data-json-viewer-item data-index="1" data-type="Title" data-text="Visible title" data-json='{"text":"Visible title","metadata":{"page":1}}'>Title</button>
                <button data-json-viewer-tab="text">Text</button><h4 data-json-viewer-title></h4>
                <p data-json-viewer-subtitle></p><p data-json-viewer-path></p><pre data-json-viewer-code></pre>`;
            document.body.append(viewer);
            window.EVSUIApp.bindJsonInspectors(document);
        }""")
        viewer = self.page.locator("#filter-regression-viewer")
        query = viewer.locator("[data-json-viewer-filter]")
        query.fill("no-such-element")
        expect(viewer.locator("[data-json-viewer-code]")).to_have_text("")
        expect(viewer.locator("[data-json-viewer-title]")).to_have_text("No matching elements")
        viewer.locator("[data-json-viewer-tab]").click()
        expect(viewer.locator("[data-json-viewer-code]")).to_have_text("")
        query.fill("")
        expect(viewer.locator("[data-json-viewer-code]")).to_have_text("Visible title")
        self.assertEqual(self.errors, [])

    def upload_document(self, form):
        with self.page.expect_response(lambda response: response.url.endswith("/ui/create/upload-documents")) as uploaded:
            form.locator("input[type='file'][name='files']").set_input_files({
                "name": "browser-fixture.txt", "mimeType": "text/plain", "buffer": b"Browser fixture document text.",
            })
        self.assertEqual(uploaded.value.status, 200)
        expect(form.locator("[data-selected-doc-paths]")).to_contain_text("browser-fixture.txt")
        expect(form.locator("input[type='file'][name='files']")).to_have_value("")

    def complete_job(self, kind, result, inspect_payload=None):
        from app.services.job_worker import PersistentJobWorker

        def handler(payload, heartbeat):
            heartbeat(45)
            if inspect_payload:
                inspect_payload(payload)
            return result

        completed = PersistentJobWorker(self.fixture.app.state.job_repository, {kind: handler}).run_once()
        self.assertIsNotNone(completed)
        self.assertEqual(completed["status"], "succeeded", completed.get("error"))
        return completed

    def test_upload_create_queue_and_successful_poll_updates_top_status(self):
        from app.services.workflow_jobs import VECTOR_STORE_CREATE_JOB

        self.login()
        form = self.create_form()
        self.upload_document(form)
        form.locator("[name='doc_pipeline_mode']").select_option("text_core")
        form.locator("[name='vector_store_name']").fill("browser_created")
        model = form.locator("[name='embeddings_model']")
        model.select_option(model.locator("option:not([value=''])").first.get_attribute("value"))
        form.locator("[name='search_algorithm']").select_option("VECTORDISTANCE")
        with self.page.expect_response(lambda response: response.url.endswith("/ui/create/upload")) as queued:
            form.get_by_role("button", name="Create Vector Store", exact=True).click()
        self.assertEqual(queued.value.status, 200)
        expect(self.page.locator("#create-result")).to_contain_text("Queued")
        self.complete_job(VECTOR_STORE_CREATE_JOB, {"create_result": {
            "status": "ok", "message": "Browser creation completed", "vector_store_name": "browser_created",
        }}, lambda payload: self.assertEqual(payload["vector_store_name"], "browser_created"))
        expect(self.page.locator("#top-op-stack-shell")).to_contain_text("Browser creation completed")
        expect(self.page.locator("#create-result [hx-get]")).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_uploaded_parse_job_cancel_stops_polling(self):
        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format")
        self.upload_document(form)
        form.locator("[data-multi-format-parse-button]").click()
        result = self.page.locator("#multi-format-document-parsing-result")
        expect(result).to_contain_text("Queued")
        poll_url = result.locator("[hx-get]").get_attribute("hx-get")
        result.get_by_role("button", name="Cancel queued job", exact=True).click()
        expect(result).to_contain_text("was cancelled")
        expect(result.locator("[hx-get]")).to_have_count(0)
        job_id = poll_url.rsplit("/", 1)[-1]
        self.assertEqual(self.fixture.app.state.job_repository.get(job_id)["status"], "cancelled")
        self.assertEqual(self.errors, [])

    def test_loaded_csv_target_is_rejected_before_a_generation_job_is_queued(self):
        from app.services import multi_format, workflow_jobs

        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format_bookrag")
        self.upload_document(form)
        form.locator("[data-bookrag-csv-vector-store-name]").fill("browser_collision_target")
        form.locator("[data-bookrag-csv-target-database]").fill("fixture")
        form.locator("[data-bookrag-parse-button]").click()
        parsing = self.page.locator("#bookrag-document-parsing-result")
        expect(parsing).to_contain_text("Queued")
        parse_summary = {
            "status": "ok",
            "parse_run_id": "browser_collision_parse",
            "file_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "workers": 1,
            "elapsed_seconds": 0.1,
            "raw_stage_dir": str(self.fixture.uploads / "bookrag_raw_stage"),
            "manifest_path": "",
            "warnings": [],
            "files": [],
        }
        self.complete_job(workflow_jobs.BOOKRAG_PARSE_JOB, {"summary": parse_summary})
        expect(parsing).to_contain_text("Document parsing completed.")

        csv_run_id = "browser_collision_loaded"
        stage_dir = self.fixture.uploads / "bookrag_csv_stage" / csv_run_id
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "manifest.json").write_text(json.dumps({
            "schema_version": multi_format.BOOKRAG_CSV_MANIFEST_SCHEMA_VERSION,
            "artifact_type": "bookrag_csv_run",
            "complete_table_contract": multi_format.BOOKRAG_COMPLETE_TABLE_CONTRACT,
            "csv_run_id": csv_run_id,
            "created_at": "2026-09-07T12:00:00",
            "status": "ready",
            "load_status": "ready",
            "vector_store_status": "not_started",
            "vector_store_name": "BROWSER_COLLISION_TARGET",
            "target_database": "FIXTURE",
            "file_count": 1,
            "csv_file_count": 1,
        }), encoding="utf-8")

        job_count = len(self.fixture.app.state.job_repository.list_recent())
        with self.page.expect_response(lambda response: response.url.endswith("/ui/create/generate-csv")) as blocked:
            parsing.get_by_role("button", name="Generate CSV from this JSON run", exact=True).click()
        self.assertEqual(blocked.value.status, 200)
        generation = self.page.locator("#bookrag-csv-generation-result")
        expect(generation).to_contain_text("already has loaded tables")
        expect(generation).to_contain_text("Use a different Target Vector Store Name")
        self.assertEqual(len(self.fixture.app.state.job_repository.list_recent()), job_count)
        self.assertEqual(self.errors, [])

    def test_oversized_key_error_preserves_form_and_allows_corrected_resubmit(self):
        self.login()
        form = self.create_form()
        form.locator("[name='doc_pipeline_mode']").select_option("multi_format")
        form.locator("[name='multi_format_strategy']").select_option("vlm")
        self.upload_document(form)
        key = form.locator("[name='multi_format_vlm_provider_api_key']")
        value = "fixture-oversized-key" * 420
        key.fill(value)
        form.locator("[data-multi-format-parse-button]").click()
        result = self.page.locator("#multi-format-document-parsing-result")
        expect(result).to_contain_text("at most 8192 characters")
        expect(key).to_have_value(value)
        expect(form).to_have_count(1)
        key.fill("fixture-valid-key")
        form.locator("[data-multi-format-parse-button]").click()
        expect(result).to_contain_text("Queued")
        result.get_by_role("button", name="Cancel queued job", exact=True).click()
        expect(result).to_contain_text("was cancelled")
        self.assertEqual(self.errors, [])

    def test_both_staged_csv_workflows_update_load_and_create_selection_after_poll(self):
        from app.services import multi_format, workflow_jobs

        self.login()
        form = self.create_form()
        self.upload_document(form)
        for prefix, mode, stage_prefix in (("multi-format", "multi_format", "multi_format"),
                                           ("bookrag", "multi_format_bookrag", "bookrag")):
            with self.subTest(mode=mode):
                target_name = f"browser_{stage_prefix}_store"
                form.locator("[name='doc_pipeline_mode']").select_option(mode)
                form.locator(f"[data-{prefix}-csv-vector-store-name]").fill(target_name)
                form.locator(f"[data-{prefix}-csv-target-database]").fill("fixture")
                raw_id, csv_id = f"browser_{stage_prefix}_raw", f"browser_{stage_prefix}_csv"
                stage_dir = self.fixture.uploads / f"{stage_prefix}_csv_stage" / csv_id
                stage_dir.mkdir(parents=True, exist_ok=True)
                summary = {
                    "status": "ready", "parse_run_id": raw_id, "csv_run_id": csv_id,
                    "vector_store_name": target_name, "target_database": "fixture",
                    "created_at": "2026-09-05T10:00:00", "file_count": 1, "success_count": 1,
                    "failure_count": 0, "workers": 1, "elapsed_seconds": 0.1, "files": [], "warnings": [],
                    "csv_files_created": 1, "csv_file_count": 1, "row_count": 3, "run_csv_files": [],
                    "run_error": "", "task_count": 1, "inserted_rows": 3, "document_relation_count": 0,
                    "node_table": "fixture.browser_bnode", "qualified_table": "fixture.browser_unstructured",
                    "raw_stage_dir": str(self.fixture.uploads / "raw"), "csv_stage_dir": str(stage_dir),
                    "manifest_path": str(stage_dir / "manifest.json"),
                }
                name = "MULTI_FORMAT" if prefix == "multi-format" else "BOOKRAG"
                parse_kind = getattr(workflow_jobs, name + "_PARSE_JOB")
                generate_kind = getattr(workflow_jobs, name + "_CSV_GENERATE_JOB")
                load_kind = getattr(workflow_jobs, name + "_CSV_LOAD_JOB")
                secret_name = mode + "_vlm_provider_api_key"
                strategy_name = mode + "_strategy"
                form.locator(f"[name='{strategy_name}']").select_option("vlm")
                provider_key = "fixture-provider-key-" + "a" * 240
                form.locator(f"[name='{secret_name}']").fill(provider_key)
                form.locator(f"[data-{prefix}-parse-button]").click()
                parsing = self.page.locator(f"#{prefix}-document-parsing-result")
                expect(parsing).to_contain_text("Queued")
                self.complete_job(parse_kind, {"summary": summary}, lambda payload:
                                  self.assertEqual(payload["create_values"][secret_name], provider_key))
                expect(parsing).to_contain_text("Document parsing completed.")
                parsing.get_by_role("button", name="Generate CSV from this JSON run", exact=True).click()
                generation = self.page.locator(f"#{prefix}-csv-generation-result")
                expect(generation).to_contain_text("Queued")
                self.complete_job(generate_kind, {"summary": summary}, lambda payload:
                                  self.assertEqual(payload["parse_run_id"], raw_id))
                load_panel = self.page.locator(f"#{prefix}-csv-load-panel")
                expect(load_panel.locator("select")).to_have_value(csv_id)
                expect(load_panel).to_have_count(1)
                manifest = {
                    **summary, "artifact_type": f"{stage_prefix}_csv_run", "schema_version": 1,
                    "complete_table_contract": multi_format.BOOKRAG_COMPLETE_TABLE_CONTRACT,
                    "connection_profile_id": self.fixture.profile["id"],
                }
                (stage_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                load_panel.locator("button").click()
                loading = self.page.locator(f"#{prefix}-csv-load-result")
                expect(loading).to_contain_text("Queued")
                self.complete_job(load_kind, {"summary": summary}, lambda payload:
                                  self.assertEqual(payload["csv_run_id"], csv_id))
                loaded = form.locator(f"[name='{stage_prefix}_loaded_csv_run_id']")
                expect(loaded).to_have_value(csv_id)
                expect(loaded).to_be_enabled()
                expect(loaded).to_have_count(1)
                self.assertEqual(form.evaluate("(form, name) => new FormData(form).get(name)",
                                               f"{stage_prefix}_loaded_csv_run_id"), csv_id)
                self.assertEqual(self.errors, [])

    def test_layout_at_desktop_laptop_and_tablet_widths(self):
        self.login()
        overflow = []
        for width in (1440, 1280, 768):
            self.page.set_viewport_size({"width": width, "height": 900})
            for section in ("section-connect", "section-create", "section-chat", "section-admin"):
                self.page.locator(f".menu-item[data-section='{section}']").click()
                if section == "section-create":
                    self.page.locator("[name='doc_pipeline_mode']").select_option("multi_format")
                extent = self.page.evaluate("""() => {
                    const active = document.querySelector('.menu-section.active .panel-content');
                    return {page:document.documentElement.scrollWidth - innerWidth,
                            panel:active.scrollWidth - active.clientWidth};
                }""")
                if extent["page"] > 2 or extent["panel"] > 2:
                    overflow.append({"width": width, "section": section, **extent})
            self.page.goto(self.fixture.base_url + "/admin/users")
            if self.page.evaluate("() => document.documentElement.scrollWidth - innerWidth") > 2:
                overflow.append({"width": width, "section": "system-configuration"})
            self.page.goto(self.fixture.base_url)
        self.assertEqual(overflow, [])
        self.assertEqual(self.errors, [])
