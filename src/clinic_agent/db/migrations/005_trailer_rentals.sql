-- Trailer-rental schema. Independent of the clinic tables.
--
-- The load-bearing piece mirrors the appointments exclusion constraint, but over whole
-- days: a physical trailer unit cannot be booked for two overlapping date ranges.
-- `rental_range` is an inclusive daterange '[]' (you hold the trailer through the return
-- day), so the next rental of the same unit can start the following day.

CREATE TABLE IF NOT EXISTS trailer_types (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    daily_rate  NUMERIC(10,2) NOT NULL CHECK (daily_rate > 0),
    deposit     NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (deposit >= 0),
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS trailer_units (
    id              SERIAL PRIMARY KEY,
    trailer_type_id INT NOT NULL REFERENCES trailer_types(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (trailer_type_id, label)
);

CREATE TABLE IF NOT EXISTS customers (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL UNIQUE,
    email      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$ BEGIN
    CREATE TYPE rental_status AS ENUM ('booked', 'cancelled', 'completed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS rentals (
    id              SERIAL PRIMARY KEY,
    trailer_unit_id INT NOT NULL REFERENCES trailer_units(id),
    customer_id     INT NOT NULL REFERENCES customers(id),
    pickup_date     DATE NOT NULL,
    return_date     DATE NOT NULL,
    rental_range    DATERANGE NOT NULL,
    daily_rate      NUMERIC(10,2) NOT NULL,
    deposit         NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_cost      NUMERIC(10,2) NOT NULL,
    status          rental_status NOT NULL DEFAULT 'booked',
    source          TEXT NOT NULL DEFAULT 'voice_agent',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (pickup_date <= return_date),
    -- The application supplies the range; this guarantees it matches the dates, so the
    -- exclusion constraint enforces real overlap and not something the app mis-built.
    CONSTRAINT rental_range_matches_dates
        CHECK (rental_range = daterange(pickup_date, return_date, '[]')),

    -- One unit, no two overlapping booked rentals.
    CONSTRAINT no_double_rental EXCLUDE USING gist (
        trailer_unit_id WITH =,
        rental_range WITH &&
    ) WHERE (status = 'booked')
);

CREATE INDEX IF NOT EXISTS rentals_unit_range
    ON rentals USING gist (trailer_unit_id, rental_range) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS rentals_pickup ON rentals (pickup_date);
