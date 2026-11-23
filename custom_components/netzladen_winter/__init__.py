"""Home Assistant integration for Netzladen Winter."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import WinterCoordinator

PLATFORMS = (Platform.SENSOR,)


async def async_setup_entry(hass, entry):
    coordinator = WinterCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(async_track_time_change(hass, coordinator.async_cycle, second=0))
    if coordinator.settings["tariff_mode"] == "price" and (price_sensor := coordinator.settings.get("price_sensor")):
        entry.async_on_unload(
            async_track_state_change_event(hass, price_sensor, coordinator.async_price_changed)
        )
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop))
    entry.async_on_unload(async_at_started(hass, coordinator.async_cycle))
    return True


async def async_unload_entry(hass, entry):
    if not await entry.runtime_data.async_stop():
        return False
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass, entry):
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.plan").async_remove()
