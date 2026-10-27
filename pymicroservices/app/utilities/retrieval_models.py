"""Lazy offline BGE models; threaded inference keeps the API event loop responsive."""
import asyncio
import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)


class ModelUnavailable(Exception):
    pass


def cache_dir(settings):
    path = Path(settings.embedding_cache_dir).expanduser()
    return path if path.is_absolute() else (Path(__file__).resolve().parents[3] / path).resolve()


class BGEM3:
    def __init__(self, settings):
        self.settings = settings
        self._tokenizer = self._model = self._device = None
        self._lock = threading.Lock()
        os.environ.setdefault('DISABLE_SAFETENSORS_CONVERSION', '1')
        os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')

    def _load_tokenizer(self):
        if self._tokenizer is None:
            try:
                from transformers import AutoTokenizer
                self._tokenizer = AutoTokenizer.from_pretrained(
                    self.settings.embedding_model, revision=self.settings.embedding_model_revision,
                    cache_dir=cache_dir(self.settings), local_files_only=True)
            except (ImportError, OSError):
                raise ModelUnavailable('Embedding model or dependencies are unavailable locally.') from None
        return self._tokenizer

    def _load_model(self):
        if self._model is None:
            try:
                import torch
                from transformers import AutoModel
                device = self.settings.embedding_device
                if device == 'auto':
                    device = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
                model = AutoModel.from_pretrained(
                    self.settings.embedding_model, revision=self.settings.embedding_model_revision,
                    cache_dir=cache_dir(self.settings), local_files_only=True)
                self._model, self._device = model.to(device).eval(), device
            except (ImportError, OSError):
                raise ModelUnavailable('Embedding model or dependencies are unavailable locally.') from None
        return self._model

    def embed(self, texts):
        with self._lock:
            tokenizer, model = self._load_tokenizer(), self._load_model()
            import torch
            batch = tokenizer(texts, padding=True, truncation=True, max_length=self.settings.embedding_max_length,
                              return_tensors='pt').to(self._device)
            with torch.inference_mode():
                vectors = model(**batch).last_hidden_state[:, 0]
                vectors = torch.nn.functional.normalize(vectors, p=2, dim=-1)
            return vectors.float().cpu().tolist()

    async def embed_async(self, texts):
        return await asyncio.to_thread(self.embed, texts)

    def warmup(self):
        with self._lock:
            self._load_tokenizer()
            self._load_model()


class Reranker:
    def __init__(self, settings):
        self.settings = settings
        self._tokenizer = self._model = self._device = None
        self._lock = threading.Lock()
        os.environ.setdefault('DISABLE_SAFETENSORS_CONVERSION', '1')
        os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')

    def _load(self):
        if self._model is None:
            try:
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer
                options = {'revision': self.settings.reranker_revision, 'cache_dir': cache_dir(self.settings),
                           'local_files_only': True}
                tokenizer = AutoTokenizer.from_pretrained(self.settings.reranker_model, **options)
                model = AutoModelForSequenceClassification.from_pretrained(self.settings.reranker_model, **options)
                device = self.settings.embedding_device
                if device == 'auto':
                    device = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
                self._tokenizer, self._model, self._device = tokenizer, model.to(device).eval(), device
            except (ImportError, OSError):
                raise ModelUnavailable('Reranker model or dependencies are unavailable locally.') from None

    def score(self, query, passages):
        with self._lock:
            self._load()
            import torch
            scores = []
            for start in range(0, len(passages), 8):
                pairs = [(query, passage) for passage in passages[start:start + 8]]
                batch = self._tokenizer(pairs, padding=True, truncation=True, max_length=512,
                                        return_tensors='pt').to(self._device)
                with torch.inference_mode():
                    scores.extend(self._model(**batch).logits.view(-1).float().cpu().tolist())
            return scores

    async def score_async(self, query, passages):
        return await asyncio.to_thread(self.score, query, passages)

    def warmup(self):
        with self._lock:
            self._load()


async def warmup_models(embedder, reranker, settings):
    for model in [embedder] + ([reranker] if settings.reranker_enabled else []):
        try:
            await asyncio.to_thread(model.warmup)
        except Exception as error:
            logger.warning('Retrieval warmup unavailable (%s).', type(error).__name__)


def download_reranker():
    from huggingface_hub import snapshot_download
    from .secrets_control import get_settings
    settings = get_settings()
    snapshot_download(settings.reranker_model, revision=settings.reranker_revision,
        cache_dir=cache_dir(settings), allow_patterns=['config.json', 'model.safetensors', 'tokenizer.json',
            'tokenizer_config.json', 'special_tokens_map.json', 'sentencepiece.bpe.model'])


if __name__ == '__main__':
    download_reranker()
