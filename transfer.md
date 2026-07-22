# EcoWorthy BW0F ground-truth probe.
# Connects to the battery's JBD BMS over BLE (independent of, and does not
# conflict with, the BW0F's own WiFi push connection) and exposes decoded
# values at http://<esp32-ip>/ so you can correlate them against the raw
# WiFi frames your TCP listener is capturing.

substitutions:
  name: ecoworthy-ble-probe
  external_components_source: github://syssi/esphome-jbd-bms@main

esphome:
  name: ${name}
  friendly_name: ${name}

esp32:
  # CHANGE THIS to match your actual board (e.g. esp32dev, esp32-s3-devkitc-1, etc.)
  board: esp32dev
  framework:
    type: esp-idf

external_components:
  - source: ${external_components_source}
    refresh: 0s

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  # Reconnect logic matters here since this rides the same weak link
  # as the HaLow camera backhaul.
  reboot_timeout: 0s
  power_save_mode: none

ota:
  platform: esphome

logger:
  level: DEBUG

api:

web_server:
  port: 80

esp32_ble_tracker:
  scan_parameters:
    active: false

ble_client:
  - id: client0
    # Best guess pulled from the MAC seen at the start of your WiFi push
    # frames -- WiFi and BLE radios on a combo chip don't always share
    # the same MAC, so if this fails to connect, BLE-scan the module
    # directly (e.g. nRF Connect app) and use the real BLE MAC instead.
    mac_address: !secret bms0_mac_address

jbd_bms_ble:
  - id: bms0
    ble_client_id: client0
    update_interval: 5s

sensor:
  - platform: jbd_bms_ble
    jbd_bms_ble_id: bms0
    battery_strings:
      name: "battery strings"
    current:
      name: "current"
    state_of_charge:
      name: "state of charge"
    nominal_capacity:
      name: "nominal capacity"
    capacity_remaining:
      name: "capacity remaining"
    total_voltage:
      name: "total voltage"
    average_cell_voltage:
      name: "average cell voltage"
    min_cell_voltage:
      name: "min cell voltage"
    max_cell_voltage:
      name: "max cell voltage"
    temperature_1:
      name: "temperature 1"
    temperature_2:
      name: "temperature 2"
    temperature_3:
      name: "temperature 3"
    temperature_4:
      name: "temperature 4"
    temperature_5:
      name: "temperature 5"
    cell_voltage_1:
      name: "cell voltage 1"
    cell_voltage_2:
      name: "cell voltage 2"
    cell_voltage_3:
      name: "cell voltage 3"
    cell_voltage_4:
      name: "cell voltage 4"
    cell_voltage_5:
      name: "cell voltage 5"
    cell_voltage_6:
      name: "cell voltage 6"
    cell_voltage_7:
      name: "cell voltage 7"
    cell_voltage_8:
      name: "cell voltage 8"

text_sensor:
  - platform: jbd_bms_ble
    jbd_bms_ble_id: bms0
    device_model:
      name: "device model"
    operation_status:
      name: "operation status"
