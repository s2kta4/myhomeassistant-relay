"""Bounded, redirect-free HTTPS client for the sessionless relay protocol."""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .const import MAX_BODY_BYTES, MAX_RESPONSE_BYTES


class RelayError(Exception):
    """A redacted protocol or connection error."""


class RelayAuthError(RelayError):
    """The token was rejected, disabled, or no longer licensed."""


class RelayRateLimitError(RelayError):
    """Retry after a bounded server-supplied interval."""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("Relay request rate limited")
        self.retry_after = max(1, min(300, retry_after))


class RelayPayloadError(RelayError):
    """A snapshot or entity selection is not accepted."""


def validate_server_url(value: str) -> str:
    """Require a bare HTTPS origin; credentials cannot move through redirects."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
    ):
        raise RelayError("Use the service HTTPS origin without a path")
    try:
        port = parsed.port
    except ValueError as error:
        raise RelayError("Invalid service origin") from error
    return f"https://{parsed.hostname.lower()}" + (f":{port}" if port and port != 443 else "")


def validate_token(value: str) -> str:
    """Accept the displayed raw token or its saved Bearer-prefixed secret."""
    token = value.strip().removeprefix("Bearer ").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise RelayAuthError("Invalid relay token")
    return token


def snapshot_bytes(entities: list[dict[str, Any]], selection_signature: str | None = None, sampled_at_ms: int | None = None) -> bytes:
    """Keep exact states and all IDs; only optional labels may shrink."""
    if selection_signature is not None and not re.fullmatch(r"[a-f0-9]{64}", selection_signature):
        raise RelayPayloadError("Invalid selection signature")
    for area_cap, unit_cap, name_cap in ((128, 128, 128), (32, 16, 64), (8, 8, 16), (0, 0, 0)):
        rows = []
        for item in entities:
            state = str(item["state"])
            if len(state) > 128 or "\0" in state:
                raise RelayPayloadError("A selected state exceeds the 128-character limit")
            rows.append({
                "entity_id": item["entity_id"],
                "state": state,
                "area": str(item.get("area", "")).replace("\0", "")[:area_cap],
                "unit": str(item.get("unit", "")).replace("\0", "")[:unit_cap],
                "changed": item.get("changed", 0),
            })
            if "display_name" in item:
                rows[-1]["display_name"] = str(item["display_name"]).replace("\0", "")[:name_cap]
            if "supported_features" in item:
                features = item["supported_features"]
                if type(features) is not int or not 0 <= features <= 2147483647:
                    raise RelayPayloadError("Invalid device features")
                rows[-1]["supported_features"] = features
        body = {"entities": rows}
        if sampled_at_ms is not None:
            body["sampled_at_ms"] = sampled_at_ms
        if selection_signature is not None:
            body["selection_signature"] = selection_signature
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) <= MAX_BODY_BYTES:
            return payload
    raise RelayPayloadError("Snapshot cannot fit the payload limit")


def validate_config(content: dict[str, Any]) -> dict[str, Any]:
    """Reject malformed metadata before it reaches local HA service execution."""
    try:
        entity_ids = content["entity_ids"]
        allowed = content["allowed_actions"]
        if (
            type(content["license_id"]) is not int or content["license_id"] < 1
            or type(content["entity_limit"]) is not int or not 1 <= content["entity_limit"] <= 200
            or type(content["cadence_seconds"]) is not int or content["cadence_seconds"] not in (10, 15, 60)
            or type(content["cadence_offset"]) is not int or not 0 <= content["cadence_offset"] <= (9 if content["cadence_seconds"] == 10 else 14)
            or not isinstance(entity_ids, list) or len(entity_ids) > content["entity_limit"]
            or not isinstance(allowed, dict)
        ):
            raise ValueError
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or len(entity_id) > 128 or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", entity_id):
                raise ValueError
            actions = allowed.get(entity_id)
            if not isinstance(actions, list):
                raise ValueError
            for action in actions:
                if not isinstance(action, dict) or not isinstance(action.get("service"), str) or not re.fullmatch(r"[a-z_]{1,32}", action["service"]) or "expect" not in action or (action["expect"] is not None and not isinstance(action["expect"], str)):
                    raise ValueError
        if len(set(entity_ids)) != len(entity_ids) or any(entity_id not in entity_ids for entity_id in allowed):
            raise ValueError
        if "selection_signature" in content and (not isinstance(content["selection_signature"], str) or not re.fullmatch(r"[a-f0-9]{64}", content["selection_signature"])):
            raise ValueError
        if "selection_version" in content and (type(content["selection_version"]) is not int or content["selection_version"] < 1):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise RelayError("Invalid relay configuration response") from error
    return content


class RelayApi:
    """Use Home Assistant's shared verified TLS session without closing it."""

    def __init__(self, session: aiohttp.ClientSession, server_url: str, token: str) -> None:
        self.session = session
        self.server_url = validate_server_url(server_url)
        self._token = validate_token(token)

    async def request(self, method: str, path: str, payload: bytes | None = None) -> dict[str, Any]:
        if path not in ("/relay/v1/config", "/relay/v1/push", "/relay/v1/commands"):
            raise RelayError("Unknown relay endpoint")
        if payload is not None and len(payload) > MAX_BODY_BYTES:
            raise RelayPayloadError("Snapshot exceeds the payload limit")
        try:
            async with self.session.request(
                method, self.server_url + path,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json", "Content-Type": "application/json"},
                data=payload, timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=False, ssl=True,
            ) as response:
                if response.status == 401:
                    raise RelayAuthError("Relay authorization rejected")
                if response.status == 429:
                    try:
                        retry = int(response.headers.get("Retry-After", "60"))
                    except ValueError:
                        retry = 60
                    raise RelayRateLimitError(retry)
                if response.status in (400, 413, 422):
                    raise RelayPayloadError("Relay payload rejected")
                if response.status != 200:
                    raise RelayError("Relay server request failed")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise RelayError("Relay response exceeds the limit")
                content = json.loads(raw)
                if not isinstance(content, dict):
                    raise RelayError("Invalid relay response")
                return content
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            # No response body, headers, selected state, token, or credential in errors.
            raise RelayError("Cannot communicate with the relay service") from error

    async def config(self, entity_ids: list[str] | None = None) -> dict[str, Any]:
        payload = None if entity_ids is None else json.dumps({"entity_ids": entity_ids}, separators=(",", ":")).encode()
        return validate_config(await self.request("GET" if payload is None else "POST", "/relay/v1/config", payload))

    async def push(self, entities: list[dict[str, Any]], selection_signature: str | None = None) -> dict[str, Any]:
        return await self.request("POST", "/relay/v1/push", snapshot_bytes(entities, selection_signature, time.time_ns() // 1_000_000))

    async def commands(self) -> dict[str, Any]:
        return await self.request("GET", "/relay/v1/commands")
