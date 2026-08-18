"""Model client interfaces for the extraction pipeline.

Thin wrappers with explicit lifecycle, no globals.
Models are loaded explicitly via context managers.
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when extraction fails after retries."""
    pass


def _free_cuda_memory() -> None:
    """Release cached GPU memory after a model is dropped.

    Extraction stages load one heavy model at a time in the same process
    (Maverick, then GLiNER); without this, PyTorch's caching allocator keeps
    holding a previous stage's memory and a later stage can OOM even though
    the earlier model is no longer referenced.
    """
    import gc
    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# ---- NuExtract Client ----

class NuExtractClient:
    """OpenAI-compatible client against the vLLM endpoint.

    NuExtract3 uses a native template mechanism via chat_template_kwargs.
    According to the spec, we prefer native templating + strict Pydantic validation
    over vLLM's guided_json if they conflict.

    Temperature is always 0 for deterministic output.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout_s: int = 120,
    ):
        """Initialize NuExtract client.

        Args:
            base_url: vLLM API base URL (e.g., http://localhost:8000/v1)
            model: Model identifier (e.g., numind/NuExtract3)
            temperature: Sampling temperature (should be 0.0 for extraction)
            max_retries: Maximum retry attempts on transient errors
            timeout_s: Request timeout in seconds
        """
        self.client = OpenAI(
            base_url=base_url,
            api_key="EMPTY",  # vLLM doesn't require an API key
            timeout=timeout_s,
            max_retries=0,  # We handle retries manually
        )
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries

    def extract(
        self,
        text: str,
        template: dict,
        examples: list[dict] | None = None,
    ) -> dict:
        """Extract structured data using NuExtract's native template mechanism.

        Args:
            text: Input text to extract from
            template: JSON schema defining fields to extract
            examples: Optional few-shot examples

        Returns:
            Parsed JSON response

        Raises:
            ExtractionError: On malformed output after retries or non-retryable errors

        Note: We use NuExtract3's native template mechanism via chat_template_kwargs.
        This is passed through extra_body since it's a vLLM extension.
        """
        # Build the chat_template_kwargs. NuExtract3's chat template splices
        # `template` directly into the prompt string (`'...' + template + '...'`),
        # so it must already be a JSON string, not a dict.
        chat_kwargs = {"template": json.dumps(template)}
        if examples:
            chat_kwargs["examples"] = examples

        messages = [
            {
                "role": "user",
                "content": text,
            }
        ]

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    extra_body={"chat_template_kwargs": chat_kwargs},
                )

                # Extract and parse the response
                content = response.choices[0].message.content
                if not content:
                    raise ExtractionError("Empty response from model")

                # Try to parse as JSON
                try:
                    result = json.loads(content)
                    return result
                except json.JSONDecodeError as e:
                    # Schema validation failure - don't retry at temperature 0
                    logger.error(f"Failed to parse JSON response: {e}")
                    logger.error(f"Raw response: {content}")
                    raise ExtractionError(f"Invalid JSON response: {e}") from e

            except OpenAIError as e:
                # Check if this is a retryable error
                is_retryable = (
                    "timeout" in str(e).lower()
                    or "connection" in str(e).lower()
                    or "500" in str(e)
                    or "502" in str(e)
                    or "503" in str(e)
                    or "504" in str(e)
                )

                if is_retryable and attempt < self.max_retries:
                    logger.warning(f"Retryable error on attempt {attempt + 1}: {e}")
                    continue
                else:
                    raise ExtractionError(f"API error: {e}") from e

        raise ExtractionError(f"Max retries ({self.max_retries}) exceeded")


# ---- GLiNER Runner ----

@dataclass
class RawSpan:
    """Raw span returned by GLiNER (before realignment)."""
    text: str
    label: str
    score: float
    start: int  # Relative to input text
    end: int    # Relative to input text


class GlinerRunner:
    """GLiNER model runner with explicit lifecycle.

    Use as a context manager to handle model loading/unloading.
    """

    def __init__(self, model_name: str, threshold: float = 0.5):
        """Initialize GLiNER runner.

        Args:
            model_name: HuggingFace model identifier
            threshold: Minimum confidence threshold
        """
        self.model_name = model_name
        self.threshold = threshold
        self.model = None

    def __enter__(self) -> "GlinerRunner":
        """Load the model."""
        try:
            from gliner import GLiNER
            logger.info(f"Loading GLiNER model: {self.model_name}")
            self.model = GLiNER.from_pretrained(self.model_name)
            return self
        except ImportError:
            raise ImportError("gliner package not installed. Run: uv add gliner")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unload the model and free its GPU memory."""
        self.model = None
        _free_cuda_memory()
        return False

    def predict_batch(
        self,
        texts: list[str],
        labels: list[str],
        threshold: float | None = None,
    ) -> list[list[RawSpan]]:
        """Predict entities in a batch of texts.

        Args:
            texts: List of text segments
            labels: Entity labels to extract
            threshold: Override default threshold

        Returns:
            List of lists of RawSpan, one list per input text
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Use as context manager.")

        threshold = threshold if threshold is not None else self.threshold

        # predict_entities takes a single string; batch_predict_entities is
        # the actual batch API and returns a list of dicts per input text.
        predictions = self.model.batch_predict_entities(
            texts,
            labels,
            threshold=threshold,
        )

        # Convert to our RawSpan format
        results = []
        for pred_list in predictions:
            spans = [
                RawSpan(
                    text=entity["text"],
                    label=entity["label"],
                    score=entity["score"],
                    start=entity["start"],
                    end=entity["end"],
                )
                for entity in pred_list
            ]
            results.append(spans)

        return results


def _fixed_maverick_preprocess(self, sample, speakers=None):
    """Drop-in replacement for Maverick.preprocess's plain-text path.

    Same tokenization and sentence splitting as the original; only the
    char-offset bookkeeping changes, from an accumulated `len(sentence) + 1`
    guess to locating each sentence's real position with `text.find()`.
    """
    from maverick.common.util import download_load_spacy, flatten
    from nltk import sent_tokenize

    nlp = download_load_spacy()
    char_offsets = []
    sentences = []
    search_from = 0
    sentence_strs = sent_tokenize(sample)
    for sent, sentence in zip(nlp.pipe(sentence_strs), sentence_strs):
        start = sample.find(sentence, search_from)
        if start == -1:
            start = search_from
        char_offsets.append([(start + tok.idx, start + tok.idx + len(tok.text) - 1) for tok in sent])
        sentences.append([tok.text for tok in sent])
        search_from = start + len(sentence)
    char_offsets = flatten(char_offsets)
    tokens = flatten(sentences)
    eos_len = [len(value) for value in sentences]
    eos = [sum(eos_len[0 : (i[0] + 1)]) for i in enumerate(eos_len)]
    if speakers is None:
        speakers = ["-"] * len(tokens)
    else:
        speakers = flatten(speakers)
    return tokens, eos, speakers, char_offsets


# ---- Maverick Coreference Runner ----

class CorefRunner:
    """Maverick coreference runner with explicit lifecycle.

    Use as a context manager to handle model loading/unloading.
    """

    def __init__(self, model_name: str):
        """Initialize coreference runner.

        Args:
            model_name: HuggingFace model identifier
        """
        self.model_name = model_name
        self.model = None

    def __enter__(self) -> "CorefRunner":
        """Load the model."""
        try:
            import torch
            from maverick import Maverick
            logger.info(f"Loading Maverick model: {self.model_name}")
            device = "cuda" if torch.cuda.is_available() else "cpu"
            # Maverick's checkpoint pickles its Lightning hyperparameters
            # (OmegaConf DictConfig, etc.), which torch>=2.6's new
            # weights_only=True default rejects one global at a time.
            # Restore the pre-2.6 weights_only=False default for this one
            # load of the pinned, trusted sapienzanlp HF checkpoint.
            original_load = torch.load

            def _unsafe_load(*args, **kwargs):
                kwargs["weights_only"] = False
                return original_load(*args, **kwargs)

            torch.load = _unsafe_load
            try:
                self.model = Maverick(hf_name_or_path=self.model_name, device=device)
            finally:
                torch.load = original_load
            # The DeBERTa encoder loads via AutoModel.from_pretrained with no
            # explicit dtype, which transformers can auto-select as fp16 from
            # the checkpoint's config while Maverick's own head layers (loaded
            # from the Lightning .ckpt) stay fp32, so matmuls between them
            # fail with a dtype mismatch. Force the whole model to one dtype.
            self.model.model = self.model.model.float()
            # Maverick's own preprocess() reconstructs each sentence's char
            # offset by accumulating len(sentence) + 1, assuming exactly one
            # separator character between sentences. Legal documents have
            # blank lines and multi-space section breaks, so that drifts from
            # the true offset and mention spans end up truncated (~4% of
            # mentions observed on real filings). Patch in a version that
            # locates each sentence's true position with text.find() instead;
            # tokenization and the model itself are unchanged.
            import types
            self.model.preprocess = types.MethodType(_fixed_maverick_preprocess, self.model)
            return self
        except ImportError:
            raise ImportError(
                "maverick-coref package not installed. Run: uv add maverick-coref"
            )

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unload the model and free its GPU memory."""
        self.model = None
        _free_cuda_memory()
        return False

    def predict(self, text: str) -> list[list[tuple[int, int]]]:
        """Predict coreference chains in text.

        Args:
            text: Input text

        Returns:
            List of mention chains, where each chain is a list of (start, end) tuples
            with character offsets into the input text.
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Use as context manager.")

        # Maverick's clusters_char_offsets gives (start, end) per mention where
        # `end` is the last character's own index (inclusive). Convert to the
        # exclusive end this codebase uses everywhere else (`text[start:end]`).
        result = self.model.predict(text)
        clusters = result.get("clusters_char_offsets") or []
        chains = [[(start, end + 1) for start, end in chain] for chain in clusters]

        return chains


# ---- GLiNER-Relex (Protocol) ----

@dataclass
class RawRelation:
    """Raw relation returned by Relex."""
    head: RawSpan
    relation: str
    tail: RawSpan
    score: float


@dataclass
class RawProtoEvent:
    """Raw proto-event from Relex."""
    predicate: RawSpan
    relations: list[RawRelation]


class RelexRunner(Protocol):
    """Protocol for relation extraction runners.

    This allows us to swap implementations or use a disabled runner.
    """

    def predict(
        self,
        text: str,
        relations: list[str],
    ) -> list[RawProtoEvent]:
        """Predict relations in text.

        Args:
            text: Input text
            relations: Relation types to extract

        Returns:
            List of proto-events
        """
        ...


class DisabledRelexRunner:
    """Default Relex runner that returns empty results.

    This is the fallback when GLiNER-Relex is not available or disabled.
    """

    def predict(
        self,
        text: str,
        relations: list[str],
    ) -> list[RawProtoEvent]:
        """Always returns empty list.

        Args:
            text: Input text (ignored)
            relations: Relation types (ignored)

        Returns:
            Empty list
        """
        return []
