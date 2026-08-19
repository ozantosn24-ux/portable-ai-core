"""`torch` + `transformers` gerektiren tek modül — ÇEKİRDEK BUNU IMPORT ETMEZ.

Ayrı dosyada olmasının sebebi mimari: `e5_embedding.py` port sözleşmesini ve önek
kuralını taşır ve test edilebilir kalır; ağır bağımlılık yalnız burada, yalnız gerçek
kullanımda yüklenir (`e5_embedding._load_encoder` tembel import eder).

Teknik, ayrı bir dahili baseline aracıyla birebir aynıdır ve orada donmuş bir qrel'e
karşı ölçülmüştür: **attention-mask mean pooling + L2 normalize**.
⚠️ Önek (`query: ` / `passage: `) BURADA UYGULANMAZ — çağıran taraf (adaptör) uygular.
Tek yerde olması, yanlış önekle sessizce yanlış vektör üretilmesini engeller.
"""

from __future__ import annotations

from collections.abc import Sequence


class TorchE5Encoder:  # pragma: no cover - model indirmesi gerektirir
    """Metinleri fp32 CPU vektörlerine çevirir. Durum taşır: model bir kez yüklenir."""

    def __init__(
        self, model_name: str, *, revision: str | None = None, torch_threads: int = 4
    ) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        torch.set_num_threads(torch_threads)
        self._torch = torch
        # `revision` HF commit SHA'si ya da tag'i; None ise `main` cekilir (PINSIZ).
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, revision=revision)
        self._model = AutoModel.from_pretrained(model_name, revision=revision)
        self._model.eval()

    def encode(
        self,
        texts: Sequence[str],
        *,
        max_length: int = 128,
        batch_size: int = 16,
    ) -> list[list[float]]:
        torch = self._torch
        out: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            chunk = list(texts[start : start + batch_size])
            encoded = self._tokenizer(
                chunk,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            with torch.no_grad():
                hidden = self._model(**encoded).last_hidden_state
            # Attention-mask mean pooling: padding token'lari ortalamaya KATILMAZ.
            # Duz `hidden.mean(1)` yaygin ve SESSIZ bir hatadir — kisa metinlerde
            # padding vektorleri sinyali seyreltir.
            mask = encoded["attention_mask"].unsqueeze(-1).to(dtype=hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
            normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out.extend(normalized.cpu().float().tolist())
        return out
