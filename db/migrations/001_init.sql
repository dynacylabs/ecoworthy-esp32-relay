-- EcoWorthy BW0F dashboard schema. No optimizer/command tables here - this
-- server is read-only monitoring, not control.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per BLE device we've ever seen data from, identified by MAC.
-- Only the BW0F is targeted today, but nothing here assumes exactly one
-- device - the ESP32 proxy and the raw_events table are already
-- per-device, so adding a second target later is just another row here.
CREATE TABLE devices (
    id SERIAL PRIMARY KEY,
    mac TEXT UNIQUE NOT NULL,
    name TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ
);

-- Every raw notify/indicate payload, verbatim, before any decoding.
-- Source of truth: if the decoder is wrong or incomplete, this is what
-- lets us re-derive readings later without having lost anything.
CREATE TABLE raw_events (
    time TIMESTAMPTZ NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    characteristic TEXT NOT NULL,
    hex TEXT NOT NULL,
    len INTEGER NOT NULL
);
SELECT create_hypertable('raw_events', 'time');
CREATE INDEX ON raw_events (device_id, time DESC);

-- Decoded readings. Deliberately nullable-everything: the BW0F protocol
-- isn't reverse-engineered yet (see app/decode.py), so rows land here with
-- most/all fields NULL until decode.py actually knows how to fill them in.
-- Structure reflects what the dashboard wants to chart (power in/out,
-- battery state) - add columns as more of the protocol gets decoded.
CREATE TABLE readings (
    time TIMESTAMPTZ NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    battery_voltage_v DOUBLE PRECISION,
    battery_current_a DOUBLE PRECISION,
    battery_soc_pct DOUBLE PRECISION,
    pv_voltage_v DOUBLE PRECISION,
    pv_current_a DOUBLE PRECISION,
    pv_power_w DOUBLE PRECISION,
    load_voltage_v DOUBLE PRECISION,
    load_current_a DOUBLE PRECISION,
    load_power_w DOUBLE PRECISION,
    temperature_c DOUBLE PRECISION
);
SELECT create_hypertable('readings', 'time');
CREATE INDEX ON readings (device_id, time DESC);
