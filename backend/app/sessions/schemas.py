from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title cannot be blank")
        return cleaned


class SessionResponse(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SessionPage(BaseModel):
    items: list[SessionResponse]
    next_cursor: str | None


class MessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    meta: dict[str, object]
    client_request_id: uuid.UUID | None
    created_at: datetime


class MessagePage(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def clean_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class FeedbackResponse(BaseModel):
    id: uuid.UUID
    message_id: uuid.UUID
    rating: Literal[-1, 1]
    comment: str | None
    created_at: datetime
