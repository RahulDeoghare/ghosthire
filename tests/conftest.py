"""Shared test configuration.

The CLI's ``-o`` file can be flushed after its process exits, so `run_collector`
waits briefly before concluding a run produced nothing. Real failures in the
suite would each pay that wait, so it is disabled by default and exercised
explicitly by the one test that is about it.
"""

from __future__ import annotations

import pytest

from ghosthire import bdata


@pytest.fixture(autouse=True)
def _no_flush_grace(monkeypatch):
    monkeypatch.setattr(bdata, "OUTPUT_FLUSH_GRACE_S", 0.0)
