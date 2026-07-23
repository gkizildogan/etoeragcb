from __future__ import annotations

import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    message: str = Field(min_length=1, max_length=4_000)
    collection_ids: tuple[uuid.UUID, ...] = Field(default_factory=tuple, max_length=100)
    document_ids: tuple[uuid.UUID, ...] = Field(default_factory=tuple, max_length=100)
    web_search: bool = False
    client_request_id: uuid.UUID

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message cannot be blank")
        return normalized

    @field_validator("collection_ids", "document_ids")
    @classmethod
    def reject_duplicate_scope_ids(
        cls, values: tuple[uuid.UUID, ...]
    ) -> tuple[uuid.UUID, ...]:
        if len(values) != len(set(values)):
            raise ValueError("scope identifiers cannot contain duplicates")
        return values


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marker: str = Field(pattern=r"^\[S[1-9][0-9]*\]$")
    source_id: str = Field(pattern=r"^S[1-9][0-9]*$")
    source_type: Literal["document", "web"]
    title: str = Field(min_length=1, max_length=1_024)
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    source_filename: str | None = Field(default=None, max_length=1_024)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    uri: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="after")
    def validate_source_target(self) -> Self:
        if self.source_type == "document" and (
            self.document_id is None or self.document_version_id is None
        ):
            raise ValueError("document citation requires document and version identifiers")
        if self.source_type == "web" and self.uri is None:
            raise ValueError("web citation requires a URI")
        return self


class UsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class StreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["start", "status", "delta", "replace", "citations", "done", "error"]
    data: dict[str, Any]


class StoredChatReplay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    assistant_message_id: uuid.UUID
    events: tuple[StreamEvent, ...]


class AcceptedChat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    request: ChatRequest
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    user_message_id: uuid.UUID
    idempotency_key: str
    replay: StoredChatReplay | None = None
