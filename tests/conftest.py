"""Shared pytest setup.

Loads .env the same way the application does (scripts/migrate.py and,
eventually, core/config.py all call load_dotenv()), so tests see the same
environment as a normal run without requiring manual shell exports.
"""

from dotenv import load_dotenv

load_dotenv()


import pytest  # noqa: E402

from revenue_engine.integrations.email_verification import (  # noqa: E402
    EmailListVerifyProvider,
)

# Captured at import, before the autouse guard below ever replaces it, so the
# `unguarded_verifier` fixture hands back the genuine method rather than
# whatever a previous test left in place.
_REAL_VERIFY_FOR_TESTS = EmailListVerifyProvider.verify


@pytest.fixture(autouse=True)
def _no_real_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sending account is in warmup at 5 sends/day: no test may ever reach
    the real Gmail API. Any code path that falls back to the default transport
    (i.e. a test that forgot to inject a stub) fails loudly instead."""
    from revenue_engine.integrations import gmail

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must inject a stub Gmail transport; the real API is forbidden")

    monkeypatch.setattr(gmail, "_default_transport", _refuse)


@pytest.fixture(autouse=True)
def _no_real_email_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification credits are finite and charged per address: no test may
    reach the real API. A test that forgets to inject a stub provider fails
    loudly instead (mirrors the Gmail guard above)."""
    from revenue_engine.integrations import email_verification

    async def _refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "tests must inject a stub verification provider; the real API is forbidden"
        )

    # Patched on the class, not on default_provider(): scripts/import_leads.py
    # binds `default_provider` into its own namespace at import, so patching
    # that name there would leave the real network path reachable from the
    # script — which is the one place it is actually called.
    monkeypatch.setattr(email_verification.EmailListVerifyProvider, "verify", _refuse)


@pytest.fixture
def unguarded_verifier(monkeypatch: pytest.MonkeyPatch, _no_real_email_verification: None) -> None:
    """Opt back in to the REAL EmailListVerifyProvider.verify, for the handful of
    tests whose subject is that method itself (its syntax short-circuit, its
    failure modes, its mapping). Restores it after the autouse guard above has
    replaced it, and leaves httpx.AsyncClient.get refusing by default so an
    opted-in test that forgets to install its own transport still cannot reach
    the network; those tests each patch `get` themselves, which overrides this."""
    import httpx

    from revenue_engine.integrations import email_verification

    async def _refuse_get(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no real HTTP from a verification unit test")

    monkeypatch.setattr(
        email_verification.EmailListVerifyProvider, "verify", _REAL_VERIFY_FOR_TESTS
    )
    monkeypatch.setattr(httpx.AsyncClient, "get", _refuse_get)
