"""Single-writer POSIX SQLite WAL/FULL evidence store; offline provider seam."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import uuid

from companion_mind.contracts.canonical_event_v1 import canonical_order_key
from .codec import (CONTRACT_COMMIT, CONTRACT_VERSION, SCHEMA_SHA256, JournalError,
                    REDACTED, encode, fingerprint, load_schema, prepare, safe_label, scrub)

STORE_VERSION = "a019-store/1"
FAULTS = {"F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "APPEND_BEFORE_COMMIT"}
_DDL = """
CREATE TABLE store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE canonical_event_log(
 journal_offset INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, event_json TEXT NOT NULL,
 session_id TEXT NOT NULL, sequence_no INTEGER NOT NULL,
 correction_id TEXT UNIQUE, correction_of TEXT REFERENCES canonical_event_log(event_id),
 commit_generation INTEGER NOT NULL, UNIQUE(session_id, sequence_no));
CREATE TRIGGER canonical_no_update BEFORE UPDATE ON canonical_event_log
 BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER canonical_no_delete BEFORE DELETE ON canonical_event_log
 BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TABLE turn_attempt_control(
 attempt_id TEXT PRIMARY KEY, user_event_id TEXT NOT NULL REFERENCES canonical_event_log(event_id),
 signature TEXT NOT NULL, template TEXT NOT NULL, script_hash TEXT NOT NULL,
 terminal_event_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, sequence_no INTEGER NOT NULL,
 phase TEXT NOT NULL, external_outcome TEXT NOT NULL, pending_kind TEXT,
 UNIQUE(session_id, sequence_no));
CREATE TABLE assistant_spool(
 attempt_id TEXT NOT NULL REFERENCES turn_attempt_control(attempt_id), frame_no INTEGER NOT NULL,
 fragment TEXT NOT NULL, redacted INTEGER NOT NULL, PRIMARY KEY(attempt_id, frame_no));
CREATE TABLE replica_outbox(
 event_id TEXT PRIMARY KEY REFERENCES canonical_event_log(event_id),
 target TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 remote_id TEXT UNIQUE, readback_fingerprint TEXT, last_error TEXT);
