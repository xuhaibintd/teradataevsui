from __future__ import annotations

from typing import Any


TWOPASS_ENRICHMENT_NODE_TYPES = {
    "twopass_image_description",
    "twopass_table2html",
}

ENRICHMENT_PROVIDER_TYPES = {
    "openai_image_description": {"openai", "azure_openai"},
    "anthropic_image_description": {"anthropic"},
    "bedrock_image_description": {"bedrock"},
    "vertexai_image_description": {"vertexai"},
    "openai_table_description": {"openai", "azure_openai"},
    "anthropic_table_description": {"anthropic"},
    "bedrock_table_description": {"bedrock"},
    "vertexai_table_description": {"vertexai"},
    "openai_table2html": {"openai", "azure_openai"},
    "anthropic_table2html": {"anthropic"},
    "openai_ocr": {"openai", "azure_openai"},
    "anthropic_ocr": {"anthropic"},
    "bedrock_ocr": {"bedrock"},
    "openai_ner": {"openai", "azure_openai"},
    "anthropic_ner": {"anthropic"},
}

ENRICHMENT_NODE_TYPES = set(ENRICHMENT_PROVIDER_TYPES) | TWOPASS_ENRICHMENT_NODE_TYPES
NER_NODE_TYPES = {"openai_ner", "anthropic_ner"}


def _partition_route(partition: dict[str, Any], settings: dict[str, Any]) -> str:
    subtype = str(partition.get("subtype") or "").strip()
    if subtype not in {"vlm", "unstructured_api"}:
        raise ValueError(f"Unsupported Unstructured partition subtype: {subtype or '<empty>'}.")

    if subtype == "vlm":
        if "strategy" in settings:
            raise ValueError("Unstructured VLM/Auto partition settings must use is_dynamic, not strategy.")
        is_dynamic = settings.get("is_dynamic")
        if not isinstance(is_dynamic, bool):
            raise ValueError("Unstructured VLM/Auto partition settings require boolean is_dynamic.")
        provider = str(settings.get("provider") or "").strip()
        model = str(settings.get("model") or "").strip()
        if bool(provider) != bool(model):
            raise ValueError("Unstructured VLM/Auto provider and model must be supplied together.")
        return "auto" if is_dynamic else "vlm"

    strategy = str(settings.get("strategy") or "").strip().lower()
    if strategy not in {"fast", "hi_res", "ocr_only"}:
        raise ValueError(f"Unstructured API partition subtype does not support strategy '{strategy}'.")
    return strategy


def _validate_prompter(node: dict[str, Any]) -> str:
    subtype = str(node.get("subtype") or "").strip()
    if subtype not in ENRICHMENT_NODE_TYPES:
        raise ValueError(f"Unsupported Unstructured prompter subtype: {subtype or '<empty>'}.")
    settings = node.get("settings")
    if settings is None:
        settings = {}
        node["settings"] = settings
    if not isinstance(settings, dict):
        raise ValueError(f"Unstructured node '{node.get('name')}' settings must be an object.")

    if subtype in TWOPASS_ENRICHMENT_NODE_TYPES:
        if settings.get("provider_type") or settings.get("model"):
            raise ValueError(f"Unstructured two-pass node '{subtype}' must omit provider_type and model.")
        return subtype

    provider_type = str(settings.get("provider_type") or "").strip().lower()
    model = str(settings.get("model") or "").strip()
    if not provider_type or not model:
        raise ValueError(f"Unstructured prompter '{subtype}' requires provider_type and model.")
    if provider_type not in ENRICHMENT_PROVIDER_TYPES[subtype]:
        allowed = ", ".join(sorted(ENRICHMENT_PROVIDER_TYPES[subtype]))
        raise ValueError(
            f"Unstructured prompter '{subtype}' does not support provider_type '{provider_type}'; "
            f"expected one of: {allowed}."
        )
    return subtype


def validate_workflow_nodes(workflow_nodes: list[dict[str, Any]]) -> None:
    """Validate the Unstructured Pipeline API workflow before network I/O."""
    if not workflow_nodes:
        raise ValueError("Unstructured workflow must contain at least one node.")

    partition_indexes = [index for index, node in enumerate(workflow_nodes) if node.get("type") == "partition"]
    if len(partition_indexes) != 1:
        raise ValueError("Unstructured workflow must contain exactly one partition node.")
    if partition_indexes[0] != 0:
        raise ValueError("Unstructured workflow partition node must be first.")

    partition = workflow_nodes[0]
    settings = partition.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Unstructured partition node settings must be an object.")
    route = _partition_route(partition, settings)

    chunk_indexes = [index for index, node in enumerate(workflow_nodes) if node.get("type") == "chunk"]
    if len(chunk_indexes) > 1:
        raise ValueError("Unstructured workflow supports at most one chunk node.")
    chunk_index = chunk_indexes[0] if chunk_indexes else None

    prompters: list[tuple[int, str, str]] = []
    for index, node in enumerate(workflow_nodes):
        if node.get("type") != "prompter":
            continue
        subtype = _validate_prompter(node)
        name = str(node.get("name") or subtype)
        prompters.append((index, subtype, name))

    redundant_vlm_prompters = [name for _, subtype, name in prompters if subtype not in NER_NODE_TYPES]
    if route == "vlm" and redundant_vlm_prompters:
        raise ValueError(
            "VLM partition does not accept separate image/table/OCR enrichment nodes; remove these nodes: "
            + ", ".join(redundant_vlm_prompters)
            + "."
        )
    if route in {"fast", "ocr_only"} and prompters:
        raise ValueError(f"Unstructured {route} partition does not accept enrichment nodes.")

    if chunk_index is not None:
        for index, subtype, name in prompters:
            if subtype in NER_NODE_TYPES and index < chunk_index:
                raise ValueError(f"Unstructured NER node '{name}' must follow the chunk node.")
            if subtype not in NER_NODE_TYPES and index > chunk_index:
                raise ValueError(f"Unstructured enrichment node '{name}' must precede the chunk node.")
