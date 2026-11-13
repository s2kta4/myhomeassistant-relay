"""One outbound sync action for a Home Assistant UI automation."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RelayApi, RelayAuthError, RelayError, RelayRateLimitError, validate_token
from .const import CONF_ENTITY_IDS, CONF_LICENSE_ID, CONF_MODE, CONF_TOKEN, DEFAULT_SERVER_URL, DOMAIN, MODE_AUTOMATIC
from .coordinator import RelayCoordinator


def selected_entities(value: Any) -> list[str]:
    """Reject malformed/duplicate selections before any outgoing call."""
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ServiceValidationError("Choose between one and 200 relay entities")
    if any(not isinstance(item, str) or len(item) > 128 or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", item) for item in value) or len(set(value)) != len(value):
        raise ServiceValidationError("Use unique Home Assistant entity IDs")
    return value


class AutomationRelayManager:
    """UI-triggered coordinators have no background timer or state listener."""

    def __init__(self, hass: Any, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self._clients: dict[int, RelayCoordinator] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_pulls: dict[int, float] = {}
        self._closing = False

    async def async_sync(self, data: dict[str, Any]) -> None:
        if self._closing:
            raise ServiceValidationError("The relay automation helper is not loaded")
        if not {CONF_TOKEN, CONF_ENTITY_IDS}.issubset(data) or set(data) - {CONF_TOKEN, CONF_ENTITY_IDS, "poll_commands"}:
            raise ServiceValidationError("Provide the relay token, selected entity IDs and optional command polling flag")
        poll_commands = data.get("poll_commands", True)
        if not isinstance(poll_commands, bool):
            raise ServiceValidationError("The command polling flag must be true or false")
        selected = selected_entities(data[CONF_ENTITY_IDS])
        try:
            token = validate_token(data[CONF_TOKEN])
        except (RelayAuthError, AttributeError, TypeError) as error:
            raise ServiceValidationError("Use the current private token from your relay account") from error
        key = hashlib.sha256(token.encode()).hexdigest()
        if key not in self._locks and len(self._locks) >= 32:
            raise ServiceValidationError("Too many relay connections are active in this Home Assistant")
        lock = self._locks.setdefault(key, asyncio.Lock())
        # A state event and scheduled tick must not perform overlapping pulls.
        if lock.locked():
            return
        async with lock:
            try:
                coordinator = next((item for item in self._clients.values() if item.api._token == token), None)
                if coordinator is not None and time.monotonic() < coordinator._backoff_until:
                    return
                if coordinator is None:
                    api = RelayApi(async_get_clientsession(self.hass), DEFAULT_SERVER_URL, token)
                    remote = await api.config()
                    license_id = remote[CONF_LICENSE_ID]
                    if len(self._clients) >= 16 and license_id not in self._clients:
                        raise ServiceValidationError("This helper supports up to 16 relay connections")
                    if previous := self._clients.pop(license_id, None):
                        await previous.async_stop()
                        old_key = hashlib.sha256(previous.api._token.encode()).hexdigest()
                        if old_key in self._locks and not self._locks[old_key].locked():
                            self._locks.pop(old_key, None)
                    coordinator = RelayCoordinator(self.hass, self.entry, api, remote, automatic=False)
                    coordinator._initial_refresh = False
                    coordinator._ui_requested_selection = list(selected)
                    self._clients[license_id] = coordinator
                elif selected != coordinator._ui_requested_selection or time.monotonic() - coordinator._last_config >= 60:
                    coordinator.remote = await coordinator.api.config()
                    coordinator._last_config = time.monotonic()
                    coordinator._ui_requested_selection = list(selected)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_MODE, MODE_AUTOMATIC) == MODE_AUTOMATIC and entry.data.get(CONF_LICENSE_ID) == coordinator.remote[CONF_LICENSE_ID]:
                        raise ServiceValidationError("Remove the automatic integration for this relay connection before using its UI automation")
                self._validate_selection(coordinator.remote, selected)
                # State-change calls publish only. Scheduled calls collect commands,
                # so rapid device changes cannot consume the pull allowance.
                last_pull = self._last_pulls.get(coordinator.remote[CONF_LICENSE_ID], 0.0)
                if poll_commands and (coordinator.cadence != 60 or time.monotonic() - last_pull >= 59.5):
                    self._last_pulls[coordinator.remote[CONF_LICENSE_ID]] = time.monotonic()
                    await coordinator.async_refresh()
                    if not coordinator.last_update_success:
                        raise HomeAssistantError("Relay did not accept this update")
                else:
                    await coordinator.async_publish()
            except RelayRateLimitError as error:
                # UI state triggers share the same backoff as scheduled updates.
                if coordinator is not None:
                    coordinator._backoff_until = time.monotonic() + error.retry_after
                raise HomeAssistantError("Relay is waiting before its next update") from error
            except ServiceValidationError:
                raise
            except (RelayError, HomeAssistantError) as error:
                # Do not include request content, entity states, or tokens in errors.
                raise HomeAssistantError("Relay sync did not finish. Check the current token, internet connection and the relay account status.") from error
            finally:
                if not any(item.api._token == token for item in self._clients.values()):
                    self._locks.pop(key, None)

    @staticmethod
    def _validate_selection(remote: dict[str, Any], selected: list[str]) -> None:
        if len(selected) > remote["entity_limit"]:
            raise ServiceValidationError("The selected entities exceed this relay plan's allowance")
        if selected != remote[CONF_ENTITY_IDS]:
            raise ServiceValidationError("Your relay selection changed. Copy the current automation from your account and save it in Home Assistant")

    async def async_stop(self) -> None:
        self._closing = True
        for coordinator in self._clients.values():
            await coordinator.async_stop()
        self._clients.clear()
        self._locks.clear()
        self._last_pulls.clear()
