"""`runtime.yaml` picks the provider of each factory kind per mode; code in settings.py wins; no file means the defaults."""

from pathlib import Path

import pytest
import yaml
from retail_kyc_onboarding._engine.app import FlowSettings, default_providers
from retail_kyc_onboarding._engine.errors import ConfigError, ProviderNotFound
from retail_kyc_onboarding._engine.factories.config import (
    BUILD_ORDER,
    DEFAULT_PROVIDERS,
)
from retail_kyc_onboarding._engine.factories.registry import REGISTRY
from retail_kyc_onboarding._engine.factories.secret import EnvSecrets
from retail_kyc_onboarding._engine.profile import load_profile, profile_providers
from retail_kyc_onboarding.settings import Settings, build_providers

RUNTIME = Path(__file__).resolve().parents[2] / "retail_kyc_onboarding" / "runtime.yaml"


def write(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def names(providers) -> dict[str, str]:
    return {kind: p.name for kind, p in providers.given().items()}


def test_no_file_gives_the_defaults(tmp_path):
    providers = profile_providers(FlowSettings(), tmp_path / "missing.yaml")
    assert names(providers) == DEFAULT_PROVIDERS
    assert providers.mode == "default"


def test_another_mode_without_a_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="FLOW_PROVIDER_MODE=arc needs"):
        profile_providers(FlowSettings(provider_mode="arc"), tmp_path / "missing.yaml")


def test_the_shipped_default_profile_equals_the_built_in_defaults():
    from_file = profile_providers(FlowSettings(), RUNTIME, env={})
    assert names(from_file) == names(default_providers(FlowSettings())) == DEFAULT_PROVIDERS
    assert from_file.mode == "default"


def test_the_shipped_default_profile_lists_every_kind_it_chooses():
    resolved = load_profile(RUNTIME, "default", env={})
    assert {k: c.provider for k, c in resolved.factories.items()}.items() <= DEFAULT_PROVIDERS.items()
    assert set(resolved.factories) <= set(BUILD_ORDER)


def test_default_settings_still_reach_the_default_providers():
    s = FlowSettings(default_model="openai/gpt-5.5", session_uri="sqlite+aiosqlite:///runs.db")
    p = profile_providers(s, RUNTIME, env={})
    assert p.model.settings.default_model == "openai/gpt-5.5"
    assert p.session.settings.uri == "sqlite+aiosqlite:///runs.db"


def test_mode_comes_from_the_file_and_the_setting_wins(tmp_path):
    path = write(tmp_path, {"mode": "${env:WHICH:-first}", "provider-profile": {"first": {"factories": {"secret": "mock"}}, "second": {"factories": {"secret": "file"}}}})
    assert load_profile(path, env={}).mode == "first"
    assert load_profile(path, env={"WHICH": "second"}).mode == "second"
    assert load_profile(path, "second", env={}).mode == "second"
    assert profile_providers(FlowSettings(provider_mode="first"), path, env={}).secret.name == "mock"


def test_a_mode_inherits_the_kinds_it_does_not_list(tmp_path):
    path = write(tmp_path, {"mode": "mine", "provider-profile": {"default": {"factories": {"secret": "env", "telemetry": "console"}}, "mine": {"factories": {"secret": "mock"}}}})
    resolved = load_profile(path, env={})
    assert {k: c.provider for k, c in resolved.factories.items()} == {"secret": "mock", "telemetry": "console"}
    p = profile_providers(FlowSettings(), path, env={})
    assert names(p)["telemetry"] == "console" and names(p)["session"] == "uri"  # untouched kinds keep the default


def test_extends_names_the_parent(tmp_path):
    path = write(tmp_path, {"mode": "c", "provider-profile": {"a": {"factories": {"secret": "mock", "telemetry": "memory"}}, "c": {"extends": "a", "factories": {"telemetry": "jsonl"}}}})
    resolved = load_profile(path, env={})
    assert {k: c.provider for k, c in resolved.factories.items()} == {"secret": "mock", "telemetry": "jsonl"}


def test_extends_cycle_is_an_error(tmp_path):
    path = write(tmp_path, {"mode": "a", "provider-profile": {"a": {"extends": "b"}, "b": {"extends": "a"}}})
    with pytest.raises(ConfigError, match="extends itself"):
        load_profile(path, env={})


def test_string_and_mapping_forms_both_work(tmp_path):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"factories": {"secret": {"provider": "mock", "values": {"a": "1"}}, "telemetry": "memory"}}}})
    p = profile_providers(FlowSettings(), path, env={})
    assert p.secret.name == "mock" and p.secret.settings.values == {"a": "1"}
    assert p.telemetry.name == "memory"


def test_a_mistyped_setting_names_the_provider_and_the_key(tmp_path):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"factories": {"secret": {"provider": "mock", "valuez": {}}}}}})
    with pytest.raises(ConfigError, match=r"factory 'secret' provider 'mock'.*valuez"):
        profile_providers(FlowSettings(), path, env={})


