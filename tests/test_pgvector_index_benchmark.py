import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import benchmark_pgvector_indexes as benchmark  # noqa: E402


def test_top_k_overlap_uses_exact_result_as_ground_truth() -> None:
    assert benchmark._top_k_overlap([1, 2, 3, 4, 5], [5, 4, 3, 9, 8]) == 0.6


def test_query_vectors_are_deterministic_for_each_filter_slice() -> None:
    for filter_slice in benchmark.FILTER_SLICES:
        first = benchmark._query_vectors(
            rows=1_000,
            queries=3,
            dimensions=8,
            seed=42,
            filter_slice=filter_slice,
        )
        second = benchmark._query_vectors(
            rows=1_000,
            queries=3,
            dimensions=8,
            seed=42,
            filter_slice=filter_slice,
        )
        assert first == second
        assert len(first) == 3
        assert all(vector.startswith("[") and vector.endswith("]") for vector in first)


def test_plan_summary_collects_nested_index_names() -> None:
    summary = benchmark._plan_summary(
        [
            {
                "Plan": {
                    "Node Type": "Limit",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Index Name": "ann_hnsw_example",
                        }
                    ],
                }
            }
        ]
    )

    assert summary == {
        "node_types": ["Limit", "Index Scan"],
        "index_names": ["ann_hnsw_example"],
    }


def test_embedded_database_password_is_rejected_without_echoing_it() -> None:
    marker = "benchmark-secret-marker"
    with pytest.raises(SystemExit, match="passfile") as exc_info:
        benchmark._validate_database_url(f"postgresql://wozto:{marker}@127.0.0.1:55432/wozto_rag")
    assert marker not in str(exc_info.value)


def test_argument_gate_preserves_one_percent_top_k_slice() -> None:
    args = argparse.Namespace(
        rows=500,
        dimensions=64,
        queries=10,
        warmup=2,
        top_k=6,
        seed=42,
        hnsw_ef_search=100,
        output=None,
    )
    with pytest.raises(SystemExit, match="one-percent"):
        benchmark._validate_args(args)
