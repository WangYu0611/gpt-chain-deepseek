"""Read one credential from the operator's local, untracked JSON file."""
from __future__ import annotations

import json
from pathlib import Path


LOCAL_SECRETS_NAME = "secrets.local.json"


def read_secret(name: str, *, config_path: str | Path | None = None,
                secrets_path: Path | None = None) -> str | None:
    if secrets_path is None:
        if not config_path:
            return None
        secrets_path = Path(config_path).with_name(LOCAL_SECRETS_NAME)
    try:
        with secrets_path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        settings = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise ValueError("无法读取本地密钥文件 secrets.local.json，请检查 JSON 格式和权限。") from None
    value = settings.get(name, "")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if len(value) <= 4096 and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            return value
    raise ValueError(f"本地密钥文件中的 {name} 格式无效。")
