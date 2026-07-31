"""Encrypted per-user Twitter credential storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import parse_cookie_input, validate_cookies

ENCRYPTION_PREFIX = b"ARTICLENATOR-TWITTER-FERNET-V1\n"


class TwitterCookieStore:
    """Private encrypted storage for one user's Twitter session cookies."""

    def __init__(
        self,
        *,
        path: Path,
        encryption_key: str | None,
        require_encryption: bool,
    ) -> None:
        self.path = path
        self.encryption_key = encryption_key.strip() if encryption_key else None
        self.require_encryption = require_encryption

    def is_configured(self) -> bool:
        return self.path.is_file()

    def status(self) -> dict[str, object]:
        if not self.is_configured():
            return {"configured": False, "encrypted": False, "cookie_names": []}
        try:
            cookie_names = sorted(
                part.split("=", 1)[0].strip() for part in self.read().split(";") if "=" in part
            )
        except (OSError, ValueError):
            cookie_names = []
        return {
            "configured": True,
            "encrypted": self._is_encrypted(),
            "cookie_names": cookie_names,
        }

    def save(self, raw_cookies: str) -> dict[str, object]:
        normalized = parse_cookie_input(raw_cookies.strip())
        validation = validate_cookies(normalized)
        if not validation["valid"]:
            raise ValueError(str(validation["message"]))
        if self.require_encryption and not self.encryption_key:
            raise ValueError("Cookie encryption key is required")

        data = normalized.encode("utf-8")
        if self.encryption_key:
            try:
                data = ENCRYPTION_PREFIX + Fernet(self.encryption_key.encode()).encrypt(data)
            except (TypeError, ValueError) as exc:
                raise ValueError("Cookie encryption key must be a valid Fernet key") from exc
        self._write_private(data)
        return self.status()

    def read(self) -> str:
        if not self.is_configured():
            raise FileNotFoundError("No Twitter cookies are configured")
        data = self.path.read_bytes()
        if data.startswith(ENCRYPTION_PREFIX):
            if not self.encryption_key:
                raise ValueError("Cookie encryption key is not configured")
            try:
                data = Fernet(self.encryption_key.encode()).decrypt(data[len(ENCRYPTION_PREFIX) :])
            except (InvalidToken, TypeError, ValueError) as exc:
                raise ValueError("Stored Twitter cookies cannot be decrypted") from exc
        elif self.require_encryption:
            raise ValueError("Stored Twitter cookies are not encrypted")
        return data.decode("utf-8")

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def _is_encrypted(self) -> bool:
        return self.path.is_file() and self.path.read_bytes().startswith(ENCRYPTION_PREFIX)

    def _write_private(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.path)
        os.chmod(self.path, 0o600)
