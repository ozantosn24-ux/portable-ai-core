from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.pairwise_scorer import TransformersTextPairScorer


class _SpyEncoder:
    def __init__(self) -> None:
        self.calls = []

    def score_pairs(self, pairs, *, max_length: int, batch_size: int):
        self.calls.append(
            {
                "pairs": pairs,
                "max_length": max_length,
                "batch_size": batch_size,
            }
        )
        return tuple(0.8 for _ in pairs)


def _scorer(encoder=None, **kwargs) -> TransformersTextPairScorer:
    return TransformersTextPairScorer(
        model_name="example/model",
        revision="abc123",
        positive_label_index=0,
        encoder=encoder,
        **kwargs,
    )


def test_pairwise_scorer_passes_ordered_pairs_and_batch_settings() -> None:
    encoder = _SpyEncoder()
    scorer = _scorer(encoder=encoder, max_length=128, batch_size=2)
    pairs = (("evidence", "claim"), ("query", "answer"))

    scores = asyncio.run(scorer.score_pairs(pairs))

    assert scores == (0.8, 0.8)
    assert encoder.calls == [
        {
            "pairs": pairs,
            "max_length": 128,
            "batch_size": 2,
        }
    ]


def test_pairwise_scorer_empty_input_skips_encoder() -> None:
    encoder = _SpyEncoder()
    assert asyncio.run(_scorer(encoder=encoder).score_pairs(())) == ()
    assert encoder.calls == []


def test_pairwise_scorer_model_id_records_revision_and_label_contract() -> None:
    assert _scorer(encoder=_SpyEncoder()).model_id == "example/model@abc123#positive=0"
    sigmoid = TransformersTextPairScorer(
        model_name="example/ranker",
        revision="def456",
        positive_label_index=None,
        encoder=_SpyEncoder(),
    )
    assert sigmoid.model_id == "example/ranker@def456#positive=sigmoid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_name": ""},
        {"revision": ""},
        {"positive_label_index": -1},
        {"max_length": 4},
        {"batch_size": 0},
    ],
)
def test_pairwise_scorer_rejects_invalid_configuration(kwargs) -> None:
    base = {
        "model_name": "example/model",
        "revision": "abc123",
        "positive_label_index": 0,
        "encoder": _SpyEncoder(),
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        TransformersTextPairScorer(**base)


def test_pairwise_scorer_missing_optional_dependency_is_actionable() -> None:
    import builtins

    from wozto_ai_reference import pairwise_scorer

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.endswith("_pairwise_torch"):
            raise ImportError("simulated missing optional dependency")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        with pytest.raises(RuntimeError, match="embeddings"):
            pairwise_scorer._load_encoder("example/model", "abc123", 0)
    finally:
        builtins.__import__ = real_import
