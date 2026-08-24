"""NuExtract3 client, an OpenAI-compatible wrapper against a vLLM endpoint.

Used by llm_validation.py's "nuextract" backend option (an alternative to the
default "ollama" backend - see Stage1Config.llm_backend).
"""

import json
import logging

from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when extraction fails after retries."""
    pass


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
