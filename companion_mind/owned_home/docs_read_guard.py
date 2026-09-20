"""Offline P3-S2 read guard. No Google transport, credentials or shared-policy edits.

Trusted setup owns Binding, clock, broker, fixture and lifecycle methods. The
untrusted entry is execute(): its caller cannot select endpoints, transports,
policy tiers or credential values. Python/OS administrators are not sandboxed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from threading import RLock

from .action_control import AtomicState
from .contracts import HomeError, fingerprint, identifier, safe_text, timestamp

PROFILE = "p3s2-docs-read-guard/1"
PROVIDER = "synthetic-google-docs"
MIME = "application/vnd.google-apps.document"
WRITES = frozenset({"edit", "append", "create", "copy", "move", "delete", "share",
                    "permission", "permission_mutation", "batchUpdate",
                    "documents.batchUpdate", "drive.write", "write",
                    "synthetic.reversible_write"})
_STATE = "p3s2-read-guard-state/1"


def _time(value):
    timestamp(value)
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Binding:
    provider: str
    app: str
    client: str
    project: str
    access_subject: str
    universe: str
    resource_id: str
    mime: str
    purpose: str
    credential_domain: str
    grant_generation: int
    policy_version: str
    not_before: str
    expires_at: str

    def __post_init__(self):
        for name in ("provider", "app", "client", "project", "access_subject",
                     "universe", "resource_id", "purpose", "credential_domain"):
            value = getattr(self, name)
            identifier(value)
            if value.upper() in {"UNKNOWN", "UNBOUND", "NOT_LOADED", "NONE"}:
                raise HomeError("BINDING_UNKNOWN")
        if self.provider != PROVIDER or self.mime != MIME or self.policy_version != PROFILE:
            raise HomeError("OFFLINE_PROFILE_REQUIRED")
        if type(self.grant_generation) is not int or not 1 <= self.grant_generation <= 1_000_000:
            raise HomeError("INVALID_GENERATION")
        if _time(self.not_before) >= _time(self.expires_at):
            raise HomeError("INVALID_VALIDITY")

    def projection(self):
        return asdict(self)

    @property
    def digest(self):
        return fingerprint(self.projection())


class OpaqueHandle:
    """An object-identity capability. No credential value or serializable ID."""
    __slots__ = ()

    def __repr__(self):
        return "<opaque-synthetic-handle>"

    def __reduce_ex__(self, protocol):
        raise HomeError("HANDLE_EXPORT_DENIED")

    def __copy__(self):
        raise HomeError("HANDLE_EXPORT_DENIED")

    def __deepcopy__(self, memo):
        raise HomeError("HANDLE_EXPORT_DENIED")


class SyntheticBroker:
    """In-memory synthetic canary only; no OS/env/file credential fallback.

This fixture is not an implementation or qualification of an OS SecretStore.
Only registered handle identities can acquire a read lease bound to one file.
"""
    def __init__(self, canary="SYNTHETIC_ONLY_RG_CANARY", *, available=True, locked=False):
        if type(canary) is not str or not canary.startswith("SYNTHETIC_ONLY_") or len(canary) > 128:
            raise HomeError("SYNTHETIC_CANARY_REQUIRED")
        self.__canary = canary
        self.__handles = {}
        self.__leases = {}
        self.available, self.locked = available, locked
        self.dereferences = 0

    def _issue(self, binding):
        handle = OpaqueHandle()
        self.__handles[handle] = binding.digest
        return handle

    def _matches(self, handle, binding):
        return type(handle) is OpaqueHandle and self.__handles.get(handle) == binding.digest

    def _lease(self, handle, binding, epoch):
        self._ready()
        if not self._matches(handle, binding):
            raise HomeError("HANDLE_DENIED")
        self.dereferences += 1
        lease = OpaqueHandle()
        self.__leases[lease] = (binding.resource_id, binding.digest, epoch)
        return lease

    def _ready(self):
        if self.available is not True:
            raise HomeError("SECRETSTORE_UNAVAILABLE")
        if self.locked is not False:
            raise HomeError("SECRETSTORE_LOCKED")

    def _allows(self, lease, resource_id, digest, epoch):
        return type(lease) is OpaqueHandle and self.__leases.get(lease) == (resource_id, digest, epoch)

    def _release(self, lease):
        self.__leases.pop(lease, None)

    def _invalidate(self):
        self.__handles.clear()
        self.__leases.clear()

    def _contains_canary(self, value):
        if isinstance(value, str):
            return self.__canary in value
        if isinstance(value, dict):
            return any(self._contains_canary(k) or self._contains_canary(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(self._contains_canary(v) for v in value)
        return False


@dataclass(frozen=True)
class _FixtureRead:
    operation: str
    resource_id: str
    binding_digest: str
    epoch: int


class OfflineDocsFixture:
    """Fixed in-memory READ responses, including adversarial synthetic replies.

