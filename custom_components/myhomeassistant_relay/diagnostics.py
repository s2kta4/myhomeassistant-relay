"""Deliberately exclude tokens, entity states, IPs and household labels."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import CONF_MODE, MODE_AUTOMATIC, MODE_AUTOMATION


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = entry.runtime_data
    if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATION:
        return {"connection_mode": MODE_AUTOMATION, "active_connections": len(coordinator._clients), "loaded": not coordinator._closing}
    return {"license_id": entry.data["license_id"], "plan": coordinator.remote.get("plan"), "cadence_seconds": coordinator.cadence, "selected_entity_count": len(coordinator.remote.get("entity_ids", [])), "last_push": coordinator.last_push.isoformat() if coordinator.last_push else None, "last_update_success": coordinator.last_update_success}
