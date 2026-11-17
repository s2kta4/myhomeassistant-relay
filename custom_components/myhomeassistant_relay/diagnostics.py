"""Diagnostics without account identifiers or sensitive home data."""
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MODE, MODE_AUTOMATIC, MODE_AUTOMATION

# Apply to the complete output so future account-level fields are also redacted.
TO_REDACT = {"license_id", "token", "access_token", "server_url"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = entry.runtime_data
    if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATION:
        data = {"connection_mode": MODE_AUTOMATION, "active_connections": len(coordinator._clients), "loaded": not coordinator._closing}
    else:
        data = {"license_id": entry.data["license_id"], "plan": coordinator.remote.get("plan"), "cadence_seconds": coordinator.cadence, "selected_entity_count": len(coordinator.remote.get("entity_ids", [])), "last_push": coordinator.last_push.isoformat() if coordinator.last_push else None, "last_update_success": coordinator.last_update_success}
    return async_redact_data(data, TO_REDACT)
