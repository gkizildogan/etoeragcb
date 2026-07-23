from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.evaluation.schemas import CorpusRecord, DatasetBundle, GoldenManifest, GoldenQuery

ModelT = TypeVar("ModelT", bound=BaseModel)


class DatasetError(RuntimeError):
    pass


def load_dataset(root: Path) -> DatasetBundle:
    manifest_path = root / "manifest.json"
    try:
        manifest = GoldenManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise DatasetError("cannot load golden-set manifest") from exc
    corpus_path = root / manifest.corpus.path
    queries_path = root / manifest.queries.path
    corpus_raw = _read(corpus_path)
    queries_raw = _read(queries_path)
    if _sha256(corpus_raw) != manifest.corpus.sha256:
        raise DatasetError("corpus hash does not match manifest")
    if _sha256(queries_raw) != manifest.queries.sha256:
        raise DatasetError("query hash does not match manifest")
    if dataset_sha256(corpus_raw, queries_raw) != manifest.dataset_sha256:
        raise DatasetError("combined dataset hash does not match manifest")
    corpus = _parse_jsonl(corpus_raw, CorpusRecord, "corpus")
    queries = _parse_jsonl(queries_raw, GoldenQuery, "queries")
    if len(corpus) != manifest.corpus.records or len(queries) != manifest.queries.records:
        raise DatasetError("record counts do not match manifest")
    _validate_references(corpus, queries)
    _validate_coverage(queries)
    return DatasetBundle(manifest=manifest, corpus=corpus, queries=queries)


def dataset_sha256(corpus_raw: bytes, queries_raw: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"corpus.jsonl\x00")
    digest.update(corpus_raw)
    digest.update(b"\x00queries.jsonl\x00")
    digest.update(queries_raw)
    return digest.hexdigest()


def evaluator_sha256(package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_root.glob("*.py")):
        if path.name == "__pycache__":
            continue
        digest.update(path.name.encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"cannot read golden-set file {path.name}") from exc


def _parse_jsonl(raw: bytes, model: type[ModelT], label: str) -> tuple[ModelT, ...]:
    records: list[ModelT] = []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{label} is not UTF-8") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise DatasetError(f"invalid {label} record on line {line_number}") from exc
    if not records:
        raise DatasetError(f"{label} is empty")
    identifiers = [str(record.model_dump().get("id")) for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise DatasetError(f"{label} contains duplicate identifiers")
    return tuple(records)


def _validate_references(
    corpus: tuple[CorpusRecord, ...],
    queries: tuple[GoldenQuery, ...],
) -> None:
    corpus_ids = {item.id for item in corpus}
    document_ids = {item.document_id for item in corpus}
    collection_ids = {value for item in corpus for value in item.collection_ids}
    headings = {item.heading.casefold() for item in corpus}
    for query in queries:
        if not set(query.relevance).issubset(corpus_ids):
            raise DatasetError(f"{query.id} references an unknown relevant record")
        if not set(query.scope.document_ids).issubset(document_ids):
            raise DatasetError(f"{query.id} references an unknown document scope")
        if not set(query.scope.collection_ids).issubset(collection_ids):
            raise DatasetError(f"{query.id} references an unknown collection scope")
        if not {value.casefold() for value in query.scope.headings}.issubset(headings):
            raise DatasetError(f"{query.id} references an unknown heading scope")
        if not set(query.boost_document_ids).issubset(document_ids):
            raise DatasetError(f"{query.id} references an unknown hint boost")
        if not query.web_search and any(
            item.source_type == "web" for item in corpus if item.id in query.relevance
        ):
            raise DatasetError(f"{query.id} labels web evidence without enabling web search")


def _validate_coverage(queries: tuple[GoldenQuery, ...]) -> None:
    categories = {item.category for item in queries}
    required = {
        "exact_id",
        "heading",
        "collection",
        "semantic",
        "repeated_passage",
        "scoped",
        "web_document",
        "ambiguous_hint",
        "unanswerable",
    }
    if categories != required:
        raise DatasetError("golden set does not cover every required query category")
    if {item.language for item in queries} != {"en", "tr"}:
        raise DatasetError("golden set must contain English and Turkish queries")
    if sum(not item.answerable for item in queries) < 5:
        raise DatasetError("golden set requires at least five unanswerable queries")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
