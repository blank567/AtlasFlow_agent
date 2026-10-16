from __future__ import annotations

from hashlib import sha256
from importlib import resources
from pathlib import Path

from atlasflow.evaluation.contracts import EvaluationDataset

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class DatasetRegistry:
    def __init__(
        self, datasets: list[EvaluationDataset] | None = None, *, root: Path | None = None
    ):
        self.root = root or PROJECT_ROOT
        if datasets is None:
            datasets = []
            for kind in ("rag", "agent"):
                local = self.root / f"evals/{kind}/dataset.json"
                packaged = resources.files("atlasflow.evaluation").joinpath(f"data/{kind}.json")
                selected = local if local.is_file() else packaged
                if selected.is_file():
                    datasets.append(
                        EvaluationDataset.model_validate_json(selected.read_text(encoding="utf-8"))
                    )
        self.datasets = {dataset.dataset_id: dataset for dataset in datasets}

    def get(self, dataset_id: str) -> EvaluationDataset:
        if dataset_id not in self.datasets:
            raise KeyError("evaluation dataset not found")
        return self.datasets[dataset_id]

    def list(self) -> list[EvaluationDataset]:
        return list(self.datasets.values())

    def validate_local_sources(self, dataset: EvaluationDataset) -> list[str]:
        errors: list[str] = []
        checked: set[str] = set()
        for case in dataset.cases:
            for source in case.expected_sources:
                if source.source_path in checked:
                    continue
                checked.add(source.source_path)
                path = (self.root / source.source_path).resolve()
                if not path.is_relative_to(self.root.resolve()):
                    errors.append(f"unsafe source path: {source.source_id}")
                elif not path.is_file():
                    errors.append(f"local source missing: {source.source_id}")
                elif sha256(path.read_bytes()).hexdigest() != source.content_hash:
                    errors.append(f"local source hash changed: {source.source_id}")
        return errors
