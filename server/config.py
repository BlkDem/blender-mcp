"""Runtime configuration, read from the environment (and an optional .env file).

Every value has a default, so the server runs with no setup at all. Nothing is
hardcoded at the call sites: tools, the WebSocket bridge and the add-on settings
all read from :class:`Settings`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Every tool this server can register. ``ENABLED_TOOLS`` is checked against
#: this list at startup so a typo fails loudly instead of silently removing a
#: capability.
KNOWN_TOOLS: tuple[str, ...] = (
    "blender.get_scene",
    "blender.get_objects",
    "blender.get_object",
    "blender.create_object",
    "blender.update_object",
    "blender.delete_object",
    "blender.render",
    "blender.render_preview",
    "blender.execute_python",
    "blender.wait_for_change",
    "blender.get_instances",
    "blender.begin_transaction",
    "blender.checkpoint",
    "blender.commit_transaction",
    "blender.rollback_transaction",
)

#: Names that are always registered, whatever the allowlist says: without them
#: a client cannot see what it is allowed to do, or undo what it just did.
ALWAYS_ENABLED: frozenset[str] = frozenset(
    {
        "blender.get_scene",
        "blender.get_objects",
        "blender.get_object",
        "blender.get_instances",
        "blender.begin_transaction",
        "blender.commit_transaction",
        "blender.rollback_transaction",
    }
)


class Settings(BaseSettings):
    """Environment-driven settings for the MCP server process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Blender bridge (WebSocket server the add-on connects to) -----------
    blender_host: str = Field(default="127.0.0.1", description="Address the add-on connects to.")
    blender_port: int = Field(default=8765, ge=1, le=65535, description="Bridge port.")
    blender_allow_takeover: bool = Field(
        default=False,
        description=(
            "Let a newly connected Blender replace the one already attached. "
            "Off by default: a silent takeover sends one user's tool calls into "
            "another user's scene."
        ),
    )
    blender_request_timeout: float = Field(
        default=30.0, gt=0, description="How long to wait for a Blender response, in seconds."
    )
    blender_connect_timeout: float = Field(
        default=5.0, gt=0, description="Timeout used by the add-on when opening the socket."
    )
    blender_render_timeout: float = Field(
        default=600.0,
        gt=0,
        description="How long to wait for a render, in seconds. Renders are much slower than queries.",
    )

    # --- MCP transport ------------------------------------------------------
    mcp_transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio", description="stdio is what AI clients expect; HTTP is for tooling/debugging."
    )
    mcp_host: str = Field(default="127.0.0.1")
    mcp_port: int = Field(default=8000, ge=1, le=65535)

    # --- Safety -------------------------------------------------------------
    allow_python_execution: bool = Field(
        default=False,
        description="Gate for the blender.execute_python tool. Off by default; opt in via .env.",
    )
    enabled_tools: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(KNOWN_TOOLS),
        description=(
            "Comma-separated allowlist of tool names to register, e.g. "
            "'blender.get_scene,blender.get_object,blender.create_object'. Use it to expose a "
            "reduced surface: a 'safe' profile with no blender.execute_python, or a read-only "
            "profile. An empty value means every tool."
        ),
    )

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("enabled_tools", mode="before")
    @classmethod
    def _split_tool_list(cls, value: object) -> object:
        """Accept a comma-separated string, which is what an env var can carry.

        ``NoDecode`` stops pydantic-settings from trying JSON first and failing
        on ``scene,objects``; this turns the string into the list instead.
        """
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(part).strip() for part in value if str(part).strip()]
        return value

    @property
    def bridge_url(self) -> str:
        """WebSocket URL the Blender add-on has to connect to."""
        return f"ws://{self.blender_host}:{self.blender_port}"

    def tool_is_enabled(self, name: str) -> bool:
        """Whether ``name`` should be registered, given the allowlist.

        An empty allowlist means "everything", which is the friendliest reading
        of an unset variable.
        """
        if not self.enabled_tools:
            return True
        if name in ALWAYS_ENABLED:
            return True
        return name in self.enabled_tools

    def unknown_tools(self) -> list[str]:
        """Names in the allowlist that no tool answers to."""
        return sorted(set(self.enabled_tools) - set(KNOWN_TOOLS) - set(ALWAYS_ENABLED))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once."""
    settings = Settings()
    logger.debug("Configuration loaded: %s", settings.model_dump())
    return settings


def setup_logging(settings: Settings) -> None:
    """Send logs to stderr only.

    stdout belongs to the MCP stdio transport; a stray print or log record there
    corrupts the JSON-RPC stream and the client drops the connection.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(settings.log_format))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    logging.getLogger("websockets").setLevel(logging.WARNING)
