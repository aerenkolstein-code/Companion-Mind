"""Drive transport contract and persistent offline service substitute.

GoogleDrive maps reserve/create/read onto Drive REST requests through an injected
authenticated sender. No default network sender or credentials are provided.
A 404 alone means absent; timeouts/403/5xx must raise, never return None.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Protocol
import uuid

from .codec import JournalError, encode, load_schema, prepare, safe_label


class ReplicaConflict(Exception):
    pass


class DriveTransport(Protocol):
    target: str
    evidence_kind: str

    def reserve_id(self) -> str: ...
    def create(self, remote_id: str, canonical_json: str) -> None: ...
    def read(self, remote_id: str) -> str | None: ...


@dataclass(frozen=True)
class DriveResponse:
    status: int
    body: bytes


class GoogleDrive:
    """Drive REST mapping, tested with a recording fake sender only.

    sender(method=, url=, params=, headers=, body=) -> DriveResponse.
    It owns authentication/timeouts and MUST NOT perform hidden retries. The
    Journal owns explicit retries. No credential or arbitrary response is stored.
    Instantiating this adapter does not issue a request.
    """
    evidence_kind = "GOOGLE_DRIVE_REQUEST_ADAPTER"

    def __init__(self, sender, *, folder_id, target):
        self.target = safe_label(target)
        self._folder = safe_label(folder_id)
        self._send = sender
        self._schema = load_schema()

    def _request(self, method, url, *, params, headers=None, body=None):
        try:
            response = self._send(method=method, url=url, params=params, headers=headers or {}, body=body)
            if not isinstance(response, DriveResponse) or not isinstance(response.body, bytes) or len(response.body) > 1_100_000:
                raise OSError()
            return response
        except Exception:
            raise OSError("REMOTE_IO_UNKNOWN") from None

    def reserve_id(self):
        response = self._request("GET", "https://www.googleapis.com/drive/v3/files/generateIds",
                                 params={"count": "1", "space": "drive", "type": "files"})
        try:
            ids = json.loads(response.body)["ids"]
            if response.status != 200 or not isinstance(ids, list) or len(ids) != 1:
                raise ValueError()
            return safe_label(ids[0])
        except Exception:
            raise OSError("REMOTE_ID_RESERVATION_FAILED") from None

    def create(self, remote_id, canonical_json):
        remote_id = safe_label(remote_id)
        try:
            event = json.loads(canonical_json)
            if encode(prepare(event, self._schema)) != canonical_json:
                raise JournalError("NON_CANONICAL_REPLICA_INPUT")
        except (ValueError, TypeError):
            raise JournalError("NON_CANONICAL_REPLICA_INPUT") from None
        boundary = "a019-" + uuid.uuid4().hex
        metadata = {"id": remote_id, "name": "a019-" + remote_id + ".json",
                    "parents": [self._folder], "mimeType": "application/json"}
        payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                   + encode(metadata) + f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
                   + canonical_json + f"\r\n--{boundary}--\r\n").encode("utf-8")
        response = self._request("POST", "https://www.googleapis.com/upload/drive/v3/files",
                                 params={"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"},
                                 headers={"Content-Type": "multipart/related; boundary=" + boundary}, body=payload)
        if response.status == 409:
            if self.read(remote_id) != canonical_json:
                raise ReplicaConflict()
        elif response.status in {200, 201}:
            try:
                if json.loads(response.body).get("id") != remote_id:
                    raise ValueError()
            except Exception:
                raise OSError("REMOTE_CREATE_ACK_UNKNOWN") from None
        else:
            raise OSError("REMOTE_CREATE_UNKNOWN")

    def read(self, remote_id):
        remote_id = safe_label(remote_id)
        response = self._request("GET", "https://www.googleapis.com/drive/v3/files/" + remote_id,
                                 params={"alt": "media", "supportsAllDrives": "true"})
        if response.status == 404:
            return None
        if response.status != 200:
            raise OSError("REMOTE_READ_UNKNOWN")
        try:
            return response.body.decode("utf-8")
        except UnicodeError:
            raise OSError("REMOTE_READ_INVALID") from None


class OfflineDrive:
    """Independent, process-persistent fake Drive with unique preallocated IDs."""
    evidence_kind = "OFFLINE_DRIVE_STUB"

    def __init__(self, directory, *, target="offline-drive", fail=False, corrupt_read=False):
        self.target = safe_label(target)
        self.fail = fail
        self.corrupt_read = corrupt_read
        directory = Path(directory).absolute()
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(directory / "replica.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self._db = sqlite3.connect(directory / "replica.sqlite3", isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS replica_meta(target TEXT NOT NULL)")
        row = self._db.execute("SELECT target FROM replica_meta").fetchone()
        if row is None:
            self._db.execute("INSERT INTO replica_meta VALUES (?)", (self.target,))
        elif row[0] != self.target:
            self._db.close()
            raise JournalError("REPLICA_TARGET_MISMATCH")
        for path in (directory, directory.parent):
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def reserve_id(self):
        if self.fail:
            raise OSError("synthetic unavailable")
        return "offline-" + uuid.uuid4().hex

    def create(self, remote_id, canonical_json):
        safe_label(remote_id)
        if self.fail:
            raise OSError("synthetic unavailable")
        old = self._db.execute("SELECT body FROM files WHERE id=?", (remote_id,)).fetchone()
        if old:
            if old[0] != canonical_json:
                raise ReplicaConflict()
            return
        self._db.execute("INSERT INTO files VALUES (?,?)", (remote_id, canonical_json))

    def read(self, remote_id):
        safe_label(remote_id)
        if self.fail:
            raise OSError("synthetic unavailable")
        row = self._db.execute("SELECT body FROM files WHERE id=?", (remote_id,)).fetchone()
        if row is None:
            return None
        return "synthetic-corrupt" if self.corrupt_read else row[0]

    def snapshot(self):
        return [r[0] for r in self._db.execute("SELECT body FROM files ORDER BY id")]

    def close(self):
        self._db.close()
