"""Tests for artifact storage."""

import pytest
import tempfile
import shutil
from pathlib import Path
from pydantic import BaseModel

from lawsuit_parser.extraction.store import ArtifactStore, compute_config_hash


class SimpleTestModel(BaseModel):
    """Simple test model."""

    value: str
    count: int


def test_artifact_store_roundtrip():
    """Test writing and reading an artifact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ArtifactStore("test_case", Path(tmpdir))

        # Write artifact
        model = SimpleTestModel(value="test", count=42)
        store.write_stage("00_test", model)

        # Read artifact
        loaded = store.read_stage("00_test", SimpleTestModel)

        assert loaded.value == "test"
        assert loaded.count == 42


def test_artifact_store_has_stage():
    """Test checking if stage exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ArtifactStore("test_case", Path(tmpdir))

        assert not store.has_stage("00_test")

        model = SimpleTestModel(value="test", count=42)
        store.write_stage("00_test", model)

        assert store.has_stage("00_test")


def test_canonical_text_roundtrip():
    """Test writing and reading canonical text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ArtifactStore("test_case", Path(tmpdir))

        text = "This is the canonical text.\nLine 2.\n"
        store.write_canonical_text("doc_001", text)

        loaded = store.read_canonical_text("doc_001")
        assert loaded == text


def test_canonical_text_has():
    """Test checking if canonical text exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ArtifactStore("test_case", Path(tmpdir))

        assert not store.has_canonical_text("doc_001")

        store.write_canonical_text("doc_001", "test")

        assert store.has_canonical_text("doc_001")


def test_compute_config_hash_stability():
    """Test that config hash is stable."""
    config = {"a": 1, "b": 2, "c": {"d": 3}}

    hash1 = compute_config_hash(config)
    hash2 = compute_config_hash(config)

    assert hash1 == hash2


def test_compute_config_hash_order_independent():
    """Test that config hash is order-independent."""
    config1 = {"a": 1, "b": 2}
    config2 = {"b": 2, "a": 1}

    hash1 = compute_config_hash(config1)
    hash2 = compute_config_hash(config2)

    assert hash1 == hash2
