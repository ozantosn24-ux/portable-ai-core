"""Torch implementation hidden behind the optional pairwise scorer adapter."""

from __future__ import annotations

from collections.abc import Sequence

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


class TorchSequenceClassificationEncoder:
    def __init__(
        self,
        model_name: str,
        *,
        revision: str,
        positive_label_index: int | None,
    ) -> None:
        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            revision=revision,
            trust_remote_code=False,
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            model_name,
            revision=revision,
            trust_remote_code=False,
            use_safetensors=True,
        )
        self._model.eval()
        self._positive_label_index = positive_label_index

    def score_pairs(
        self,
        pairs: Sequence[tuple[str, str]],
        *,
        max_length: int,
        batch_size: int,
    ) -> tuple[float, ...]:
        scores: list[float] = []
        for start in range(0, len(pairs), batch_size):
            batch = pairs[start : start + batch_size]
            encoded = self._tokenizer(
                [left for left, _ in batch],
                [right for _, right in batch],
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            with torch.inference_mode():
                logits = self._model(**encoded).logits
            if logits.shape[-1] == 1:
                probabilities = torch.sigmoid(logits[:, 0])
            else:
                if self._positive_label_index is None or self._positive_label_index >= logits.shape[-1]:
                    raise ValueError("positive_label_index does not match model labels")
                probabilities = torch.softmax(logits, dim=-1)[:, self._positive_label_index]
            scores.extend(float(value) for value in probabilities.tolist())
        return tuple(scores)
