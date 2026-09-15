"""Pinned Phase-0 encoding and a pre-persistence secret boundary.

The small schema reader supports exactly the keywords in the pinned schema;
it is not a replacement schema or a general JSON Schema implementation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

from companion_mind.contracts import canonical_event_v1 as contract

CONTRACT_VERSION = "canonical_event/v1"
CONTRACT_COMMIT = "63ac8d7de8eb35915cc291b0f7ea67c33b366922"
SCHEMA_SHA256 = "37c8c6df4433b9bd070be6e8b07405b9a67e15dd5c93ed54c04251f4d36c9b1e"
VALIDATOR_SHA256 = "016163c05eb08ffed33910047ec57a1d5772aede9157a5079187cc1781e48508"
REDACTED = "[SECRET_REDACTED]"


class JournalError(ValueError):
    """Safe error code only: never attach input, paths or transport exceptions."""


def encode(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise JournalError("NON_JSON_INPUT") from None


def fingerprint(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def load_schema():
    candidates = [Path(__file__).resolve().parents[2] / "schemas/canonical_event_v1.schema.json",
                  Path(sys.prefix) / "share/companion-mind/schemas/canonical_event_v1.schema.json"]
    for path in candidates:
        if path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != SCHEMA_SHA256:
                raise JournalError("CONTRACT_CHANGE_REQUIRED")
            if hashlib.sha256(Path(contract.__file__).read_bytes()).hexdigest() != VALIDATOR_SHA256:
                raise JournalError("CONTRACT_CHANGE_REQUIRED")
            return json.loads(data)
    raise JournalError("CONTRACT_UNAVAILABLE")


def _shape(value, spec, root):
    if "$ref" in spec:
        target = root
        for part in spec["$ref"].split("/")[1:]:
            target = target[part]
        return _shape(value, target, root)
    if "anyOf" in spec:
        for alternative in spec["anyOf"]:
            try:
                _shape(value, alternative, root)
                return
            except JournalError:
                pass
        raise JournalError("CONTRACT_INVALID")
    types = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "null": value is None,
             "integer": type(value) is int}
    if "type" in spec:
        expected = spec["type"]
        if not any(types.get(t, False) for t in ([expected] if isinstance(expected, str) else expected)):
            raise JournalError("CONTRACT_INVALID")
    if "enum" in spec and value not in spec["enum"]:
        raise JournalError("CONTRACT_INVALID")
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise JournalError("CONTRACT_INVALID")
        if any(k not in value for k in spec.get("required", [])):
            raise JournalError("CONTRACT_INVALID")
        properties = spec.get("properties", {})
        for key, child in value.items():
            child_spec = properties.get(key, spec.get("additionalProperties", {}))
            if child_spec is False:
                raise JournalError("CONTRACT_INVALID")
            if isinstance(child_spec, dict):
                _shape(child, child_spec, root)
    if isinstance(value, list) and "items" in spec:
        for child in value:
            _shape(child, spec["items"], root)
    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0) or ("pattern" in spec and not re.search(spec["pattern"], value)):
            raise JournalError("CONTRACT_INVALID")
    if type(value) is int and value < spec.get("minimum", value):
        raise JournalError("CONTRACT_INVALID")


_CREDENTIAL = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|otp|"
    r"cookies?|authorization|csrf(?:[_ -]?token)?|card[_ -]?number|cvv|cvc)\s*[:=]\s*[^\n,;]+|"
    r"\bBearer\s+[^\s,;]+|\bsk-[A-Za-z0-9_-]+|\bgh[pousr]_[A-Za-z0-9_]+|"
    r"\b\d{13,19}\b|-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?-----END[^\n]*PRIVATE KEY-----|"
    r"https?://[^\s/@:]+:[^\s/@]+@[^\s]+)"
)


def scrub(value):
    """Conservative declared patterns + structured secret fields, not a DLP oracle."""
    if isinstance(value, str):
        if value == REDACTED:
            return value
        return _CREDENTIAL.sub(REDACTED, value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or scrub(key) != key:
                raise JournalError("UNSAFE_KEY")
            normalized = key.strip().lower().replace("-", "_")
            result[key] = REDACTED if normalized in contract._SECRET_KEYS else scrub(child)
        return result
    return value


def safe_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) or scrub(value) != value:
        raise JournalError("UNSAFE_CONTROL_ID")
    return value


def prepare(event, schema):
    serialized = encode(event)
    if len(serialized.encode("utf-8")) > 1_048_576:
        raise JournalError("EVENT_TOO_LARGE")
    try:
        _shape(event, schema, schema)
        clean = deepcopy(event)
        for key, value in clean.items():
            sanitized = scrub(value)
            if key in {"content_payload", "metadata"}:
                clean[key] = sanitized
            elif sanitized != value:
                raise JournalError("SECRET_IN_ENVELOPE")
        if clean != event:
            clean["redaction_state"] = "redacted"
        return contract.validate_canonical_event(clean)
    except JournalError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise JournalError("CONTRACT_INVALID") from None
