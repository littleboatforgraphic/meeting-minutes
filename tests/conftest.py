import pytest
from fakes import reset_caches, setup_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    yield tmp_path
    reset_caches()


@pytest.fixture
def make_env(tmp_path, monkeypatch):
    def _make(**overrides):
        setup_env(tmp_path, monkeypatch, **overrides)
        return tmp_path

    yield _make
    reset_caches()
