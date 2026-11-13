"""Independent snapshot publishing and bounded, at-most-once command execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import area_registry, device_registry, entity_registry
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RelayApi, RelayAuthError, RelayError, RelayPayloadError, RelayRateLimitError
from .const import CONF_ENTITY_IDS, MAX_COMMAND_BATCH, MAX_PARALLEL_COMMANDS

_LOGGER = logging.getLogger(__name__)


def command_allowed(command: Any, config: dict[str, Any], now: float) -> bool:
    """Use the entity-specific policy supplied by the same server map as the UI."""
    if not isinstance(command, dict):
        return False
    entity_id = command.get("entity_id")
    service = command.get("service")
    expiry = command.get("expires_at")
    if (
        not isinstance(entity_id, str)
        or entity_id not in config.get(CONF_ENTITY_IDS, [])
        or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", entity_id)
        or not isinstance(service, str)
        or not re.fullmatch(r"[a-z_]{1,32}", service)
        or not isinstance(command.get("id"), str)
        or not re.fullmatch(r"[a-f0-9]{16}", command["id"])
        or type(expiry) is not int
        or expiry <= now
        or "expect" not in command
    ):
        return False
    return any(
        action.get("service") == service and action.get("expect") == command["expect"]
        for action in config.get("allowed_actions", {}).get(entity_id, [])
        if isinstance(action, dict)
    )


class RelayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll once per cadence; command waits never hold up polling or heartbeats."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: RelayApi, remote: dict[str, Any], *, automatic: bool = True) -> None:
        super().__init__(hass, _LOGGER, name="My Home Assistant relay", config_entry=entry, update_interval=None)
        self.api = api
        self.entry = entry
        self.remote = remote
        self.automatic = automatic
        self.last_push: datetime | None = None
        self._last_publish = 0.0
        self._last_config = time.monotonic()
        self._publish_lock = asyncio.Lock()
        self._command_tasks: set[asyncio.Task] = set()
        self._seen: dict[str, int] = {}
        self._unsubscribe: Any = None
        self._debounce: asyncio.Task | None = None
        self._failure_count = 0
        self._backoff_until = 0.0
        self._auth_failed = False
        self._closing = False
        self._initial_refresh = True
        self._timer_unsubscribe: Any = None
        self._scheduled_task: asyncio.Task | None = None
        self._install_state_listener()
        self._install_timer()

    @property
    def cadence(self) -> int:
        return int(self.remote["cadence_seconds"])

    def _install_state_listener(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if not self.automatic or self.cadence == 60:
            return
        controlled = [entity_id for entity_id, actions in self.remote.get("allowed_actions", {}).items() if actions]
        if controlled:
            self._unsubscribe = async_track_state_change_event(self.hass, controlled, self._state_changed)

    def _install_timer(self) -> None:
        if self._timer_unsubscribe:
            self._timer_unsubscribe()
            self._timer_unsubscribe = None
        if not self.automatic:
            return
        offset = int(self.remote["cadence_offset"])
        seconds = [offset] if self.cadence == 60 else list(range(offset, 60, self.cadence))
        self._timer_unsubscribe = async_track_utc_time_change(self.hass, self._scheduled_tick, second=seconds)

    @callback
    def _scheduled_tick(self, now: datetime) -> None:
        if self._closing or self._auth_failed or time.monotonic() < self._backoff_until or (self._scheduled_task and not self._scheduled_task.done()):
            return
        self._scheduled_task = self.entry.async_create_background_task(self.hass, self.async_refresh(), "Relay scheduled push and pull")

    @callback
    def _state_changed(self, event: Any) -> None:
        if self._closing or self._auth_failed or (self._debounce and not self._debounce.done()):
            return
        self._debounce = self.entry.async_create_background_task(self.hass, self._debounced_publish(), "Relay coalesced snapshot")

    async def _debounced_publish(self) -> None:
        await asyncio.sleep(1)
        try:
            await self.async_publish()
        except RelayAuthError:
            self._auth_failed = True
            await self.async_request_refresh()
        except RelayRateLimitError as error:
            self._backoff_until = time.monotonic() + error.retry_after
        except RelayError:
            # Scheduled updates retry and report health; never log payloads or secrets.
            self._backoff_until = max(self._backoff_until, time.monotonic() + self.cadence)

    def snapshot(self) -> list[dict[str, Any]]:
        entities = entity_registry.async_get(self.hass)
        devices = device_registry.async_get(self.hass)
        areas = area_registry.async_get(self.hass)
        rows = []
        for entity_id in self.remote[CONF_ENTITY_IDS]:
            state = self.hass.states.get(entity_id)
            registry_entry = entities.async_get(entity_id)
            area_id = registry_entry.area_id if registry_entry else None
            if not area_id and registry_entry and registry_entry.device_id:
                device = devices.async_get(registry_entry.device_id)
                area_id = device.area_id if device else None
            area = areas.async_get_area(area_id) if area_id else None
            changed = 0
            if state:
                changed_at = state.attributes.get("last_triggered") if entity_id.startswith("script.") else state.last_changed
                if isinstance(changed_at, datetime):
                    changed = max(0, min(253402300799, int(changed_at.timestamp())))
                elif isinstance(changed_at, str):
                    try:
                        changed = max(0, min(253402300799, int(datetime.fromisoformat(changed_at).timestamp())))
                    except ValueError:
                        pass
            rows.append({"entity_id": entity_id, "state": state.state if state else "unknown", "display_name": str(state.attributes.get("friendly_name") or "") if state else "", "area": area.name if area else "", "unit": str(state.attributes.get("unit_of_measurement") or "") if state else "", "changed": changed, "supported_features": max(0, int(state.attributes.get("supported_features") or 0)) if state else 0})
        return rows

    async def async_publish(self) -> None:
        """Coalesce extras; never reset the periodic pull timer after a state event."""
        now = time.monotonic()
        # Aligned minute callbacks can arrive a few milliseconds early. The
        # server's rolling window is authoritative; allow ordinary clock jitter.
        minimum = 59.5 if self.cadence == 60 else 3
        if self._closing or self._auth_failed or self._publish_lock.locked() or now < self._backoff_until or now - self._last_publish < minimum:
            return
        async with self._publish_lock:
            signature=self.remote.get("selection_signature")
            if signature is None:
                await self.api.push(self.snapshot())
            else:
                await self.api.push(self.snapshot(), signature)
            # Measure from request start. Response latency must not turn the
            # next scheduled one-minute update into an accidental two-minute gap.
            self._last_publish = now
            self.last_push = datetime.now(timezone.utc)
            if self.data is not None:
                self.data = self._health_data()
                # async_set_updated_data would postpone command polling on every push.
                self.async_update_listeners()

    def _health_data(self) -> dict[str, Any]:
        return {"last_push": self.last_push, "plan": self.remote.get("plan"), "cadence_seconds": self.cadence, "selected_entities": len(self.remote[CONF_ENTITY_IDS])}

    async def _async_update_data(self) -> dict[str, Any]:
        # Initial setup checks metadata only; requests start at the next aligned tick.
        if self._initial_refresh:
            self._initial_refresh = False
            return self._health_data()
        if self._auth_failed:
            raise ConfigEntryAuthFailed("Relay authorization rejected")
        try:
            if time.monotonic() < self._backoff_until:
                raise RelayRateLimitError(max(1, int(self._backoff_until - time.monotonic())))
            if time.monotonic() - self._last_config >= 60:
                self.remote = await self.api.config()
                self._last_config = time.monotonic()
                self._install_state_listener()
                self._install_timer()
            await self.async_publish()
            if len(self._command_tasks) > MAX_PARALLEL_COMMANDS - MAX_COMMAND_BATCH:
                # Leave commands pending at the server until an entire batch fits.
                return self._health_data()
            response = await self.api.commands()
            commands = response.get("commands", [])
            if not isinstance(commands, list) or len(commands) > MAX_COMMAND_BATCH:
                raise RelayError("Invalid relay command batch")
            now = time.time()
            self._seen = {command_id: expiry for command_id, expiry in self._seen.items() if expiry > now}
            for command in commands:
                if not command_allowed(command, self.remote, now) or command["id"] in self._seen:
                    continue
                self._seen[command["id"]] = command["expires_at"]
                if len(self._command_tasks) >= MAX_PARALLEL_COMMANDS:
                    continue  # Never replay an at-most-once command or build an unbounded queue.
                task = self.entry.async_create_background_task(self.hass, self._execute(command), "Relay selected command")
                self._command_tasks.add(task)
                task.add_done_callback(self._command_tasks.discard)
            self._failure_count = 0
            return self._health_data()
        except RelayAuthError as error:
            self._auth_failed = True
            raise ConfigEntryAuthFailed("Relay authorization rejected") from error
        except RelayRateLimitError as error:
            self._backoff_until = time.monotonic() + error.retry_after
            raise UpdateFailed("Relay rate limit; waiting before retry") from error
        except RelayPayloadError as error:
            self._last_config = 0.0  # Account-side entity changes need a fresh selected list.
            raise UpdateFailed("Relay configuration or snapshot was rejected") from error
        except RelayError as error:
            self._failure_count += 1
            retry = min(300, self.cadence * 2 ** min(self._failure_count, 4))
            self._backoff_until = time.monotonic() + retry
            raise UpdateFailed("Relay connection failed; retrying with backoff") from error

    async def _execute(self, command: dict[str, Any]) -> None:
        # Validate again when the task starts, including account-side selection changes.
        if not command_allowed(command, self.remote, time.time()):
            return
        entity_id = command["entity_id"]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return
        domain = entity_id.split(".", 1)[0]
        try:
            async with asyncio.timeout(min(35, max(0.01, command["expires_at"] - time.time()))):
                await self.hass.services.async_call(domain, command["service"], {"entity_id": entity_id}, blocking=domain != "script")
                if command["expect"] is not None:
                    await self._wait_for_state(entity_id, command["expect"], min(30, max(0.01, command["expires_at"] - time.time())))
        except (TimeoutError, HomeAssistantError):
            pass  # A later snapshot reports reality; failure is not a successful transition.
        if self.cadence != 60 and not self._closing:
            try:
                await self.async_publish()
            except RelayAuthError:
                self._auth_failed = True
                await self.async_request_refresh()
            except RelayRateLimitError as error:
                self._backoff_until = time.monotonic() + error.retry_after
            except RelayError:
                pass

    async def _wait_for_state(self, entity_id: str, expected: str, timeout: float) -> None:
        if self.hass.states.is_state(entity_id, expected):
            return
        finished = asyncio.get_running_loop().create_future()

        @callback
        def changed(event: Any) -> None:
            if self.hass.states.is_state(entity_id, expected) and not finished.done():
                finished.set_result(None)

        unsubscribe = async_track_state_change_event(self.hass, [entity_id], changed)
        try:
            if self.hass.states.is_state(entity_id, expected):
                return
            await asyncio.wait_for(finished, timeout)
        finally:
            unsubscribe()

    async def async_stop(self) -> None:
        self._closing = True
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._timer_unsubscribe:
            self._timer_unsubscribe()
            self._timer_unsubscribe = None
        tasks = list(self._command_tasks)
        if self._debounce and not self._debounce.done():
            tasks.append(self._debounce)
        if self._scheduled_task and not self._scheduled_task.done():
            tasks.append(self._scheduled_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
