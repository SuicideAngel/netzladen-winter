"""Configuration shared by the UI and controller."""

DOMAIN = "netzladen_winter"
NAME = "Netzladen Winter"

DEFAULTS = {
    "capacity_kwh": 16.0,
    "backup_percent": 0.0,
    "usable_soc": True,
    "load_source": "balance",
    "has_heat_pump": False,
    "has_wallbox": False,
    "fallback_load_kw": 0.8,
    "charge_w": 3500,
    "tariff_mode": "fixed",
    "price_limit_ct": 20.0,
    "low_pv_kwh": 5.0,
    "buffer_kwh": 1.0,
    "end_time": "05:00:00",
    "winter_months": ["11", "12", "1", "2"],
    "control_enabled": False,
    "forced_mode": "Forced mode",
    "normal_mode": "Self-consumption mode (default)",
    "charge_option": "Forced charge",
    "stop_option": "Stop (default)",
}

CONTROL_TARGETS = ("charge_limit", "forced_power", "max_soc_control", "ems_control", "command_control")
CONTROL_READBACK = ("ems_state", "command_state")
CONTROL_OPTIONS = ("forced_mode", "normal_mode", "charge_option", "stop_option")
LEARNING_KEYS = (
    "load_source", "house_energy", "pv_energy", "second_pv_energy", "grid_import",
    "grid_export", "battery_charge", "battery_discharge", "has_wallbox",
    "wallbox_energy", "has_heat_pump", "heat_pump_energy", "outdoor_temperature",
)
OPERATING_STATES = ("preview", "idle", "hold", "charge", "blocked", "release_pending", "error")
