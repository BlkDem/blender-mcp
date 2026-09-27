"""Runtime configuration, read from the environment (and an optional .env file).

Every value has a default, so the server runs with no setup at all. Nothing is
hardcoded at the call sites: tools, the WebSocket bridge and the add-on settings
all read from :class:`Settings`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def bridge_url(self) -> str:
        """WebSocket URL the Blender add-on has to connect to."""
        return f"ws://{self.blender_host}:{self.blender_port}"


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