"""


@dataclass(frozen=True)
class StubScript:
    frames: tuple[str, ...] = ("synthetic response",)
    outcome: str = "complete"

    def sanitized(self):
        if self.outcome not in {"complete", "partial", "failed"} or not all(isinstance(x, str) for x in self.frames):
            raise JournalError("INVALID_STUB_SCRIPT")
        whole = "".join(self.frames)
        if len(whole.encode("utf-8")) > 524288:
            raise JournalError("SCRIPT_TOO_LARGE")
        clean = scrub(whole)
        # Offline scripts are known in full before any staging. This also catches
        # credentials split across frames. This is not a live streaming adapter.
        return StubScript((clean,) if clean != whole else tuple(self.frames), self.outcome)

    def digest(self):
        return fingerprint({"frames": list(self.frames), "outcome": self.outcome})


class Journal:
    """Append/export/recovery only. Construction completes local recovery first.

    `fault` is an explicit test-only hard process exit (86), never an exception
    that gracefully closes SQLite. Call only in a subprocess for fault tests.
    """

    def __init__(self, directory, *, replica_target="offline-drive", fault=None):
        self._schema = load_schema()
        self._target = safe_label(replica_target)
        if fault is not None and fault not in FAULTS:
            raise JournalError("UNKNOWN_FAULT")
        self._fault = fault
        self.trace = []
        self._ready = False
        self._healthy = True
        self.__db = None
        self.__lock = None
        self._pid = os.getpid()
        self._dir = Path(directory).absolute()
        try:
            if not self._dir.exists():
                self._dir.mkdir(mode=0o700)  # caller owns the existing parent
                self._fsync_directory(self._dir.parent)
            if self._dir.is_symlink() or not self._dir.is_dir():
                raise JournalError("UNSAFE_STORE_PATH")
            lock_path = self._dir / "writer.lock"
            self.__lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.__lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalError("WRITER_BUSY") from None
            db_path = self._dir / "journal.sqlite3"
            descriptor = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            self.__db = sqlite3.connect(db_path, isolation_level=None, timeout=0)
            self.__db.row_factory = sqlite3.Row
            self.__db.execute("PRAGMA foreign_keys=ON")
            if self.__db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise JournalError("DURABILITY_UNAVAILABLE")
            self.__db.execute("PRAGMA synchronous=FULL")
            if self.__db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise JournalError("DURABILITY_UNAVAILABLE")
            existing = self.__db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not existing:
                try:
                    self.__db.executescript("BEGIN IMMEDIATE;\n" + _DDL)
                    meta = {"schema_version": STORE_VERSION, "contract_version": CONTRACT_VERSION,
                            "contract_commit": CONTRACT_COMMIT, "schema_sha256": SCHEMA_SHA256,
                            "generation": uuid.uuid4().hex, "commit_generation": "0",
                            "recovery_generation": "0", "migration_version": "0",
                            "clean_shutdown": "0", "replica_target": self._target}
                    self.__db.executemany("INSERT INTO store_meta VALUES (?,?)", meta.items())
                    self.__db.execute("COMMIT")
                except BaseException:
                    if self.__db.in_transaction:
                        self.__db.execute("ROLLBACK")
                    raise
            self._fsync_directory(self._dir)
            self._verify_store()
            self.recovery_receipt = self._recover()
            self._ready = True
        except BaseException:
            self.close(clean=False)
            raise

    @staticmethod
    def _fsync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _check(self):
        if not self._healthy or self.__db is None or os.getpid() != self._pid:
            raise JournalError("REOPEN_REQUIRED")

    def _hit(self, point):
        if self._fault == point:
            os._exit(86)

    def _meta(self):
        return dict(self.__db.execute("SELECT key,value FROM store_meta").fetchall())

    @contextmanager
    def _transaction(self):
        self._check()
        try:
            self.__db.execute("BEGIN IMMEDIATE")
            generation = int(self._meta()["commit_generation"]) + 1
            self.__db.execute("UPDATE store_meta SET value=? WHERE key='commit_generation'", (str(generation),))
            yield generation
            self.__db.execute("COMMIT")  # WAL + FULL synchronizes WAL before return
            self._fsync_directory(self._dir)
        except BaseException as error:
            if self.__db.in_transaction:
                self.__db.execute("ROLLBACK")
            if isinstance(error, (sqlite3.Error, OSError)):
                self._healthy = False
                raise JournalError("STORE_IO_REOPEN_REQUIRED") from None
            raise

    def _verify_store(self):
        if self.__db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or self.__db.execute("PRAGMA foreign_key_check").fetchall():
            raise JournalError("STORE_INTEGRITY_FAILED")
        meta = self._meta()
        expected = {"schema_version": STORE_VERSION, "contract_version": CONTRACT_VERSION,
                    "contract_commit": CONTRACT_COMMIT, "schema_sha256": SCHEMA_SHA256,
                    "migration_version": "0", "replica_target": self._target}
        if any(meta.get(k) != v for k, v in expected.items()):
            raise JournalError("STORE_CONTRACT_MISMATCH")
        for row in self.__db.execute("SELECT * FROM canonical_event_log ORDER BY journal_offset"):
            event = json.loads(row["event_json"])
            if (prepare(event, self._schema) != event or fingerprint(event) != row["fingerprint"] or
                (event["event_id"], event["session_id"], event["sequence_no"], event["correction_id"], event["correction_of"]) !=
                (row["event_id"], row["session_id"], row["sequence_no"], row["correction_id"], row["correction_of"])):
                raise JournalError("STORE_EVIDENCE_MISMATCH")
        if self.__db.execute("SELECT count(*) FROM canonical_event_log").fetchone()[0] != self.__db.execute("SELECT count(*) FROM replica_outbox").fetchone()[0]:
            raise JournalError("STORE_OUTBOX_MISMATCH")

    def _receipt(self, row, disposition):
        return {"disposition": disposition, "event_id": row["event_id"],
                "journal_offset": row["journal_offset"], "fingerprint": row["fingerprint"],
                "contract_version": CONTRACT_VERSION, "contract_commit": CONTRACT_COMMIT,
                "store_generation": self._meta()["generation"],
                "commit_generation": row["commit_generation"], "durability": "SQLITE_WAL_FULL"}

    def _append_tx(self, event, generation, *, attempt_id=None):
        event_id, digest = event["event_id"], fingerprint(event)
        old = self.__db.execute("SELECT * FROM canonical_event_log WHERE event_id=?", (event_id,)).fetchone()
        if old:
            if old["fingerprint"] != digest:
                raise JournalError("IDENTITY_CONFLICT")
            return self._receipt(old, "ALREADY_COMMITTED")
        reserved = self.__db.execute(
            "SELECT attempt_id FROM turn_attempt_control WHERE terminal_event_id=? OR (session_id=? AND sequence_no=?)",
            (event_id, event["session_id"], event["sequence_no"])).fetchall()
        if any(r[0] != attempt_id for r in reserved):
            raise JournalError("ATTEMPT_SLOT_RESERVED")
        if self.__db.execute("SELECT 1 FROM canonical_event_log WHERE session_id=? AND sequence_no=?", (event["session_id"], event["sequence_no"])).fetchone():
            raise JournalError("SEQUENCE_CONFLICT")
        if event["correction_of"] is not None:
            if not self.__db.execute("SELECT 1 FROM canonical_event_log WHERE event_id=?", (event["correction_of"],)).fetchone():
                raise JournalError("CORRECTION_TARGET_MISSING")
            if self.__db.execute("SELECT 1 FROM canonical_event_log WHERE correction_id=?", (event["correction_id"],)).fetchone():
                raise JournalError("CORRECTION_ID_CONFLICT")
        self.__db.execute(
            "INSERT INTO canonical_event_log(event_id,fingerprint,event_json,session_id,sequence_no,correction_id,correction_of,commit_generation) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, digest, encode(event), event["session_id"], event["sequence_no"], event["correction_id"], event["correction_of"], generation))
        self.__db.execute("INSERT INTO replica_outbox(event_id,target,state) VALUES (?,?,'PENDING')", (event_id, self._target))
        self._hit("APPEND_BEFORE_COMMIT")
        row = self.__db.execute("SELECT * FROM canonical_event_log WHERE event_id=?", (event_id,)).fetchone()
        return self._receipt(row, "COMMITTED")

    def append(self, event):
        self._check()
        if not self._ready:
            raise JournalError("RECOVERY_REQUIRED")
        clean = prepare(event, self._schema)
        with self._transaction() as generation:
            receipt = self._append_tx(clean, generation)
        self._hit("F6")
        return receipt

    def ingest(self, adapter, event):
        expected = {"A019": ("owned_client", "observed"), "A018": ("browser_sidecar", "observed"),
                    "A020": ("historical_backfill", "imported")}
        clean = prepare(event, self._schema)
        source = clean["source_ref"]
        if (adapter not in expected or (source["source_kind"], source["observation_type"]) != expected[adapter]
                or clean["metadata"].get("adapter") != adapter):
            raise JournalError("ADAPTER_CONTRACT_MISMATCH")
        return self.append(clean)

    def correct(self, event):
        clean = prepare(event, self._schema)
        if clean["correction_of"] is None:
            raise JournalError("CORRECTION_TARGET_REQUIRED")
        return self.append(clean)

    def export(self, *, order="canonical"):
        self._check()
        if order not in {"canonical", "journal"}:
            raise JournalError("ORDER_REQUIRED")
        events = [json.loads(r[0]) for r in self.__db.execute("SELECT event_json FROM canonical_event_log ORDER BY journal_offset")]
        return sorted(events, key=canonical_order_key) if order == "canonical" else events

    def attempts(self):
        self._check()
        return [dict(r) for r in self.__db.execute("SELECT attempt_id,user_event_id,terminal_event_id,phase,external_outcome FROM turn_attempt_control ORDER BY attempt_id")]

    def _recover(self):
        with self._transaction():
            previous = self._meta()["clean_shutdown"]
            count = int(self._meta()["recovery_generation"]) + 1
            self.__db.execute("UPDATE store_meta SET value=? WHERE key='recovery_generation'", (str(count),))
            self.__db.execute("UPDATE store_meta SET value='0' WHERE key='clean_shutdown'")
        recovered = []
        rows = self.__db.execute("SELECT * FROM turn_attempt_control WHERE phase!='TERMINAL_COMMITTED' ORDER BY rowid").fetchall()
        for row in rows:
            if row["phase"] == "USER_DURABLE":
                recovered.append({"attempt_id": row["attempt_id"], "action": "AWAIT_EXPLICIT_RESUME", "external_outcome": "NOT_SENT"})
                continue
            if row["phase"] not in {"PROVIDER_INTENT", "STREAMING", "TERMINAL_PENDING"}:
                raise JournalError("UNKNOWN_RECOVERY_PHASE")
            kind = row["pending_kind"] if row["phase"] == "TERMINAL_PENDING" else None
            receipt = self._terminalize(row["attempt_id"], kind)
            recovered.append({"attempt_id": row["attempt_id"], "action": "TERMINALIZED", "receipt": receipt})
        return {"previous_clean_shutdown": previous == "1", "recovery_generation": count,
                "actions": recovered, "attempts": self.attempts(),
                "pending_replica": self.__db.execute("SELECT count(*) FROM replica_outbox WHERE state!='READBACK_VERIFIED'").fetchone()[0],
                "provider_invocations": 0, "local_recovery_complete": True}

    def _terminalize(self, attempt_id, kind):
        with self._transaction() as generation:
            row = self.__db.execute("SELECT * FROM turn_attempt_control WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row["phase"] == "TERMINAL_COMMITTED":
                old = self.__db.execute("SELECT * FROM canonical_event_log WHERE event_id=?", (row["terminal_event_id"],)).fetchone()
                return self._receipt(old, "ALREADY_COMMITTED")
            spool = self.__db.execute("SELECT * FROM assistant_spool WHERE attempt_id=? ORDER BY frame_no", (attempt_id,)).fetchall()
            text = "".join(r["fragment"] for r in spool)
            status = kind if kind is not None else ("partial" if text else "failed")
            if not text:
                status = "failed"
            event = json.loads(row["template"])
            event["status"] = status
            event["content_payload"] = {"text": text}
            ext = event["metadata"].setdefault("extensions", {})
            ext["a019_attempt"] = {"attempt_id": attempt_id, "external_outcome": row["external_outcome"],
                                   "terminal_evidence": "OBSERVED" if kind is not None else "RECOVERY_INTERRUPTED"}
            if status == "failed":
                ext["a019_attempt"]["error_class"] = "STUB_FAILED" if kind == "failed" else "NO_VISIBLE_OUTPUT"
            if any(r["redacted"] for r in spool):
                event["redaction_state"] = "redacted"
            event = prepare(event, self._schema)
            receipt = self._append_tx(event, generation, attempt_id=attempt_id)
            self.__db.execute("UPDATE turn_attempt_control SET phase='TERMINAL_COMMITTED' WHERE attempt_id=?", (attempt_id,))
        self.trace.append("ASSISTANT_DURABLE")
        self._hit("F5")
        return receipt

    def append_user_then_invoke_stub(self, user, assistant_template, *, attempt_id, script=StubScript()):
        self._check()
        self.trace.clear()
        if not self._ready:
            raise JournalError("RECOVERY_REQUIRED")
        attempt_id = safe_label(attempt_id)
        script = script.sanitized()
        user, template = prepare(user, self._schema), prepare(assistant_template, self._schema)
        if (user["actor_role"] != "user" or user["status"] != "complete" or template["actor_role"] != "assistant"
                or any(template[k] != user[k] for k in ("session_id", "turn_id", "persona_id", "relationship_id"))
                or template["sequence_no"] <= user["sequence_no"] or template["event_id"] == user["event_id"]
                or user["source_ref"]["source_kind"] != "owned_client" or template["source_ref"]["source_kind"] != "owned_client"
                or template["content_payload"] != {"text": ""} or template["correction_of"] is not None
                or "a019_attempt" in template["metadata"].get("extensions", {})):
            raise JournalError("ATTEMPT_CONTRACT_MISMATCH")
        signature = fingerprint({"user": user, "template": template, "script": script.digest()})
        row = self.__db.execute("SELECT * FROM turn_attempt_control WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row:
            if row["signature"] != signature:
                raise JournalError("ATTEMPT_IDENTITY_CONFLICT")
            if row["phase"] == "TERMINAL_COMMITTED":
                return {"assistant": self._terminalize(attempt_id, None), "provider_invocations": 0, "trace": []}
            if row["phase"] != "USER_DURABLE":
                raise JournalError("NEEDS_DECISION")
            user_row = self.__db.execute("SELECT * FROM canonical_event_log WHERE event_id=?", (user["event_id"],)).fetchone()
            user_receipt = self._receipt(user_row, "ALREADY_COMMITTED")
        else:
            with self._transaction() as generation:
                user_receipt = self._append_tx(user, generation)
                if self.__db.execute("SELECT 1 FROM canonical_event_log WHERE event_id=? OR (session_id=? AND sequence_no=?)",
                                     (template["event_id"], template["session_id"], template["sequence_no"])).fetchone():
                    raise JournalError("TERMINAL_SLOT_CONFLICT")
                if self.__db.execute("SELECT 1 FROM turn_attempt_control WHERE terminal_event_id=? OR (session_id=? AND sequence_no=?)",
                                     (template["event_id"], template["session_id"], template["sequence_no"])).fetchone():
                    raise JournalError("TERMINAL_SLOT_CONFLICT")
                self.__db.execute("INSERT INTO turn_attempt_control VALUES (?,?,?,?,?,?,?,?,'USER_DURABLE','NOT_SENT',NULL)",
                                  (attempt_id, user["event_id"], signature, encode(template), script.digest(), template["event_id"],
                                   template["session_id"], template["sequence_no"],))
        self.trace.append("USER_DURABLE_RECEIPT")
        self._hit("F1")
        with self._transaction():
            self.__db.execute("UPDATE turn_attempt_control SET phase='PROVIDER_INTENT',external_outcome='UNKNOWN' WHERE attempt_id=?", (attempt_id,))
        self.trace.append("PROVIDER_INTENT_DURABLE")
        self._hit("F2")
        self.trace.append("PROVIDER_STUB_INVOKED")
        for number, fragment in enumerate(script.frames):
            with self._transaction():
                self.__db.execute("INSERT INTO assistant_spool VALUES (?,?,?,?)", (attempt_id, number, fragment, int(REDACTED in fragment)))
                self.__db.execute("UPDATE turn_attempt_control SET phase='STREAMING' WHERE attempt_id=?", (attempt_id,))
            self.trace.append("SANITIZED_FRAME_DURABLE")
            self._hit("F3")
        with self._transaction():
            self.__db.execute("UPDATE turn_attempt_control SET phase='TERMINAL_PENDING',pending_kind=?,external_outcome=? WHERE attempt_id=?",
                              (script.outcome, "SUCCESS" if script.outcome == "complete" else "FAILED" if script.outcome == "failed" else "UNKNOWN", attempt_id))
        self._hit("F4")
        receipt = self._terminalize(attempt_id, script.outcome)
        self.trace.append("CLIENT_COMPLETION_ACK")
        return {"user": user_receipt, "assistant": receipt, "provider_invocations": 1, "trace": list(self.trace)}

    def replica_state(self):
        self._check()
        return [dict(r) for r in self.__db.execute("SELECT * FROM replica_outbox ORDER BY rowid")]

    def drain_replica(self, transport):
        """One bounded attempt per event; completion includes fresh readback.

        A remote file ID is reserved and committed BEFORE any create. A retry
        reads/creates that exact ID, never allocates a second file for an event.
        Authentication belongs to the injected transport, never to this store.
        """
        self._check()
        if not self._ready or transport.target != self._target:
            raise JournalError("REPLICA_TARGET_MISMATCH")
        evidence_kind = safe_label(transport.evidence_kind)
        from .replica import ReplicaConflict
        high_water = self.__db.execute("SELECT coalesce(max(journal_offset),0) FROM canonical_event_log").fetchone()[0]
        rows = self.__db.execute("SELECT e.*,o.remote_id FROM canonical_event_log e JOIN replica_outbox o USING(event_id) WHERE journal_offset<=? ORDER BY journal_offset", (high_water,)).fetchall()
        verified = []
        for row in rows:
            remote_id = row["remote_id"]
            try:
                if remote_id is None:
                    remote_id = safe_label(transport.reserve_id())
                    with self._transaction():
                        self.__db.execute("UPDATE replica_outbox SET remote_id=? WHERE event_id=?", (remote_id, row["event_id"]))
                with self._transaction():
                    self.__db.execute("UPDATE replica_outbox SET attempts=attempts+1,state='PENDING',last_error=NULL WHERE event_id=?", (row["event_id"],))
                remote = transport.read(remote_id)  # None is explicit not-found only
                if remote is None:
                    transport.create(remote_id, row["event_json"])
                elif remote != row["event_json"]:
                    raise ReplicaConflict()
                self._hit("F7")
                with self._transaction():
                    self.__db.execute("UPDATE replica_outbox SET state='WRITTEN' WHERE event_id=?", (row["event_id"],))
                remote = transport.read(remote_id)
                if remote != row["event_json"]:
                    raise ReplicaConflict()
                self._hit("F8")
                with self._transaction():
                    self.__db.execute("UPDATE replica_outbox SET state='READBACK_VERIFIED',readback_fingerprint=?,last_error=NULL WHERE event_id=?", (row["fingerprint"], row["event_id"]))
                verified.append({"event_id": row["event_id"], "remote_id": remote_id, "fingerprint": row["fingerprint"]})
            except Exception as error:
                if not self._healthy:
                    raise
                # Never persist str(error), request headers, bodies or credentials.
                blocked = isinstance(error, (ReplicaConflict, JournalError))
                code = "REMOTE_IDENTITY_CONFLICT" if blocked else "REMOTE_IO_UNKNOWN"
                with self._transaction():
                    self.__db.execute("UPDATE replica_outbox SET state=?,last_error=?,readback_fingerprint=NULL WHERE event_id=?",
                                      ("PERMANENT_BLOCKED" if blocked else "RETRYABLE_FAILED", code, row["event_id"]))
        return {"completion": "READBACK_VERIFIED" if len(verified) == len(rows) else "INCOMPLETE",
                "target": self._target, "transport_evidence": evidence_kind,
                "journal_high_water": high_water, "expected_events": len(rows), "verified_events": len(verified),
                "receipts": verified,
                "ordered_fingerprint": fingerprint([json.loads(r["event_json"]) for r in rows]) if len(verified) == len(rows) else None}

    def close(self, *, clean=True):
        if self.__db is not None:
            try:
                if clean and self._healthy and self._ready and os.getpid() == self._pid:
                    with self._transaction():
                        self.__db.execute("UPDATE store_meta SET value='1' WHERE key='clean_shutdown'")
            finally:
                self.__db.close()
                self.__db = None
        if self.__lock is not None:
            os.close(self.__lock)
            self.__lock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
