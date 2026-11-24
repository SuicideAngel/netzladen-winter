"""Exercise inverter acknowledgements with simulated Home Assistant entities."""

import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class HomeAssistantError(Exception):
    """Stand-in for the Home Assistant service error."""


class MockStore:
    """Retain recovery data until the controller explicitly removes it."""

    def __init__(self, *args, **kwargs):
        self.data = None

    async def async_save(self, data):
        self.data = data.copy()

    async def async_load(self):
        return self.data

    async def async_remove(self):
        self.data = None


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load_controls():
    namespace = "_netzladen_winter_test"
    component = Path(__file__).resolve().parents[1] / "custom_components" / "netzladen_winter"
    modules = {
        namespace: module(namespace, __path__=[str(component)]),
        "homeassistant": module("homeassistant"),
        "homeassistant.components": module("homeassistant.components", persistent_notification=SimpleNamespace(
            async_create=lambda *args, **kwargs: None, async_dismiss=lambda *args: None,
        )),
        "homeassistant.exceptions": module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError),
        "homeassistant.helpers": module("homeassistant.helpers"),
        "homeassistant.helpers.storage": module("homeassistant.helpers.storage", Store=MockStore),
        "homeassistant.util": module("homeassistant.util", dt=SimpleNamespace()),
    }
    with patch.dict(sys.modules, modules):
        controls = importlib.import_module(f"{namespace}.controls")
        defaults = importlib.import_module(f"{namespace}.const").DEFAULTS
    return controls, defaults


controls, DEFAULTS = load_controls()


class ControlTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, 2, tzinfo=timezone.utc)
        self.clock = patch.object(controls, "dt_util", SimpleNamespace(now=lambda: self.now, utcnow=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)

        async def missing_confirmation(_delay):
            # Polling means a mock entity did not acknowledge the requested value.
            raise TimeoutError

        timer = patch.object(controls, "asyncio", SimpleNamespace(
            timeout=asyncio.timeout, sleep=missing_confirmation,
        ))
        timer.start()
        self.addCleanup(timer.stop)

        self.settings = {
            **DEFAULTS, "control_enabled": True, "tariff_mode": "price",
            "price_sensor": "sensor.price", "storage_charge_w": 5000,
            **{key: f"mock.{key}" for key in (*controls.CONTROL_TARGETS, *controls.CONTROL_READBACK)},
        }
        cfg = self.settings
        self.states = {
            cfg["price_sensor"]: SimpleNamespace(state="12", last_reported=self.now,
                                                 attributes={"unit_of_measurement": "ct/kWh"}),
            cfg["charge_limit"]: self.number(4, "kW", 20, 0.1),
            cfg["forced_power"]: self.number(0, "W", 20000, 1),
            cfg["max_soc_control"]: self.number(85, "%", 100, 1),
            cfg["ems_control"]: self.select(cfg["normal_mode"], cfg["forced_mode"]),
            cfg["ems_state"]: self.select(cfg["normal_mode"]),
            cfg["command_control"]: self.select(cfg["stop_option"], cfg["charge_option"]),
            cfg["command_state"]: self.select(cfg["stop_option"]),
        }
        self.ignored = set()
        self.writes = []
        hass = SimpleNamespace(states=self.states, services=SimpleNamespace(async_call=self.write))
        self.control = controls.BatteryControl(hass, "test", cfg)

    @staticmethod
    def number(value, unit, max_value, step):
        return SimpleNamespace(
            state=str(value), attributes={"unit_of_measurement": unit, "min": 0, "max": max_value, "step": step},
        )

    @staticmethod
    def select(value, *others):
        return SimpleNamespace(state=value, attributes={"options": (value, *others)})

    async def write(self, domain, action, data, *, blocking):
        entity = data["entity_id"]
        value = data["value"] if action == "set_value" else data["option"]
        self.writes.append((entity, value))
        if entity in self.ignored:
            return
        self.states[entity].state = str(value)
        cfg = self.settings
        for key in ("ems", "command"):
            if entity == cfg[f"{key}_control"]:
                self.states[cfg[f"{key}_state"]].state = value

    async def test_each_unconfirmed_numeric_control_prevents_start(self):
        cfg = self.settings
        for key in ("charge_limit", "max_soc_control", "forced_power"):
            with self.subTest(control=key), self.assertLogs(controls.LOGGER, level="WARNING"):
                self.ignored = {cfg[key]}
                self.writes.clear()
                self.assertEqual(await self.control.async_apply("charge"), "blocked")
                self.assertNotIn((cfg["command_control"], cfg["charge_option"]), self.writes)
                self.assertIsNone(self.control.recovery)

    async def test_release_retains_recovery_until_regulators_confirm(self):
        cfg = self.settings
        self.assertEqual(await self.control.async_apply("charge"), "charge")
        self.assertTrue(self.control.matches(cfg["charge_limit"], 3.5))

        self.ignored = {cfg["forced_power"]}
        with self.assertLogs(controls.LOGGER, level="WARNING"):
            self.assertFalse(await self.control.async_release())
        self.assertIsNotNone(self.control.store.data)

        self.ignored = {cfg["max_soc_control"]}
        with self.assertLogs(controls.LOGGER, level="WARNING"):
            self.assertFalse(await self.control.async_release())
        self.assertIsNotNone(self.control.store.data)

        self.ignored.clear()
        self.assertTrue(await self.control.async_release())
        self.assertTrue(self.control.matches(cfg["forced_power"], 0))
        self.assertTrue(self.control.matches(cfg["charge_limit"], 4))
        self.assertTrue(self.control.matches(cfg["max_soc_control"], 85))
        self.assertIsNone(self.control.store.data)

    async def test_unchanged_but_recent_price_is_valid_and_expiry_releases(self):
        cfg = self.settings
        price = self.states[cfg["price_sensor"]]
        price.last_changed = self.now - timedelta(hours=6)
        price.last_reported = self.now - timedelta(minutes=90)
        self.assertEqual(await self.control.async_apply("charge"), "charge")
        self.now += timedelta(seconds=1)
        self.assertIsNone(self.control.current_price())
        self.assertEqual(await self.control.async_apply("charge"), "blocked")
        self.assertTrue(self.control.matches(cfg["command_state"], cfg["stop_option"]))
        self.assertIsNone(self.control.recovery)