The trusted test hook runs after a dispatched exchange to model revoke races.
Neither arbitrary URLs nor write operations exist in this transport fixture.
"""
    def __init__(self, resources, *, metadata_after=None, error=None, after_exchange=None):
        self.__resources = deepcopy(resources)
        self.__metadata_after = deepcopy(metadata_after)
        self.error = error
        self.after_exchange = after_exchange
        self.calls = []
        self.business_writes = 0

    def _read(self, call, broker, lease):
        if (type(call) is not _FixtureRead or call.operation not in {"metadata", "body"}
                or not broker._allows(lease, call.resource_id, call.binding_digest, call.epoch)):
            raise HomeError("FIXTURE_DISPATCH_DENIED")
        self.calls.append((call.operation, call.resource_id))
        # Only fixed error classes cross the boundary; never provider text.
        if self.error is not None:
            code = {"403": "ACCESS_DENIED_403", "404": "NOT_FOUND_OR_UNREADABLE_404",
                    "timeout": "TIMEOUT_UNKNOWN"}.get(self.error, "PROVIDER_UNKNOWN")
            raise HomeError(code)
        value = self.__resources.get(call.resource_id)
        if value is None:
            raise HomeError("NOT_FOUND_OR_UNREADABLE_404")
        if call.operation == "body":
            return deepcopy(value.get("body"))
        if self.__metadata_after is not None and self.calls[-2:-1] == [("body", call.resource_id)]:
            return deepcopy(self.__metadata_after)
        return deepcopy(value.get("metadata"))


class ReadGuard:
    """P3-S2-only policy, independent of generic synthetic P2 ALLOW decisions."""
    def __init__(self, directory, binding, broker, adapter, clock):
        if type(binding) is not Binding or type(broker) is not SyntheticBroker or type(adapter) is not OfflineDocsFixture:
            raise HomeError("EXACT_OFFLINE_COMPONENTS_REQUIRED")
        self._lock = RLock()
        self._binding, self._broker, self._adapter, self._clock = binding, broker, adapter, clock
        self._closed = self._blocked = False
        self._resumes = {}
        directory = Path(directory)
        # A damaged existing control directory cannot bootstrap a fresh grant.
        if directory.exists() and not (directory / "state.json").is_file():
            raise HomeError("LIFECYCLE_STATE_MISSING")
        initial = {"binding_digest": binding.digest, "generation": binding.grant_generation,
                   "epoch": 0, "revoked": False}
        try:
            self._state = AtomicState(directory, _STATE, initial)
            data = self._state.data
            if (set(data) != {"format", *initial} or data["binding_digest"] != binding.digest
                    or type(data["generation"]) is not int or data["generation"] != binding.grant_generation
                    or type(data["epoch"]) is not int or data["epoch"] < 0
                    or type(data["revoked"]) is not bool):
                raise HomeError("LIFECYCLE_BINDING_MISMATCH")
        except Exception:
            if hasattr(self, "_state"):
                self._state.close()
            raise HomeError("LIFECYCLE_STATE_UNAVAILABLE") from None
        self.handle = self._broker._issue(binding)

    @property
    def binding(self):
        return self._binding

    @property
    def epoch(self):
        return self._state.data["epoch"]

    def request(self, **overrides):
        """Convenience for synthetic callers; execute still validates every field."""
        result = {"action": "read", "binding": self.binding.projection(),
                  "epoch": self.epoch, "handle": self.handle, "require_fresh": True}
        result.update(overrides)
        return result

    def _receipt(self, status, reason, **extra):
        return {"profile": PROFILE, "status": status, "reason": reason,
                "authority": False, "binding_digest": self.binding.digest,
                "resource_id": self.binding.resource_id,
                "grant_generation": self.binding.grant_generation, "revocation_epoch": self.epoch,
                "automatic_retry_count": 0, "scope_expansion_count": 0,
                "business_write_count": 0, **extra}

    def _live(self, stamp=None):
        if self._closed or self._blocked:
            raise HomeError("GUARD_CLOSED")
        if self._state.data["revoked"]:
            raise HomeError("REVOKED")
        if stamp is not None and stamp != (self.binding.digest, self.epoch):
            raise HomeError("GRANT_CHANGED")
        now = _time(self._clock())
        if now < _time(self.binding.not_before):
            raise HomeError("NOT_YET_VALID")
        if now >= _time(self.binding.expires_at):
            raise HomeError("EXPIRED")

    def _validate(self, request):
        if type(request) is not dict:
            raise HomeError("INVALID_REQUEST")
        if self._broker._contains_canary(request):
            raise HomeError("SECRET_INPUT_BLOCKED")
        required = {"action", "binding", "epoch", "handle"}
        if set(request) - required - {"require_fresh", "resume_handle"} or not required <= set(request):
            raise HomeError("INVALID_REQUEST")
        action = request["action"]
        if type(action) is not str:
            raise HomeError("INVALID_REQUEST")
        if action in WRITES:
            raise HomeError("WRITE_DENIED")
        if action != "read":
            raise HomeError("ACTION_NOT_ALLOWED")
        if type(request.get("require_fresh", True)) is not bool:
            raise HomeError("INVALID_REQUEST")
        candidate = request["binding"]
        if type(candidate) is not dict or set(candidate) != {f.name for f in fields(Binding)}:
            raise HomeError("BINDING_MISMATCH")
        try:
            bound = Binding(**candidate)
        except (HomeError, TypeError, ValueError):
            raise HomeError("BINDING_MISMATCH") from None
        if bound != self.binding:
            raise HomeError("BINDING_MISMATCH")
        if type(request["epoch"]) is not int or request["epoch"] != self.epoch:
            raise HomeError("EPOCH_MISMATCH")
        self._live()
        if not self._broker._matches(request["handle"], self.binding):
            raise HomeError("HANDLE_DENIED")
        self._broker._ready()

    def _exchange(self, operation, stamp, lease):
        # Linearize dispatch against revoke. The fixture does no I/O under lock.
        with self._lock:
            self._live(stamp)
            call = _FixtureRead(operation, self.binding.resource_id, *stamp)
            response = self._adapter._read(call, self._broker, lease)
        if self._adapter.after_exchange is not None:
            self._adapter.after_exchange(operation)
        return response

    def _check_response(self, before, body, after):
        if self._broker._contains_canary((before, body, after)):
            return self._receipt("BLOCKED", "SECRET_OUTPUT_BLOCKED")
        meta_keys = {"id", "mime", "version", "modified_at", "trashed"}
        body_keys = {"id", "mime", "version", "revision", "coverage", "text"}
        if (type(before) is not dict or type(after) is not dict or type(body) is not dict
                or set(before) != meta_keys or set(after) != meta_keys or set(body) != body_keys):
            return self._receipt("UNKNOWN", "MISSING_OR_UNEXPECTED_FIELDS")
        if any(x["id"] != self.binding.resource_id or x["mime"] != MIME for x in (before, body, after)):
            return self._receipt("DENY", "RESPONSE_IDENTITY_MISMATCH")
        if type(before["trashed"]) is not bool or type(after["trashed"]) is not bool:
            return self._receipt("UNKNOWN", "MALFORMED_METADATA")
        if before["trashed"] or after["trashed"]:
            return self._receipt("DENY", "RESOURCE_TRASHED")
        # Missing reader signals stay UNKNOWN, never trigger permission changes.
        # No promise that live Google supplies this fixture's version evidence.
        if any(type(x["version"]) is not str or not x["version"] for x in (before, body, after)):
            return self._receipt("UNKNOWN", "VERSION_UNAVAILABLE")
        if type(body["revision"]) is not str or not body["revision"]:
            return self._receipt("UNKNOWN", "REVISION_UNAVAILABLE")
        if before != after or body["version"] != before["version"]:
            return self._receipt("STALE", "VERSION_CHANGED")
        if body["coverage"] != "FULL":
            return self._receipt("UNKNOWN", "COVERAGE_INCOMPLETE")
        try:
            identifier(body["version"])
            if body["revision"] is not None:
                identifier(body["revision"])
            safe_text(body["text"])
            captured = _time(before["modified_at"])
            observed = _time(self._clock())
            if captured > observed:
                raise HomeError("FUTURE_VERSION")
        except (HomeError, TypeError, ValueError):
            return self._receipt("UNKNOWN", "UNSAFE_OR_INVALID_CONTENT")
        return self._receipt("SUCCESS", "EXACT_READ_AS_OF", freshness="AS_OF",
                             observed_at=observed.isoformat(), as_of=before["modified_at"],
                             version=body["version"], revision=body["revision"],
                             coverage="FULL", text=body["text"],
                             content_digest=fingerprint(body["text"]))

    def execute(self, request):
        lease = None
        with self._lock:
            try:
                self._validate(request)
                stamp = (self.binding.digest, self.epoch)
                if "resume_handle" in request:
                    handle = request["resume_handle"]
                    cached = self._resumes.get(handle) if type(handle) is OpaqueHandle else None
                    if cached is None or cached[0] != stamp:
                        return self._receipt("DENY", "RESUME_INVALID")
                    if request.get("require_fresh", True):
                        return self._receipt("UNKNOWN", "FRESH_READ_REQUIRED")
                    return {**deepcopy(cached[1]), "status": "AS_OF", "reason": "CACHED_AS_OF"}
                lease = self._broker._lease(request["handle"], self.binding, self.epoch)
            except (HomeError, TypeError, ValueError, RecursionError) as exc:
                # Never echo arbitrary caller values or exception messages.
                codes = {"INVALID_REQUEST", "SECRET_INPUT_BLOCKED", "WRITE_DENIED", "ACTION_NOT_ALLOWED",
                         "BINDING_MISMATCH", "EPOCH_MISMATCH", "GUARD_CLOSED", "REVOKED",
                         "NOT_YET_VALID", "EXPIRED", "HANDLE_DENIED", "SECRETSTORE_UNAVAILABLE",
                         "SECRETSTORE_LOCKED"}
                reason = str(exc) if type(exc) is HomeError and str(exc) in codes else "INVALID_REQUEST"
                status = "BLOCKED" if reason.startswith("SECRET") or reason == "GUARD_CLOSED" else "DENY"
                return self._receipt(status, reason)
        try:
            before = self._exchange("metadata", stamp, lease)
            body = self._exchange("body", stamp, lease)
            after = self._exchange("metadata", stamp, lease)
            outcome = (before, body, after)
            error = None
        except Exception as exc:
            allowed = {"ACCESS_DENIED_403", "NOT_FOUND_OR_UNREADABLE_404", "TIMEOUT_UNKNOWN"}
            error = str(exc) if type(exc) is HomeError and str(exc) in allowed else "PROVIDER_UNKNOWN"
            outcome = None
        finally:
            self._broker._release(lease)
        with self._lock:
            try:
                self._live(stamp)
            except (HomeError, TypeError, ValueError):
                return self._receipt("DISCARDED", "GRANT_INVALIDATED_IN_FLIGHT")
            if error is not None:
                status = "DENY" if error == "ACCESS_DENIED_403" else "UNKNOWN"
                return self._receipt(status, error)
            try:
                result = self._check_response(*outcome)
            except Exception:
                result = self._receipt("UNKNOWN", "RESPONSE_INVALID")
            if result["status"] == "SUCCESS":
                if len(self._resumes) >= 128:
                    self._resumes.clear()
                handle = OpaqueHandle()
                self._resumes[handle] = (stamp, deepcopy(result))
                result["resume_handle"] = handle
            return result

    def _save_lifecycle(self):
        try:
            self._state.save()
        except Exception:
            self._blocked = True
            self._broker._invalidate()
            self._resumes.clear()
            raise HomeError("LIFECYCLE_PERSISTENCE_FAILED") from None

    def revoke(self, *, remote_result="UNKNOWN", after_durable=None):
        """Trusted local control. remote_result is a fixture label, never a call."""
        with self._lock:
            if self._closed or self._blocked:
                raise HomeError("GUARD_CLOSED")
            self._state.data.update(epoch=self.epoch + 1, revoked=True)
            self._save_lifecycle()
            self._broker._invalidate()
            self._resumes.clear()
            if after_durable is not None:
                after_durable()
            # Do not inspect/echo provider response text or reopen on failure.
            return self._receipt("REVOKED", "LOCAL_REVOKE_DURABLE", remote_state="UNKNOWN")

    def install_generation(self, binding, *, after_durable=None):
        """Separate trusted synthetic regrant; ordinary execute cannot invoke it."""
        with self._lock:
            if type(binding) is not Binding or self._closed or self._blocked:
                raise HomeError("REGRANT_DENIED")
            stable = {f.name for f in fields(Binding)} - {"grant_generation", "not_before", "expires_at"}
            if (any(getattr(binding, k) != getattr(self.binding, k) for k in stable)
                    or binding.grant_generation <= self.binding.grant_generation):
                raise HomeError("REGRANT_DENIED")
            self._state.data.update(binding_digest=binding.digest, generation=binding.grant_generation,
                                    epoch=self.epoch + 1, revoked=False)
            self._save_lifecycle()
            self._binding = binding
            self._broker._invalidate()
            self._resumes.clear()
            if after_durable is not None:
                after_durable()
            self.handle = self._broker._issue(binding)

    def close(self):
        with self._lock:
            self._closed = True
            self._broker._invalidate()
            self._resumes.clear()
            self._state.close()
