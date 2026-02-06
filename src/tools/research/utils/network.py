"""Network configuration for research tools.

Provides proxy configuration for routing requests through institutional
networks (university VPN, SSH tunnels, HTTP proxies) to access paywalled content.
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ProxyType(Enum):
    """Supported proxy types."""

    NONE = "none"
    HTTP = "http"
    SOCKS5 = "socks5"


@dataclass
class ProxyConfig:
    """Proxy configuration for routing requests through institutional networks.

    Loaded from environment variables or agent config. Supports HTTP and SOCKS5
    proxies (e.g., SSH tunnels to university networks).

    Usage with SSH tunnel:
        ssh -D 1080 -N user@university.edu
        RESEARCH_PROXY_TYPE=socks5 RESEARCH_PROXY_HOST=localhost RESEARCH_PROXY_PORT=1080
    """

    type: ProxyType = ProxyType.NONE
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def url(self) -> Optional[str]:
        """Get the proxy URL string (e.g., 'socks5://localhost:1080')."""
        if self.type == ProxyType.NONE or not self.host or not self.port:
            return None
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.type.value}://{auth}{self.host}:{self.port}"

    @property
    def is_configured(self) -> bool:
        """Check if a proxy is actually configured and usable."""
        return self.type != ProxyType.NONE and self.host is not None and self.port is not None

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        """Load proxy config from environment variables.

        Env vars:
            RESEARCH_PROXY_TYPE: "http", "socks5", or "none" (default: "none")
            RESEARCH_PROXY_HOST: Proxy host (e.g., "localhost")
            RESEARCH_PROXY_PORT: Proxy port (e.g., "1080")
            RESEARCH_PROXY_USER: Optional proxy username
            RESEARCH_PROXY_PASS: Optional proxy password
        """
        proxy_type_str = os.getenv("RESEARCH_PROXY_TYPE", "none").lower()

        try:
            proxy_type = ProxyType(proxy_type_str)
        except ValueError:
            logger.warning(
                f"Unknown RESEARCH_PROXY_TYPE: {proxy_type_str}. Using 'none'."
            )
            proxy_type = ProxyType.NONE

        if proxy_type == ProxyType.NONE:
            return cls()

        port_str = os.getenv("RESEARCH_PROXY_PORT")
        port = int(port_str) if port_str else None

        return cls(
            type=proxy_type,
            host=os.getenv("RESEARCH_PROXY_HOST"),
            port=port,
            username=os.getenv("RESEARCH_PROXY_USER"),
            password=os.getenv("RESEARCH_PROXY_PASS"),
        )

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ProxyConfig":
        """Load proxy config from agent config dict.

        Expected format (from defaults.yaml research.proxy section):
            proxy:
              enabled: true
              type: socks5
              host: localhost
              port: 1080

        Falls back to environment variables if not in config.

        Args:
            config: Dict with proxy configuration fields
        """
        if not config or not config.get("enabled", False):
            # Try env vars as fallback
            return cls.from_env()

        proxy_type_str = config.get("type", "none")
        try:
            proxy_type = ProxyType(proxy_type_str)
        except ValueError:
            logger.warning(f"Unknown proxy type in config: {proxy_type_str}")
            proxy_type = ProxyType.NONE

        return cls(
            type=proxy_type,
            host=config.get("host"),
            port=config.get("port"),
            username=config.get("username") or os.getenv("RESEARCH_PROXY_USER"),
            password=config.get("password") or os.getenv("RESEARCH_PROXY_PASS"),
        )

    def to_playwright_proxy(self) -> Optional[Dict[str, str]]:
        """Convert to Playwright proxy format (dict).

        Returns:
            Dict for Playwright's proxy parameter, or None if no proxy.
        """
        if not self.is_configured:
            return None

        proxy: Dict[str, str] = {"server": self.url}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy

    def to_browser_use_proxy(self) -> Optional[Any]:
        """Convert to browser-use ProxySettings format.

        Returns:
            browser_use ProxySettings instance, or None if no proxy.
        """
        if not self.is_configured:
            return None

        try:
            from browser_use.browser.profile import ProxySettings

            kwargs: Dict[str, str] = {"server": self.url}
            if self.username:
                kwargs["username"] = self.username
            if self.password:
                kwargs["password"] = self.password
            return ProxySettings(**kwargs)
        except ImportError:
            logger.debug("browser-use not installed, cannot create ProxySettings")
            return None
