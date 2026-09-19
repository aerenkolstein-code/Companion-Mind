"""P3-S1 public-safe synthetic SourcePack contract and atomic local activation.

This module never reaches a network service and never treats payload prose as
authorization.  The trusted grant is supplied out-of-band by TestPort/server
setup.  Source bytes remain immutable after activation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .contracts import HomeError, encode, identifier, safe_text

READONLY_PROFILE = "owned-home-readonly/1"
SOURCE_PACK_VERSION = "readonly-source-pack/1"
GRANT_VERSION = "readonly-source-grant/1"
ANSWER_VERSION = "readonly-answer/1"
RESUME_VERSION = "readonly-resume/1"

READONLY_OPS = (
    "ro_info", "ro_package_validate", "ro_package_ingest", "ro_turn",
    "ro_observe", "ro_resume", "ro_source_view", "ro_session_state",
    "ro_safe_export",
)
READONLY_FAULTS = {
    "RO_AFTER_STAGE", "RO_BEFORE_ACTIVATE", "RO_AFTER_ACTIVATE",
    "RO_AFTER_CONTEXT",
}
COVERAGE = {"FULL", "EXCERPT", "TRUNCATED", "MISSING_ATTACHMENT", "NOT_LOADED"}
ROLES = {"AUTHORITY", "PROJECTION", "HISTORY"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")

MANIFEST_FIELDS = {
    "schema_version", "package_id", "package_version", "task_id", "purpose",
    "target_environment", "synthetic", "public_safe", "created_at",
    "expires_at", "max_total_bytes", "required_source_ids", "sources",
    "manifest_digest",
}
SOURCE_FIELDS = {
    "source_id", "source_kind", "owning_authority", "role", "selector",
    "source_revision", "captured_at", "as_of", "expires_at", "scope",
    "classification", "coverage", "byte_length", "content_digest",
    "payload_path", "transform_record", "supersession_ref",
}
SCOPE_FIELDS = {
    "universe_id", "access_subject_id", "purpose", "display_allowed",
    "model_egress_allowed",
}
GRANT_FIELDS = {
    "schema_version", "grant_id", "grant_version", "task_id", "package_id",
    "package_version", "manifest_digest", "universe_id", "access_subject_id",
    "purpose", "target_environment", "allowed_ops", "not_before",
    "expires_at", "revocation_epoch", "revoked", "grant_digest",
}


def _exact(value, fields, code="INVALID_SHAPE"):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise HomeError(code)


def _int(value, minimum, maximum, code):
    if type(value) is not int or not minimum <= value <= maximum:
        raise HomeError(code)
    return value


def _aware(value, code="INVALID_TIMESTAMP"):
    if not isinstance(value, str):
        raise HomeError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HomeError(code) from None
    if parsed.tzinfo is None:
        raise HomeError(code)
    return parsed.astimezone(timezone.utc)


def _no_floats(value):
    if isinstance(value, float):
        raise HomeError("FLOAT_NOT_ALLOWED")
    if isinstance(value, list):
        for item in value:
            _no_floats(item)
    elif isinstance(value, dict):
        for item in value.values():
            _no_floats(item)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HomeError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def strict_json_bytes(data):
    if not isinstance(data, (bytes, bytearray)):
        raise HomeError("INVALID_JSON_BYTES")
    try:
        text = bytes(data).decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=lambda _: (_ for _ in ()).throw(HomeError("FLOAT_NOT_ALLOWED")),
            parse_constant=lambda _: (_ for _ in ()).throw(HomeError("NONFINITE_JSON")),
        )
    except UnicodeDecodeError:
        raise HomeError("INVALID_UTF8") from None
    except json.JSONDecodeError:
        raise HomeError("INVALID_JSON") from None
    _no_floats(value)
    return value


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _canonical_manifest(value):
    item = dict(value)
    item.pop("manifest_digest", None)
    item["required_source_ids"] = sorted(item.get("required_source_ids", []))
    item["sources"] = sorted(item.get("sources", []), key=lambda s: s.get("source_id", ""))
    return item


def manifest_digest(value):
    return sha256_bytes(encode(_canonical_manifest(value)).encode("utf-8"))


def _canonical_grant(value):
    item = dict(value)
    item.pop("grant_digest", None)
    item["allowed_ops"] = sorted(set(item.get("allowed_ops", [])))
    return item


def grant_digest(value):
    return sha256_bytes(encode(_canonical_grant(value)).encode("utf-8"))


def _digest(value, field, calculator, code):
    digest = value.get(field)
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise HomeError(code)
    if digest != calculator(value):
        raise HomeError(code)


def _scope(value):
    _exact(value, SCOPE_FIELDS, "INVALID_SOURCE_SCOPE")
    identifier(value["universe_id"])
    identifier(value["access_subject_id"])
    safe_text(value["purpose"], maximum=256)
    if type(value["display_allowed"]) is not bool or value["model_egress_allowed"] is not False:
        raise HomeError("INVALID_SOURCE_SCOPE")
    return value


def read_manifest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise HomeError("INVALID_BUNDLE_ROOT")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise HomeError("MANIFEST_MISSING")
    data = manifest_path.read_bytes()
    if len(data) > 524288:
        raise HomeError("MANIFEST_TOO_LARGE")
    value = strict_json_bytes(data)
    _exact(value, MANIFEST_FIELDS, "INVALID_MANIFEST_SHAPE")
    if value["schema_version"] != SOURCE_PACK_VERSION:
        raise HomeError("SOURCE_PACK_VERSION_MISMATCH")
    for field in ("package_id", "package_version", "task_id"):
        identifier(value[field])
    safe_text(value["purpose"], maximum=256)
    identifier(value["target_environment"])
    if value["synthetic"] is not True or value["public_safe"] is not True:
        raise HomeError("PUBLIC_SYNTHETIC_PACKAGE_REQUIRED")
    created = _aware(value["created_at"])
    expires = _aware(value["expires_at"])
    if not created < expires:
        raise HomeError("INVALID_PACKAGE_LIFETIME")
    _int(value["max_total_bytes"], 0, 524288, "INVALID_PACKAGE_BUDGET")
    if not isinstance(value["required_source_ids"], list):
        raise HomeError("INVALID_REQUIRED_SOURCES")
    required = []
    for source_id in value["required_source_ids"]:
        identifier(source_id)
        required.append(source_id)
    if len(required) != len(set(required)):
        raise HomeError("DUPLICATE_REQUIRED_SOURCE")
    if not isinstance(value["sources"], list) or len(value["sources"]) > 16:
        raise HomeError("SOURCE_LIMIT")
    seen = set()
    for source in value["sources"]:
        _exact(source, SOURCE_FIELDS, "INVALID_SOURCE_SHAPE")
        source_id = source["source_id"]
        identifier(source_id)
        if source_id in seen:
            raise HomeError("DUPLICATE_SOURCE_ID")
        seen.add(source_id)
        safe_text(source["source_kind"], maximum=80)
        safe_text(source["owning_authority"], maximum=160)
        if source["role"] not in ROLES:
            raise HomeError("INVALID_SOURCE_ROLE")
        safe_text(source["selector"], maximum=512)
        identifier(source["source_revision"])
        for field in ("captured_at", "as_of", "expires_at"):
            _aware(source[field], "INVALID_SOURCE_TIMESTAMP")
        _scope(source["scope"])
        if source["classification"] != "PUBLIC_SYNTHETIC":
            raise HomeError("PUBLIC_SYNTHETIC_SOURCE_REQUIRED")
        if source["coverage"] not in COVERAGE:
            raise HomeError("INVALID_COVERAGE")
        if source["supersession_ref"] is not None:
            identifier(source["supersession_ref"])
        _no_floats(source["transform_record"])
        if source["coverage"] == "NOT_LOADED":
            if any(source[k] is not None for k in ("payload_path", "byte_length", "content_digest")):
                raise HomeError("NOT_LOADED_HAS_PAYLOAD")
        else:
            expected = f"payload/{source_id}.txt"
            if source["payload_path"] != expected:
                raise HomeError("INVALID_PAYLOAD_PATH")
            _int(source["byte_length"], 0, 32768, "SOURCE_TOO_LARGE")
            if not isinstance(source["content_digest"], str) or not HEX64.fullmatch(source["content_digest"]):
                raise HomeError("INVALID_CONTENT_DIGEST")
    if not set(required).issubset(seen):
        raise HomeError("REQUIRED_SOURCE_MISSING")
    for source in value["sources"]:
        if source["source_id"] in required and source["coverage"] == "NOT_LOADED":
            raise HomeError("REQUIRED_SOURCE_NOT_LOADED")
    _digest(value, "manifest_digest", manifest_digest, "MANIFEST_DIGEST_MISMATCH")
    return value


def validate_grant(grant, manifest, now, *, operation=None, minimum_epoch=None):
    _exact(grant, GRANT_FIELDS, "INVALID_GRANT_SHAPE")
    if grant["schema_version"] != GRANT_VERSION:
        raise HomeError("GRANT_VERSION_MISMATCH")
    for field in ("grant_id", "grant_version", "task_id", "package_id", "package_version",
                  "universe_id", "access_subject_id", "target_environment"):
        identifier(grant[field])
    safe_text(grant["purpose"], maximum=256)
    if not isinstance(grant["allowed_ops"], list):
        raise HomeError("INVALID_ALLOWED_OPS")
    allowed = []
    for op in grant["allowed_ops"]:
        if op not in READONLY_OPS:
            raise HomeError("INVALID_ALLOWED_OP")
        allowed.append(op)
    if len(allowed) != len(set(allowed)):
        raise HomeError("DUPLICATE_ALLOWED_OP")
    _int(grant["revocation_epoch"], 0, 2**31 - 1, "INVALID_REVOCATION_EPOCH")
    if type(grant["revoked"]) is not bool:
        raise HomeError("INVALID_REVOKED")
    _digest(grant, "grant_digest", grant_digest, "GRANT_DIGEST_MISMATCH")
    if (grant["task_id"], grant["package_id"], grant["package_version"],
        grant["manifest_digest"], grant["purpose"], grant["target_environment"]) != (
        manifest["task_id"], manifest["package_id"], manifest["package_version"],
        manifest["manifest_digest"], manifest["purpose"], manifest["target_environment"]):
        raise HomeError("GRANT_PACKAGE_BINDING_MISMATCH")
    source_scopes = {(s["scope"]["universe_id"], s["scope"]["access_subject_id"])
                     for s in manifest["sources"]}
    if source_scopes and source_scopes != {(grant["universe_id"], grant["access_subject_id"])}:
        raise HomeError("GRANT_SCOPE_MISMATCH")
    if grant["revoked"]:
        raise HomeError("GRANT_REVOKED")
    if minimum_epoch is not None and grant["revocation_epoch"] < minimum_epoch:
        raise HomeError("REVOCATION_EPOCH_ROLLBACK")
    moment = _aware(now, "READONLY_CLOCK_REQUIRED")
    if not (_aware(grant["not_before"]) <= moment < _aware(grant["expires_at"])):
        raise HomeError("GRANT_NOT_ACTIVE")
    if not (_aware(manifest["created_at"]) <= moment < _aware(manifest["expires_at"])):
        raise HomeError("PACKAGE_NOT_ACTIVE")
    if operation is not None and operation not in allowed:
        raise HomeError("OPERATION_NOT_GRANTED")
    return {
        "grant_id": grant["grant_id"], "grant_version": grant["grant_version"],
        "grant_digest": grant["grant_digest"], "revocation_epoch": grant["revocation_epoch"],
        "expires_at": grant["expires_at"], "allowed_ops": sorted(allowed),
    }


def _payload_path(root, source):
    raw = source["payload_path"]
    if raw is None:
        return None
    if "\\" in raw or raw.startswith("/") or ".." in Path(raw).parts:
        raise HomeError("INVALID_PAYLOAD_PATH")
    root_resolved = Path(root).resolve()
    path = Path(root, raw)
    if path.is_symlink() or not path.is_file():
        raise HomeError("PAYLOAD_NOT_REGULAR")
    resolved = path.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise HomeError("PAYLOAD_OUTSIDE_BUNDLE") from None
    st = path.stat()
    if st.st_nlink != 1:
        raise HomeError("HARDLINK_NOT_ALLOWED")
    return path


def validate_bundle(root, grant, now, *, operation=None, minimum_epoch=None):
    root = Path(root)
    manifest = read_manifest(root)
    grant_ref = validate_grant(grant, manifest, now, operation=operation, minimum_epoch=minimum_epoch)
    expected_top = {"manifest.json", "payload"}
    actual_top = {p.name for p in root.iterdir()}
    if actual_top - expected_top:
        raise HomeError("UNDECLARED_BUNDLE_ENTRY")
    payload_dir = root / "payload"
    declared_files = {Path(s["payload_path"]).name for s in manifest["sources"] if s["payload_path"]}
    if payload_dir.exists():
        if payload_dir.is_symlink() or not payload_dir.is_dir():
            raise HomeError("INVALID_PAYLOAD_DIRECTORY")
        actual = {p.name for p in payload_dir.iterdir()}
        if actual != declared_files:
            raise HomeError("UNDECLARED_PAYLOAD")
    elif declared_files:
        raise HomeError("PAYLOAD_DIRECTORY_MISSING")
    payloads = {}
    total = 0
    inodes = set()
    moment = _aware(now, "READONLY_CLOCK_REQUIRED")
    for source in manifest["sources"]:
        if source["coverage"] == "NOT_LOADED":
            continue
        if moment >= _aware(source["expires_at"]):
            raise HomeError("SOURCE_EXPIRED")
        path = _payload_path(root, source)
        inode = (path.stat().st_dev, path.stat().st_ino)
        if inode in inodes:
            raise HomeError("PAYLOAD_ALIAS")
        inodes.add(inode)
        data = path.read_bytes()
        if len(data) != source["byte_length"] or len(data) > 32768:
            raise HomeError("SOURCE_LENGTH_MISMATCH")
        if sha256_bytes(data) != source["content_digest"]:
            raise HomeError("CONTENT_DIGEST_MISMATCH")
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise HomeError("INVALID_UTF8") from None
        safe_text(text, maximum=32768)
        payloads[source["source_id"]] = text
        total += len(data)
    if total > manifest["max_total_bytes"] or total > 524288:
        raise HomeError("PACKAGE_BUDGET_EXCEEDED")
    return {
        "manifest": manifest, "payloads": payloads, "grant_ref": grant_ref,
        "total_payload_bytes": total, "bundle_root": str(root.resolve()),
    }


def _fault(name, selected):
    if selected == name:
        os._exit(86)


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    data = encode(value).encode("utf-8")
    with open(temp, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def activate_bundle(root, store_root, grant, now, *, fault=None):
    checked = validate_bundle(root, grant, now, operation="ro_package_ingest")
    manifest = checked["manifest"]
    root = Path(root)
    package_home = Path(store_root) / "packages" / manifest["package_id"] / manifest["package_version"]
    package_home.mkdir(parents=True, exist_ok=True)
    digest = manifest["manifest_digest"]
    siblings = [p.name for p in package_home.iterdir() if p.is_dir() and not p.name.startswith(".staging-")]
    if siblings and digest not in siblings:
        raise HomeError("PACKAGE_KEY_DIGEST_CONFLICT")
    target = package_home / digest
    existed = target.exists()
    if not existed:
        stage = package_home / (".staging-" + os.urandom(8).hex())
        stage.mkdir(mode=0o700)
        shutil.copyfile(root / "manifest.json", stage / "manifest.json")
        (stage / "payload").mkdir(mode=0o700)
        for source in manifest["sources"]:
            if source["payload_path"] is not None:
                shutil.copyfile(root / source["payload_path"], stage / source["payload_path"])
        _fault("RO_AFTER_STAGE", fault)
        validate_bundle(stage, grant, now, operation="ro_package_ingest")
        _fault("RO_BEFORE_ACTIVATE", fault)
        os.replace(stage, target)
        fd = os.open(package_home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fault("RO_AFTER_ACTIVATE", fault)
    else:
        validate_bundle(target, grant, now, operation="ro_package_ingest")
    pointer = Path(store_root) / "active" / manifest["package_id"] / (manifest["package_version"] + ".json")
    _write_json_atomic(pointer, {
        "package_id": manifest["package_id"], "package_version": manifest["package_version"],
        "manifest_digest": digest,
    })
    return {
        "status": "ACTIVE", "package_id": manifest["package_id"],
        "package_version": manifest["package_version"], "manifest_digest": digest,
        "idempotent_replay": existed, "total_payload_bytes": checked["total_payload_bytes"],
        "grant_ref": checked["grant_ref"],
    }


def load_active(store_root, package_id, package_version, digest, grant, now, *, operation):
    identifier(package_id)
    identifier(package_version)
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise HomeError("INVALID_MANIFEST_DIGEST")
    pointer = Path(store_root) / "active" / package_id / (package_version + ".json")
    if not pointer.is_file() or pointer.is_symlink():
        raise HomeError("PACKAGE_NOT_ACTIVE")
    ref = strict_json_bytes(pointer.read_bytes())
    if ref != {"package_id": package_id, "package_version": package_version, "manifest_digest": digest}:
        raise HomeError("PACKAGE_BINDING_MISMATCH")
    target = Path(store_root) / "packages" / package_id / package_version / digest
    checked = validate_bundle(target, grant, now, operation=operation)
    return checked


def make_manifest(*, package_id, package_version, task_id, purpose, target_environment,
                  scope, created_at, expires_at, sources, required_source_ids=(),
                  max_total_bytes=524288):
    built = []
    for item in sources:
        source = dict(item)
        text = source.pop("text", None)
        source_id = source["source_id"]
        coverage = source.get("coverage", "FULL")
        if coverage == "NOT_LOADED":
            payload_path = byte_length = content_digest_value = None
        else:
            data = (text or "").encode("utf-8")
            payload_path = f"payload/{source_id}.txt"
            byte_length = len(data)
            content_digest_value = sha256_bytes(data)
        built.append({
            "source_id": source_id,
            "source_kind": source.get("source_kind", "archive_snapshot"),
            "owning_authority": source.get("owning_authority", "synthetic-authority"),
            "role": source.get("role", "AUTHORITY"),
            "selector": source.get("selector", "body"),
            "source_revision": source.get("source_revision", "r1"),
            "captured_at": source.get("captured_at", created_at),
            "as_of": source.get("as_of", created_at),
            "expires_at": source.get("expires_at", expires_at),
            "scope": dict(scope),
            "classification": "PUBLIC_SYNTHETIC",
            "coverage": coverage,
            "byte_length": byte_length,
            "content_digest": content_digest_value,
            "payload_path": payload_path,
            "transform_record": source.get("transform_record", {"kind": "NONE"}),
            "supersession_ref": source.get("supersession_ref"),
            "_text": text,
        })
    manifest_sources = [{k: v for k, v in s.items() if k != "_text"} for s in built]
    manifest = {
        "schema_version": SOURCE_PACK_VERSION, "package_id": package_id,
        "package_version": package_version, "task_id": task_id, "purpose": purpose,
        "target_environment": target_environment, "synthetic": True, "public_safe": True,
        "created_at": created_at, "expires_at": expires_at,
        "max_total_bytes": max_total_bytes,
        "required_source_ids": list(required_source_ids),
        "sources": manifest_sources, "manifest_digest": "0" * 64,
    }
    manifest["manifest_digest"] = manifest_digest(manifest)
    return manifest, {s["source_id"]: s["_text"] for s in built if s["payload_path"] is not None}


def write_bundle(root, manifest, payloads):
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    (root / "payload").mkdir(parents=True, mode=0o700)
    (root / "manifest.json").write_bytes(encode(manifest).encode("utf-8"))
    for source_id, text in payloads.items():
        (root / "payload" / f"{source_id}.txt").write_bytes((text or "").encode("utf-8"))
    return root


def make_grant(manifest, *, universe_id, access_subject_id, allowed_ops=READONLY_OPS,
               not_before=None, expires_at=None, revocation_epoch=1, revoked=False,
               grant_id="readonly-grant", grant_version="g1"):
    grant = {
        "schema_version": GRANT_VERSION, "grant_id": grant_id,
        "grant_version": grant_version, "task_id": manifest["task_id"],
        "package_id": manifest["package_id"], "package_version": manifest["package_version"],
        "manifest_digest": manifest["manifest_digest"], "universe_id": universe_id,
        "access_subject_id": access_subject_id, "purpose": manifest["purpose"],
        "target_environment": manifest["target_environment"],
        "allowed_ops": sorted(set(allowed_ops)), "not_before": not_before or manifest["created_at"],
        "expires_at": expires_at or manifest["expires_at"],
        "revocation_epoch": revocation_epoch, "revoked": revoked, "grant_digest": "0" * 64,
    }
    grant["grant_digest"] = grant_digest(grant)
    return grant


def ensure_demo_bundle(root, now):
    moment = _aware(now, "READONLY_CLOCK_REQUIRED")
    created = moment.isoformat()
    expires = moment.replace(year=moment.year + 1).isoformat()
    scope = {
        "universe_id": "synthetic-home", "access_subject_id": "synthetic-owner",
        "purpose": "readonly-demo", "display_allowed": True, "model_egress_allowed": False,
    }
    manifest, payloads = make_manifest(
        package_id="readonly-demo", package_version="v1", task_id="readonly-demo-task",
        purpose="readonly-demo", target_environment="local-shell", scope=scope,
        created_at=created, expires_at=expires,
        sources=[{"source_id": "current", "source_kind": "current_snapshot",
                  "text": "Synthetic current agenda: continue the bounded offline task."}],
        required_source_ids=["current"],
    )
    root = write_bundle(root, manifest, payloads)
    grant = make_grant(manifest, universe_id=scope["universe_id"],
                       access_subject_id=scope["access_subject_id"])
    return root, grant, manifest
