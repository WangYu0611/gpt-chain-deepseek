"""Shared proxy routing for unattended API clients and native provider processes."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler

from .storage import read_json


NETWORK_NAME = "network.local.json"
NO_PROXY = "localhost,127.0.0.1,::1"


def _read(path, *, required):
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        value = json.loads(raw.decode("utf-8-sig"))
        if type(value) is not dict:
            raise ValueError
        return value
    except FileNotFoundError:
        if not required:
            return None
        raise ValueError("Configured network routing file is unavailable") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Network routing configuration is unreadable or invalid") from None


def _path(value, parent):
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
        raise ValueError("Network routing requires a valid configuration path")
    path = Path(value).expanduser()
    return (parent / path).resolve() if not path.is_absolute() else path.resolve()


def load_network(*, config_path=None, secrets_file=None, network_file=None):
    config = Path(config_path).expanduser().resolve() if config_path else None
    parent = config.parent if config is not None else Path(__file__).resolve().parent.parent
    settings = {}
    if config is not None:
        try:
            settings = read_json(config)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            raise ValueError("Cannot read the source configuration for network routing") from None
    if network_file is None:
        network_file = settings.get("network_file")
    explicit = network_file is not None
    if explicit:
        path = _path(network_file, parent)
    else:
        secret = secrets_file if secrets_file is not None else settings.get("secrets_file")
        path = _path(secret, parent).with_name(NETWORK_NAME) if secret is not None else parent / NETWORK_NAME
    route = _read(path, required=explicit)
    if route is None and not explicit:
        return None
    try:
        if set(route) != {"proxy_url", "region"} or route["region"] != "SG":
            raise ValueError
        url = route["proxy_url"]
        if (not isinstance(url, str) or not url or
                any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
                "?" in url or "#" in url or "\\" in url):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Network routing must name an SG HTTP(S) proxy with an explicit port") from None
    return {"proxy_url": url.rstrip("/"), "region": "SG"}


def proxy_environment(*, config_path=None, secrets_file=None, network_file=None):
    route = load_network(config_path=config_path, secrets_file=secrets_file, network_file=network_file)
    if route is None:
        return {}
    environment = {"HTTP_PROXY": route["proxy_url"], "HTTPS_PROXY": route["proxy_url"],
                   "ALL_PROXY": route["proxy_url"], "NO_PROXY": NO_PROXY}
    environment.update({name.lower(): value for name, value in environment.items()})
    environment["NODE_USE_ENV_PROXY"] = "1"
    return environment


def _loopback(host):
    if isinstance(host, str) and host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _FixedProxyHandler(ProxyHandler):
    """Apply the selected proxy to external hosts regardless of inherited bypass rules."""
    def proxy_open(self, request, proxy, protocol):
        if _loopback(urlsplit(request.full_url).hostname):
            return None
        target = urlsplit(proxy)
        original = request.type
        request.set_proxy(target.netloc, target.scheme)
        if original == target.scheme or original == "https":
            return None
        return self.parent.open(request, timeout=request.timeout)


def proxy_handler(*, config_path=None, secrets_file=None, network_file=None):
    route = load_network(config_path=config_path, secrets_file=secrets_file, network_file=network_file)
    if route is None:
        return ProxyHandler()
    return _FixedProxyHandler({"http": route["proxy_url"], "https": route["proxy_url"]})
