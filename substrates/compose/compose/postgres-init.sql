-- Roles: worker (inserts), probe (admin reader), analytics (a reporting
-- credential; the admin pghog uses it for resource_exhaustion).
CREATE ROLE worker LOGIN PASSWORD 'worker';
CREATE ROLE probe LOGIN PASSWORD 'probe';
CREATE ROLE analytics LOGIN PASSWORD 'analytics';

CREATE TABLE jobs_done (
    seq          BIGSERIAL PRIMARY KEY,
    job_id       TEXT NOT NULL UNIQUE,
    sig          TEXT NOT NULL,
    worker       TEXT NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT INSERT, SELECT ON jobs_done TO worker;
GRANT USAGE, SELECT ON SEQUENCE jobs_done_seq_seq TO worker;
GRANT SELECT ON jobs_done TO probe;
GRANT SELECT ON jobs_done TO analytics;
