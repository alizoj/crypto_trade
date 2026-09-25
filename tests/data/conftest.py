"""Фикстуры тестов слоя данных (хелперы — в _datahelpers.py)."""

from __future__ import annotations

import pytest

from _datahelpers import DL_URL, LIST_URL, FakeArchive


@pytest.fixture
def fake_archive():
    return FakeArchive()


@pytest.fixture
def archive_factory(tmp_settings):
    from quant.data.archive import BinanceArchive

    def make(fake: FakeArchive, **kw):
        return BinanceArchive(data_dir=tmp_settings.data_dir, transport=fake.transport(), list_url=LIST_URL,
                              download_url=DL_URL, backoff=0.0, **kw)
    return make
