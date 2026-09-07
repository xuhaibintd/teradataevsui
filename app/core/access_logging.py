from __future__ import annotations

import logging


class SuccessfulJobPollFilter(logging.Filter):
    """Hide successful browser job polls while retaining actionable access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        _client, method, path, _http_version, status_code = args[:5]
        path_without_query = str(path).split("?", 1)[0]
        path_parts = path_without_query.strip("/").split("/")
        is_job_status_poll = (
            str(method).upper() == "GET"
            and len(path_parts) == 3
            and path_parts[:2] == ["ui", "jobs"]
        )
        try:
            is_success = int(status_code) == 200
        except (TypeError, ValueError):
            is_success = False
        return not (is_job_status_poll and is_success)


def configure_uvicorn_access_logging() -> None:
    """Install the job-poll filter once for this process."""

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SuccessfulJobPollFilter) for item in access_logger.filters):
        access_logger.addFilter(SuccessfulJobPollFilter())
