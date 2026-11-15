"""Content-addressed media deduplication store backed by SQLite (WAL mode)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"})


class DuplicateMediaError(RuntimeError):
    """Raised when attempting to reuse a source asset already recorded in history."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_file_sha256(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file in fixed chunks and return its lowercase SHA-256 hex digest."""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Media file does not exist: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"Expected a file, got directory: {target}")

    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def detect_media_type(path: Path | str) -> str:
    """Infer logical media category from file suffix."""
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


class MediaHistoryStore:
    """Persistent SQLite WAL store ensuring source files are never processed twice across batches."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._initialize_schema()

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

    def _initialize_schema(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_sources (
                    source_sha256 TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    preset TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rendered_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    output_path TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    preset TEXT NOT NULL,
                    source_hashes_json TEXT NOT NULL,
                    duration_seconds REAL,
                    batch_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rendered_outputs_sha256
                ON rendered_outputs(output_sha256);
                """
            )
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> MediaHistoryStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def is_source_processed(self, source_sha256: str) -> bool:
        """Return True if a source SHA-256 digest has already been recorded."""
        row = self._conn.execute(
            "SELECT 1 FROM processed_sources WHERE source_sha256 = ? LIMIT 1;",
            (source_sha256.lower(),),
        ).fetchone()
        return row is not None

    def is_path_processed(self, path: Path | str) -> bool:
        """Hash a file and check whether its content already exists in processed history."""
        digest = compute_file_sha256(path)
        return self.is_source_processed(digest)

    def get_source_record(self, source_sha256: str) -> dict | None:
        """Fetch stored metadata for a previously processed source hash."""
        row = self._conn.execute(
            "SELECT * FROM processed_sources WHERE source_sha256 = ? LIMIT 1;",
            (source_sha256.lower(),),
        ).fetchone()
        return dict(row) if row is not None else None

    def filter_unprocessed(self, paths: Iterable[Path | str]) -> list[tuple[Path, str]]:
        """Return unique (Path, sha256) pairs excluding already-processed or in-list duplicate content."""
        fresh: list[tuple[Path, str]] = []
        seen_hashes: set[str] = set()
        for raw in paths:
            candidate = Path(raw)
            if not candidate.is_file():
                continue
            digest = compute_file_sha256(candidate)
            if digest in seen_hashes or self.is_source_processed(digest):
                continue
            seen_hashes.add(digest)
            fresh.append((candidate, digest))
        return fresh

    def record_render(
        self,
        source_paths: Sequence[Path | str],
        output_path: Path | str,
        preset: str,
        duration_seconds: float | None = None,
        output_sha256: str | None = None,
        batch_id: str = "",
        allow_reuse: bool = False,
    ) -> str:
        """Atomically record rendered output and lock all contributing source hashes against reuse."""
        if not source_paths:
            raise ValueError("At least one source file must be provided when recording a render.")

        out_file = Path(output_path)
        resolved_out_hash = output_sha256 or (
            compute_file_sha256(out_file) if out_file.is_file() else ""
        )
        now = _utc_now_iso()

        prepared_sources: list[tuple[str, str, str, int]] = []
        seen_in_call: set[str] = set()
        for raw_src in source_paths:
            src_path = Path(raw_src)
            digest = compute_file_sha256(src_path)
            if digest in seen_in_call:
                continue
            seen_in_call.add(digest)
            prepared_sources.append(
                (
                    digest,
                    str(src_path.resolve()),
                    detect_media_type(src_path),
                    src_path.stat().st_size,
                )
            )

        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            for digest, src_resolved, media_type, file_size in prepared_sources:
                existing = self._conn.execute(
                    "SELECT source_path, output_path FROM processed_sources WHERE source_sha256 = ?;",
                    (digest,),
                ).fetchone()
                if existing is not None:
                    if not allow_reuse:
                        raise DuplicateMediaError(
                            f"Source already processed ({digest[:12]}...): {src_resolved}"
                        )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO processed_sources (
                            source_sha256, source_path, media_type, file_size,
                            preset, output_path, output_sha256, batch_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            digest,
                            src_resolved,
                            media_type,
                            file_size,
                            preset,
                            str(out_file.resolve()) if out_file.exists() else str(out_file),
                            resolved_out_hash,
                            batch_id,
                            now,
                        ),
                    )

            self._conn.execute(
                """
                INSERT INTO rendered_outputs (
                    output_path, output_sha256, preset, source_hashes_json,
                    duration_seconds, batch_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    str(out_file.resolve()) if out_file.exists() else str(out_file),
                    resolved_out_hash,
                    preset,
                    json.dumps([item[0] for item in prepared_sources]),
                    duration_seconds,
                    batch_id,
                    now,
                ),
            )
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

        return resolved_out_hash

    def list_processed(self, limit: int = 100) -> list[dict]:
        """Return most recently recorded processed source entries."""
        rows = self._conn.execute(
            "SELECT * FROM processed_sources ORDER BY created_at DESC LIMIT ?;",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_outputs(self, limit: int = 100) -> list[dict]:
        """Return most recently recorded rendered outputs."""
        rows = self._conn.execute(
            "SELECT * FROM rendered_outputs ORDER BY id DESC LIMIT ?;",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        """Return aggregate counts for processed sources and rendered outputs."""
        src_count = self._conn.execute("SELECT COUNT(*) FROM processed_sources;").fetchone()[0]
        out_count = self._conn.execute("SELECT COUNT(*) FROM rendered_outputs;").fetchone()[0]
        return {"processed_sources": int(src_count), "rendered_outputs": int(out_count)}

    def reset_history(self) -> None:
        """Clear all recorded history tables inside an atomic transaction."""
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            self._conn.execute("DELETE FROM processed_sources;")
            self._conn.execute("DELETE FROM rendered_outputs;")
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise
