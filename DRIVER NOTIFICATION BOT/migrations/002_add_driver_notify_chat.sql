ALTER TABLE drivers ADD COLUMN IF NOT EXISTS notify_chat_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_drivers_notify_chat ON drivers(notify_chat_id) WHERE notify_chat_id IS NOT NULL;
