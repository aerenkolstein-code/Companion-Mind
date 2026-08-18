from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ADAPTER_VERSION = "chatgpt-web-queryclient/v0.1"
SCHEMA_VERSION = "cm-c2-recovery/v0.1"

_CREDENTIAL_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "csrf",
    "csrf_token",
    "x-csrf-token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "otp",
}


class RecoveryError(ValueError):
    """Fail-closed error raised for malformed or unsafe recovery inputs."""


@dataclass(frozen=True)
class ReconciliationResult:
    entries: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    summary: dict[str, int]


def _json_clone(value: Any) -> Any:
    """Return a JSON-safe deep copy and reject unserializable surfaces."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"value is not JSON serializable: {exc}") from exc


def _iter_mapping_items(mapping: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for node_id, node in mapping.items():
        if not isinstance(node_id, str) or not node_id:
            raise RecoveryError("mapping contains a non-string or empty node id")
        if not isinstance(node, Mapping):
            raise RecoveryError(f"mapping node {node_id!r} is not an object")
        yield node_id, node


def scan_credential_surfaces(value: Any, path: str = "$") -> list[str]:
    """Find credential/header-shaped object keys without inspecting message semantics."""
    hits: list[str] = []
    normalized_keys = {item.replace("-", "_") for item in _CREDENTIAL_KEYS}
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            if normalized in normalized_keys:
                hits.append(f"{path}.{key_text}")
            hits.extend(scan_credential_surfaces(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(scan_credential_surfaces(child, f"{path}[{index}]"))
    return hits


def reconstruct_active_path(mapping: Mapping[str, Any], current_node: str) -> list[tuple[str, Mapping[str, Any]]]:
    """Follow current_node -> parent to root, failing closed on corruption."""
    if not isinstance(current_node, str) or not current_node:
        raise RecoveryError("current_node must be a non-empty string")
    normalized = dict(_iter_mapping_items(mapping))
    if current_node not in normalized:
        raise RecoveryError(f"current_node {current_node!r} is absent from mapping")

    seen: set[str] = set()
    reversed_path: list[tuple[str, Mapping[str, Any]]] = []
    node_id: str | None = current_node

    while node_id is not None:
        if node_id in seen:
            raise RecoveryError(f"cycle detected at node {node_id!r}")
        seen.add(node_id)
        node = normalized.get(node_id)
        if node is None:
            raise RecoveryError(f"missing parent node {node_id!r}")
        reversed_path.append((node_id, node))

        parent = node.get("parent")
        if parent in (None, ""):
            node_id = None
        elif isinstance(parent, str):
            node_id = parent
        else:
            raise RecoveryError(f"node {node_id!r} has a non-string parent")

    reversed_path.reverse()
    return reversed_path


def node_record(path_index: int, node_id: str, node: Mapping[str, Any]) -> dict[str, Any]:
    message = node.get("message")
    if message is None:
        message_obj: Mapping[str, Any] = {}
    elif isinstance(message, Mapping):
        message_obj = message
    else:
        raise RecoveryError(f"node {node_id!r} has non-object message")

    author = message_obj.get("author")
    author_obj = author if isinstance(author, Mapping) else {}
    content = message_obj.get("content")
    content_obj = content if isinstance(content, Mapping) else {}

    children = node.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list) or not all(isinstance(item, str) for item in children):
        raise RecoveryError(f"node {node_id!r} has invalid children")

    record = {
        "path_index": path_index,
        "node_id": node_id,
        "parent_id": node.get("parent"),
        "children_ids": children,
        "role": author_obj.get("role"),
        "content_type": content_obj.get("content_type"),
        "create_time": message_obj.get("create_time"),
        "message_id": message_obj.get("id"),
        "content_parts": content_obj.get("parts"),
        "metadata": message_obj.get("metadata", {}),
    }
    return _json_clone(record)


def _conversation_envelope(data: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"mapping", "current_node"}
    envelope = {key: value for key, value in data.items() if key not in excluded}
    hits = scan_credential_surfaces(envelope)
    if hits:
        raise RecoveryError("credential/header-shaped surface detected in envelope: " + ", ".join(hits[:5]))
    return _json_clone(envelope)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_recovery_artifact(
    conversation_data: Mapping[str, Any],
    *,
    conversation_id: str | None = None,
    exported_at: str | None = None,
    adapter_version: str = ADAPTER_VERSION,
) -> dict[str, Any]:
    mapping = conversation_data.get("mapping")
    current_node = conversation_data.get("current_node")
    if not isinstance(mapping, Mapping):
        raise RecoveryError("conversation payload has no object mapping")
    if not isinstance(current_node, str) or not current_node:
        raise RecoveryError("conversation payload has no current_node")

    path = reconstruct_active_path(mapping, current_node)
    ordered_nodes = [node_record(index, node_id, node) for index, (node_id, node) in enumerate(path)]
    detected_id = conversation_id or conversation_data.get("id") or conversation_data.get("conversation_id")
    if detected_id is not None and not isinstance(detected_id, str):
        raise RecoveryError("conversation id must be a string when present")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": adapter_version,
        "source": "chatgpt-web-queryclient-memory",
        "conversation_id": detected_id,
        "exported_at": exported_at or datetime.now(timezone.utc).isoformat(),
        "mapping_size": len(mapping),
        "active_path_size": len(ordered_nodes),
        "current_node": current_node,
        "conversation_envelope": _conversation_envelope(conversation_data),
        "ordered_nodes": ordered_nodes,
    }
    hits = scan_credential_surfaces(payload)
    if hits:
        raise RecoveryError("credential/header-shaped surface detected: " + ", ".join(hits[:5]))
    payload["sha256"] = sha256_hex(payload)
    return payload


def verify_recovery_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError(f"unsupported schema_version: {artifact.get('schema_version')!r}")
    stored_hash = artifact.get("sha256")
    if not isinstance(stored_hash, str) or len(stored_hash) != 64:
        raise RecoveryError("artifact sha256 is missing or malformed")
    body = dict(artifact)
    body.pop("sha256", None)
    expected = sha256_hex(body)
    if expected != stored_hash:
        raise RecoveryError("artifact checksum mismatch")
    nodes = artifact.get("ordered_nodes")
    if not isinstance(nodes, list):
        raise RecoveryError("artifact ordered_nodes must be a list")
    if artifact.get("active_path_size") != len(nodes):
        raise RecoveryError("active_path_size does not match ordered_nodes")
    hits = scan_credential_surfaces(artifact)
    if hits:
        raise RecoveryError("credential/header-shaped surface detected: " + ", ".join(hits[:5]))
    return {
        "schema_version": artifact["schema_version"],
        "mapping_size": artifact.get("mapping_size"),
        "active_path_size": len(nodes),
        "sha256": stored_hash,
        "credential_surface_count": 0,
    }


def _candidate_ids(node: Mapping[str, Any]) -> set[str]:
    values = {node.get("node_id"), node.get("message_id")}
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        values.update(
            {
                metadata.get("turn_id"),
                metadata.get("turn_id_container"),
                metadata.get("dom_turn_id"),
            }
        )
    return {value for value in values if isinstance(value, str) and value}


def _ledger_identifier(entry: Mapping[str, Any]) -> str | None:
    for key in ("known_turn_id_or_dom_id", "turn_id", "message_id", "node_id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def reconcile_renderable_ledger(
    artifact: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
) -> ReconciliationResult:
    verify_recovery_artifact(artifact)
    raw_nodes = artifact.get("ordered_nodes")
    assert isinstance(raw_nodes, list)
    nodes: list[Mapping[str, Any]] = [node for node in raw_nodes if isinstance(node, Mapping)]
    if len(nodes) != len(raw_nodes):
        raise RecoveryError("ordered_nodes contains a non-object entry")

    id_to_indexes: dict[str, list[int]] = {}
    for index, node in enumerate(nodes):
        for candidate in _candidate_ids(node):
            id_to_indexes.setdefault(candidate, []).append(index)

    matched_indexes: set[int] = set()
    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    for ordinal, ledger_entry in enumerate(ledger):
        if not isinstance(ledger_entry, Mapping):
            raise RecoveryError("renderable ledger contains a non-object entry")
        renderable_index = ledger_entry.get("renderable_index", ordinal)
        expected_role = ledger_entry.get("expected_role", ledger_entry.get("role"))
        stable_id = _ledger_identifier(ledger_entry)

        if stable_id:
            candidates = id_to_indexes.get(stable_id, [])
            if len(candidates) == 1:
                node_index = candidates[0]
                node = nodes[node_index]
                role = node.get("role")
                if expected_role and role and expected_role != role:
                    disposition = "AMBIGUOUS_MAPPING"
                    reason = f"stable id matched but role differs: expected {expected_role!r}, got {role!r}"
                else:
                    disposition = "MATCHED"
                    reason = "stable id matched node_id/message_id/known turn metadata"
                    matched_indexes.add(node_index)
            elif len(candidates) == 0:
                node_index = None
                disposition = "MISSING_FROM_BULK"
                reason = "stable ledger id is absent from bulk artifact"
            else:
                node_index = None
                disposition = "AMBIGUOUS_MAPPING"
                reason = "stable ledger id maps to multiple bulk nodes"
        else:
            node_index = None
            disposition = "AMBIGUOUS_MAPPING"
            reason = "ledger item has no stable id; text-similarity fallback is intentionally disabled"

        nearest = []
        if node_index is not None:
            lo = max(0, node_index - 1)
            hi = min(len(nodes), node_index + 2)
            nearest = [nodes[i].get("node_id") for i in range(lo, hi)]

        entry = {
            "renderable_index": renderable_index,
            "expected_role": expected_role,
            "known_turn_id_or_dom_id": stable_id,
            "matched_bulk_path_index": node_index,
            "nearest_bulk_node_ids": nearest,
            "disposition": disposition,
            "reason": reason,
        }
        entries.append(entry)
        if disposition in {"MISSING_FROM_BULK", "AMBIGUOUS_MAPPING"}:
            gap = dict(entry)
            gap["recommended_backfill_action"] = "sidebar_random_access" if stable_id else "capture_stable_id_then_retry"
            gaps.append(gap)

    for node_index, node in enumerate(nodes):
        if node_index in matched_indexes:
            continue
        entries.append(
            {
                "renderable_index": None,
                "expected_role": None,
                "known_turn_id_or_dom_id": None,
                "matched_bulk_path_index": node_index,
                "nearest_bulk_node_ids": [node.get("node_id")],
                "disposition": "EXTRA_GRAPH_NODE",
                "reason": "active-path graph node has no matched renderable-ledger entry",
            }
        )

    summary = {key: 0 for key in ("MATCHED", "MISSING_FROM_BULK", "EXTRA_GRAPH_NODE", "AMBIGUOUS_MAPPING")}
    for entry in entries:
        summary[entry["disposition"]] += 1
    return ReconciliationResult(entries=entries, gaps=gaps, summary=summary)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only ChatGPT long-conversation recovery helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build a recovery artifact from a saved exact-conversation payload")
    build.add_argument("payload", type=Path)
    build.add_argument("output", type=Path)
    build.add_argument("--conversation-id")

    verify = sub.add_parser("verify", help="Verify checksum and structural safety")
    verify.add_argument("artifact", type=Path)

    reconcile = sub.add_parser("reconcile", help="Reconcile artifact against a renderable-turn ledger")
    reconcile.add_argument("artifact", type=Path)
    reconcile.add_argument("ledger", type=Path)
    reconcile.add_argument("--out", type=Path, required=True)
    reconcile.add_argument("--gaps-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            payload = _load_json(args.payload)
            if not isinstance(payload, Mapping):
                raise RecoveryError("payload root must be an object")
            artifact = build_recovery_artifact(payload, conversation_id=args.conversation_id)
            _write_json(args.output, artifact)
            print(json.dumps(verify_recovery_artifact(artifact), sort_keys=True))
            return 0

        if args.command == "verify":
            artifact = _load_json(args.artifact)
            if not isinstance(artifact, Mapping):
                raise RecoveryError("artifact root must be an object")
            print(json.dumps(verify_recovery_artifact(artifact), sort_keys=True))
            return 0

        if args.command == "reconcile":
            artifact = _load_json(args.artifact)
            ledger = _load_json(args.ledger)
            if not isinstance(artifact, Mapping) or not isinstance(ledger, list):
                raise RecoveryError("artifact must be an object and ledger must be a list")
            result = reconcile_renderable_ledger(artifact, ledger)
            report = {"schema_version": "cm-c2-reconciliation/v0.1", "summary": result.summary, "entries": result.entries}
            _write_json(args.out, report)
            gaps_out = args.gaps_out or args.out.with_name(args.out.stem + ".gaps.json")
            _write_json(gaps_out, {"schema_version": "cm-c2-gap-ledger/v0.1", "gaps": result.gaps})
            print(json.dumps(result.summary, sort_keys=True))
            return 0
    except RecoveryError as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}, ensure_ascii=False))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
