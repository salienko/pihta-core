CREATE TABLE IF NOT EXISTS profiles(telegram_id bigint PRIMARY KEY, name text NOT NULL, phone text NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(telegram_id bigint PRIMARY KEY,token text NOT NULL UNIQUE,state text NOT NULL,data jsonb NOT NULL,revision integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bookings(
 id text PRIMARY KEY, draft_token text NOT NULL UNIQUE, telegram_id bigint NOT NULL,
 starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, blocked_until timestamptz NOT NULL,
 data jsonb NOT NULL, estimate jsonb NOT NULL, rules_hash text NOT NULL, created_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','confirmed','cancelled','rejected','expired','completed')),
 reason text NOT NULL DEFAULT '', paid bigint NOT NULL DEFAULT 0 CHECK(paid>=0),
 actual_guests integer CHECK(actual_guests BETWEEN 1 AND 8), actual_total bigint CHECK(actual_total>=0),
 no_show boolean NOT NULL DEFAULT false, revision integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bookings_customer ON bookings(telegram_id,starts_at);
ALTER TABLE bookings ALTER COLUMN telegram_id DROP NOT NULL;
CREATE TABLE IF NOT EXISTS booking_payment_windows(
 booking_id text PRIMARY KEY REFERENCES bookings(id),link_received_at timestamptz NOT NULL,
 warned_at timestamptz,next_check_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bookings_calendar ON bookings(starts_at,blocked_until);
CREATE TABLE IF NOT EXISTS audit(id bigserial PRIMARY KEY,booking_id text REFERENCES bookings(id),actor bigint NOT NULL,action text NOT NULL,detail jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS outbox(id bigserial PRIMARY KEY,booking_id text NOT NULL REFERENCES bookings(id),recipient bigint NOT NULL,event text NOT NULL,text text NOT NULL,due_at timestamptz NOT NULL DEFAULT now(),attempts integer NOT NULL DEFAULT 0,locked_until timestamptz,delivered_at timestamptz,disabled boolean NOT NULL DEFAULT false,last_error text,UNIQUE(booking_id,recipient,event));
CREATE TABLE IF NOT EXISTS web_tokens(hash text PRIMARY KEY,actor bigint NOT NULL,expires_at timestamptz NOT NULL,used boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS web_sessions(hash text PRIMARY KEY,actor bigint NOT NULL,csrf text NOT NULL,expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS operations(key text PRIMARY KEY,actor bigint NOT NULL,result text NOT NULL);
CREATE TABLE IF NOT EXISTS service_settings(code text PRIMARY KEY,available boolean NOT NULL);
CREATE TABLE IF NOT EXISTS holiday_rates(day date PRIMARY KEY,label text NOT NULL,base bigint NOT NULL CHECK(base>0 AND base%2=0),extra bigint NOT NULL CHECK(extra>0));
CREATE TABLE IF NOT EXISTS calendar_blocks(id bigserial PRIMARY KEY,starts_at timestamptz NOT NULL,ends_at timestamptz NOT NULL,reason text NOT NULL,actor bigint NOT NULL,active boolean NOT NULL DEFAULT true,CHECK(ends_at>starts_at));
CREATE TABLE IF NOT EXISTS payment_requests(
 id text PRIMARY KEY,booking_id text NOT NULL REFERENCES bookings(id),
 kind text NOT NULL CHECK(kind IN ('deposit','full','balance')),
 amount bigint NOT NULL CHECK(amount>0),paid_before bigint NOT NULL CHECK(paid_before>=0),
 state text NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','linked','reported','paid','superseded')),
 url text NOT NULL DEFAULT '',revision integer NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_requests_booking ON payment_requests(booking_id,created_at);
CREATE TABLE IF NOT EXISTS payment_messages(chat_id bigint NOT NULL,message_id bigint NOT NULL,request_id text NOT NULL REFERENCES payment_requests(id),PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS paykeeper_invoices(
 request_id text PRIMARY KEY,booking_id text REFERENCES bookings(id),actor bigint NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),paid_before bigint NOT NULL CHECK(paid_before>=0),
 client_id text NOT NULL,phone text NOT NULL,email text NOT NULL DEFAULT '',payment_type text NOT NULL,
 invoice_id text UNIQUE,url text NOT NULL DEFAULT '',state text NOT NULL DEFAULT 'queued'
 CHECK(state IN ('queued','creating','uncertain','ready','received','expired')),
 created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,
 next_check_at timestamptz NOT NULL DEFAULT now(),last_error text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS paykeeper_due ON paykeeper_invoices(next_check_at) WHERE state='ready';
CREATE TABLE IF NOT EXISTS paykeeper_payments(
 payment_id text PRIMARY KEY,request_id text NOT NULL REFERENCES paykeeper_invoices(request_id),
 client_id text NOT NULL,amount bigint NOT NULL CHECK(amount>0),
 disposition text NOT NULL CHECK(disposition IN ('applied','review','standalone')),
 created_at timestamptz NOT NULL DEFAULT now(),next_receipt_at timestamptz NOT NULL DEFAULT now(),
 receipt_attempts integer NOT NULL DEFAULT 0,receipt_done boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS paykeeper_fiscal_receipts(
 id text PRIMARY KEY,payment_id text NOT NULL REFERENCES paykeeper_payments(payment_id),
 url text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS paykeeper_unmatched(
 payment_id text PRIMARY KEY,order_id text NOT NULL,client_id text NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS paykeeper_settlements(
 booking_id text PRIMARY KEY REFERENCES bookings(id),actor bigint NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),phone text NOT NULL,tax text NOT NULL,
 receipt_key text NOT NULL UNIQUE,payment_ids jsonb NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','uncertain','pending','success','failed')),
 receipt_id text UNIQUE,url text NOT NULL DEFAULT '',last_error text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),next_check_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE outbox ALTER COLUMN booking_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS standalone_outbox_event ON outbox(recipient,event) WHERE booking_id IS NULL;
CREATE OR REPLACE FUNCTION protect_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.id,NEW.telegram_id,NEW.draft_token,NEW.starts_at,NEW.ends_at,NEW.blocked_until,NEW.data,NEW.estimate,NEW.rules_hash,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.telegram_id,OLD.draft_token,OLD.starts_at,OLD.ends_at,OLD.blocked_until,OLD.data,OLD.estimate,OLD.rules_hash,OLD.created_at) THEN RAISE EXCEPTION 'booking snapshot is immutable'; END IF;
 RETURN NEW;
END;$$;
DROP TRIGGER IF EXISTS snapshot_immutable ON bookings;
CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION protect_snapshot();

-- which messenger an actor last spoke from; delivery needs it to route notifications
CREATE TABLE IF NOT EXISTS actor_platforms(
 actor bigint PRIMARY KEY,
 platform text NOT NULL CHECK (platform IN ('telegram','max')),
 updated_at timestamptz NOT NULL DEFAULT now());

-- Телефон можно исправить, не ломая снимок брони: правка живёт отдельной записью
CREATE TABLE IF NOT EXISTS booking_contacts(
 booking_id text PRIMARY KEY REFERENCES bookings(id),
 phone text NOT NULL,
 updated_by bigint NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now());

-- Одно поле, которое бот ждёт от гостя прямо сейчас (например, новый телефон брони)
CREATE TABLE IF NOT EXISTS pending_inputs(
 actor bigint PRIMARY KEY,
 kind text NOT NULL,
 ref text,
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
