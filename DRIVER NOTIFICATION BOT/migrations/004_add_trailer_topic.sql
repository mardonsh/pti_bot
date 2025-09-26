ALTER TABLE groups
    ADD COLUMN IF NOT EXISTS trailer_topic_id BIGINT;
