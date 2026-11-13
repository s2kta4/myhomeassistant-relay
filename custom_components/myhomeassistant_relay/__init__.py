"""My Home Assistant outbound relay custom integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RelayApi, RelayAuthError, RelayError
from .const import CONF_MODE, CONF_SERVER_URL, CONF_TOKEN, DOMAIN, MODE_AUTOMATIC, MODE_AUTOMATION, SERVICE_SYNC
from .coordinator import RelayCoordinator
from .automation import AutomationRelayManager

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the action before entries load so automations stay editable."""
    hass.data.setdefault(DOMAIN, {})

    async def sync(call: ServiceCall) -> None:
        manager = hass.data[DOMAIN].get("automation_manager")
        if manager is None:
            raise ServiceValidationError("Add My Home Assistant Relay and choose Use a UI automation first")
        await manager.async_sync(dict(call.data))

    hass.services.async_register(DOMAIN, SERVICE_SYNC, sync)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATION:
        manager = AutomationRelayManager(hass, entry)
        entry.runtime_data = manager
        hass.data.setdefault(DOMAIN, {})["automation_manager"] = manager
        return True
    api = RelayApi(async_get_clientsession(hass), entry.data[CONF_SERVER_URL], entry.data[CONF_TOKEN])
    try:
        remote = await api.config()
    except RelayAuthError as error:
        raise ConfigEntryAuthFailed("Relay authorization rejected") from error
    except RelayError as error:
        raise ConfigEntryNotReady("Cannot reach relay service") from error
    coordinator = RelayCoordinator(hass, entry, api, remote)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATION:
        manager = entry.runtime_data
        if hass.data.get(DOMAIN, {}).get("automation_manager") is manager:
            hass.data[DOMAIN].pop("automation_manager", None)
        await manager.async_stop()
        return True
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
        return True
    return False
