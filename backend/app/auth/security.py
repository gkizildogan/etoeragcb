from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings

JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "rag-chatbot-api"
JWT_ISSUER = "rag-chatbot"


class InvalidAccessTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    auth_version: int
    jti: uuid.UUID
    expires_at: datetime


class SecurityService:
    def __init__(
        self,
        settings: Settings,
        *,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self._jwt_secret = settings.resolved_jwt_secret().get_secret_value()
        self._refresh_secret = settings.resolved_signing_secret().get_secret_value().encode()
        self._access_ttl = settings.access_token_ttl
        self._refresh_ttl = settings.refresh_token_ttl
        self._password_hasher = password_hasher or PasswordHasher(type=Type.ID)
        self._dummy_password_hash = self._password_hasher.hash(secrets.token_urlsafe(32))

    @property
    def access_ttl(self) -> int:
        return self._access_ttl

    @property
    def refresh_ttl(self) -> int:
        return self._refresh_ttl

    def hash_password(self, password: str) -> str:
        validate_password(password)
        return self._password_hasher.hash(password)

    def verify_password(self, password_hash: str | None, password: str) -> bool:
        candidate = password_hash or self._dummy_password_hash
        try:
            return self._password_hasher.verify(candidate, password)
        except (InvalidHashError, VerificationError):
            return False

    def password_needs_rehash(self, password_hash: str) -> bool:
        return self._password_hasher.check_needs_rehash(password_hash)

    def issue_access_token(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role: str,
        auth_version: int,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=self._access_ttl)
        payload: dict[str, Any] = {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role,
            "ver": auth_version,
            "jti": str(uuid.uuid4()),
            "type": "access",
            "iat": issued_at,
            "exp": expires_at,
        }
        return jwt.encode(payload, self._jwt_secret, algorithm=JWT_ALGORITHM), expires_at

    def decode_access_token(self, token: str) -> AccessClaims:
        try:
            payload = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=[JWT_ALGORITHM],
                audience=JWT_AUDIENCE,
                issuer=JWT_ISSUER,
                options={"require": ["exp", "iat", "sub", "tenant_id", "jti", "type", "ver"]},
            )
            if payload["type"] != "access" or payload["role"] not in {"admin", "member"}:
                raise InvalidAccessTokenError
            return AccessClaims(
                user_id=uuid.UUID(payload["sub"]),
                tenant_id=uuid.UUID(payload["tenant_id"]),
                role=payload["role"],
                auth_version=int(payload["ver"]),
                jti=uuid.UUID(payload["jti"]),
                expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
            )
        except (KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
            raise InvalidAccessTokenError from exc

    def new_refresh_token(self) -> str:
        return secrets.token_urlsafe(48)

    def hash_refresh_token(self, token: str) -> str:
        return hmac.new(self._refresh_secret, token.encode(), hashlib.sha256).hexdigest()


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    if len(password) > 128:
        raise ValueError("password must contain at most 128 characters")


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def audit_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
