from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easy_office.core import EasyOfficeCore  # noqa: E402


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@pytest.fixture
def core_factory(tmp_path):
    """Create isolated cores, each with its own config and data directory."""
    cores: list[EasyOfficeCore] = []
    counter = {"n": 0}

    def make(**patch: Any) -> EasyOfficeCore:
        counter["n"] += 1
        base = tmp_path / f"case{counter['n']}"
        base.mkdir(parents=True, exist_ok=True)
        merged = _deep_merge({"embedding": {"embedding_dim": 8}}, patch)
        core = EasyOfficeCore.bootstrap(
            config_path=base / "config.yaml",
            data_dir=base / "data",
            config_patch=merged,
        )
        cores.append(core)
        return core

    yield make
    for core in cores:
        core.close()


@pytest.fixture
def core(core_factory) -> EasyOfficeCore:
    return core_factory()
