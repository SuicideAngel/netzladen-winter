"""Regression checks for planning independently of Home Assistant."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_module(name):
    path = Path(__file__).resolve().parents[1] / "custom_components" / "netzladen_winter" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = load_module("planner")
DEFAULTS = load_module("const").DEFAULTS


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 0, 0, tzinfo=timezone(timedelta(hours=1)))
        self.settings = {**DEFAULTS, "load_source": "direct", "pv_today": "sensor.pv"}
        self.readings = {"min_soc": 5, "max_soc": 100, "battery_soc": 20, "house_energy": 100}
        self.rows = [
            {"period_start": self.now.replace(hour=hour).isoformat(),
             "pv_estimate": 2 if 8 <= hour < 16 else 0,
             "pv_estimate10": 0.4 if 8 <= hour < 16 else 0}
            for hour in range(24)
        ]
        self.weather = [
            {"datetime": self.now.replace(hour=hour).isoformat(), "temperature": 3,
             "cloud_coverage": 100, "humidity": 90, "condition": "cloudy"}
            for hour in range(24)
        ]

    def plan(self, previous=None, owned=False, price_ct=None, charging=False):
        return planner.calculate(
            self.now, self.settings, lambda key, kind: self.readings.get(key),
            {"sensor.pv": SimpleNamespace(state="16", attributes={"detailedHourly": self.rows})},
            self.weather, previous or {}, owned, price_ct=price_ct, charging=charging,
        )

    def test_dynamic_tariff_starts_below_limit_and_stops_above_it(self):
        self.now += timedelta(hours=13)
        self.settings.update(tariff_mode="price", price_limit_ct=20, buffer_kwh=8)
        limit_price = planner.price_in_cents("0.20", "EUR/kWh")
        idle = self.plan(price_ct=limit_price)
        self.assertEqual(idle["command"], "idle")
        self.assertFalse(idle["charging_allowed"])
        self.assertEqual(idle["electricity_price_ct"], 20)

        start = self.plan(previous=idle, price_ct=19.99)
        self.assertEqual(start["command"], "charge")
        self.assertTrue(start["charging_allowed"])

        continued = self.plan(previous=start, owned=True, price_ct=limit_price, charging=True)
        self.assertEqual(continued["command"], "charge")
        self.assertTrue(continued["charging_allowed"])

        released = self.plan(previous=continued, owned=True, price_ct=20.01, charging=True)
        self.assertEqual(released["command"], "release")
        self.assertFalse(released["charging_allowed"])
        self.assertEqual(self.plan(previous=released, price_ct=limit_price)["command"], "idle")
        self.assertEqual(self.plan(owned=True, charging=True)["command"], "release")
        self.assertEqual(self.plan()["command"], "idle")

    def test_dynamic_tariff_checks_active_months_and_target(self):
        self.settings.update(tariff_mode="price", price_limit_ct=20)
        self.readings["battery_soc"] = 100
        self.assertEqual(self.plan(owned=True, price_ct=15)["command"], "release")
        self.settings["winter_months"] = ["12"]
        self.assertFalse(self.plan(price_ct=-5)["charging_allowed"])

    def test_price_units_and_legacy_fixed_window(self):
        self.assertEqual(planner.price_in_cents("20", "ct/kWh"), 20)
        self.assertEqual(planner.price_in_cents("0.07", "€ / kWh"), 7)
        self.assertEqual(planner.price_in_cents("-50", "EUR/MWh"), -5)
        for state, unit in (("unknown", "EUR/kWh"), ("NaN", "ct/kWh"),
                            ("1", "USD/kWh"), ("1", None)):
            with self.subTest(state=state, unit=unit):
                self.assertIsNone(planner.price_in_cents(state, unit))

        self.settings.pop("tariff_mode")
        self.assertTrue(self.plan(price_ct=100)["charging_allowed"])
        self.now += timedelta(hours=5)
        self.assertFalse(self.plan(price_ct=-100)["charging_allowed"])

    def test_learning_without_wallbox_uses_direct_meter(self):
        first = self.plan()
        self.now += timedelta(hours=1)
        self.readings["house_energy"] += 1.75
        result = self.plan(first)
        self.assertEqual(result["hours"][0][3], 1.75)
        self.assertEqual(result["wp_profile"], [0] * 24)

    def test_wallbox_subtracted_and_missing_reading_skips_hour(self):
        self.settings["has_wallbox"] = True
        self.readings["wallbox_energy"] = 50
        first = self.plan()
        self.now += timedelta(hours=1)
        self.readings.update(house_energy=105, wallbox_energy=53)
        second = self.plan(first)
        self.assertEqual(second["hours"][0][3], 2)
        self.now += timedelta(hours=1)
        self.readings.pop("wallbox_energy")
        self.assertEqual(len(self.plan(second)["hours"]), 1)

    def test_optional_second_pv_meter_in_energy_balance(self):
        self.settings["load_source"] = "balance"
        self.readings.update(dict.fromkeys(
            ("pv_energy", "grid_import", "grid_export", "battery_charge", "battery_discharge"), 100
        ))
        first = self.plan()
        self.now += timedelta(hours=1)
        self.readings.update(pv_energy=101, grid_import=102, battery_charge=100.5)
        self.assertEqual(self.plan(first)["hours"][0][3], 2.5)

    def test_missing_forecast_does_not_request_charge(self):
        self.rows.pop()
        self.assertEqual(self.plan()["command"], "idle")
        self.assertEqual(self.plan(owned=True)["command"], "release")
        self.assertIsNone(self.plan()["target"])

    def test_accepts_native_datetime_forecast_values(self):
        for row in self.rows:
            row["period_start"] = datetime.fromisoformat(row["period_start"]).astimezone(timezone.utc)
        for row in self.weather:
            row["datetime"] = datetime.fromisoformat(row["datetime"])
        result = self.plan()
        self.assertTrue(result["forecast_valid"])
        self.assertTrue(result["weather_valid"])
        self.assertIsNotNone(result["target"])

    def test_fog_lowers_pv_and_higher_reserve_can_start_charge(self):
        baseline = self.plan()
        self.assertAlmostEqual(baseline["pv_kwh"], 8.32)
        self.weather[7]["condition"] = "fog"
        for row in self.rows:
            row["pv_estimate10"] /= 2
        result = self.plan()
        self.assertLess(result["pv_kwh"], baseline["pv_kwh"])
        self.assertGreaterEqual(result["target"], baseline["target"])
        self.assertEqual(result["command"], "charge")

    def test_charge_power_affects_start_time_and_end_time_releases(self):
        self.settings.update(end_time="03:00:00", capacity_kwh=20, charge_w=12000)
        self.readings["battery_soc"] = 20
        for row in self.rows:
            row["pv_estimate"] *= 3
            row["pv_estimate10"] *= 3
        first = self.plan()
        self.assertGreater(first["target"], 20)
        self.assertLess(first["target"], 100)
        self.assertEqual(first["command"], "hold")
        self.settings["charge_w"] = 100
        self.assertEqual(self.plan()["command"], "charge")
        self.now += timedelta(hours=3)
        self.assertEqual(self.plan(owned=True)["command"], "release")

    def test_storage_and_grid_charge_limits_are_independent(self):
        self.settings["charge_w"] = 3500
        self.assertEqual(self.plan()["max_charge_w"], 3500)
        self.settings["storage_charge_w"] = 5000
        self.assertEqual(self.plan()["max_charge_w"], 3500)
        self.assertEqual(self.plan()["storage_charge_w"], 5000)
        self.settings["charge_w"] = 10000
        self.assertEqual(self.plan()["max_charge_w"], 5000)

    def test_repeated_hour_still_accepts_complete_forecast(self):
        try:
            tz = ZoneInfo("Europe/Berlin")
        except ZoneInfoNotFoundError:
            self.skipTest("system time zone data unavailable")
        self.now = datetime(2026, 10, 25, tzinfo=tz)
        self.rows = [
            {"period_start": datetime.fromtimestamp(self.now.timestamp() + 3600 * hour, tz).isoformat(),
             "pv_estimate": 1}
            for hour in range(25)
        ]
        self.assertTrue(self.plan()["forecast_valid"])
        self.settings.update(winter_months=["10"], end_time="02:30:00")
        self.now = self.now.replace(hour=2, minute=15, fold=1)
        self.assertEqual(self.plan(owned=True)["command"], "release")


if __name__ == "__main__":
    unittest.main()
