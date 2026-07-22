from __future__ import annotations

import asyncio
import uuid

import orjson

from app.config import get_settings
from app.ingest.embedder import TeiClient
from app.ingest.hashing import sparse_lexical_vector
from app.ingest.indexer import IndexPoint, QdrantChunkIndex
from app.ingest.normalization import normalize_lexical
from app.rag.planner import VllmPlanner
from app.rag.retriever import QdrantHybridSearch
from app.rag.scope import ResolvedScope


async def run() -> dict[str, int | str | bool]:
    settings = get_settings()
    planner = VllmPlanner(
        str(settings.vllm_base_url),
        settings.vllm_model,
        timeout_seconds=30,
    )
    planning = await planner.plan('Teknik koleksiyonundaki "ZX-42" ağ ayar\u0131 nedir?')
    if planning.used_fallback or planning.plan.intent != "knowledge":
        raise RuntimeError("live planner did not return a valid knowledge plan")

    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    relevant_document = uuid.uuid4()
    distractor_document = uuid.uuid4()
    relevant_version = uuid.uuid4()
    distractor_version = uuid.uuid4()
    other_version = uuid.uuid4()
    section_id = uuid.uuid4()
    relevant_id = uuid.uuid4()
    distractor_id = uuid.uuid4()
    other_id = uuid.uuid4()
    texts = [
        "ZX-42 ağ ayar\u0131 güvenli VLAN üzerinde etkinleştirilir.",
        "This unrelated paragraph describes cafeteria opening hours.",
        "ZX-42 cross-tenant content must never be visible.",
    ]
    tei = TeiClient(str(settings.embed_url), expected_dimension=settings.embed_dim)
    dense = await tei.embed(texts)
    query_dense = (await tei.embed([planning.plan.query]))[0]
    index = QdrantChunkIndex(
        str(settings.qdrant_url),
        settings.qdrant_collection,
        dense_dimension=settings.embed_dim,
    )
    search = QdrantHybridSearch(str(settings.qdrant_url), settings.qdrant_collection)
    indexed = False
    try:
        await index.prepare()
        points = [
            _point(
                relevant_id,
                dense[0],
                texts[0],
                tenant_id=tenant_id,
                document_id=relevant_document,
                version_id=relevant_version,
                section_id=section_id,
            ),
            _point(
                distractor_id,
                dense[1],
                texts[1],
                tenant_id=tenant_id,
                document_id=distractor_document,
                version_id=distractor_version,
                section_id=section_id,
            ),
            _point(
                other_id,
                dense[2],
                texts[2],
                tenant_id=other_tenant_id,
                document_id=uuid.uuid4(),
                version_id=other_version,
                section_id=uuid.uuid4(),
            ),
        ]
        await index.upsert(points)
        indexed = True
        scope = ResolvedScope(
            tenant_id=tenant_id,
            generation_id=1,
            retrieval_revision=1,
            document_ids=(relevant_document, distractor_document),
            version_ids=(relevant_version, distractor_version),
        )
        branches = await search.query_branches(
            dense=query_dense,
            sparse=sparse_lexical_vector(normalize_lexical(planning.plan.query)),
            scope=scope,
            dense_limit=10,
            sparse_limit=10,
        )
        dense_ids = {item.chunk_id for item in branches.dense}
        sparse_ids = {item.chunk_id for item in branches.sparse}
        if other_id in dense_ids | sparse_ids:
            raise RuntimeError("cross-tenant point escaped the P5 filter")
        if relevant_id not in dense_ids or relevant_id not in sparse_ids:
            raise RuntimeError("relevant point was missing from a hybrid branch")
        return {
            "status": "ok",
            "planner_fallback": planning.used_fallback,
            "dense_candidates": len(branches.dense),
            "sparse_candidates": len(branches.sparse),
            "dense_dimension": len(query_dense),
        }
    finally:
        if indexed:
            await index.delete_versions(
                tenant_id=tenant_id,
                version_ids=[relevant_version, distractor_version],
            )
            await index.delete_versions(
                tenant_id=other_tenant_id,
                version_ids=[other_version],
            )
        await search.close()
        await index.close()


def _point(
    point_id: uuid.UUID,
    dense: list[float],
    text: str,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    section_id: uuid.UUID,
) -> IndexPoint:
    lexical = normalize_lexical(text)
    sparse = sparse_lexical_vector(lexical)
    return IndexPoint(
        id=point_id,
        dense=dense,
        sparse_indices=sparse.indices,
        sparse_values=sparse.values,
        payload={
            "tenant_id": str(tenant_id),
            "created_generation_id": 1,
            "document_id": str(document_id),
            "document_version_id": str(version_id),
            "collection_ids": [],
            "section_id": str(section_id),
            "section_path_original": "Smoke",
            "section_path_lexical": "smoke",
            "page_start": 1,
            "page_end": 1,
            "char_start": 0,
            "char_end": len(text),
            "occurrence_index": 0,
            "content_sha256": "0" * 64,
            "lexical_sha256": "1" * 64,
            "text_original": text,
            "text_lexical": lexical,
        },
    )


def main() -> None:
    print(orjson.dumps(asyncio.run(run())).decode())


if __name__ == "__main__":
    main()
