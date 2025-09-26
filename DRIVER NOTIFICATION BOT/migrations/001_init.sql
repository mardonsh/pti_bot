CREATE TYPE check_status AS ENUM ('pending', 'submitted', 'pass', 'fail', 'needs_fix', 'excused');
CREATE TYPE media_kind AS ENUM ('photo', 'video');

CREATE TABLE groups (
    id BIGINT PRIMARY KEY,
    title TEXT NOT NULL,
    paused BOOLEAN NOT NULL DEFAULT false,
    rolling_topic_id BIGINT,
    tz VARCHAR(64) NOT NULL DEFAULT 'UTC',
    autosend_enabled BOOLEAN NOT NULL DEFAULT false,
    autosend_time TIME,
    digest_time TIME NOT NULL DEFAULT '10:30',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE drivers (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    display_name TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    notify_chat_id BIGINT,
    streak_current INTEGER NOT NULL DEFAULT 0,
    streak_best INTEGER NOT NULL DEFAULT 0,
    last_check_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE daily_checkins (
    id SERIAL PRIMARY KEY,
    driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    sent_at TIMESTAMPTZ,
    responded_at TIMESTAMPTZ,
    status check_status NOT NULL DEFAULT 'pending',
    reason TEXT,
    reviewer_user_id BIGINT,
    reviewed_at TIMESTAMPTZ,
    review_message_id BIGINT,
    media_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_checkins UNIQUE (driver_id, date)
);

CREATE TABLE media (
    id SERIAL PRIMARY KEY,
    checkin_id INTEGER NOT NULL REFERENCES daily_checkins(id) ON DELETE CASCADE,
    kind media_kind NOT NULL,
    file_id TEXT NOT NULL,
    media_group_id TEXT,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_daily_checkins_group_date ON daily_checkins(group_id, date);
CREATE INDEX idx_media_checkin_id ON media(checkin_id);
CREATE INDEX idx_media_group_id ON media(media_group_id);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_groups_updated
    BEFORE UPDATE ON groups
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_drivers_updated
    BEFORE UPDATE ON drivers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_daily_checkins_updated
    BEFORE UPDATE ON daily_checkins
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE UNIQUE INDEX uq_drivers_notify_chat ON drivers(notify_chat_id) WHERE notify_chat_id IS NOT NULL;
