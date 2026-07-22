from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from qdrant_client import AsyncQdrantClient, models


class IndexValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IndexPoint:
    id: uuid.UUID
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    payload: dict[str, Any]


class ChunkIndex(Protocol):
    async def prepare(self) -> None: ...

    async def upsert(self, points: list[IndexPoint]) -> None: ...

    async def validate_version(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_count: int,
        sample_id: uuid.UUID,
    ) -> None: ...

    async def delete_versions(
        self, *, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]
    ) -> None: ...

    async def close(self) -> None: ...


class QdrantChunkIndex:
    def __init__(self, url: str, collection: str, *, dense_dimension: int) -> None:
        self._client = AsyncQdrantClient(url=url)
        self._collection = collection
        self._dense_dimension = dense_dimension
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def prepare(self) -> None:
        async with self._prepare_lock:
            if self._prepared:
                return
            if not await self._client.collection_exists(self._collection):
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config={
                        "dense": models.VectorParams(
                            size=self._dense_dimension, distance=models.Distance.COSINE
                        )
                    },
                    sparse_vectors_config={
                        "sparse": models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=True)
                        )
                    },
                    on_disk_payload=True,
                )
            indexes: dict[str, models.PayloadSchemaType | models.TextIndexParams] = {
                "tenant_id": models.PayloadSchemaType.KEYWORD,
                "created_generation_id": models.PayloadSchemaType.INTEGER,
                "document_id": models.PayloadSchemaType.KEYWORD,
                "document_version_id": models.PayloadSchemaType.KEYWORD,
                "collection_ids": models.PayloadSchemaType.KEYWORD,
                "section_id": models.PayloadSchemaType.KEYWORD,
                "page_start": models.PayloadSchemaType.INTEGER,
                "page_end": models.PayloadSchemaType.INTEGER,
                "content_sha256": models.PayloadSchemaType.KEYWORD,
                "lexical_sha256": models.PayloadSchemaType.KEYWORD,
                "text_lexical": models.TextIndexParams(
                    type="text", tokenizer=models.TokenizerType.WORD, lowercase=False
                ),
            }
            for field_name, field_schema in indexes.items():
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=field_schema,
                    wait=True,
                )
            self._prepared = True

    async def upsert(self, points: list[IndexPoint]) -> None:
        if not points:
            return
        await self._client.upsert(
            collection_name=self._collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=str(point.id),
                    vector={
                        "dense": point.dense,
                        "sparse": models.SparseVector(
                            indices=point.sparse_indices, values=point.sparse_values
                        ),
                    },
                    payload=point.payload,
                )
                for point in points
            ],
        )

    async def validate_version(
        self,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_count: int,
        sample_id: uuid.UUID,
    ) -> None:
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=_version_filter(tenant_id, [version_id]),
            exact=True,
        )
        if result.count != expected_count:
            raise IndexValidationError(
                f"Qdrant count mismatch: expected {expected_count}, found {result.count}"
            )
        sample = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(sample_id)],
            with_payload=True,
            with_vectors=False,
        )
        if (
            len(sample) != 1
            or sample[0].payload is None
            or sample[0].payload.get("document_version_id") != str(version_id)
            or sample[0].payload.get("tenant_id") != str(tenant_id)
        ):
            raise IndexValidationError("Qdrant sample point is missing or has an invalid scope")

    async def delete_versions(self, *, tenant_id: uuid.UUID, version_ids: list[uuid.UUID]) -> None:
        if not version_ids:
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=_version_filter(tenant_id, version_ids)),
            wait=True,
        )

    async def close(self) -> None:
        await self._client.close()


def _version_filter(tenant_id: uuid.UUID, version_ids: list[uuid.UUID]) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=str(tenant_id))),
            models.FieldCondition(
                key="document_version_id",
                match=models.MatchAny(any=[str(version_id) for version_id in version_ids]),
            ),
        ]
    )
