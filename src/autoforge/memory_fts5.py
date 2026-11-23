"""One private, bounded SQLite FTS5 generation; no paths or dynamic table names."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import weakref

from .memory import MAX_EVENTS, MAX_FACTS, MAX_TEXT_BYTES
from .memory_retrieval import (
    BackendError,
    DocumentSeal,
    IndexDocument,
    IndexUnavailable,
    IntegrityError,
)

_KEY = re.compile(r"t(?:[0-9a-f]{2})+")
# Includes Unicode compatibility decomposition, case folding, hex and separators.
_BODY_LIMIT = MAX_TEXT_BYTES * 64
_TABLES = {"docs", "docs_config", "docs_content", "docs_data", "docs_docsize", "docs_idx"}


class Fts5Index:
    """Build in a new connection; the retriever publishes the finished object."""

    def __init__(self, documents: tuple[IndexDocument, ...]):
        if type(documents) is not tuple or len(documents) > MAX_FACTS:
            raise BackendError("invalid index batch")
        rows = []
        for doc in documents:
            if (
                type(doc) is not IndexDocument
                or type(doc.id) is not int
                or not 1 <= doc.id <= MAX_EVENTS
                or type(doc.terms) is not tuple
                or len(doc.terms) > _BODY_LIMIT
                or sum(len(term) + 1 for term in doc.terms if type(term) is str) > _BODY_LIMIT
                or any(type(term) is not str or _KEY.fullmatch(term) is None for term in doc.terms)
            ):
                raise BackendError("invalid index document")
            rows.append((doc.id, " ".join(doc.terms)))
        try:
            connection = sqlite3.connect(":memory:")
        except sqlite3.Error:
            raise BackendError("cannot open memory index") from None
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("BEGIN")
            connection.execute("CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='ascii')")
            connection.executemany("INSERT INTO docs(rowid, body) VALUES (?, ?)", rows)
            connection.commit()
            self._connection = connection
            self._ids = tuple(record for record, _ in rows)
            # The publication holds the connection; an abandoned scratch
            # connection or superseded generation has no other owner.
            weakref.finalize(self, connection.close)
        except BaseException as error:
            connection.close()
            if isinstance(error, sqlite3.OperationalError) and str(error) == "no such module: fts5":
                raise IndexUnavailable("SQLite does not provide FTS5") from None
            if isinstance(error, (sqlite3.Error, OverflowError, UnicodeError)):
                raise BackendError("memory index build failed") from None
            raise

    def search(self, terms: tuple[str, ...]) -> tuple[int, ...]:
        if not terms or not self._ids:
            return ()
        # Terms are ASCII keys from the controller. MATCH is a bound parameter.
        query = " OR ".join(terms)
        placeholders = ", ".join("?" for _ in self._ids)
        try:
            rows = self._connection.execute(
                f"SELECT rowid FROM docs WHERE docs MATCH ? AND +rowid IN ({placeholders}) LIMIT ?",
                (query, *self._ids, MAX_FACTS + 1),
            )
            return tuple(row[0] for row in rows)
        except sqlite3.Error:
            raise IntegrityError("memory index lookup failed") from None

    def verify(self, manifest: tuple[DocumentSeal, ...]) -> None:
        """Reconcile the schema, count, stored bodies and FTS posting structure."""
        try:
            schema = self._connection.execute(
                "SELECT type, substr(name, 1, 32) FROM sqlite_schema LIMIT ?",
                (len(_TABLES) + 1,),
            ).fetchall()
            if len(schema) != len(_TABLES) or set(schema) != {("table", n) for n in _TABLES}:
                raise IntegrityError("unexpected memory index schema")
            # Use a capped BLOB so even non-UTF-8 and oversized TEXT are
            # represented as bounded read-back, never decoded or copied whole.
            rows = self._connection.execute(
                "SELECT rowid, typeof(body), substr(CAST(body AS BLOB), 1, ?) "
                "FROM docs ORDER BY rowid LIMIT ?",
                (_BODY_LIMIT + 1, MAX_FACTS + 1),
            ).fetchall()
            if len(rows) != len(manifest):
                raise IntegrityError("unexpected memory index count")
            for (record, kind, body), seal in zip(rows, manifest, strict=True):
                if (
                    record != seal.id
                    or kind != "text"
                    or type(body) is not bytes
                    or len(body) != seal.size
                    or hashlib.sha256(body).digest() != seal.digest
                ):
                    raise IntegrityError("unexpected memory index document")
            # No logical change; SQLite checks the index against its content.
            with self._connection:
                self._connection.execute(
                    "INSERT INTO docs(docs, rank) VALUES ('integrity-check', 1)"
                )
        except (sqlite3.Error, UnicodeError):
            raise IntegrityError("memory index verification failed") from None

    @property
    def size_bytes(self) -> int:
        """Logical main database size, not allocator or process memory."""
        pages = self._connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
        return int(pages) * int(page_size)
