"""Runtime configuration sourced from environment variables."""
from __future__ import annotations

import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://origin:origin@db:5432/origin"
)
# Size of each bytea slice pulled from PostgreSQL while streaming a body.
CHUNK_SIZE = int(os.environ.get("ORIGIN_CHUNK_SIZE", str(1024 * 1024)))
# Default / upper bound for entries per copy-session segment.
COPY_SEGMENT_SIZE = int(os.environ.get("ORIGIN_COPY_SEGMENT_SIZE", "16"))
COPY_MAX_SEGMENT_SIZE = int(os.environ.get("ORIGIN_COPY_MAX_SEGMENT_SIZE", "1024"))
# Upper bound for object paths accepted by one revision-GC request.
GC_MAX_PATHS = int(os.environ.get("ORIGIN_GC_MAX_PATHS", "100"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objects (
    path         text PRIMARY KEY,
    current_id   bigint,
    -- Per-path high-water mark of ever-allocated revision numbers.  A
    -- move sets current_id to NULL without deleting revisions, and GC may
    -- delete every surviving revision of such a path, so the next
    -- revision number MUST NOT be derived from max(revision_no) at insert
    -- time (that could reuse or regress numbers); it comes from this
    -- monotonically increasing counter instead.
    revision_seq bigint NOT NULL DEFAULT 0
);

-- Upgrade for databases created before revision_seq existed.
ALTER TABLE objects ADD COLUMN IF NOT EXISTS revision_seq
    bigint NOT NULL DEFAULT 0;
-- The counter is seeded from the highest historical revision number at
-- the END of this script (the revisions table must exist first); see the
-- backfill UPDATE below.

CREATE TABLE IF NOT EXISTS revisions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_path text NOT NULL REFERENCES objects (path) ON DELETE CASCADE,
    revision_no bigint NOT NULL,
    content     bytea NOT NULL,
    sha256      text NOT NULL CHECK (char_length(sha256) = 64),
    media_type  text NOT NULL DEFAULT 'application/octet-stream',
    created_at  timestamptz NOT NULL
                            DEFAULT date_trunc('second', now()),
    UNIQUE (object_path, revision_no)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'objects_current_id_fkey'
    ) THEN
        ALTER TABLE objects
            ADD CONSTRAINT objects_current_id_fkey
            FOREIGN KEY (current_id) REFERENCES revisions (id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS revisions_object_idx
    ON revisions (object_path, revision_no);

-- Server-side singletons (e.g. the HMAC key that signs copy cursors).
CREATE TABLE IF NOT EXISTS service_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- Resumable copy sessions.  A session pins an immutable manifest of
-- (source path, revision id) pairs captured in one database snapshot;
-- segments of the manifest are pulled and acknowledged by cursor.
CREATE TABLE IF NOT EXISTS copy_sessions (
    id              text PRIMARY KEY,
    target_prefix   text NOT NULL,
    source_hash     text NOT NULL,
    manifest_sha256 text NOT NULL CHECK (char_length(manifest_sha256) = 64),
    segment_size    integer NOT NULL,
    total_entries   integer NOT NULL,
    total_segments  integer NOT NULL,
    next_segment    integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL
                            DEFAULT date_trunc('second', now())
);

CREATE TABLE IF NOT EXISTS copy_session_entries (
    session_id  text NOT NULL REFERENCES copy_sessions (id) ON DELETE CASCADE,
    ordinal     integer NOT NULL,
    segment_no  integer NOT NULL,
    source_path text NOT NULL,
    target_path text NOT NULL,
    revision_id bigint NOT NULL REFERENCES revisions (id),
    revision_no bigint NOT NULL,
    sha256      text NOT NULL CHECK (char_length(sha256) = 64),
    media_type  text NOT NULL,
    length      bigint NOT NULL,
    PRIMARY KEY (session_id, ordinal)
);

CREATE INDEX IF NOT EXISTS copy_session_entries_segment_idx
    ON copy_session_entries (session_id, segment_no, ordinal);

-- Lookups "is this revision still referenced?" run on every GC
-- classification, so both referencing columns get an index.
CREATE INDEX IF NOT EXISTS copy_session_entries_revision_idx
    ON copy_session_entries (revision_id);

-- Revision baseline captured for EVERY target path in the same
-- transaction (and lock snapshot) as the manifest.  A NULL revision_id
-- records "the target did not exist when the session was created"; this
-- is the identity acknowledgements compare against, so a re-publish of
-- the very same bytes after baseline is a conflict rather than an
-- idempotent no-op.
CREATE TABLE IF NOT EXISTS copy_session_target_baselines (
    session_id  text NOT NULL REFERENCES copy_sessions (id) ON DELETE CASCADE,
    ordinal     integer NOT NULL,
    target_path text NOT NULL,
    revision_id bigint REFERENCES revisions (id),
    revision_no bigint,
    sha256      text,
    PRIMARY KEY (session_id, target_path)
);

CREATE INDEX IF NOT EXISTS copy_session_baselines_revision_idx
    ON copy_session_target_baselines (revision_id);

-- One row per acknowledged segment: the persisted batch result that
-- makes repeated acknowledgements and replays byte-identical.  Stored
-- as text (not jsonb) to preserve the exact key order of the original
-- response.
CREATE TABLE IF NOT EXISTS copy_session_acks (
    session_id text NOT NULL REFERENCES copy_sessions (id) ON DELETE CASCADE,
    segment_no integer NOT NULL,
    cursor     text NOT NULL,
    status     text NOT NULL,
    result     text NOT NULL,
    created_at timestamptz NOT NULL
                           DEFAULT date_trunc('second', now()),
    PRIMARY KEY (session_id, segment_no)
);

-- Backfill the per-path revision counter for databases created before
-- revision_seq existed: seed from the highest historical revision number
-- so numbering keeps strictly increasing after the upgrade as well.
-- Runs after every table above has been created so a fresh database
-- initialises without an undefined-table error.
UPDATE objects o
   SET revision_seq = sub.m
  FROM (
        SELECT object_path, max(revision_no) AS m
          FROM revisions
         GROUP BY object_path
       ) AS sub
 WHERE o.path = sub.object_path AND o.revision_seq = 0;
"""
