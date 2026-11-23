"""Set up and edit Netzladen Winter through Home Assistant forms."""

from datetime import time

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONTROL_OPTIONS, CONTROL_READBACK, CONTROL_TARGETS, DEFAULTS, DOMAIN, NAME
from .planner import BALANCE


def entity(*domains):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=list(domains)))


def number(minimum, maximum, unit, step=1):
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, unit_of_measurement=unit,
        mode=selector.NumberSelectorMode.BOX,
    ))


def select(values, *, multiple=False, translation_key=None):
    config = selector.SelectSelectorConfig(
        options=list(values), multiple=multiple,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
    if translation_key is not None:
        config["translation_key"] = translation_key
    return selector.SelectSelector(config)


class SetupForms:
    """Use the same sequential forms for setup and options."""

    def __init__(self):
        self.values = {}

    def form(self, step, fields, optional=(), user_input=None, errors=None):
        suggested = {**DEFAULTS, **self.saved, **self.values, **(user_input or {})}
        schema = vol.Schema({
            (vol.Optional(key) if key in optional else vol.Required(key)): field
            for key, field in fields.items()
        })
        return self.async_show_form(
            step_id=step, data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors or {},
        )

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self.values.update(user_input)
            return await self.async_step_consumption()
        return self.form("user", {
            "battery_soc": entity("sensor"),
            "min_soc": entity("sensor", "number"),
            "max_soc": entity("sensor", "number"),
            "capacity_kwh": number(1, 1000, "kWh", 0.1),
            "capacity_sensor": entity("sensor"),
            "backup_percent": number(0, 100, "%", 0.1),
            "backup_soc": entity("sensor", "number"),
            "usable_soc": selector.BooleanSelector(),
            "load_source": select(("balance", "direct"), translation_key="load_source"),
            "has_heat_pump": selector.BooleanSelector(),
            "has_wallbox": selector.BooleanSelector(),
        }, ("capacity_sensor", "backup_soc"))

    async def async_step_consumption(self, user_input=None):
        fields = {"house_energy": entity("sensor")} if self.values["load_source"] == "direct" else {
            key: entity("sensor") for key in BALANCE
        }
        if self.values["has_heat_pump"]:
            fields["heat_pump_energy"] = entity("sensor")
        if self.values["has_wallbox"]:
            fields["wallbox_energy"] = entity("sensor")
        if user_input is not None and len(set(user_input.values())) == len(user_input):
            self.values.update(user_input)
            return await self.async_step_planning()
        return self.form("consumption", fields, ("second_pv_energy",), user_input,
                         {"base": "duplicate_entities"} if user_input is not None else None)

    async def async_step_planning(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("pv_tomorrow") == user_input["pv_today"]:
                errors["pv_tomorrow"] = "duplicate_entities"
            if not user_input["winter_months"]:
                errors["winter_months"] = "no_months"
            if not errors:
                self.values.update(user_input)
                return await self.async_step_tariff()
        return self.form("planning", {
            "pv_today": entity("sensor"),
            "pv_tomorrow": entity("sensor"),
            "weather": entity("weather"),
            "outdoor_temperature": entity("sensor"),
            "charge_w": number(1, 50000, "W"),
            "low_pv_kwh": number(0, 100, "kWh", 0.1),
            "buffer_kwh": number(0, 100, "kWh", 0.1),
            "fallback_load_kw": number(0.1, 50, "kW", 0.1),
            "winter_months": select((str(month) for month in range(1, 13)), multiple=True),
        }, ("pv_tomorrow", "weather", "outdoor_temperature"), user_input, errors)

    async def async_step_tariff(self, user_input=None):
        if user_input is not None:
            self.values.update(user_input)
            if self.values["tariff_mode"] == "price":
                return await self.async_step_price_tariff()
            return await self.async_step_fixed_tariff()
        return self.form("tariff", {
            "tariff_mode": select(("fixed", "price"), translation_key="tariff_mode"),
        })

    async def async_step_fixed_tariff(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                end = time.fromisoformat(user_input["end_time"])
                if end.second or end.microsecond or end.tzinfo or end == time(0):
                    raise ValueError
            except (TypeError, ValueError):
                errors["end_time"] = "invalid_time"
            else:
                self.values.update(user_input)
                return await self.async_step_operation()
        return self.form("fixed_tariff", {"end_time": selector.TimeSelector()},
                         user_input=user_input, errors=errors)

    async def async_step_price_tariff(self, user_input=None):
        if user_input is not None:
            self.values.update(user_input)
            return await self.async_step_operation()
        return self.form("price_tariff", {
            "price_sensor": entity("sensor"),
            "price_limit_ct": number(-1000, 1000, "ct/kWh", 0.01),
        })

    async def async_step_operation(self, user_input=None):
        if user_input is not None:
            self.values.update(user_input)
            if self.values["control_enabled"]:
                return await self.async_step_controls()
            return self.finish()
        return self.form("operation", {
            "control_enabled": selector.BooleanSelector(),
        })

    async def async_step_controls(self, user_input=None):
        fields = {
            **{key: entity("number", "input_number") for key in CONTROL_TARGETS[:3]},
            **{key: entity("select", "input_select") for key in CONTROL_TARGETS[3:]},
            **{key: entity("sensor", "select") for key in CONTROL_READBACK},
            **{key: selector.TextSelector() for key in CONTROL_OPTIONS},
            "interlock_automation": entity("automation"),
        }
        errors = {}
        if user_input is not None:
            targets = [user_input[key] for key in CONTROL_TARGETS]
            if len(set(targets)) != len(targets) or any(
                self.values.get(key) in targets for key in ("min_soc", "backup_soc", "battery_soc")
            ):
                errors["base"] = "duplicate_entities"
            if user_input["ems_state"] == user_input["command_state"]:
                errors["base"] = "duplicate_entities"
            for enabled, disabled in (("forced_mode", "normal_mode"), ("charge_option", "stop_option")):
                if (not user_input[enabled].strip() or not user_input[disabled].strip()
                        or user_input[enabled] == user_input[disabled]):
                    errors[enabled] = "invalid_options"
            if not errors:
                self.values.update(user_input)
                return self.finish()
        return self.form("controls", fields, ("interlock_automation",), user_input, errors)


class ConfigFlow(SetupForms, config_entries.ConfigFlow, domain=DOMAIN):
    """Set up one battery controller per Home Assistant instance."""

    VERSION = 1
    saved = {}

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await super().async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    def finish(self):
        return self.async_create_entry(title=NAME, data=self.values)


class OptionsFlow(SetupForms, config_entries.OptionsFlowWithReload):
    """Update all sensor assignments and operating parameters."""

    @property
    def saved(self):
        return self.config_entry.options or self.config_entry.data

    def finish(self):
        return self.async_create_entry(title="", data=self.values)
