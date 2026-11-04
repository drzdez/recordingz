import pytest


@pytest.fixture(autouse=True)
def _no_real_ollama_unload(monkeypatch):
    """A recording pause frees the GPU by unloading the Ollama model; tests must never touch a real Ollama."""
    from recordingz.web import jobs
    calls = []
    monkeypatch.setattr(jobs, "_unload_ollama", lambda cfg: calls.append(cfg.summarize.model))
    return calls


@pytest.fixture(autouse=True)
def _no_real_ollama_lookup(monkeypatch):
    """The page asks the local Ollama whether the minutes model is there; tests must not depend on this PC."""
    from recordingz import settings
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: True)


@pytest.fixture(autouse=True)
def _no_real_key_vault(monkeypatch):
    """A key stored on this PC's settings page wins over the environment: tests must never read the real Credential
    Manager (a test sets its keys in the environment or in a FakeVault of its own)."""
    from recordingz import settings
    from test_settings import FakeVault
    monkeypatch.setattr(settings, "_keyring", lambda: FakeVault())
