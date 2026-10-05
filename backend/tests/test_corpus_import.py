from __future__ import annotations

import pytest
from atlasflow.config import PROJECT_ROOT
from atlasflow.knowledge.manifest import load_manifest

from scripts.import_llm_corpus import DEFAULT_SOURCES, source_path, source_url, validate_content


def test_initial_corpus_has_explicit_official_download_urls() -> None:
    manifest = load_manifest(PROJECT_ROOT / "corpus_manifests/llm-engineering-v1.yaml")
    sources = {source.source_id: source for source in manifest.sources}

    assert len(DEFAULT_SOURCES) == 18
    assert all(source_id in sources for source_id in DEFAULT_SOURCES)
    assert source_url(sources["attention-is-all-you-need"]) == (
        "https://arxiv.org/pdf/1706.03762"
    )
    assert source_url(sources["transformers-kv-cache"]) == (
        "https://raw.githubusercontent.com/huggingface/transformers/main/"
        "docs/source/en/kv_cache.md"
    )
    assert source_url(sources["llm-survey-2026"]) == (
        "https://link.springer.com/content/pdf/10.1007/s11704-026-60308-3.pdf"
    )
    assert source_path(PROJECT_ROOT / "data/knowledge/corpus", sources["react"]).name == (
        "react.pdf"
    )


def test_source_validation_rejects_error_pages() -> None:
    manifest = load_manifest(PROJECT_ROOT / "corpus_manifests/llm-engineering-v1.yaml")
    sources = {source.source_id: source for source in manifest.sources}

    with pytest.raises(ValueError, match="not a PDF"):
        validate_content(sources["react"], b"<html>" + b"x" * 100)
    with pytest.raises(ValueError, match="HTML, not Markdown"):
        validate_content(sources["transformers-kv-cache"], b"<html>" + b"x" * 100)


def test_source_id_cannot_escape_corpus_directory() -> None:
    manifest = load_manifest(PROJECT_ROOT / "corpus_manifests/llm-engineering-v1.yaml")
    malicious = manifest.sources[0].model_copy(update={"source_id": "../outside"})

    with pytest.raises(ValueError, match="invalid source ID"):
        source_path(PROJECT_ROOT / "data/knowledge/corpus", malicious)
