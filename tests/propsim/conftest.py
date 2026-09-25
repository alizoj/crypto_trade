from __future__ import annotations

import pytest

from quant.config import load_challenge_config


@pytest.fixture
def cfg():
    return load_challenge_config()
