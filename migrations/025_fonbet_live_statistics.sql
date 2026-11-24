-- Additive migration only. No API-Football data or existing registry is changed.
CREATE TABLE fonbet_live_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete','partial','failed')),
    summary JSONB NOT NULL,
    CHECK (finished_at >= started_at)
);
CREATE TABLE fonbet_live_observations (
    observation_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES fonbet_live_runs(run_id) ON DELETE RESTRICT,
    fonbet_event_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object')
);
CREATE INDEX fonbet_live_observations_event_idx ON fonbet_live_observations(fonbet_event_id, observed_at);
CREATE TABLE fonbet_live_match_statistics_75 (
    fonbet_event_id TEXT PRIMARY KEY CHECK (fonbet_event_id ~ '^[0-9]+$'),
    event_url TEXT NOT NULL,
    home_name TEXT NOT NULL,
    away_name TEXT NOT NULL,
    competition_name TEXT NOT NULL,
    kickoff_at TIMESTAMPTZ,
    period TEXT NOT NULL CHECK (period='2H'),
    elapsed SMALLINT NOT NULL CHECK (elapsed BETWEEN 65 AND 75),
    clock_text TEXT NOT NULL,
    goals_home SMALLINT CHECK (goals_home >= 0),
    goals_away SMALLINT CHECK (goals_away >= 0),
    shots_on_target_2h_home SMALLINT CHECK (shots_on_target_2h_home >= 0),
    shots_on_target_2h_away SMALLINT CHECK (shots_on_target_2h_away >= 0),
    shots_on_target_2h_total INTEGER GENERATED ALWAYS AS
        (shots_on_target_2h_home + shots_on_target_2h_away) STORED,
    source_period_label TEXT NOT NULL,
    source_metric_label TEXT NOT NULL CHECK (source_metric_label IN ('Удары в створ','Ударов в створ')),
    discovered_at TIMESTAMPTZ NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    context_observed_at TIMESTAMPTZ NOT NULL,
    minute_basis TEXT NOT NULL CHECK (minute_basis IN ('match_center','list_context')),
    quality_status TEXT NOT NULL CHECK (quality_status IN ('both_teams','partial')),
    observation_id UUID NOT NULL REFERENCES fonbet_live_observations(observation_id) ON DELETE RESTRICT,
    run_id UUID NOT NULL REFERENCES fonbet_live_runs(run_id) ON DELETE RESTRICT,
    fixture_id INTEGER,
    mapping_status TEXT NOT NULL DEFAULT 'unmatched' CHECK (mapping_status IN ('confirmed','unmatched','ambiguous')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (home_name <> away_name),
    CHECK (shots_on_target_2h_home IS NOT NULL OR shots_on_target_2h_away IS NOT NULL),
    CHECK ((quality_status='both_teams') = (shots_on_target_2h_home IS NOT NULL AND shots_on_target_2h_away IS NOT NULL)),
    CHECK (observed_at >= requested_at AND observed_at >= context_observed_at
           AND observed_at-context_observed_at <= interval '30 seconds'),
    CHECK ((mapping_status='confirmed') = (fixture_id IS NOT NULL))
);
CREATE INDEX fonbet_live_statistics_fixture_idx ON fonbet_live_match_statistics_75(fixture_id) WHERE fixture_id IS NOT NULL;
