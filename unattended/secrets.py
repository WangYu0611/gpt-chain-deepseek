"""Read one credential from the operator's local, untracked JSON file."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from .storage import read_json

LOCAL_SECRETS_NAME = "secrets.local.json"


def _read_settings(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        settings = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        raise ValueError("无法读取本地密钥文件 secrets.local.json，请检查 JSON 格式和权限。") from None
    return settings


def _secret_settings(config_path=None, secrets_path=None, secrets_file=None) -> dict:
    if secrets_path is not None:
        return _read_settings(Path(secrets_path).expanduser().resolve())
    config = Path(config_path).expanduser().resolve() if config_path else None
    if secrets_file is None and config is not None:
        try:
            secrets_file = read_json(config).get("secrets_file")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            raise ValueError("无法读取密钥路由配置，请检查 JSON 格式和权限。") from None
    if secrets_file is not None:
        if not isinstance(secrets_file, (str, Path)) or not str(secrets_file).strip() or "\0" in str(secrets_file):
            raise ValueError("secrets_file 必须为有效文件路径。")
        path = Path(secrets_file).expanduser()
        if not path.is_absolute() and config is not None:
            path = config.parent / path
        return _read_settings(path.resolve())
    return _read_settings(config.with_name(LOCAL_SECRETS_NAME)) if config is not None else {}


def _validate_secret(value, name: str) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if len(value) <= 4096 and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            return value
    raise ValueError(f"本地密钥文件中的 {name} 格式无效。")


def read_secret(name: str, *, config_path: str | Path | None = None,
                secrets_path: str | Path | None = None,
                secrets_file: str | Path | None = None) -> str | None:
    settings = _secret_settings(config_path, secrets_path, secrets_file)
    return _validate_secret(settings.get(name, ""), name)


def read_secret_list(name: str, *, config_path=None, secrets_file=None) -> list[str]:
    values = _secret_settings(config_path=config_path, secrets_file=secrets_file).get(name, [])
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError(f"本地密钥文件中的 {name} 应为最多 16 项的列表。")
    return [key for value in values if (key := _validate_secret(value, name))]


def secret_values(config_path=None, secrets_file=None) -> list[str]:
    """Collect central credential values for output redaction, including archived keys."""
    pending = [_secret_settings(config_path=config_path, secrets_file=secrets_file)]
    result = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(item for name, item in value.items() if not str(name).upper().endswith("BASE_URL"))
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and len(value.strip()) >= 8:
            result.add(value.strip())
    return sorted(result, key=len, reverse=True)


def validate_api_base_url(value) -> str:
    try:
        if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "\\" in value):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise ValueError("API 地址必须为有效 HTTPS 地址，且不能包含凭据、查询参数或片段。") from None
    return value.rstrip("/")
