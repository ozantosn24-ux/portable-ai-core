"""Optional Transformers adapter for calibrated text-pair classification scores.

The core remains lightweight. ``torch`` and ``transformers`` are imported only on
first use through the existing ``embeddings`` optional dependency group.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any


class TransformersTextPairScorer:
    """Return a positive-class probability for ordered text pairs."""

    def __init__(
        self,
        *,
        model_name: str,
        revision: str,
        positive_label_index: int | None,
        max_length: int = 512,
        batch_size: int = 8,
        encoder: Any | None = None,
    ) -> None:
        clean_model = model_name.strip()
        clean_revision = revision.strip()
        if not clean_model:
            raise ValueError("model_name must not be empty")
        if not clean_revision:
            raise ValueError("revision must not be empty")
        if positive_label_index is not None and positive_label_index < 0:
            raise ValueError("positive_label_index must be non-negative")
        if max_length < 8:
            raise ValueError("max_length must be at least 8")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._model_name = clean_model
        self._revision = clean_revision
        self._positive_label_index = positive_label_index
        self._max_length = max_length
        self._batch_size = batch_size
        self._encoder = encoder

    @property
    def model_id(self) -> str:
        label = "sigmoid" if self._positive_label_index is None else str(self._positive_label_index)
        return f"{self._model_name}@{self._revision}#positive={label}"

    async def score_pairs(
        self,
        pairs: Sequence[tuple[str, str]],
    ) -> Sequence[float]:
        if not pairs:
            return ()
        if self._encoder is None:
            self._encoder = _load_encoder(
                self._model_name,
                self._revision,
                self._positive_label_index,
            )
        return await asyncio.to_thread(
            self._encoder.score_pairs,
            tuple(pairs),
            max_length=self._max_length,
            batch_size=self._batch_size,
        )


def _load_encoder(
    model_name: str,
    revision: str,
    positive_label_index: int | None,
) -> Any:  # pragma: no cover
    try:
        from ._pairwise_torch import TorchSequenceClassificationEncoder
    except ImportError as exc:
        raise RuntimeError(
            "TransformersTextPairScorer requires `torch` and `transformers`. "
            'Install with: pip install -e ".[embeddings]".'
        ) from exc
    return TorchSequenceClassificationEncoder(
        model_name,
        revision=revision,
        positive_label_index=positive_label_index,
    )
