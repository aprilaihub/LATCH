#!/usr/bin/env python3
"""Regenerate the generic WP-016A retrieval protocol without live calls."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import (  # noqa: E402
    RETRIEVAL_FRAGMENTATION_VERSION, canonical_bytes,
    compact_model_visible_protocol, compact_protocol_definition,
    fragment_retrieval_registry, sha256,
)


CONFIG = ROOT / "configs/wp016a"
REGISTRY = CONFIG / "closed-retrieval-registry.json"
LEDGER = CONFIG / "source-configurations.csv"
PAYLOADS = CONFIG / "source-payloads"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _update_compact_payload(path: Path, registry: dict[str, Any],
                            protocol: dict[str, Any]) -> dict[str, int]:
    body = json.loads(path.read_text(encoding="utf-8"))
    context_text = str(body["messages"][1]["content"])
    original_bytes = len(context_text.encode("utf-8"))
    context = json.loads(context_text)
    padding = str(context.pop("padding"))
    context["schema_version"] = "wp016a.c1-compact-initial.v3"
    context["protocol"] = protocol
    starter_ids = list(context["starter_content_ids"])
    context["starter_content_index"] = [{
        "content_id": content_id,
        "size_bytes": registry["entries"][content_id]["retrieval_size_bytes"],
    } for content_id in starter_ids]
    context["schema_fragmentation_version"] = RETRIEVAL_FRAGMENTATION_VERSION
    context["padding"] = ""
    unpadded = canonical_bytes(context).decode("utf-8")
    deficit = original_bytes - len(unpadded.encode("utf-8"))
    if deficit < 0 or deficit > len(padding):
        raise RuntimeError("compact_protocol_update_cannot_preserve_context_bytes")
    context["padding"] = "0" * deficit
    updated = canonical_bytes(context).decode("utf-8")
    if len(updated.encode("utf-8")) != original_bytes:
        raise AssertionError("compact_context_size_drift")
    body["messages"][1]["content"] = updated
    _write_json(path, body)
    return {
        "context_bytes": original_bytes,
        "prompt_bytes": len(str(body["messages"][0]["content"]).encode("utf-8")),
        "transport_payload_bytes": len(canonical_bytes(body)),
        "padding_bytes": deficit,
    }


def build() -> dict[str, Any]:
    definition = compact_protocol_definition()
    visible = compact_model_visible_protocol(definition)
    registry = fragment_retrieval_registry(
        json.loads(REGISTRY.read_text(encoding="utf-8")), protocol=definition,
    )
    _write_json(REGISTRY, registry)

    with LEDGER.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = list(reader.fieldnames or ())
    compact_count = 0
    for row in rows:
        if not row["arm"].startswith("COMPACT"):
            continue
        compact_count += 1
        path = PAYLOADS / f"{row['configuration_id']}.json"
        sizes = _update_compact_payload(path, registry, visible)
        body = json.loads(path.read_text(encoding="utf-8"))
        prompt = str(body["messages"][0]["content"]).encode("utf-8")
        context = str(body["messages"][1]["content"]).encode("utf-8")
        row.update({
            "canonical_guard_input_bytes": str(len(prompt) + len(context)),
            "canonical_payload_sha256": sha256(body),
            "configuration_status": "WP016A_METERED_FRONTIER_REPAIR_R1_PENDING_GPT_PRO_REVIEW",
            "context_bytes": str(sizes["context_bytes"]),
            "context_hash": hashlib.sha256(context).hexdigest(),
            "contract_hash": sha256(visible),
            "historical_parity_status": "METERED_DELIVERY_PROTOCOL_R1_PENDING_GPT_PRO_REVIEW",
            "payload_hash": sha256(body),
            "prompt_bytes": str(sizes["prompt_bytes"]),
            "prompt_hash": hashlib.sha256(prompt).hexdigest(),
            "serialized_file_sha256": _file_sha256(path),
            "transport_payload_bytes": str(sizes["transport_payload_bytes"]),
            "true_guard_headroom_bytes": str(65_536 - len(prompt) - len(context)),
        })
    if compact_count != 4:
        raise RuntimeError("exactly_four_compact_payloads_required")
    with LEDGER.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return {
        "status": "PASS_ZERO_CALL_REGENERATION",
        "registry_entries": len(registry["entries"]),
        "registry_sha256": registry["registry_sha256"],
        "compact_payloads": compact_count,
        "protocol_sha256": sha256(definition),
        "model_visible_protocol_sha256": sha256(visible),
        "provider_calls": 0,
        "backend_calls": 0,
    }


def main() -> int:
    print(json.dumps(build(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
