"""Choose relay entities inside Home Assistant without elevating control rights."""
from __future__ import annotations

from typing import Any

try:
    import probatio as schema
except ImportError:  # Stable releases before the September 2026 schema migration.
    import voluptuous as schema

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import RelayApi, RelayAuthError, RelayError, RelayPayloadError, RelayRateLimitError
from .const import DOMAIN, DEFAULT_SERVER_URL, CONF_ENTITY_IDS, CONF_LICENSE_ID, CONF_MODE, CONF_SERVER_URL, CONF_TOKEN, MODE_AUTOMATIC, MODE_AUTOMATION


def connection_schema() -> Any:
    return schema.Schema({
        schema.Required(CONF_TOKEN): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    })


def entities_schema(selected: list[str]) -> Any:
    return schema.Schema({
        schema.Required(CONF_ENTITY_IDS, default=selected): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
    })


class RelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One config entry per stable service license ID."""

    VERSION = 1
    _connection: dict[str, Any]
    _remote: dict[str, Any]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        return self.async_show_menu(step_id="user", menu_options=["automation", "automatic"])

    async def async_step_automation(self, user_input: dict[str, Any] | None = None) -> Any:
        await self.async_set_unique_id("ui-automation-helper")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Relay UI automation helper", data={CONF_MODE: MODE_AUTOMATION})

    async def async_step_automatic(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                api = RelayApi(async_get_clientsession(self.hass), DEFAULT_SERVER_URL, user_input[CONF_TOKEN])
                remote = await api.config()
                license_id = int(remote[CONF_LICENSE_ID])
                await self.async_set_unique_id(str(license_id))
                self._abort_if_unique_id_configured()
                self._connection = {CONF_SERVER_URL: api.server_url, CONF_TOKEN: api._token, CONF_LICENSE_ID: license_id, CONF_MODE: MODE_AUTOMATIC}
                self._remote = remote
                return await self.async_step_entities()
            except RelayAuthError:
                errors["base"] = "invalid_auth"
            except RelayRateLimitError:
                errors["base"] = "rate_limited"
            except (RelayError, KeyError, TypeError, ValueError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="automatic", data_schema=connection_schema(), errors=errors)

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input[CONF_ENTITY_IDS]
            if not selected or len(selected) > int(self._remote["entity_limit"]):
                errors["base"] = "entity_limit"
            else:
                try:
                    api = RelayApi(async_get_clientsession(self.hass), self._connection[CONF_SERVER_URL], self._connection[CONF_TOKEN])
                    await api.config(selected)
                    return self.async_create_entry(title=f"My Home Assistant relay {self._connection[CONF_LICENSE_ID]}", data=self._connection, options={CONF_ENTITY_IDS: selected})
                except RelayAuthError:
                    errors["base"] = "invalid_auth"
                except RelayRateLimitError:
                    errors["base"] = "rate_limited"
                except RelayPayloadError:
                    errors["base"] = "entity_limit"
                except RelayError:
                    errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="entities", data_schema=entities_schema(self._remote.get(CONF_ENTITY_IDS, [])), errors=errors, description_placeholders={"limit": str(self._remote["entity_limit"])})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> Any:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                api = RelayApi(async_get_clientsession(self.hass), entry.data[CONF_SERVER_URL], user_input[CONF_TOKEN])
                remote = await api.config()
                await self.async_set_unique_id(str(remote[CONF_LICENSE_ID]))
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: api._token})
            except RelayAuthError:
                errors["base"] = "invalid_auth"
            except RelayRateLimitError:
                errors["base"] = "rate_limited"
            except (RelayError, KeyError, TypeError, ValueError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema.Schema({schema.Required(CONF_TOKEN): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> RelayOptionsFlow:
        return RelayOptionsFlow()


class RelayOptionsFlow(config_entries.OptionsFlowWithReload):
    """Synchronize entity selections; sensitive permissions remain account-only."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATION:
            if user_input is not None:
                return self.async_create_entry(title="", data={})
            return self.async_show_form(step_id="automation", data_schema=schema.Schema({}), errors=errors)
        selected = entry.options.get(CONF_ENTITY_IDS, [])
        try:
            api = RelayApi(async_get_clientsession(self.hass), entry.data[CONF_SERVER_URL], entry.data[CONF_TOKEN])
            remote = await api.config()
            selected = remote.get(CONF_ENTITY_IDS, [])
            if user_input is not None:
                chosen = user_input[CONF_ENTITY_IDS]
                if not chosen or len(chosen) > int(remote["entity_limit"]):
                    errors["base"] = "entity_limit"
                else:
                    await api.config(chosen)
                    return self.async_create_entry(title="", data={CONF_ENTITY_IDS: chosen})
        except RelayAuthError:
            errors["base"] = "invalid_auth"
        except RelayRateLimitError:
            errors["base"] = "rate_limited"
        except RelayPayloadError:
            errors["base"] = "entity_limit"
        except (RelayError, KeyError, TypeError, ValueError):
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="init", data_schema=entities_schema(selected), errors=errors)
