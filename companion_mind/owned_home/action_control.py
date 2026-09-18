"""Bounded mutable execution control; not a journal or fact Authority.

Atomic snapshots and an exclusive process lock prevent a second dispatch.
The separate synthetic adapter owns its simulated resources. Only A019 owns
canonical user/terminal evidence. No internal storage is a TestPort oracle.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .contracts import HomeError, encode, fingerprint

STATES = ("PERMISSION_ALLOWED", "DISPATCH_INTENT_DURABLE", "EXECUTING_OR_UNKNOWN",
          "RECEIPT_OBSERVED", "READBACK_VERIFIED", "TERMINAL")
TOOL_FAULTS = {"TOOL_AFTER_USER_DURABLE", "TOOL_AFTER_PERMISSION", "TOOL_AFTER_DISPATCH_INTENT",
               "TOOL_AFTER_EFFECT", "TOOL_AFTER_RECEIPT", "TOOL_AFTER_READBACK",
               "TOOL_AFTER_TERMINAL_CONTROL", "TOOL_BEFORE_DISPLAY"}


class AtomicState:
    """One fsync/replace snapshot, with no append-only event history."""

    def __init__(self, directory, kind, initial):
        self.directory, self.kind, self.lock = Path(directory), kind, None
        created = not self.directory.exists()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise HomeError("UNSAFE_CONTROL_PATH")
        self.path = self.directory / "state.json"
        try:
            self.lock = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if os.fstat(self.lock).st_nlink != 1:
                raise HomeError("UNSAFE_CONTROL_PATH")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HomeError("ACTION_CONTROL_BUSY") from None
            if created:
                self._sync(self.directory.parent)
            if self.path.exists() or self.path.is_symlink():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    if os.fstat(stream.fileno()).st_nlink != 1:
                        raise HomeError("UNSAFE_CONTROL_PATH")
                    raw = stream.read(4_194_305)
                    if len(raw) > 4_194_304:
                        raise HomeError("CONTROL_LIMIT")
                    self.data = json.loads(raw)
                if not isinstance(self.data, dict) or self.data.get("format") != kind:
                    raise HomeError("CONTROL_FORMAT_MISMATCH")
            else:
                self.data = {"format": kind, **initial}
                self.save()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _sync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save(self):
        payload = encode(self.data).encode("utf-8")
        if len(payload) > 4_194_304:
            raise HomeError("CONTROL_LIMIT")
        fd, name = tempfile.mkstemp(prefix="snapshot-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self._sync(self.directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


class ToolActionControl:
    def __init__(self, directory):
        self.state = AtomicState(directory, "tool-action-control/1", {"actions": {}})
        if not isinstance(self.state.data.get("actions"), dict):
            self.close()
            raise HomeError("CONTROL_FORMAT_MISMATCH")

    def close(self):
        self.state.close()

    def lookup(self, action_id, scope):
        matches = [r for r in self.state.data["actions"].values() if r["intent"]["action_id"] == action_id]
        if not matches:
            return None
        record = matches[0]
        if record["intent"]["scope"] != scope:
            raise HomeError("SCOPE_DENIED")
        return record

    def check(self, intent):
        records = self.state.data["actions"]
        key = fingerprint(intent["idempotency_key"])
        prior = records.get(key)
        if prior and prior["intent"]["intent_fingerprint"] != intent["intent_fingerprint"]:
            raise HomeError("IDEMPOTENCY_CONFLICT")
        for r in records.values():
            if (r["intent"]["action_id"] == intent["action_id"] or
                    (r["intent"]["scope"] == intent["scope"] and
                     r["intent"]["identity"]["request_id"] == intent["identity"]["request_id"])) and r is not prior:
                raise HomeError("ACTION_IDENTITY_CONFLICT")
        return prior

    def bind(self, intent, decision):
        prior = self.check(intent)
        if prior:
            return prior
        if len(self.state.data["actions"]) >= 1024:
            raise HomeError("ACTION_CONTROL_LIMIT")
        allowed = decision["execution_allowed"]
        state = "PERMISSION_ALLOWED" if allowed else "TERMINAL"
        record = {"intent": intent, "permission": decision, "state": state,
                  "milestones": {s: s == state for s in STATES}, "receipt": None,
                  "terminal_reason": None if allowed else decision["reason_code"]}
        self.state.data["actions"][fingerprint(intent["idempotency_key"])] = record
        self.state.save()
        return record

    def advance(self, record, state, **updates):
        current = record["state"]
        if current == "TERMINAL" or state not in STATES or STATES.index(state) <= STATES.index(current):
            raise HomeError("INVALID_ACTION_TRANSITION")
        if state == "DISPATCH_INTENT_DURABLE" and not record["permission"]["execution_allowed"]:
            raise HomeError("DISPATCH_WITHOUT_PERMISSION")
        record.update(updates, state=state)
        record["milestones"][state] = True
        self.state.save()

    def reauthorize(self, record, decision):
        if record["state"] != "PERMISSION_ALLOWED":
            raise HomeError("REDISPATCH_FORBIDDEN")
        record["permission"] = decision
        self.state.save()

    @staticmethod
    def projection(record):
        return {"state": record["state"], "milestones": dict(record["milestones"]),
                "authority": False, "canonical_evidence_owner": "A019",
                "mutable_execution_control_only": True, "automatic_redispatches": 0}
