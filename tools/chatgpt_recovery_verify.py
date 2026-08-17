from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "cm-c2-recovery/v0.1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "csrf",
    "csrf_token",
    "x_csrf_token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "otp",
}


class VerificationError(ValueError):
    pass


class RawNumber(str):
    """A JSON numeric token preserved exactly as it appeared in the downloaded file."""


def _reject_constant(value: str) -> None:
    raise VerificationError(f"non-finite JSON number is not allowed: {value}")


def _load_normal(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, VerificationError) as exc:
        raise VerificationError(f"cannot parse artifact JSON: {exc}") from exc


def _load_preserving_numbers(text: str) -> Any:
    try:
        return json.loads(
            text,
            parse_int=RawNumber,
            parse_float=RawNumber,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, VerificationError) as exc:
        raise VerificationError(f"cannot parse artifact JSON with preserved number tokens: {exc}") from exc


def _js_utf16_sort_key(value: str) -> bytes:
    return value.encode("utf-16-be", errors="surrogatepass")


def _js_string(value: str) -> str:
    escapes = {
        '"': '\\"',
        '\\': '\\\\',
        '\b': '\\b',
        '\f': '\\f',
        '\n': '\\n',
        '\r': '\\r',
        '\t': '\\t',
    }
    pieces = ['"']
    for char in value:
        if char in escapes:
            pieces.append(escapes[char])
            continue
        codepoint = ord(char)
        if codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            pieces.append(f"\\u{codepoint:04x}")
        else:
            pieces.append(char)
    pieces.append('"')
    return ''.join(pieces)


def _canonical_browser_json(value: Any) -> str:
    if isinstance(value, RawNumber):
        return str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_browser_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise VerificationError("artifact contains a non-string object key")
        pieces = []
        for key in sorted(value, key=_js_utf16_sort_key):
            pieces.append(_js_string(key) + ":" + _canonical_browser_json(value[key]))
        return "{" + ",".join(pieces) + "}"
    raise VerificationError(f"artifact contains unsupported JSON value type: {type(value).__name__}")


def _scan_credential_surfaces(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                hits.append(f"{path}.{key_text}")
            hits.extend(_scan_credential_surfaces(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_scan_credential_surfaces(child, f"{path}[{index}]"))
    return hits


def verify_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerificationError(f"cannot read artifact: {exc}") from exc

    artifact = _load_normal(text)
    raw_artifact = _load_preserving_numbers(text)
    if not isinstance(artifact, Mapping) or not isinstance(raw_artifact, Mapping):
        raise VerificationError("artifact root must be an object")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise VerificationError(f"unsupported schema_version: {artifact.get('schema_version')!r}")

    stored_hash = artifact.get("sha256")
    if not isinstance(stored_hash, str) or not _SHA256_RE.fullmatch(stored_hash):
        raise VerificationError("artifact sha256 is missing or malformed")

    raw_body = dict(raw_artifact)
    raw_stored = raw_body.pop("sha256", None)
    if raw_stored != stored_hash:
        raise VerificationError("artifact sha256 field changed during parsing")
    canonical = _canonical_browser_json(raw_body)
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if expected_hash != stored_hash:
        raise VerificationError(
            "artifact checksum mismatch under browser canonicalization "
            f"(stored={stored_hash}, computed={expected_hash})"
        )

    nodes = artifact.get("ordered_nodes")
    if not isinstance(nodes, list):
        raise VerificationError("artifact ordered_nodes must be a list")
    if artifact.get("active_path_size") != len(nodes):
        raise VerificationError("active_path_size does not match ordered_nodes")

    hits = _scan_credential_surfaces(artifact)
    if hits:
        raise VerificationError("credential/header-shaped surface detected: " + ", ".join(hits[:5]))

    return {
        "status": "PASS",
        "schema_version": artifact["schema_version"],
        "mapping_size": artifact.get("mapping_size"),
        "active_path_size": len(nodes),
        "sha256": stored_hash,
        "credential_surface_count": 0,
        "canonicalization": "browser-json-number-lexeme/v1",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a browser-exported C2 recovery artifact without uploading its bodies")
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_file(args.artifact), sort_keys=True))
        return 0
    except VerificationError as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
