from __future__ import annotations

import argparse
from pathlib import Path

from atlasflow.knowledge.manifest import load_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlasflow-knowledge",
        description="Validate AtlasFlow corpus manifests without downloading source content.",
    )
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    manifest = load_manifest(arguments.manifest)
    policies: dict[str, int] = {}
    for source in manifest.sources:
        policies[source.ingestion_policy] = policies.get(source.ingestion_policy, 0) + 1
    print(
        f"valid manifest: space={manifest.space} sources={len(manifest.sources)} "
        f"auto_download={manifest.auto_download} policies={policies}"
    )


if __name__ == "__main__":
    main()
