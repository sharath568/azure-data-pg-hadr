-- pghadr: the heartbeat table every drill writes to.
--
-- One row per acknowledged write. (run_id, seq) is the primary key, so after a
-- failover you can ask exactly which acknowledged writes survived:
--
--   SELECT seq FROM pghadr_heartbeat WHERE run_id = '<run id>';
--
-- client_ts is when the probe sent the write; server_ts is when the server ran it.
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS pghadr_heartbeat (
    run_id     text        NOT NULL,
    seq        bigint      NOT NULL,
    client_ts  timestamptz NOT NULL,
    server_ts  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, seq)
);
