CREATE TABLE IF NOT EXISTS compliance_resets (
    id SERIAL PRIMARY KEY,
    performed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader') THEN
        CREATE ROLE dashboard_reader LOGIN PASSWORD 'dashboard_reader';
    END IF;
END
$$;

DO $$
DECLARE
    db_name text := current_database();
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO dashboard_reader', db_name);
END
$$;

GRANT USAGE ON SCHEMA public TO dashboard_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_reader;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO dashboard_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dashboard_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO dashboard_reader;
