"""Only explicitly supplied session credentials may enter a requested export."""
import pytest

from jolup.env_export import export_dotenv


def test_export_uses_explicit_values_and_does_not_call_api(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: pytest.fail("No API call"))
    config = {"api_key": "sk-proj-offline-export-example", "provider": "auto", "model": "gpt-4.1-mini"}
    result = export_dotenv(config).decode()
    assert "JOLUP_API_KEY=sk-proj-offline-export-example\n" in result
    assert "JOLUP_LLM_PROVIDER=openai\n" in result
    assert "JOLUP_LLM_MODEL=gpt-4.1-mini\n" in result
    assert "LANGSMITH" not in result


def test_server_environment_key_is_never_exported(monkeypatch):
    monkeypatch.setenv("JOLUP_API_KEY", "sk-proj-server-only-example")
    with pytest.raises(ValueError):
        export_dotenv({"provider": "openai", "model": "gpt-4.1-mini"})


@pytest.mark.parametrize("key", ["sk-proj-one\nOTHER=value", "sk-proj-$(command)", "sk-proj-'quoted'", "sk-proj-a#b"])
def test_export_rejects_env_syntax_without_echoing_secret(key):
    with pytest.raises(ValueError) as exc:
        export_dotenv({"api_key": key, "provider": "openai", "model": "gpt-4.1-mini"})
    assert key not in str(exc.value)


def test_model_must_be_explicit_and_match_provider():
    for model in ("", "claude-example", "gpt-example\nOTHER=value"):
        with pytest.raises(ValueError):
            export_dotenv({"api_key": "sk-proj-offline-example", "model": model})


def test_claude_workspace_is_included_only_when_explicit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "server-only")
    config = {"api_key": "sk-ant-offline-export-example", "model": "claude-example"}
    assert "WORKSPACE" not in export_dotenv(config).decode()
    result = export_dotenv({**config, "workspace_id": "wrkspc_example"}).decode()
    assert "ANTHROPIC_WORKSPACE_ID=wrkspc_example\n" in result
