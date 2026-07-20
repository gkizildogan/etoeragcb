from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    tenant_id: uuid.UUID | None = None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=32, max_length=512)


class LogoutRequest(RefreshRequest):
    pass


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a credential
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    tenant_id: uuid.UUID


class TenantMembershipResponse(BaseModel):
    tenant_id: uuid.UUID
    slug: str
    name: str
    role: str
    active: bool


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: EmailStr
    is_superuser: bool
    active_tenant_id: uuid.UUID
    memberships: list[TenantMembershipResponse]
