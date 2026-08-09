BEGIN;

CREATE SCHEMA IF NOT EXISTS context_control;

CREATE TABLE IF NOT EXISTS context_control.projects (
    project_id TEXT PRIMARY KEY CHECK (length(project_id) > 0),
    revision BIGINT NOT NULL CHECK (revision >= 0),
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    last_event_sha256 CHARACTER(64),
    snapshot JSONB NOT NULL,
    snapshot_sha256 CHARACTER(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (last_event_sha256 IS NULL OR last_event_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (
        (last_sequence = 0 AND last_event_sha256 IS NULL)
        OR (last_sequence > 0 AND last_event_sha256 IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS context_control.state_events (
    project_id TEXT NOT NULL REFERENCES context_control.projects(project_id) ON DELETE RESTRICT,
    sequence_no BIGINT NOT NULL CHECK (sequence_no > 0),
    event_id TEXT NOT NULL CHECK (length(event_id) > 0),
    revision_before BIGINT NOT NULL CHECK (revision_before >= 0),
    revision_after BIGINT NOT NULL CHECK (revision_after = revision_before + 1),
    previous_event_sha256 CHARACTER(64),
    event_sha256 CHARACTER(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    envelope JSONB NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (project_id, sequence_no),
    UNIQUE (project_id, event_id),
    UNIQUE (project_id, revision_after),
    UNIQUE (project_id, event_sha256),
    CHECK (
        previous_event_sha256 IS NULL
        OR previous_event_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (event_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION context_control.reject_state_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'context_control.state_events is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS state_events_append_only
ON context_control.state_events;

CREATE TRIGGER state_events_append_only
BEFORE UPDATE OR DELETE ON context_control.state_events
FOR EACH ROW
EXECUTE FUNCTION context_control.reject_state_event_mutation();

COMMIT;
