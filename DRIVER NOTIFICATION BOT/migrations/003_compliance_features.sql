ALTER TABLE groups
    ADD COLUMN compliance_topic_id BIGINT,
    ADD COLUMN trailer_topic_id BIGINT;

ALTER TABLE drivers
    ADD COLUMN last_pass_at TIMESTAMPTZ,
    ADD COLUMN last_congrats_at TIMESTAMPTZ;

CREATE TABLE compliance_tracking (
    driver_id INTEGER PRIMARY KEY REFERENCES drivers(id) ON DELETE CASCADE,
    consecutive_reports INTEGER NOT NULL DEFAULT 0,
    last_report_at TIMESTAMPTZ,
    last_driver_alert_at TIMESTAMPTZ,
    last_dispatch_alert_at TIMESTAMPTZ,
    last_status TEXT,
    last_comment_thread_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE compliance_notes (
    id SERIAL PRIMARY KEY,
    driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    author_id BIGINT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_compliance_tracking_updated
    BEFORE UPDATE ON compliance_tracking
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_compliance_notes_driver_created ON compliance_notes(driver_id, created_at DESC);

UPDATE drivers
SET last_pass_at = sub.last_pass
FROM (
    SELECT driver_id, MAX(reviewed_at) AS last_pass
    FROM daily_checkins
    WHERE status = 'pass' AND reviewed_at IS NOT NULL
    GROUP BY driver_id
) AS sub
WHERE drivers.id = sub.driver_id;
