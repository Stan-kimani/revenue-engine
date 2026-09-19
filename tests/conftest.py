"""Shared pytest setup.

Loads .env the same way the application does (scripts/migrate.py and,
eventually, core/config.py all call load_dotenv()), so tests see the same
environment as a normal run without requiring manual shell exports.
"""

from dotenv import load_dotenv

load_dotenv()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sending account is in warmup at 5 sends/day: no test may ever reach
    the real Gmail API. Any code path that falls back to the default transport
    (i.e. a test that forgot to inject a stub) fails loudly instead."""
    from revenue_engine.integrations import gmail

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must inject a stub Gmail transport; the real API is forbidden")

    monkeypatch.setattr(gmail, "_default_transport", _refuse)
