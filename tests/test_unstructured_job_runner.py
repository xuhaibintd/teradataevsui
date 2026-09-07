from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.unstructured_job_runner import (
    UNSTRUCTURED_ON_DEMAND_MAX_FILE_BYTES,
    create_unstructured_on_demand_job,
    get_unstructured_job_diagnostics,
    wait_for_unstructured_job,
)


class _Response:
    def __init__(self, status_code: int, payload: dict, *, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class UnstructuredJobSubmissionTests(unittest.TestCase):
    def test_oversized_file_is_rejected_before_network_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "oversized.pdf"
            with source.open("wb") as stream:
                stream.truncate(UNSTRUCTURED_ON_DEMAND_MAX_FILE_BYTES + 1)
            with mock.patch("app.services.unstructured_job_runner.httpx.post") as post_mock, self.assertRaisesRegex(
                RuntimeError, "up to 10 MB"
            ):
                create_unstructured_on_demand_job(
                    request_parameters={"workflow_nodes": []},
                    src=source,
                    api_key="secret",
                    api_url="https://example.invalid/api/v1",
                )

        post_mock.assert_not_called()

    def test_rate_limit_response_retries_using_server_delay(self) -> None:
        responses = [
            _Response(
                429,
                {
                    "code": "rate_limit_exceeded",
                    "message": "Job submission rate limit exceeded.",
                    "retry_after": 1,
                },
            ),
            _Response(200, {"id": "job-123"}),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "sample.pdf"
            source.write_bytes(b"pdf")
            with mock.patch(
                "app.services.unstructured_job_runner.httpx.post",
                side_effect=responses,
            ) as post_mock, mock.patch(
                "app.services.unstructured_job_runner.time.sleep"
            ) as sleep_mock:
                job_id, payload = create_unstructured_on_demand_job(
                    request_parameters={"workflow_nodes": []},
                    src=source,
                    api_key="secret",
                    api_url="https://example.invalid/api/v1",
                )

        self.assertEqual(job_id, "job-123")
        self.assertEqual(payload, {"id": "job-123"})
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.35)

    def test_non_rate_limit_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "sample.pdf"
            source.write_bytes(b"pdf")
            with mock.patch(
                "app.services.unstructured_job_runner.httpx.post",
                return_value=_Response(400, {"message": "bad request"}),
            ) as post_mock, self.assertRaisesRegex(RuntimeError, "status=400"):
                create_unstructured_on_demand_job(
                    request_parameters={"workflow_nodes": []},
                    src=source,
                    api_key="secret",
                    api_url="https://example.invalid/api/v1",
                )

        self.assertEqual(post_mock.call_count, 1)

    def test_job_diagnostics_reads_details_and_failed_files(self) -> None:
        jobs = mock.Mock()
        jobs.get_job_details.return_value = mock.Mock(
            status_code=200,
            job_details={"processed_files": 2},
        )
        jobs.get_job_failed_files.return_value = mock.Mock(
            status_code=200,
            job_failed_files={"files": [{"filename": "bad.pdf"}]},
        )

        diagnostics = get_unstructured_job_diagnostics(mock.Mock(jobs=jobs), job_id="job-123")

        self.assertEqual(diagnostics["details"], {"processed_files": 2})
        self.assertEqual(diagnostics["failed_files"]["files"][0]["filename"], "bad.pdf")

    def test_failed_job_error_contains_diagnostics(self) -> None:
        failed_info = mock.Mock(status="FAILED")
        jobs = mock.Mock()
        jobs.get_job.return_value = mock.Mock(status_code=200, job_information=failed_info)
        client = mock.Mock(jobs=jobs)
        with mock.patch(
            "app.services.unstructured_job_runner.get_unstructured_job_diagnostics",
            return_value={"failed_files": {"files": [{"filename": "bad.pdf"}]}},
        ), self.assertRaisesRegex(RuntimeError, "bad.pdf"):
            wait_for_unstructured_job(
                client,
                job_id="job-123",
                timeout_seconds=10,
                poll_interval_seconds=1,
            )


if __name__ == "__main__":
    unittest.main()
