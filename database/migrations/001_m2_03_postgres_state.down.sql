BEGIN;

DROP TABLE IF EXISTS context_control.state_events;
DROP TABLE IF EXISTS context_control.projects;
DROP FUNCTION IF EXISTS context_control.reject_state_event_mutation();
DROP SCHEMA IF EXISTS context_control;

COMMIT;
