"""Rebuildable, physically separate lexical projection; never an Authority.

Only previously authorized synthetic sources enter this index. Search returns
refs, never source bodies. The runtime resolves original fixture evidence.
"""
import os
from pathlib import Path
import re
import sqlite3

from .contracts import HomeError, fingerprint, safe_text


class LexicalIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / "lexical.sqlite3"
        if self.directory.is_symlink() or self.path.is_symlink():
            raise HomeError("UNSAFE_INDEX_PATH")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(self.path)
        try:
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5("
                                    "source_id UNINDEXED, version UNINDEXED, universe_id UNINDEXED, "
                                    "access_subject_id UNINDEXED, source_fingerprint UNINDEXED, body)")
        except sqlite3.Error:
            self.connection.close()
            raise HomeError("FTS5_UNAVAILABLE") from None

    @staticmethod
    def require_allowed(decision):
        if decision["decision"] != "ALLOW" or decision["tier"] != "P1_SCOPED_READ":
            raise HomeError("INDEX_PERMISSION_DENIED")

    def rebuild(self, fixtures, decision):
        self.require_allowed(decision)
        rows = []
        for fixture in fixtures:
            if (fixture.universe_id, fixture.access_subject_id, fixture.source_id, fixture.version) != (
                    decision["universe_id"], decision["access_subject_id"], decision["source_id"],
                    decision["source_version"]):
                raise HomeError("INDEX_SCOPE_MISMATCH")
            rows.append((fixture.source_id, fixture.version, fixture.universe_id,
                         fixture.access_subject_id, fixture.ref()["content_fingerprint"], fixture.text))
        # One authorized route per slice, atomically replacing the derived view.
        with self.connection:
            self.connection.execute("DELETE FROM lexical")
            self.connection.executemany("INSERT INTO lexical VALUES (?,?,?,?,?,?)", rows)
        return {"derived_only": True, "index_version": "fts5/1", "source_refs": [f.ref() for f in fixtures],
                "index_fingerprint": fingerprint(sorted(rows)), "authority_writes": 0,
                "storage_relation": "SEPARATE_FROM_A019"}

    def search(self, query, decision):
        self.require_allowed(decision)
        safe_text(query)
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if len(terms) > 64:
            raise HomeError("LEXICAL_QUERY_BUDGET_EXCEEDED")
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        rows = self.connection.execute(
            "SELECT source_id,version,source_fingerprint FROM lexical WHERE lexical MATCH ? "
            "AND universe_id=? AND access_subject_id=? AND source_id=? AND version=? ORDER BY source_id,version",
            (expression, decision["universe_id"], decision["access_subject_id"],
             decision["source_id"], decision["source_version"])).fetchall()
        return [{"source_id": r[0], "version": r[1], "content_fingerprint": r[2]} for r in rows]

    def close(self):
        self.connection.close()
