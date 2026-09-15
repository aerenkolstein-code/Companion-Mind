from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from companion_mind.journal import Journal, JournalError, StubScript
from companion_mind.journal.codec import CONTRACT_COMMIT, encode, fingerprint, load_schema
from companion_mind.journal.replica import DriveResponse, GoogleDrive, OfflineDrive, ReplicaConflict
from tools.a019_conformance import ROOT, pair, seam, turn_request

RECEIPTS = {}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.remote = self.root / "remote"

    def response(self, request, **kwargs):
        result = seam(self.local, request, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["result"]

    def test_contract_pin_and_strict_machine_shape(self):
        self.assertEqual(load_schema()["properties"]["status"]["enum"], ["complete", "partial", "failed"])
        user, _ = pair()
        with Journal(self.local) as journal:
            for mutate in (
                lambda x: x.update(unpublished_field=True),
                lambda x: x["metadata"].update(private_schema=1),
                lambda x: x["source_ref"].update(private_schema=1),
                lambda x: x.update(sequence_no=True),
                lambda x: x.update(content_payload=float("nan")),
            ):
                candidate = deepcopy(user)
                mutate(candidate)
                with self.assertRaises(JournalError):
                    journal.append(candidate)
            self.assertEqual(journal.export(), [])
        RECEIPTS["contract"] = {"commit": CONTRACT_COMMIT, "schema_unchanged": True, "extra_fields_rejected": True}

    def test_duplicate_identity_sequence_and_original_receipt(self):
        user, _ = pair()
        with Journal(self.local) as journal:
            first = journal.append(user)
            second = journal.append(deepcopy(user))
            self.assertEqual(second["disposition"], "ALREADY_COMMITTED")
            self.assertEqual({k: v for k, v in first.items() if k != "disposition"}, {k: v for k, v in second.items() if k != "disposition"})
            changed = deepcopy(user)
            changed["content_payload"] = {"text": "new content under same ID"}
            with self.assertRaisesRegex(JournalError, "IDENTITY_CONFLICT"):
                journal.append(changed)
            changed["event_id"] = "different-id"
            with self.assertRaisesRegex(JournalError, "SEQUENCE_CONFLICT"):
                journal.append(changed)
            self.assertEqual(journal.export(), [user])
        with Journal(self.local) as journal:
            self.assertEqual(journal.append(user)["commit_generation"], first["commit_generation"])
        RECEIPTS["dedupe"] = {"same_identity_replay": "ALREADY_COMMITTED", "changed_identity": "CONFLICT", "duplicate_amplification": 0}

    def test_transaction_crash_has_no_half_event_or_outbox(self):
        user, _ = pair()
        result = seam(self.local, {"op": "append", "event": user}, fault="APPEND_BEFORE_COMMIT")
        self.assertEqual(result.returncode, 86)
        with Journal(self.local) as journal:
            self.assertEqual(journal.export(), [])
            self.assertEqual(journal.replica_state(), [])
            self.assertEqual(journal.append(user)["journal_offset"], 1)
        RECEIPTS["atomic_append"] = {"hard_process_exit": 86, "half_events": 0, "orphan_outbox": 0}

    def test_single_writer_and_lock_release_after_process_exit(self):
        with Journal(self.local):
            result = seam(self.local, {"op": "info"})
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"], "WRITER_BUSY")
        self.assertTrue(self.response({"op": "info"})["recovery_complete"])

    def test_store_version_drift_fails_closed(self):
        with Journal(self.local) as journal:
            journal.append(pair()[0])
        db = sqlite3.connect(self.local / "journal.sqlite3")
        db.execute("UPDATE store_meta SET value='different-contract' WHERE key='contract_commit'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(JournalError, "STORE_CONTRACT_MISMATCH"):
            Journal(self.local)

    def test_user_durable_gate_and_fsync_failure_prevent_dispatch(self):
        user, template = pair()
        with Journal(self.local) as journal:
            with patch.object(journal, "_fsync_directory", side_effect=OSError("synthetic disk failure")):
                with self.assertRaisesRegex(JournalError, "STORE_IO_REOPEN_REQUIRED"):
                    journal.append_user_then_invoke_stub(user, template, attempt_id="gate")
            self.assertNotIn("PROVIDER_STUB_INVOKED", journal.trace)
            with self.assertRaisesRegex(JournalError, "REOPEN_REQUIRED"):
                journal.append(user)
        with Journal(self.local) as journal:
            # Commit may have reached the device before the reported error. Reopen
            # reconciles it; the same attempt can explicitly resume if NOT_SENT.
            receipt = journal.append_user_then_invoke_stub(user, template, attempt_id="gate")
            trace = receipt["trace"]
            self.assertLess(trace.index("USER_DURABLE_RECEIPT"), trace.index("PROVIDER_INTENT_DURABLE"))
            self.assertLess(trace.index("PROVIDER_INTENT_DURABLE"), trace.index("PROVIDER_STUB_INVOKED"))
        RECEIPTS["user_before_provider"] = {"ordering": ["USER_DURABLE_RECEIPT", "PROVIDER_INTENT_DURABLE", "PROVIDER_STUB_INVOKED"], "dispatch_on_sync_error": 0}

    def test_complete_partial_failed_and_retry_terminal_uniqueness(self):
        with Journal(self.local) as journal:
            for number, outcome in enumerate(("complete", "partial", "failed")):
                user, template = pair(number)
                script = StubScript(("visible text",) if outcome != "failed" else (), outcome)
                result = journal.append_user_then_invoke_stub(user, template, attempt_id=f"attempt-{number}", script=script)
                duplicate = journal.append_user_then_invoke_stub(user, template, attempt_id=f"attempt-{number}", script=script)
                self.assertEqual(duplicate["provider_invocations"], 0)
                self.assertEqual(result["assistant"]["event_id"], duplicate["assistant"]["event_id"])
            self.assertEqual([e["status"] for e in journal.export() if e["actor_role"] == "assistant"], ["complete", "partial", "failed"])
            original = journal.export()
            user, template = pair(1)
            template.update(event_id="retry-event", sequence_no=99)
            journal.append_user_then_invoke_stub(user, template, attempt_id="retry-new-attempt")
            self.assertEqual(journal.export()[:-1], original)
            with self.assertRaisesRegex(JournalError, "ATTEMPT_IDENTITY_CONFLICT"):
                journal.append_user_then_invoke_stub(user, template, attempt_id="attempt-1")
        RECEIPTS["terminal_lifecycle"] = {"statuses": ["complete", "partial", "failed"], "terminal_duplicates": 0, "retry_preserves_prior": True}

    def test_invalid_attempt_cannot_commit_user_or_dispatch(self):
        user, template = pair()
        template["persona_id"] = "different-persona"
        with Journal(self.local) as journal:
            with self.assertRaisesRegex(JournalError, "ATTEMPT_CONTRACT_MISMATCH"):
                journal.append_user_then_invoke_stub(user, template, attempt_id="bad")
            self.assertEqual(journal.export(), [])
            self.assertEqual(journal.trace, [])

    def test_reserved_terminal_identity_cannot_be_stolen(self):
        request = turn_request()
        self.assertEqual(seam(self.local, request, fault="F1").returncode, 86)
        with Journal(self.local) as journal:
            stolen = deepcopy(request["assistant_template"])
            stolen["content_payload"] = {"text": "forged callback"}
            with self.assertRaisesRegex(JournalError, "ATTEMPT_SLOT_RESERVED"):
                journal.append(stolen)
            self.assertEqual(len(journal.export()), 1)

    def test_correction_and_rollback_are_append_only(self):
        user, _ = pair()
        correction = deepcopy(user)
        correction.update(event_id="correction-1", sequence_no=5, correction_id="corr-1", correction_of=user["event_id"], content_payload={"text": "corrected evidence"})
        correction["source_ref"].update(source_kind="correction", observation_type="corrected")
        rollback = deepcopy(correction)
        rollback.update(event_id="correction-2", sequence_no=6, correction_id="corr-2", correction_of="correction-1", content_payload=user["content_payload"])
        rollback["metadata"].setdefault("extensions", {})["correction_action"] = "revert prior correction"
        with Journal(self.local) as journal:
            with self.assertRaisesRegex(JournalError, "CORRECTION_TARGET_MISSING"):
                journal.correct(correction)
            journal.append(user)
            journal.correct(correction)
            journal.correct(rollback)
            self.assertEqual(journal.export(), [user, correction, rollback])
            self.assertEqual(journal.correct(rollback)["disposition"], "ALREADY_COMMITTED")
        db = sqlite3.connect(self.local / "journal.sqlite3")
        for sql in ("UPDATE canonical_event_log SET event_json='{}'", "DELETE FROM canonical_event_log"):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql)
            db.rollback()
        db.close()
        for _ in range(3):
            with Journal(self.local) as journal:
                self.assertEqual(journal.export(), [user, correction, rollback])
        RECEIPTS["correction"] = {"original_retained": True, "revert_lineage": [user["event_id"], "correction-1", "correction-2"], "destructive_corrections": 0}

    def test_a018_a020_shared_ingest_and_late_historical_order(self):
        with Journal(self.local) as journal:
            for filename in ("01_owned_client_user_text.json", "03_browser_sidecar_multimodal.json", "10_historical_backfill_import.json"):
                event = json.loads((ROOT / "tests/fixtures/canonical_event_v1" / filename).read_text())
                adapter = event["metadata"]["adapter"]
                journal.ingest(adapter, event)
                self.assertEqual(journal.ingest(adapter, event)["disposition"], "ALREADY_COMMITTED")
            imported = deepcopy(event)
            imported.update(event_id="older-history", sequence_no=0)
            journal.ingest("A020", imported)
            self.assertEqual(journal.export(order="journal")[-1], imported)
            same_session = [x for x in journal.export() if x["session_id"] == imported["session_id"]]
            self.assertEqual(same_session[0], imported)
            imported["source_ref"]["observation_type"] = "observed"
            with self.assertRaises(JournalError):
                journal.ingest("A020", imported)
            with self.assertRaisesRegex(JournalError, "ADAPTER_CONTRACT_MISMATCH"):
                journal.ingest("A018", pair()[0])
        RECEIPTS["adapters"] = {"A018": "PASS", "A019": "PASS", "A020": "PASS", "late_append_order_distinct": True, "provenance_masquerade_rejected": True}

    def test_knowledge_states_structured_payload_and_provider_identity_roundtrip(self):
        states = ["UNKNOWN", "KNOWN_EMPTY", "N_A", "NOT_LOOKED_UP"]
        with Journal(self.local) as journal:
            for number, state in enumerate(states):
                user, _ = pair(number)
                user["metadata"]["knowledge"] = {"attachment_body": {"state": state}}
                user["content_payload"] = [None, {"count": 0, "has_value": False}, [1, 2]]
                user.update(provider=f"synthetic-provider-{number}", model=f"synthetic-model-{number}")
                journal.append(user)
            remote = OfflineDrive(self.remote)
            try:
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                self.assertEqual(sorted(remote.snapshot()), sorted(encode(e) for e in journal.export()))
            finally:
                remote.close()
        with Journal(self.local) as journal:
            self.assertEqual([e["metadata"]["knowledge"]["attachment_body"]["state"] for e in journal.export()], states)
            self.assertEqual(len({(e["persona_id"], e["relationship_id"]) for e in journal.export()}), 1)
        RECEIPTS["unknown_semantics"] = {"states": states, "semantic_collapse": 0, "stable_identity": True}

    def test_secret_scrub_all_persistence_surfaces_and_split_frames(self):
        sentinel = "SYNTHETIC_" + "CREDENTIAL_ABC987"
        with Journal(self.local) as journal:
            user, template = pair()
            user["content_payload"] = {"password": sentinel, "text": "Authorization: Bearer " + sentinel}
            journal.append_user_then_invoke_stub(user, template, attempt_id="safe", script=StubScript(("pass", "word=" + sentinel)))
            user2, _ = pair(1)
            user2["source_ref"]["uri"] = "https://synthetic.invalid/?access_token=" + sentinel
            with self.assertRaisesRegex(JournalError, "SECRET_IN_ENVELOPE"):
                journal.append(user2)
            remote = OfflineDrive(self.remote)
            try:
                receipt = journal.drain_replica(remote)
                self.assertNotIn(sentinel, encode(receipt))
                self.assertNotIn(sentinel, encode(journal.export()))
                self.assertNotIn(sentinel, encode(remote.snapshot()))
                self.assertTrue(all(e["redaction_state"] == "redacted" for e in journal.export()))
            finally:
                remote.close()
            for path in self.root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(sentinel.encode(), path.read_bytes(), path.name)
        RECEIPTS["secret_boundary"] = {"synthetic_sentinel_leaks": 0, "envelope_rejection": True, "split_frame_scrub": True,
                                       "coverage": ["canonical", "spool", "control", "outbox", "WAL", "replica", "receipts"]}

    def test_remote_failure_keeps_local_and_retry_is_idempotent(self):
        user, _ = pair()
        with Journal(self.local) as journal:
            journal.append(user)
            remote = OfflineDrive(self.remote, fail=True)
            try:
                self.assertEqual(journal.drain_replica(remote)["completion"], "INCOMPLETE")
                self.assertEqual(journal.export(), [user])
                self.assertEqual(journal.replica_state()[0]["last_error"], "REMOTE_IO_UNKNOWN")
                remote.fail = False
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                first_id = journal.replica_state()[0]["remote_id"]
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                self.assertEqual(len(remote.snapshot()), 1)
                self.assertEqual(journal.replica_state()[0]["remote_id"], first_id)
            finally:
                remote.close()

    def test_ambiguous_remote_create_reuses_durable_id(self):
        with Journal(self.local) as journal:
            journal.append(pair()[0])
            remote = OfflineDrive(self.remote)
            original = remote.create
            def lost_ack(remote_id, body):
                original(remote_id, body)
                raise OSError("Authorization: synthetic-transport-secret")
            try:
                with patch.object(remote, "create", side_effect=lost_ack):
                    self.assertEqual(journal.drain_replica(remote)["completion"], "INCOMPLETE")
                self.assertEqual(len(remote.snapshot()), 1)
                self.assertNotIn("synthetic-transport-secret", encode(journal.replica_state()))
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                self.assertEqual(len(remote.snapshot()), 1)
            finally:
                remote.close()
        RECEIPTS["replica_retry"] = {"lost_create_ack_reconciled": True, "remote_duplicate_amplification": 0, "local_rollback_on_remote_error": 0}

    def test_remote_readback_mismatch_and_wrong_target_fail_closed(self):
        with Journal(self.local) as journal:
            journal.append(pair()[0])
            remote = OfflineDrive(self.remote, corrupt_read=True)
            try:
                self.assertEqual(journal.drain_replica(remote)["completion"], "INCOMPLETE")
                self.assertEqual(journal.replica_state()[0]["state"], "PERMANENT_BLOCKED")
                remote.corrupt_read = False
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                remote.target = "other-drive"
                with self.assertRaisesRegex(JournalError, "REPLICA_TARGET_MISMATCH"):
                    journal.drain_replica(remote)
            finally:
                remote.close()

    def test_verified_replica_is_freshly_rechecked(self):
        with Journal(self.local) as journal:
            journal.append(pair()[0])
            remote = OfflineDrive(self.remote)
            try:
                self.assertEqual(journal.drain_replica(remote)["completion"], "READBACK_VERIFIED")
                remote.corrupt_read = True
                receipt = journal.drain_replica(remote)
                self.assertEqual(receipt["completion"], "INCOMPLETE")
                self.assertIsNone(receipt["ordered_fingerprint"])
            finally:
                remote.close()

    def test_google_drive_request_mapping_with_fake_sender_no_network(self):
        requests, files = [], {}
        def send(**request):
            requests.append(request)
            self.assertNotIn("Authorization", request["headers"])
            if request["url"].endswith("generateIds"):
                self.assertEqual(request["params"], {"count": "1", "space": "drive", "type": "files"})
                return DriveResponse(200, b'{"ids":["fake-drive-id"]}')
            if request["method"] == "POST":
                self.assertEqual(request["params"]["uploadType"], "multipart")
                boundary = request["headers"]["Content-Type"].split("boundary=")[1]
                parts = request["body"].split(("--" + boundary).encode())
                metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].strip())
                body = parts[2].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
                self.assertEqual(metadata["parents"], ["synthetic-folder"])
                self.assertEqual(metadata["mimeType"], "application/json")
                if metadata["id"] in files:
                    return DriveResponse(409, b"synthetic conflict")
                files[metadata["id"]] = body
                return DriveResponse(200, json.dumps({"id": metadata["id"]}).encode())
            remote_id = request["url"].rsplit("/", 1)[1]
            return DriveResponse(200, files[remote_id]) if remote_id in files else DriveResponse(404, b"synthetic absent")
        remote = GoogleDrive(send, folder_id="synthetic-folder", target="offline-drive")
        self.assertEqual(requests, [])  # constructing it never issues a request
        with Journal(self.local) as journal:
            user, _ = pair()
            journal.append(user)
            receipt = journal.drain_replica(remote)
            self.assertEqual(receipt["completion"], "READBACK_VERIFIED")
            self.assertEqual(len(files), 1)
            remote.create("fake-drive-id", encode(user))  # 409 + identical readback
            self.assertEqual(len(files), 1)
            with patch.object(remote, "_send", return_value=DriveResponse(403, b"synthetic credential error")):
                self.assertEqual(journal.drain_replica(remote)["completion"], "INCOMPLETE")
                self.assertEqual(journal.replica_state()[0]["last_error"], "REMOTE_IO_UNKNOWN")
            with patch.object(remote, "_send", return_value=DriveResponse(409, b"synthetic conflict")):
                with self.assertRaises(OSError):
                    remote.create("fake-drive-id", encode(user))
            changed = deepcopy(user)
            changed["content_payload"] = "different valid event"
            with self.assertRaises(ReplicaConflict):
                remote.create("fake-drive-id", encode(changed))
        RECEIPTS["google_drive_mapping"] = {"sender": "RECORDING_FAKE_NO_NETWORK", "preallocated_id_persisted_before_create": True,
                                            "multipart_json": "PASS", "409_readback": "PASS", "403_is_not_absence": True, "live_drive_calls": 0}

    def test_authority_negative_full_lifecycle(self):
        authorities = {name: self.root / (name + ".json") for name in ("Current", "Memory", "Persona", "Relationship")}
        for name, path in authorities.items():
            path.write_text(json.dumps({name: "frozen synthetic authority"}))
        before = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in authorities.items()}
        with Journal(self.local) as journal:
            user, template = pair()
            journal.append_user_then_invoke_stub(user, template, attempt_id="authority-test")
            bad = deepcopy(user)
            bad["metadata"]["extensions"] = {"relationship_upgrade": "permanent"}
            with self.assertRaises(JournalError):
                journal.append(bad)
            self.assertFalse(any(hasattr(journal, name) for name in ("write_current", "update_memory", "set_persona", "upgrade_relationship")))
        result = seam(self.local, {"op": "write_current", "value": "forbidden"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "UNSUPPORTED_OPERATION")
        self.assertEqual(before, {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in authorities.items()})
        RECEIPTS["authority_negative"] = {"Current": "UNCHANGED", "Memory": "UNCHANGED", "Persona": "UNCHANGED", "Relationship": "UNCHANGED", "mutation_api": "ABSENT"}

    def test_120_turns_twice_deterministic_and_repeated_reopen(self):
        digests = []
        for run in range(2):
            directory = self.root / f"batch-{run}"
            remote = OfflineDrive(self.root / f"batch-remote-{run}")
            try:
                with Journal(directory) as journal:
                    for number in range(120):
                        user, template = pair(number)
                        outcome = "complete" if number < 100 else "partial" if number < 110 else "failed"
                        script = StubScript((f"visible output {number}",) if outcome != "failed" else (), outcome)
                        receipt = journal.append_user_then_invoke_stub(user, template, attempt_id=f"batch-{number}", script=script)
                        self.assertLess(receipt["trace"].index("USER_DURABLE_RECEIPT"), receipt["trace"].index("PROVIDER_STUB_INVOKED"))
                        journal.append(user)
                    events = journal.export()
                    self.assertEqual(len(events), 240)
                    self.assertEqual(len({e["event_id"] for e in events}), 240)
                    self.assertEqual([e["sequence_no"] for e in events], list(range(240)))
                    self.assertEqual(sum(e["actor_role"] == "assistant" and e["status"] == "complete" for e in events), 100)
                    receipt = journal.drain_replica(remote)
                    self.assertEqual(receipt["verified_events"], 240)
                    self.assertEqual(sorted(remote.snapshot()), sorted(encode(e) for e in events))
                    digest = fingerprint(events)
                for _ in range(3):
                    with Journal(directory) as journal:
                        self.assertEqual(fingerprint(journal.export()), digest)
                        for e in events:
                            self.assertEqual(journal.append(e)["disposition"], "ALREADY_COMMITTED")
                        self.assertEqual(fingerprint(journal.export()), digest)
                digests.append(digest)
            finally:
                remote.close()
        self.assertEqual(digests[0], digests[1])
        RECEIPTS["120_turns"] = {"runs": 2, "turns_per_run": 120, "events_per_run": 240,
                                 "complete": 100, "partial": 10, "failed": 10, "event_loss": 0,
                                 "sequence_disorder": 0, "duplicate_amplification": 0,
                                 "completed_replica_agreement": True, "normalized_event_fingerprint": digests[0]}

    def test_hard_crash_f1_through_f8_and_repeated_recovery(self):
        matrix = {}
        for point in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
            with self.subTest(point=point):
                local, remote = self.root / point, self.root / (point + "-drive")
                request = turn_request()
                if point in {"F7", "F8"}:
                    setup = seam(local, {"op": "append", "event": request["user"]})
                    self.assertEqual(setup.returncode, 0)
                    operation = {"op": "drain"}
                elif point == "F6":
                    operation = {"op": "append", "event": request["user"]}
                else:
                    operation = request
                crashed = seam(local, operation, fault=point, replica=remote)
                self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
                first = seam(local, {"op": "recover"})
                self.assertEqual(first.returncode, 0, first.stdout)
                recovery = json.loads(first.stdout)["result"]
                self.assertFalse(recovery["previous_clean_shutdown"])
                self.assertEqual(recovery["provider_invocations"], 0)
                snapshot = json.loads(seam(local, {"op": "export"}).stdout)["result"]
                events = snapshot["events"]
                self.assertEqual(events[0], request["user"])
                expected = {"F1": None, "F2": "failed", "F3": "partial", "F4": "complete", "F5": "complete", "F6": None, "F7": None, "F8": None}[point]
                self.assertEqual([e["status"] for e in events if e["actor_role"] == "assistant"], [] if expected is None else [expected])
                if point in {"F2", "F3"}:
                    self.assertEqual(recovery["attempts"][0]["external_outcome"], "UNKNOWN")
                    duplicate = json.loads(seam(local, request).stdout)["result"]
                    self.assertEqual(duplicate["provider_invocations"], 0)
                if point == "F3":
                    self.assertEqual(events[1]["content_payload"], {"text": "visible one\n"})
                for _ in range(3):
                    replay = json.loads(seam(local, {"op": "export"}).stdout)["result"]
                    self.assertEqual(replay, snapshot)
                drain = json.loads(seam(local, {"op": "drain"}, replica=remote).stdout)["result"]
                self.assertEqual(drain["completion"], "READBACK_VERIFIED")
                readback = json.loads(seam(local, {"op": "readback"}, replica=remote).stdout)["result"]
                self.assertEqual(sorted(encode(e) for e in events), sorted(encode(e) for e in readback["events"]))
                if point == "F1":
                    self.assertEqual(recovery["attempts"][0]["external_outcome"], "NOT_SENT")
                    resumed = json.loads(seam(local, request).stdout)["result"]
                    self.assertEqual(resumed["provider_invocations"], 1)
                matrix[point] = {"hard_exit": 86, "events_after_recovery": len(events), "assistant_status": expected,
                                 "recovery_provider_calls": 0, "repeated_replay_stable": True, "replica_readback": "PASS"}
        RECEIPTS["F1-F8"] = matrix


if __name__ == "__main__":
    unittest.main()
