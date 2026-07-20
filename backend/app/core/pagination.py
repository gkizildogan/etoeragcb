from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import orjson


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CursorPosition:
    occurred_at: datetime
    resource_id: uuid.UUID


class CursorCodec:
    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def encode(self, *, kind: str, occurred_at: datetime, resource_id: uuid.UUID) -> str:
        body = orjson.dumps(
            {"kind": kind, "at": occurred_at.astimezone(UTC).isoformat(), "id": str(resource_id)},
            option=orjson.OPT_SORT_KEYS,
        )
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_encode(body)}.{_encode(signature)}"

    def decode(self, token: str, *, expected_kind: str) -> CursorPosition:
        try:
            encoded_body, encoded_signature = token.split(".", maxsplit=1)
            body = _decode(encoded_body)
            signature = _decode(encoded_signature)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise InvalidCursorError
            payload = orjson.loads(body)
            if not isinstance(payload, dict) or payload.get("kind") != expected_kind:
                raise InvalidCursorError
            occurred_at = datetime.fromisoformat(payload["at"])
            if occurred_at.tzinfo is None:
                raise InvalidCursorError
            return CursorPosition(
                occurred_at=occurred_at.astimezone(UTC),
                resource_id=uuid.UUID(payload["id"]),
            )
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise InvalidCursorError from exc


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
