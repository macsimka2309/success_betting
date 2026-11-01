CREATE TABLE fixture_fetch_results (
    fixture_id INTEGER NOT NULL REFERENCES fixtures(fixture_id),
    kind TEXT NOT NULL CHECK (kind IN ('events','statistics','halves','lineups')),
    status TEXT NOT NULL CHECK (status IN ('loaded','empty','partial','error','exhausted')),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at TIMESTAMPTZ,
    PRIMARY KEY (fixture_id, kind)
);
CREATE TABLE api_observations (
    observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES collection_runs(run_id),
    endpoint TEXT NOT NULL,
    params JSONB NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX api_observations_run_idx ON api_observations(run_id);
CREATE INDEX api_observations_time_idx ON api_observations USING BRIN(observed_at);
