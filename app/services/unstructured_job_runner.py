from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import httpx


UNSTRUCTURED_JOB_SUBMIT_MAX_ATTEMPTS = 6
UNSTRUCTURED_JOB_SUBMIT_RETRY_MARGIN_SECONDS = 0.35
UNSTRUCTURED_ON_DEMAND_MAX_FILE_BYTES = 10_000_000


def _job_submit_retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    candidates: list[Any] = [response.headers.get("retry-after")]
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        candidates.append(payload.get("retry_after"))
    for value in candidates:
        try:
            return max(1.0, float(value)) + UNSTRUCTURED_JOB_SUBMIT_RETRY_MARGIN_SECONDS
        except (TypeError, ValueError):
            continue
    return 1.0 + UNSTRUCTURED_JOB_SUBMIT_RETRY_MARGIN_SECONDS + (max(1, attempt) - 1) * 0.5


def create_unstructured_client(*, api_key: str, api_url: str, timeout_ms: int | None = None):
    from unstructured_client import UnstructuredClient

    return UnstructuredClient(
        api_key_auth=api_key,
        server_url=api_url.rstrip("/"),
        timeout_ms=timeout_ms,
    )


def _sdk_payload(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_sdk_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sdk_payload(item) for key, item in value.items()}
    if is_dataclass(value):
        return _sdk_payload(asdict(value))
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _sdk_payload(method())
            except TypeError:
                continue
    if hasattr(value, "__dict__"):
        return {
            str(key): _sdk_payload(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def get_unstructured_job_diagnostics(client, *, job_id: str) -> dict[str, Any]:
    """Return best-effort processing details without hiding the original error."""
    from unstructured_client.models import operations

    diagnostics: dict[str, Any] = {}
    calls = (
        ("details", "get_job_details", "GetJobDetailsRequest", "job_details"),
        ("failed_files", "get_job_failed_files", "GetJobFailedFilesRequest", "job_failed_files"),
    )
    for key, method_name, request_type_name, response_attr in calls:
        method = getattr(client.jobs, method_name, None)
        if not callable(method):
            continue
        try:
            # These endpoint-specific request classes were added after the
            # original Jobs SDK. Their wire shape is the same as GetJobRequest,
            # so retain compatibility with partially upgraded SDK clients.
            request_type = getattr(operations, request_type_name, operations.GetJobRequest)
            response = method(request=request_type(job_id=job_id))
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code >= 400:
                diagnostics[f"{key}_error"] = f"status={status_code}"
                continue
            diagnostics[key] = _sdk_payload(getattr(response, response_attr, None))
        except Exception as ex:
            diagnostics[f"{key}_error"] = str(ex)
    return diagnostics


def _format_job_diagnostics(diagnostics: dict[str, Any]) -> str:
    if not diagnostics:
        return ""
    rendered = json.dumps(diagnostics, ensure_ascii=False, default=str)
    if len(rendered) > 4000:
        rendered = rendered[:3997] + "..."
    return f" diagnostics={rendered}"


def create_unstructured_on_demand_job(
    *,
    request_parameters: dict[str, Any],
    src: Path,
    api_key: str,
    api_url: str,
) -> tuple[str, dict[str, Any]]:
    source_size = src.stat().st_size
    if source_size > UNSTRUCTURED_ON_DEMAND_MAX_FILE_BYTES:
        raise RuntimeError(
            "Unstructured on-demand jobs accept files up to 10 MB; "
            f"'{src.name}' is {source_size} bytes."
        )
    content_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    endpoint = f"{api_url.rstrip('/')}" + "/jobs/"
    request_data = json.dumps({"job_nodes": request_parameters.get("workflow_nodes", [])}, ensure_ascii=False)
    source_bytes = src.read_bytes()
    response: httpx.Response | None = None
    for attempt in range(1, UNSTRUCTURED_JOB_SUBMIT_MAX_ATTEMPTS + 1):
        response = httpx.post(
            endpoint,
            headers={
                "unstructured-api-key": api_key,
                "accept": "application/json",
            },
            files=[
                ("request_data", (None, request_data, "application/json")),
                ("input_files", (src.name, source_bytes, content_type)),
            ],
            timeout=120.0,
        )
        if response.status_code != 429 or attempt >= UNSTRUCTURED_JOB_SUBMIT_MAX_ATTEMPTS:
            break
        time.sleep(_job_submit_retry_after_seconds(response, attempt))
    if response is None:
        raise RuntimeError("Unstructured create_job returned no response.")
    if response.status_code >= 400:
        raise RuntimeError(f"Unstructured create_job failed. status={response.status_code} body={response.text}")
    try:
        payload = response.json()
    except Exception as ex:
        raise RuntimeError(f"Unstructured create_job returned non-JSON response. status={response.status_code}") from ex
    job_id = str(payload.get("id") or "").strip()
    if not job_id:
        raise RuntimeError("Unstructured create_job returned no job ID.")
    return job_id, payload


def wait_for_unstructured_job(client, *, job_id: str, timeout_seconds: int, poll_interval_seconds: int):
    from unstructured_client.models import operations

    started = time.time()
    last_status = ""
    while True:
        response = client.jobs.get_job(request=operations.GetJobRequest(job_id=job_id))
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise RuntimeError(f"Unstructured get_job failed. status={getattr(response, 'status_code', '?')}, job_id={job_id}")
        job_info = getattr(response, "job_information", None)
        if job_info is None:
            raise RuntimeError(f"Unstructured get_job returned no job information. job_id={job_id}")
        raw_status = getattr(job_info, "status", "")
        status = str(getattr(raw_status, "value", raw_status) or "").strip().upper()
        if status == "COMPLETED":
            return job_info
        if status in {"FAILED", "STOPPED"}:
            diagnostics = get_unstructured_job_diagnostics(client, job_id=job_id)
            raise RuntimeError(
                f"Unstructured job ended with status={status}. job_id={job_id}"
                f"{_format_job_diagnostics(diagnostics)}"
            )
        last_status = status or last_status
        if time.time() - started >= timeout_seconds:
            raise RuntimeError(
                "Timed out waiting for Unstructured job completion. "
                f"job_id={job_id}, last_status={last_status or 'UNKNOWN'}, timeout_seconds={timeout_seconds}. "
                "Increase the mode-specific workflow poll timeout or UNSTRUCTURED_WORKFLOW_POLL_SECONDS if this is expected for large files."
            )
        time.sleep(max(1, poll_interval_seconds))


def download_unstructured_job_output_payload(client, job_info) -> Any:
    from unstructured_client.models import operations

    output_node_files = list(getattr(job_info, "output_node_files", None) or [])
    if output_node_files:
        target = output_node_files[-1]
        request = operations.DownloadJobOutputRequest(
            job_id=str(getattr(job_info, "id", "") or ""),
            file_id=str(getattr(target, "file_id", "") or ""),
            node_id=str(getattr(target, "node_id", "") or ""),
        )
    else:
        input_file_ids = list(getattr(job_info, "input_file_ids", None) or [])
        if not input_file_ids:
            raise RuntimeError(f"Unstructured job returned no downloadable output references. job_id={getattr(job_info, 'id', '')}")
        request = operations.DownloadJobOutputRequest(
            job_id=str(getattr(job_info, "id", "") or ""),
            file_id=str(input_file_ids[0]),
        )

    response = client.jobs.download_job_output(request=request)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise RuntimeError(f"Unstructured download_job_output failed. status={getattr(response, 'status_code', '?')}, job_id={getattr(job_info, 'id', '')}")
    return getattr(response, "any", None)


def extract_elements_from_unstructured_job_output(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("elements", "output", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        list_values = [value for value in payload.values() if isinstance(value, list)]
        if len(list_values) == 1:
            return [item for item in list_values[0] if isinstance(item, dict)]
    raise RuntimeError("Unsupported Unstructured workflow output format; expected a JSON array or object containing an elements array.")


def enforce_unstructured_job_submission_spacing(last_submitted_at: float | None, *, minimum_spacing_seconds: float = 1.35) -> float:
    now = time.time()
    if last_submitted_at is not None:
        remaining = minimum_spacing_seconds - (now - last_submitted_at)
        if remaining > 0:
            time.sleep(remaining)
    return time.time()


def run_unstructured_workflow_job_for_file(
    client,
    *,
    request_parameters: dict[str, Any],
    src: Path,
    timeout_seconds: int,
    poll_interval_seconds: int,
    api_key: str,
    api_url: str,
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], str, str, str]:
    job_id, create_job_payload = create_unstructured_on_demand_job(
        request_parameters=request_parameters,
        src=src,
        api_key=api_key,
        api_url=api_url,
    )
    job_info = wait_for_unstructured_job(
        client,
        job_id=job_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    payload = download_unstructured_job_output_payload(client, job_info)
    elements = extract_elements_from_unstructured_job_output(payload)
    workflow_id = str(getattr(job_info, "workflow_id", "") or create_job_payload.get("workflow_id") or "").strip()
    workflow_name = str(getattr(job_info, "workflow_name", "") or create_job_payload.get("workflow_name") or request_parameters.get("workflow_name") or "").strip()
    file_request_parameters = dict(request_parameters)
    file_request_parameters.update(
        {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "source_file": str(src),
            "job_id": job_id,
            "poll_timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        }
    )
    return payload, elements, file_request_parameters, job_id, workflow_id, workflow_name
