"""Apply a plan through selected Home Assistant entities."""

import asyncio
import logging
import math

from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONTROL_OPTIONS, CONTROL_READBACK, CONTROL_TARGETS, DOMAIN
from .planner import finite, in_window

LOGGER = logging.getLogger(__name__)
UNAVAILABLE = (None, "unknown", "unavailable")


class BatteryControl:
    """Remember which controls were taken over and release them after a restart."""

    def __init__(self, hass, entry_id, settings):
        self.hass = hass
        self.settings = settings
        self.store = Store(hass, 1, f"{DOMAIN}.control", private=True, atomic_writes=True)
        self.recovery = None
        self.releasing = False
        self.stopping = False
        self.notification = f"{DOMAIN}_{entry_id}_release"

    async def async_load(self):
        self.recovery = await self.store.async_load() or None
        self.releasing = self.recovery is not None

    def state(self, entity_id):
        obj = self.hass.states.get(entity_id)
        return obj.state if obj else None

    def number_value(self, entity_id, value, unit):
        obj = self.hass.states.get(entity_id)
        if obj is None or obj.state in UNAVAILABLE:
            raise HomeAssistantError(f"{entity_id} ist nicht verfügbar")
        entity_unit = obj.attributes.get("unit_of_measurement") or unit
        if unit == "W" and entity_unit == "kW":
            value /= 1000
        elif entity_unit != unit:
            raise HomeAssistantError(f"{entity_id} hat eine ungeeignete Einheit")
        return value

    def prepare_number(self, entity_id, value):
        obj = self.hass.states.get(entity_id)
        attributes = obj.attributes if obj else {}
        minimum, maximum = finite(attributes.get("min")), finite(attributes.get("max"))
        step = finite(attributes.get("step"), 0)
        if minimum is None or maximum is None or not minimum <= value <= maximum:
            raise HomeAssistantError(f"{entity_id} akzeptiert den Sollwert {value:g} nicht")
        if step and not math.isclose((value - minimum) / step, round((value - minimum) / step),
                                     abs_tol=1e-6, rel_tol=0):
            raise HomeAssistantError(f"{entity_id} unterstützt die Schrittweite für {value:g} nicht")

    def prepare_select(self, entity_id, *options):
        obj = self.hass.states.get(entity_id)
        if obj is None or obj.state in UNAVAILABLE or not set(options) <= set(obj.attributes.get("options", [])):
            raise HomeAssistantError(f"{entity_id} bietet die eingestellten Optionen nicht an")

    async def async_write(self, entity_id, value):
        domain = entity_id.split(".", 1)[0]
        if isinstance(value, str):
            action, data = "select_option", {"option": value}
            self.prepare_select(entity_id, value)
            if self.state(entity_id) == value:
                return
        else:
            action, data = "set_value", {"value": value}
            self.prepare_number(entity_id, value)
            if finite(self.state(entity_id)) == value:
                return
        async with asyncio.timeout(8):
            await self.hass.services.async_call(
                domain, action, {"entity_id": entity_id, **data}, blocking=True,
            )

    async def async_confirm(self, checks):
        async with asyncio.timeout(12):
            while any(self.state(entity) != value for entity, value in checks.items()):
                await asyncio.sleep(0.5)

    def can_control(self):
        automation = self.settings.get("interlock_automation")
        return (not self.stopping and self.settings["control_enabled"]
                and in_window(self.settings, dt_util.now())
                and (not automation or self.state(automation) == "off"))

    async def async_apply(self, command):
        if self.releasing:
            return "idle" if await self.async_release() else "release_pending"
        if command not in ("charge", "hold") or not self.can_control():
            if self.recovery and not await self.async_release():
                return "release_pending"
            if self.settings["control_enabled"] and command in ("charge", "hold"):
                return "blocked"
            return "idle" if self.settings["control_enabled"] else "preview"

        cfg = self.settings
        try:
            limit = self.number_value(cfg["charge_limit"], cfg["charge_w"], "W")
            power = self.number_value(cfg["forced_power"], cfg["charge_w"], "W")
            self.number_value(cfg["max_soc_control"], 100, "%")
            self.prepare_number(cfg["charge_limit"], limit)
            self.prepare_number(cfg["forced_power"], power)
            self.prepare_number(cfg["forced_power"], 0)
            self.prepare_number(cfg["max_soc_control"], 100)
            self.prepare_select(cfg["ems_control"], cfg["forced_mode"], cfg["normal_mode"])
            self.prepare_select(cfg["command_control"], cfg["charge_option"], cfg["stop_option"])
            if self.recovery is None:
                if (self.state(cfg["ems_state"]) != cfg["normal_mode"]
                        or self.state(cfg["command_state"]) != cfg["stop_option"]
                        or self.state(cfg["ems_control"]) != cfg["normal_mode"]
                        or self.state(cfg["command_control"]) != cfg["stop_option"]):
                    return "blocked"
                old_limit = finite(self.state(cfg["charge_limit"]))
                old_max = finite(self.state(cfg["max_soc_control"]))
                if old_limit is None or old_max is None:
                    return "blocked"
                recovery = {key: cfg[key] for key in (*CONTROL_TARGETS, *CONTROL_READBACK, *CONTROL_OPTIONS)}
                recovery.update(old_limit=old_limit, old_max=old_max)
                await self.store.async_save(recovery)
                if await self.store.async_load() != recovery:
                    raise HomeAssistantError("Vorherige Wechselrichtereinstellungen konnten nicht gesichert werden")
                self.recovery = recovery
            elif (self.state(cfg["ems_state"]) not in (cfg["forced_mode"], cfg["normal_mode"])
                  or self.state(cfg["command_state"]) not in (cfg["charge_option"], cfg["stop_option"])):
                raise HomeAssistantError("Unerwarteter Betriebszustand am Wechselrichter")

            if command == "hold":
                await self.async_write(cfg["command_control"], cfg["stop_option"])
            await self.async_write(cfg["charge_limit"], limit)
            await self.async_write(cfg["max_soc_control"], 100)
            await self.async_write(cfg["ems_control"], cfg["forced_mode"])
            await self.async_confirm({cfg["ems_state"]: cfg["forced_mode"]})
            if command == "charge":
                await self.async_write(cfg["forced_power"], power)
                if not self.can_control():
                    return "idle" if await self.async_release() else "release_pending"
                await self.async_write(cfg["command_control"], cfg["charge_option"])
                await self.async_confirm({cfg["command_state"]: cfg["charge_option"]})
            else:
                await self.async_confirm({cfg["command_state"]: cfg["stop_option"]})
            return command
        except (HomeAssistantError, TimeoutError, OSError) as err:
            LOGGER.warning("Wechselrichtersteuerung fehlgeschlagen: %s", err)
            if self.recovery and not await self.async_release():
                return "release_pending"
            return "blocked"

    async def async_release(self):
        cfg = self.recovery
        if cfg is None:
            return True
        self.releasing = True
        try:
            # Try each stop action even if another service is temporarily down.
            for key, value in (("command_control", cfg["stop_option"]), ("forced_power", 0),
                               ("ems_control", cfg["normal_mode"])):
                try:
                    await self.async_write(cfg[key], value)
                except (HomeAssistantError, TimeoutError) as err:
                    LOGGER.debug("Freigabe von %s: %s", cfg[key], err)
            await self.async_confirm({
                cfg["ems_state"]: cfg["normal_mode"], cfg["command_state"]: cfg["stop_option"],
            })
            await self.async_write(cfg["charge_limit"], cfg["old_limit"])
            await self.async_write(cfg["max_soc_control"], cfg["old_max"])
            await self.store.async_remove()
        except (HomeAssistantError, TimeoutError, OSError) as err:
            LOGGER.warning("Freigabe des Wechselrichters steht aus: %s", err)
            persistent_notification.async_create(
                self.hass,
                "Der Wechselrichter konnte noch nicht vollständig freigegeben werden. "
                "Netzladen Winter versucht es erneut. Bitte den Wechselrichterzustand prüfen.",
                title="Netzladen Winter: Freigabe ausstehend", notification_id=self.notification,
            )
            return False
        self.recovery = None
        self.releasing = False
        persistent_notification.async_dismiss(self.hass, self.notification)
        return True
