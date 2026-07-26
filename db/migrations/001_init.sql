-- Victron SmartSolar MPPT dashboard schema.
--
-- No optimizer/command tables here - this server is read-only monitoring,
-- not control. Decoding of the BLE "Instant Readout" protocol happens on
-- the ESP32 itself (Fabian-Schmidt/esphome-victron_ble - see
-- esphome-victron-ble/*.yaml), so unlike the earlier EcoWorthy attempt
-- there's no raw-payload table here: the server only ever receives
-- already-decoded sensor values over the ESPHome native API.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per Victron device we've ever seen data from, identified by
-- BLE MAC. Only the SmartSolar MPPT is targeted today, but nothing here
-- assumes exactly one device - the ESP32's victron_ble component
-- supports multiple devices, so adding a second target later is just
-- another entry in esphome-victron-ble/*.yaml plus another row here.
CREATE TABLE devices (
    id SERIAL PRIMARY KEY,
    mac TEXT UNIQUE NOT NULL,
    name TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ
);

-- Decoded readings, one row per sensor update received from the ESP32.
-- Fields match what the SmartSolar's BLE "Instant Readout" broadcast
-- carries for a solar charger (see the victron_ble component's
-- SolarChargerData): battery voltage/charging current, current PV power,
-- today's yield, and load output current (only present on models with a
-- load output terminal). device_state/charger_error are the controller's
-- own status text (e.g. "Bulk"/"Absorption"/"Float", "No error").
CREATE TABLE readings (
    time TIMESTAMPTZ NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    battery_voltage_v DOUBLE PRECISION,
    battery_current_a DOUBLE PRECISION,
    pv_power_w DOUBLE PRECISION,
    yield_today_kwh DOUBLE PRECISION,
    load_current_a DOUBLE PRECISION,
    device_state TEXT,
    charger_error TEXT
);
SELECT create_hypertable('readings', 'time');
CREATE INDEX ON readings (device_id, time DESC);
