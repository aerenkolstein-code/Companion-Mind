"""One SQLite fact base for the approved local synthetic Q1 contract.

Paths and worker/owner identities are supplied by the trusted local caller.
This module provides no web authentication, dispatcher, HTTP or execution.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

WO_ID = 'A1-CMER-Q1-SYNTHETIC-01'
AUTHORITY = 'Q1LOCAL-SYNTHETIC-AUTHORITY-01'
CONTEXT = 'Q1 synthetic input only.\n'
FILE = 'synthetic/input.txt'
OWNERS = frozenset({'A1_OWNER', 'CMER_OWNER'})
_NO_VERSION = object()
CONTRACT_ERROR_CODES = frozenset({'WRONG_IDENTITY', 'WORK_ORDER_NOT_FOUND', 'VERSION_MISMATCH',
    'BASE_HASH_MISMATCH', 'INVALID_CANDIDATE_ID', 'INVALID_CANDIDATE', 'CANDIDATE_CONFLICT',
    'WORK_ORDER_NOT_OPEN', 'CANDIDATE_LIMIT', 'OWNER_DENIED', 'INVALID_VERDICT',
    'CANDIDATE_NOT_FOUND', 'ATTESTATION_CONFLICT', 'TERMINAL_CONFLICT', 'OWNER_ATTESTATIONS_REQUIRED'})
UNCERTAIN_ERROR_CODES = frozenset({'STORAGE_UNTRUSTED', 'WORK_ORDER_CONFIGURATION_MISMATCH', 'IO_COMMIT_UNKNOWN'})


class StoreError(ValueError):
    """Only fixed, non-secret error codes leave the store."""
    @property
    def code(self):
        return str(self)


def _require(condition, code):
    if not condition:
        raise StoreError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, 'INVALID_CANDIDATE')
        value[key] = item
    return value


def _candidate_id(value):
    _require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value), 'INVALID_CANDIDATE_ID')


def _work_order(value):
    expected = {'schema': 'webchat-local-work-order/1', 'synthetic': True, 'wo_id': WO_ID,
                'version': 1, 'base_hash': _hash(CONTEXT), 'allowed_files': [FILE],
                'context': CONTEXT, 'authority_id': AUTHORITY}
    _require(type(value) is dict and value == expected and type(value.get('version')) is int
             and value.get('synthetic') is True, 'INVALID_WORK_ORDER')
    return _json(expected)


def _candidate(text):
    _require(type(text) is str, 'INVALID_CANDIDATE')
    try:
        _require(len(text.encode('utf-8')) <= 131072, 'INVALID_CANDIDATE')
        value = json.loads(text, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(StoreError('INVALID_CANDIDATE')))
        _require(type(value) is dict and set(value) == {'files'} and type(value['files']) is dict
                 and set(value['files']) == {FILE} and type(value['files'][FILE]) is str
                 and len(value['files'][FILE].encode('utf-8')) <= 16384, 'INVALID_CANDIDATE')
        return _json(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError('INVALID_CANDIDATE') from None


class LocalWorkOrderStore:
    """Fresh connections, explicit transactions, and validated persisted facts."""
    def __init__(self, db_path, work_order):
        self._work_order = _work_order(work_order)
        _require(isinstance(db_path, (str, Path)), 'INVALID_DATABASE_PATH')
        self._path = Path(db_path)
        _require(self._path.is_absolute() and self._path.suffix in {'.db', '.sqlite3'}
                 and self._path.parent.is_dir() and not self._path.is_symlink(), 'INVALID_DATABASE_PATH')
        created = False
        try:
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                _require(self._path.is_file(), 'STORAGE_UNTRUSTED')
            else:
                os.close(descriptor)
                created = True
            connection = self._connect()
            try:
                if created:
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('BEGIN IMMEDIATE')
                    for statement in (
                        'CREATE TABLE metadata(id INTEGER PRIMARY KEY CHECK(id=1), work_order TEXT NOT NULL, state TEXT NOT NULL)',
                        'CREATE TABLE candidates(id TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL)',
                        'CREATE TABLE attestations(candidate_id TEXT NOT NULL REFERENCES candidates(id), owner TEXT NOT NULL, verdict TEXT NOT NULL, PRIMARY KEY(candidate_id,owner))',
                        'CREATE TABLE terminal(id INTEGER PRIMARY KEY CHECK(id=1), candidate_id TEXT NOT NULL REFERENCES candidates(id))',
                        'CREATE TABLE events(seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)',
                    ):
                        connection.execute(statement)
                    connection.execute('PRAGMA user_version=1')
                    connection.execute('INSERT INTO metadata VALUES(1,?,?)', (self._work_order, 'OPEN'))
                    self._event(connection, 'INIT', {'work_order_hash': _hash(self._work_order)})
                    connection.commit()
                connection.execute('BEGIN IMMEDIATE')
                self._validate(connection)
                connection.commit()
            finally:
                connection.close()
        except StoreError:
            raise
        except (sqlite3.Error, OSError, ValueError):
            raise StoreError('STORAGE_UNTRUSTED') from None

    def _connect(self):
        connection = sqlite3.connect(self._path.as_uri() + '?mode=rw', uri=True, timeout=5, isolation_level=None)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA synchronous=FULL')
        return connection

    @staticmethod
    def _event(db, kind, payload):
        last = db.execute('SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        seq, previous = (1, '0' * 64) if last is None else (last[0] + 1, last[1])
        raw = _json(payload)
        event_hash = _hash(_json([seq, kind, raw, previous]))
        db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (seq, kind, raw, previous, event_hash))

    def _validate(self, db):
        _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
                 and db.execute('PRAGMA user_version').fetchone() == (1,), 'STORAGE_UNTRUSTED')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _require(tables == {'metadata', 'candidates', 'attestations', 'terminal', 'events'}, 'STORAGE_UNTRUSTED')
        meta = db.execute('SELECT id,work_order,state FROM metadata').fetchall()
        _require(len(meta) == 1 and meta[0][0] == 1, 'STORAGE_UNTRUSTED')
        _require(meta[0][1] == self._work_order, 'WORK_ORDER_CONFIGURATION_MISMATCH')
        candidates = {}
        for cid, payload, fingerprint in db.execute('SELECT id,payload,hash FROM candidates'):
            try:
                _candidate_id(cid)
                _require(_candidate(payload) == payload and _hash(payload) == fingerprint, 'STORAGE_UNTRUSTED')
            except StoreError:
                raise StoreError('STORAGE_UNTRUSTED') from None
            candidates[cid] = fingerprint
        _require(len(candidates) <= 128, 'STORAGE_UNTRUSTED')
        attestations = {(cid, owner): verdict for cid, owner, verdict in
                        db.execute('SELECT candidate_id,owner,verdict FROM attestations')}
        terminals = db.execute('SELECT id,candidate_id FROM terminal').fetchall()
        events = db.execute('SELECT seq,kind,payload,previous_hash,hash FROM events ORDER BY seq').fetchall()
        _require(1 <= len(events) <= 1024, 'STORAGE_UNTRUSTED')
        state, previous, seen, approvals, terminal = 'OPEN', '0' * 64, {}, {}, None
        for expected_seq, (seq, kind, raw, prior, fingerprint) in enumerate(events, 1):
            _require(seq == expected_seq and prior == previous and fingerprint == _hash(_json([seq, kind, raw, prior])),
                     'STORAGE_UNTRUSTED')
            payload = json.loads(raw)
            _require(raw == _json(payload), 'STORAGE_UNTRUSTED')
            if seq == 1:
                _require(kind == 'INIT' and payload == {'work_order_hash': _hash(self._work_order)}, 'STORAGE_UNTRUSTED')
            elif kind == 'SUBMIT':
                _require(state == 'OPEN' and set(payload) == {'candidate_id', 'candidate_hash'}
                         and payload['candidate_id'] not in seen, 'STORAGE_UNTRUSTED')
                seen[payload['candidate_id']] = payload['candidate_hash']
            elif kind == 'ATTEST':
                _require(state == 'OPEN' and set(payload) == {'candidate_id', 'owner', 'verdict'}
                         and payload['candidate_id'] in seen and payload['owner'] in OWNERS
                         and payload['verdict'] in {'PASS', 'NOT_PASS'}, 'STORAGE_UNTRUSTED')
                key = (payload['candidate_id'], payload['owner'])
                _require(key not in approvals, 'STORAGE_UNTRUSTED')
                approvals[key] = payload['verdict']
                if payload['verdict'] == 'NOT_PASS': state = 'STOP'
            elif kind in {'UNKNOWN', 'CANCEL'}:
                _require(payload == {} and state in ({'OPEN'} if kind == 'UNKNOWN' else {'OPEN', 'UNKNOWN'}), 'STORAGE_UNTRUSTED')
                state = 'UNKNOWN' if kind == 'UNKNOWN' else 'STOP'
            elif kind == 'FINALIZE':
                _require(state == 'OPEN' and set(payload) == {'candidate_id'} and payload['candidate_id'] in seen
                         and all(approvals.get((payload['candidate_id'], owner)) == 'PASS' for owner in OWNERS),
                         'STORAGE_UNTRUSTED')
                terminal, state = payload['candidate_id'], 'ACCEPTED'
            else:
                raise StoreError('STORAGE_UNTRUSTED')
            previous = fingerprint
        _require(seen == candidates and approvals == attestations and state == meta[0][2]
                 and terminals == ([] if terminal is None else [(1, terminal)]), 'STORAGE_UNTRUSTED')
        return state

    def _transaction(self, action):
        db = None
        try:
            db = self._connect()
            db.execute('BEGIN IMMEDIATE')
            state = self._validate(db)
            result = action(db, state)
            self._validate(db)
            db.commit()
            return result
        except StoreError as error:
            code = str(error)
            return {'ok': False, 'error': code, 'error_kind': 'CONTRACT' if code in CONTRACT_ERROR_CODES else 'UNCERTAIN'}
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, UnicodeError):
            return {'ok': False, 'error': 'IO_COMMIT_UNKNOWN', 'error_kind': 'UNCERTAIN'}
        finally:
            if db is not None:
                try:
                    if db.in_transaction: db.rollback()
                    db.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _worker(wo_id, actor, version=_NO_VERSION):
        _require(type(actor) is str and actor == 'worker', 'WRONG_IDENTITY')
        _require(type(wo_id) is str and wo_id == WO_ID, 'WORK_ORDER_NOT_FOUND')
        if version is not _NO_VERSION:
            _require(type(version) is int and version == 1, 'VERSION_MISMATCH')

    @staticmethod
    def _receipt(cid, fingerprint, duplicate=False, status='CANDIDATE_PENDING'):
        return {'ok': True, 'wo_id': WO_ID, 'version': 1, 'candidate_id': cid,
                'candidate_hash': fingerprint, 'status': status, 'duplicate': duplicate}

    def fetch_work_order(self, wo_id, actor, version):
        def action(db, state):
            self._worker(wo_id, actor, version)
            _require(state == 'OPEN', 'WORK_ORDER_NOT_OPEN')
            return {'ok': True, 'work_order': json.loads(self._work_order)}
        return self._transaction(action)

    def submit_candidate(self, wo_id, actor, version, candidate_id, base_sha256, candidate_text):
        def action(db, state):
            self._worker(wo_id, actor, version)
            _require(type(base_sha256) is str and base_sha256 == _hash(CONTEXT), 'BASE_HASH_MISMATCH')
            _candidate_id(candidate_id)
            payload = _candidate(candidate_text)
            fingerprint = _hash(payload)
            old = db.execute('SELECT hash FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if old is not None:
                _require(old[0] == fingerprint, 'CANDIDATE_CONFLICT')
                return self._receipt(candidate_id, old[0], True)
            _require(state == 'OPEN', 'WORK_ORDER_NOT_OPEN')
            _require(db.execute('SELECT count(*) FROM candidates').fetchone()[0] < 128, 'CANDIDATE_LIMIT')
            db.execute('INSERT INTO candidates VALUES(?,?,?)', (candidate_id, payload, fingerprint))
            self._event(db, 'SUBMIT', {'candidate_id': candidate_id, 'candidate_hash': fingerprint})
            return self._receipt(candidate_id, fingerprint)
        return self._transaction(action)

    def get_status(self, wo_id, actor):
        def action(db, state):
            self._worker(wo_id, actor)
            terminal = db.execute('SELECT candidate_id FROM terminal').fetchone()
            result = {'ok': True, 'wo_id': WO_ID, 'version': 1, 'state': state,
                      'candidate_count': db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                      'event_count': db.execute('SELECT count(*) FROM events').fetchone()[0], 'terminal': None}
            if terminal:
                fingerprint = db.execute('SELECT hash FROM candidates WHERE id=?', terminal).fetchone()[0]
                result['terminal'] = self._receipt(terminal[0], fingerprint, status='ACCEPTED')
            return result
        return self._transaction(action)

    def _owner_state(self, wo_id, kind):
        def action(db, state):
            _require(wo_id == WO_ID and type(wo_id) is str, 'WORK_ORDER_NOT_FOUND')
            target = 'UNKNOWN' if kind == 'UNKNOWN' else 'STOP'
            if state == target or state == 'STOP':
                return {'ok': True, 'state': state, 'duplicate': True}
            _require(state in ({'OPEN'} if kind == 'UNKNOWN' else {'OPEN', 'UNKNOWN'}), 'WORK_ORDER_NOT_OPEN')
            db.execute('UPDATE metadata SET state=? WHERE id=1', (target,))
            self._event(db, kind, {})
            return {'ok': True, 'state': target, 'duplicate': False}
        return self._transaction(action)

    def mark_unknown(self, wo_id):
        return self._owner_state(wo_id, 'UNKNOWN')

    def cancel(self, wo_id):
        return self._owner_state(wo_id, 'CANCEL')

    def attest(self, wo_id, owner, candidate_id, verdict):
        def action(db, state):
            _require(type(wo_id) is str and wo_id == WO_ID, 'WORK_ORDER_NOT_FOUND')
            _require(type(owner) is str and owner in OWNERS, 'OWNER_DENIED')
            _candidate_id(candidate_id)
            _require(type(verdict) is str and verdict in {'PASS', 'NOT_PASS'}, 'INVALID_VERDICT')
            _require(db.execute('SELECT 1 FROM candidates WHERE id=?', (candidate_id,)).fetchone(), 'CANDIDATE_NOT_FOUND')
            old = db.execute('SELECT verdict FROM attestations WHERE candidate_id=? AND owner=?', (candidate_id, owner)).fetchone()
            if old:
                _require(old[0] == verdict, 'ATTESTATION_CONFLICT')
                return {'ok': True, 'status': 'ATTESTED', 'duplicate': True}
            _require(state == 'OPEN', 'WORK_ORDER_NOT_OPEN')
            db.execute('INSERT INTO attestations VALUES(?,?,?)', (candidate_id, owner, verdict))
            self._event(db, 'ATTEST', {'candidate_id': candidate_id, 'owner': owner, 'verdict': verdict})
            if verdict == 'NOT_PASS': db.execute("UPDATE metadata SET state='STOP' WHERE id=1")
            return {'ok': True, 'status': 'ATTESTED', 'duplicate': False}
        return self._transaction(action)

    def finalize(self, wo_id, candidate_id):
        def action(db, state):
            _require(type(wo_id) is str and wo_id == WO_ID, 'WORK_ORDER_NOT_FOUND')
            _candidate_id(candidate_id)
            old = db.execute('SELECT candidate_id FROM terminal').fetchone()
            if old:
                _require(old[0] == candidate_id, 'TERMINAL_CONFLICT')
                fingerprint = db.execute('SELECT hash FROM candidates WHERE id=?', old).fetchone()[0]
                return self._receipt(candidate_id, fingerprint, True, 'ACCEPTED')
            _require(state == 'OPEN', 'WORK_ORDER_NOT_OPEN')
            row = db.execute('SELECT hash FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            _require(row is not None, 'CANDIDATE_NOT_FOUND')
            approvals = dict(db.execute('SELECT owner,verdict FROM attestations WHERE candidate_id=?', (candidate_id,)))
            _require(all(approvals.get(owner) == 'PASS' for owner in OWNERS), 'OWNER_ATTESTATIONS_REQUIRED')
            db.execute('INSERT INTO terminal VALUES(1,?)', (candidate_id,))
            db.execute("UPDATE metadata SET state='ACCEPTED' WHERE id=1")
            self._event(db, 'FINALIZE', {'candidate_id': candidate_id})
            return self._receipt(candidate_id, row[0], status='ACCEPTED')
        return self._transaction(action)
