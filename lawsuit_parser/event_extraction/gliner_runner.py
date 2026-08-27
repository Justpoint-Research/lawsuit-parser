"""GLiNER model runner with explicit lifecycle, used by Stage 2 entity detection."""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def free_cuda_memory() -> None:
    """Release cached GPU memory held by the current process.

    Without this, PyTorch's caching allocator keeps holding a dropped
    model's memory and a later run in the same process can OOM even though
    the model is no longer referenced. Safe to call even if torch/CUDA
    aren't available (e.g. Stage 1/3, which never load a GPU model) or if
    nothing is currently loaded.
    """
    import gc

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


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
            import torch
            from gliner import GLiNER

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Loading GLiNER model: {self.model_name} on device: {device}")

            self.model = GLiNER.from_pretrained(self.model_name)
            self.model = self.model.to(device)
            self.model.eval()  # Set to evaluation mode

            if device == "cuda":
                logger.info(f"GLiNER loaded on GPU: {torch.cuda.get_device_name(0)}")
                # Verify the internal encoder is on GPU
                if hasattr(self.model, 'model') and hasattr(self.model.model, 'device'):
                    logger.info(f"GLiNER encoder device: {self.model.model.device}")

            return self
        except ImportError:
            raise ImportError("gliner package not installed. Run: uv add gliner")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Unload the model and free its GPU memory."""
        self.model = None
        free_cuda_memory()
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
