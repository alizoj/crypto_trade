"""Общие фикстуры. Сетевые тесты включаются QUANT_NETWORK_TESTS=1."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("QUANT_NETWORK_TESTS", "") in {"1", "true", "yes"}:
        return
    skip = pytest.mark.skip(reason="сетевой тест: QUANT_NETWORK_TESTS=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Изолированные настройки: data_dir и SQLite во временной папке."""
    from quant import config

    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QUANT_DB_URL", f"sqlite:///{(tmp_path / 'q.db').as_posix()}")
    monkeypatch.setenv("QUANT_REPORTS_DIR", str(tmp_path / "reports"))
    config.get_settings.cache_clear()
    from quant import db

    db.get_engine.cache_clear()
    db._factory.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    db._factory.cache_clear()
