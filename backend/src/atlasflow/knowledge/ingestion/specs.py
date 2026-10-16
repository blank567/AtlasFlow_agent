"""Capture the actual local parsing/chunking configuration without provider calls."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from atlasflow.knowledge.domain.index_specs import EmbeddingSpec, IndexBuildSpec, stable_hash
from atlasflow.knowledge.domain.models import CURRENT_CHUNKER_VERSION, CURRENT_PARSER_VERSION
from atlasflow.knowledge.ingestion.chunker import StructuralChunker


def capture_build_spec(embedding: EmbeddingSpec, chunker: StructuralChunker) -> IndexBuildSpec:
    dependencies = {}
    for name in ("pypdf", "jieba"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "not-installed"
    root = Path(__file__).parent
    files = [root / "chunker.py", *(root / "parsers").glob("*.py")]
    implementation = {str(path.relative_to(root)).replace("\\", "/"): path.read_text(encoding="utf-8")
                      for path in sorted(files)}
    return IndexBuildSpec(
        parser_version=CURRENT_PARSER_VERSION, chunker_version=CURRENT_CHUNKER_VERSION,
        child_tokens=chunker.child_tokens, parent_tokens=chunker.parent_tokens,
        lexicalizer="regex-lowercase-v1" if dependencies["jieba"] == "not-installed" else "jieba-lowercase-v1",
        implementation_hash=stable_hash(implementation), dependencies=dependencies, embedding=embedding,
    )
