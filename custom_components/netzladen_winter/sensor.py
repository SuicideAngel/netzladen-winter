"""Display the planned target, conservative PV forecast and controller state."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, OPERATING_STATES

PLAN_ATTRIBUTES = (
    "date", "calculated_at", "reason", "command", "night_self_consumption",
    "tariff_mode", "charging_allowed", "electricity_price_ct", "price_limit_ct",
    "night_required_kwh", "night_load_kwh", "weather_valid", "wp_model_source",
    "wp_slope", "wp_profile", "weather_adjustment_kwh", "pv_kwh",
    "forecast_valid", "forecast_source", "max_charge_w", "storage_charge_w", "load_profile",
    "profile_counts", "forecast_today", "actual_today", "actual_today_hours",
    "forecast_yesterday", "actual_yesterday", "deviation_yesterday",
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([
        WinterSensor(coordinator, entry.entry_id, "target", PERCENTAGE, attributes=PLAN_ATTRIBUTES),
        WinterSensor(coordinator, entry.entry_id, "pv_kwh", UnitOfEnergy.KILO_WATT_HOUR,
                     SensorDeviceClass.ENERGY),
        WinterSensor(coordinator, entry.entry_id, "operation", device_class=SensorDeviceClass.ENUM,
                     options=list(OPERATING_STATES)),
    ])


class WinterSensor(CoordinatorEntity, SensorEntity):
    """Expose a single measurement from the current plan."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, key, unit=None, device_class=None, attributes=(), options=None):
        super().__init__(coordinator)
        self.key = key
        self.attributes = attributes
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_options = options

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self.key)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        if self.key == "pv_kwh":
            return data.get("pv_diagnostics")
        if self.attributes:
            return {key: data[key] for key in self.attributes if key in data}
        return None
