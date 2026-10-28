import pytest

from sc2_harness.settings import Settings


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("   ", None), ("8192", 8192), ("16384", 16384)])
def test_context_environment_is_optional_and_explicit(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SC2H_MODEL_CONTEXT_TOKENS", raising=False)
    else:
        monkeypatch.setenv("SC2H_MODEL_CONTEXT_TOKENS", raw)
    assert Settings.from_env().model_context_tokens == expected


@pytest.mark.parametrize("raw", ["0", "-1", "true", "8192.0", "unbounded"])
def test_invalid_context_environment_is_rejected_instead_of_assuming_a_window(monkeypatch, raw):
    monkeypatch.setenv("SC2H_MODEL_CONTEXT_TOKENS", raw)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("value", [0, -1, True, "16384", 16384.0])
def test_direct_context_configuration_requires_a_positive_integer(tmp_path, value):
    with pytest.raises(ValueError):
        Settings(tmp_path, model_context_tokens=value)
