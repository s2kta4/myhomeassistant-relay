"""Shared integration constants; tokens are never part of a URL or log message."""

DOMAIN = "myhomeassistant_relay"
DEFAULT_SERVER_URL = "https://myhomeassistant.top"
CONF_SERVER_URL = "server_url"
CONF_TOKEN = "token"
CONF_ENTITY_IDS = "entity_ids"
CONF_LICENSE_ID = "license_id"
CONF_MODE = "connection_mode"
MODE_AUTOMATIC = "automatic"
MODE_AUTOMATION = "automation"
SERVICE_SYNC = "sync"
MAX_BODY_BYTES = 65_536
MAX_RESPONSE_BYTES = 262_144
MAX_COMMAND_BATCH = 32
MAX_PARALLEL_COMMANDS = 64
