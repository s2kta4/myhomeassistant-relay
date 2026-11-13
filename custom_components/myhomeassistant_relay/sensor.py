"""Expose the most recent successful relay snapshot without credentials."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LICENSE_ID
from .coordinator import RelayCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([RelayLastPush(entry.runtime_data, entry.data[CONF_LICENSE_ID])])


class RelayLastPush(CoordinatorEntity[RelayCoordinator], SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-upload-outline"

    def __init__(self, coordinator: RelayCoordinator, license_id: int) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"mha_relay_{license_id}_last_push"
        self._attr_name = f"My Home Assistant relay {license_id} last push"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_push

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"plan": self.coordinator.remote.get("plan"), "cadence_seconds": self.coordinator.cadence, "selected_entities": len(self.coordinator.remote.get("entity_ids", []))}