def test_unknown_mode_lists_the_profiles(tmp_path):
    path = write(tmp_path, {"mode": "nope", "provider-profile": {"default": {}, "arc": {}}})
    with pytest.raises(ConfigError, match=r"unknown mode 'nope'. Profiles: arc, default"):
        load_profile(path, env={})


@pytest.mark.parametrize("kind", ["execution", "modell"])
def test_unknown_kind_lists_the_kinds(tmp_path, kind):
    path = write(tmp_path, {"mode": "default", "provider-profile": {"default": {"factories": {kind: "embedded"}}}})
    with pytest.raises(ConfigError, match=rf"unknown kind '{kind}'. Kinds: secret, "):
        load_profile(path, env={})


def test_unknown_provider_lists_the_registered_names(tmp_path):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"factories": {"secret": "vaultt"}}}})
    with pytest.raises(ProviderNotFound) as err:
        profile_providers(FlowSettings(), path, env={})
    assert "env" in str(err.value) and "mock" in str(err.value)


def test_an_unknown_key_in_the_file_is_an_error(tmp_path):
    path = write(tmp_path, {"mode": "default", "provider-profile": {"default": {"factorys": {}}}})
    with pytest.raises(ConfigError, match="factorys"):
        load_profile(path, env={})


def test_interpolation_reads_the_environment_with_defaults(tmp_path):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"config": {"a": "${env:A}", "b": "${env:B:-fallback}", "c": "x-${env:A}-y"}}}})
    assert load_profile(path, env={"A": "1"}).config == {"a": "1", "b": "fallback", "c": "x-1-y"}
    assert load_profile(path, env={"A": "1", "B": "2"}).config["b"] == "2"


def test_a_missing_variable_fails_only_in_the_selected_mode(tmp_path):
    path = write(tmp_path, {"mode": "default", "provider-profile": {"default": {}, "other": {"config": {"url": "${env:NEEDED}"}}}})
    assert load_profile(path, env={}).mode == "default"  # `other` is never resolved
    with pytest.raises(ConfigError, match=r"provider-profile.other.config.url: environment variable NEEDED is not set"):
        load_profile(path, "other", env={})


class Probe(EnvSecrets):
    name = "probe"


def test_a_class_path_names_a_provider_without_registering_it(tmp_path):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"factories": {"secret": {"provider": f"{__name__}:Probe", "prefix": "X_"}}}}})
    p = profile_providers(FlowSettings(), path, env={})
    assert isinstance(p.secret, Probe) and p.secret.settings.prefix == "X_"


@pytest.mark.parametrize(
    ("path", "message"),
    [("no.such.module:Thing", "cannot be imported"), (f"{__name__}:RUNTIME", "is not a Provider class"), ("retail_kyc_onboarding._engine.factories.telemetry:ConsoleTelemetry", "'telemetry' provider, not 'secret'")],
)
def test_a_bad_class_path_is_a_clear_error(path, message):
    with pytest.raises(ConfigError, match=message):
        REGISTRY.resolve("secret", path)


def test_the_arc_config_goes_only_to_providers_named_arc(tmp_path, monkeypatch):
    from retail_kyc_onboarding._engine.factories.base import (
        Provider,
        ProviderSettings,
    )
    from retail_kyc_onboarding._engine.factories.registry import Registry

    class Wants(ProviderSettings):
        url: str
        timeout: float = 1

    class ArcLike(EnvSecrets):
        name = "arc"
        Settings = Wants

    scratch = Registry()
    scratch.register_extra(ArcLike)
    monkeypatch.setattr("retail_kyc_onboarding._engine.profile.REGISTRY", scratch)
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"config": {"url": "http://arc", "timeout": 30}, "factories": {"secret": {"provider": "arc", "timeout": 5}}}}})
    p = profile_providers(FlowSettings(), path, env={})
    assert isinstance(p.secret, Provider) and (p.secret.settings.url, p.secret.settings.timeout) == ("http://arc", 5)


def test_code_in_build_providers_wins_over_the_profile(tmp_path, monkeypatch):
    path = write(tmp_path, {"mode": "m", "provider-profile": {"m": {"factories": {"secret": "mock"}}}})
    monkeypatch.setenv("FLOW_RUNTIME_FILE", str(path))
    s = Settings()
    assert build_providers(s).secret.name == "mock"

    p = build_providers(s)
    p.secret = EnvSecrets()  # what a client writes after the profile is loaded
    assert p.secret.name == "env" and p.mode == "m"


def test_flow_provider_mode_selects_the_mode(tmp_path, monkeypatch):
    path = write(tmp_path, {"mode": "default", "provider-profile": {"default": {}, "m": {"factories": {"secret": "mock"}}}})
    monkeypatch.setenv("FLOW_RUNTIME_FILE", str(path))
    monkeypatch.setenv("FLOW_PROVIDER_MODE", "m")
    p = build_providers(Settings())
    assert (p.mode, p.secret.name) == ("m", "mock")
