from __future__ import annotations

import logging
import unittest

from app.core.access_logging import SuccessfulJobPollFilter, configure_uvicorn_access_logging


def _access_record(method: str, path: str, status_code: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:12345", method, path, "1.1", status_code),
        None,
    )


class AccessLoggingTests(unittest.TestCase):
    def test_successful_job_status_poll_is_hidden(self) -> None:
        access_filter = SuccessfulJobPollFilter()

        self.assertFalse(access_filter.filter(_access_record("GET", "/ui/jobs/job-id", 200)))
        self.assertFalse(access_filter.filter(_access_record("GET", "/ui/jobs/job-id?view=partial", 200)))

    def test_actionable_access_logs_are_retained(self) -> None:
        access_filter = SuccessfulJobPollFilter()

        self.assertTrue(access_filter.filter(_access_record("GET", "/ui/jobs/job-id", 404)))
        self.assertTrue(access_filter.filter(_access_record("POST", "/ui/jobs/job-id/cancel", 200)))
        self.assertTrue(access_filter.filter(_access_record("GET", "/ui/create", 200)))

    def test_configuration_is_idempotent(self) -> None:
        logger = logging.getLogger("uvicorn.access")
        original_filters = list(logger.filters)
        try:
            logger.filters = []
            configure_uvicorn_access_logging()
            configure_uvicorn_access_logging()
            self.assertEqual(
                sum(isinstance(item, SuccessfulJobPollFilter) for item in logger.filters),
                1,
            )
        finally:
            logger.filters = original_filters


if __name__ == "__main__":
    unittest.main()
