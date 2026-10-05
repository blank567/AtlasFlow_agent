from pathlib import Path

from atlasflow.knowledge.manifest import load_manifest


def test_llm_engineering_manifest_is_metadata_only_and_has_18_unique_sources() -> None:
    path = Path(__file__).resolve().parents[2] / "corpus_manifests" / "llm-engineering-v1.yaml"
    manifest = load_manifest(path)

    assert manifest.space == "llm-engineering-v1"
    assert manifest.auto_download is False
    assert len(manifest.sources) == 18
    assert len({item.source_id for item in manifest.sources}) == 18
    assert all("utm_source" not in item.canonical_uri for item in manifest.sources)
    assert all(
        item.license_spdx_id
        for item in manifest.sources
        if item.ingestion_policy == "redistributable_with_notice"
    )
