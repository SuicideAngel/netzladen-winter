"""Read local entities, calculate the plan and synchronize the battery controller."""

import asyncio
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DEFAULTS, DOMAIN, LEARNING_KEYS, NAME
from .controls import BatteryControl, price_from_state
from .planner import calculate, finite

LOGGER = logging.getLogger(__name__)

def convert(value, unit, kind):
    value = finite(value)
    if value is None:
        return None
    if kind == "energy":
        factor = {None: 1, "kWh": 1, "Wh": 0.001, "MWh": 1000}.get(unit)
        return value * factor if factor is not None and value >= 0 else None
    if kind == "temperature":
        if unit == "°F":
            value = (value - 32) * 5 / 9
        elif unit == "K":
            value -= 273.15
        elif unit not in (None, "°C"):
            return None
        return finite(value, -40, 60)
    return value if unit in (None, "%") else None


class WinterCoordinator(DataUpdateCoordinator):
    """Run a serialized cycle at local minute boundaries."""

    def __init__(self, hass, entry):
        super().__init__(hass, LOGGER, name=NAME, config_entry=entry)
        self.settings = {**DEFAULTS, **(entry.options or entry.data)}
        self.control = BatteryControl(hass, entry.entry_id, self.settings)
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.plan", atomic_writes=True)
        self.learning_source = {key: self.settings.get(key) for key in LEARNING_KEYS}
        self.previous = {}
        self.lock = asyncio.Lock()
        self.stopping = False
        self.weather_hour = None
        self.weather_fetched = None
        self.weather_rows = []
        self.persisted_plan = {}

    async def async_load(self):
        await self.control.async_load()
        saved = await self.store.async_load() or {}
        if saved.get("learning_source") == self.learning_source:
            self.previous = saved.get("plan", {})
            self.persisted_plan = self.previous

    def read(self, key, kind):
        entity_id = self.settings.get(key)
        entity = self.hass.states.get(entity_id) if entity_id else None
        return convert(entity.state, entity.attributes.get("unit_of_measurement"), kind) if entity else None

    async def async_weather(self, now):
        entity_id = self.settings.get("weather")
        entity = self.hass.states.get(entity_id) if entity_id else None
        if entity is None or entity.state in ("unknown", "unavailable"):
            return []
        hour = int(now.timestamp() // 3600)
        if self.weather_hour != hour:
            self.weather_hour = hour
            try:
                async with asyncio.timeout(8):
                    result = await self.hass.services.async_call(
                        "weather", "get_forecasts", {"entity_id": entity_id, "type": "hourly"},
                        blocking=True, return_response=True,
                    )
                rows = result.get(entity_id, {}).get("forecast", []) if result else []
                if isinstance(rows, list) and rows:
                    unit = entity.attributes.get("temperature_unit")
                    self.weather_rows = [
                        {**row, "temperature": convert(row.get("temperature"), unit, "temperature")}
                        for row in rows if isinstance(row, dict)
                    ]
                    self.weather_fetched = now.timestamp()
            except (HomeAssistantError, TimeoutError):
                LOGGER.debug("Stündliche Wetterprognose nicht verfügbar", exc_info=True)
        if self.weather_fetched is not None and now.timestamp() - self.weather_fetched <= 6 * 3600:
            return self.weather_rows
        return []

    async def async_price_changed(self, event):
        if price_from_state(event.data["old_state"]) != price_from_state(event.data["new_state"]):
            await self.async_cycle()

    async def async_cycle(self, _event_time=None):
        async with self.lock:
            if self.stopping:
                if await self.control.async_release():
                    self.async_set_updated_data({**(self.data or {}), "operation": "idle"})
                else:
                    self.async_set_updated_data({**(self.data or {}), "operation": "release_pending"})
                return
            now = dt_util.now()
            owned = self.control.recovery is not None
            status = None
            try:
                if owned and (self.control.releasing or not self.control.can_control()):
                    status = "idle" if await self.control.async_release() else "release_pending"
                weather = await self.async_weather(now)
                entities = {}
                for key in ("pv_today", "pv_tomorrow"):
                    if entity_id := self.settings.get(key):
                        entities[entity_id] = self.hass.states.get(entity_id)
                charging = (self.control.is_charging() if self.settings["control_enabled"]
                            else self.previous.get("command") == "charge")
                plan = calculate(
                    now, self.settings, self.read, entities, weather, self.previous, owned,
                    price_ct=self.control.current_price(), charging=charging,
                )
                if status is None:
                    status = await self.control.async_apply(plan["command"])
                elif not self.settings["control_enabled"]:
                    status = "preview" if status == "idle" else status
                self.previous = plan
            except Exception:
                LOGGER.exception("Ladeplan konnte nicht berechnet oder ausgeführt werden")
                released = await self.control.async_release()
                self.async_set_updated_data({"operation": "error" if released else "release_pending"})
                return

            self.async_set_updated_data({**plan, "operation": status})
            stored = {key: plan[key] for key in (
                "date", "full_date", "snapshot", "hours", "daily_forecasts",
            )}
            if stored != self.persisted_plan:
                self.persisted_plan = stored
                self.store.async_delay_save(self.storage_data, 10)

    def storage_data(self):
        return {"learning_source": self.learning_source, "plan": self.persisted_plan}

    async def async_stop(self, _event=None):
        self.stopping = True
        self.control.stopping = True
        async with self.lock:
            released = await self.control.async_release()
            if self.persisted_plan:
                await self.store.async_save(self.storage_data())
            return released
