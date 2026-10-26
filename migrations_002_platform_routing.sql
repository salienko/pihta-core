-- Unified booking system migration 002.
-- Preparation only; do not execute without backup and maintenance approval.
-- DRAFT / BLOCKED: identity migration and historical MAX attribution unresolved.
BEGIN;
SET LOCAL search_path TO pihta_booking_v2;
SELECT pg_advisory_xact_lock(7319845);
CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram';
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram';
ALTER TABLE outbox ALTER COLUMN booking_id DROP NOT NULL;
ALTER TABLE payment_messages ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram';
ALTER TABLE paykeeper_invoices ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'telegram';
ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_booking_id_recipient_event_key;
DROP INDEX IF EXISTS standalone_outbox_event;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_platform_event ON outbox(booking_id,recipient,platform,event) WHERE booking_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS standalone_outbox_platform_event ON outbox(recipient,platform,event) WHERE booking_id IS NULL;
ALTER TABLE payment_messages DROP CONSTRAINT IF EXISTS payment_messages_pkey;
ALTER TABLE payment_messages ADD PRIMARY KEY(platform,chat_id,message_id);
INSERT INTO schema_migrations(version) VALUES ('002_platform_routing') ON CONFLICT (version) DO NOTHING;
COMMIT;
