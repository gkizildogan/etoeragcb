from __future__ import annotations

import asyncio
import uuid

import orjson

from app.config import get_settings
from app.ingest.embedder import TeiClient
from app.ingest.hashing import sparse_lexical_vector
from app.ingest.indexer import IndexPoint, QdrantChunkIndex
from app.ingest.normalization import normalize_lexical


async def run() -> dict[str, int | str]:
    settings = get_settings()
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    section_id = uuid.uuid4()
    point_id = uuid.uuid4()
    text = "Merhaba dünya. Dense and sparse staged ingestion smoke test."
    lexical = normalize_lexical(text)
    tei = TeiClient(str(settings.embed_url), expected_dimension=settings.embed_dim)
    index = QdrantChunkIndex(
        str(settings.qdrant_url),
        settings.qdrant_collection,
        dense_dimension=settings.embed_dim,
    )
    indexed = False
    try:
        spans = await tei.token_spans(text)
        if not spans or spans[-1].end != len(text):
            raise RuntimeError("TEI token offsets were not converted to character offsets")
        dense = (await tei.embed([text]))[0]
        sparse = sparse_lexical_vector(lexical)
        await index.prepare()
        await index.upsert(
            [
                IndexPoint(
                    id=point_id,
                    dense=dense,
                    sparse_indices=sparse.indices,
                    sparse_values=sparse.values,
                    payload={
                        "tenant_id": str(tenant_id),
                        "created_generation_id": 0,
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
            ]
        )
        indexed = True
        await index.validate_version(
            tenant_id=tenant_id,
            version_id=version_id,
            expected_count=1,
            sample_id=point_id,
        )
        return {
            "status": "ok",
            "token_count": len(spans),
            "dense_dimension": len(dense),
            "sparse_terms": len(sparse.indices),
        }
    finally:
        if indexed:
            await index.delete_versions(tenant_id=tenant_id, version_ids=[version_id])
        await index.close()


def main() -> None:
    print(orjson.dumps(asyncio.run(run())).decode())


if __name__ == "__main__":
    main()
