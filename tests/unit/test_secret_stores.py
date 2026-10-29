"""The secret stores of `_engine/factories/secret.py`, offline: a fake Secret Manager client and an in-process Vault."""

import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from retail_kyc_onboarding._engine.errors import ConfigError
from retail_kyc_onboarding._engine.factories.base import BuildContext
from retail_kyc_onboarding._engine.factories.registry import REGISTRY
from retail_kyc_onboarding._engine.factories.secret import (
    EnvSecrets,
    FileSecrets,
    GoogleSecretManagerSecrets,
    HashicorpVaultSecrets,
    MockSecrets,
    SecretNotFound,
)

VALUE = "s3cr3t-value"


def test_every_store_is_registered_for_the_secret_kind():
    """Each store can be named in runtime.yaml under the secret kind."""
    assert {"env", "file", "mock", "google_secret_manager", "hashicorp_vault"} <= set(REGISTRY.names("secret"))


# ---------------------------------------------------------------- env


async def test_env_maps_a_logical_name_to_an_upper_cased_variable_with_the_prefix():
    """A logical name becomes the prefix plus the name upper-cased with every non-alphanumeric as an underscore."""
    store = EnvSecrets({"prefix": "FLOW_"}, BuildContext(env={"FLOW_REGISTRY_API_KEY": VALUE}))
    assert await store.get("registry-api.key") == VALUE


async def test_env_falls_back_to_the_raw_name():
    """When the mapped variable is unset the raw logical name is read."""
    store = EnvSecrets(None, BuildContext(env={"model-key": VALUE}))
    assert await store.get("model-key") == VALUE


async def test_env_miss_names_the_secret_and_get_optional_returns_none():
    """A name set in neither variable raises SecretNotFound naming the secret and the store."""
    store = EnvSecrets(None, BuildContext(env={"OTHER": VALUE}))
    with pytest.raises(SecretNotFound, match="secret 'missing' was not found by provider 'env'") as err:
        await store.get("missing")
    assert VALUE not in str(err.value)
    assert await store.get_optional("missing") is None


async def test_env_health_is_ok_by_default():
    """The env store makes no check of its own."""
    assert (await EnvSecrets(None, BuildContext(env={})).health()).ok


# ---------------------------------------------------------------- file


async def test_file_returns_the_stripped_content(tmp_path):
    """A secret is the content of the file named after it, without surrounding whitespace."""
    (tmp_path / "db_password").write_text(f"  {VALUE}\n")
    assert await FileSecrets({"directory": str(tmp_path)}).get("db_password") == VALUE


async def test_file_miss_names_the_secret(tmp_path):
    """A missing file raises SecretNotFound naming the secret and the store."""
    with pytest.raises(SecretNotFound, match="secret 'absent' was not found by provider 'file'"):
        await FileSecrets({"directory": str(tmp_path)}).get("absent")


@pytest.mark.parametrize("name", ["../outside", "sub/inner", ".."])
async def test_file_refuses_a_name_that_would_leave_the_directory(tmp_path, name):
    """A name holding a slash or two dots is not found, even when the file exists."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner").write_text(VALUE)
    (tmp_path.parent / "outside").write_text(VALUE)
    with pytest.raises(SecretNotFound) as err:
        await FileSecrets({"directory": str(tmp_path)}).get(name)
    assert VALUE not in str(err.value)


async def test_file_treats_a_directory_as_a_miss(tmp_path):
    """A name that is a directory, not a file, is not found."""
    (tmp_path / "folder").mkdir()
    with pytest.raises(SecretNotFound):
        await FileSecrets({"directory": str(tmp_path)}).get("folder")


async def test_file_health_needs_the_directory(tmp_path):
    """Health is ok for an existing directory and names a missing one."""
    assert (await FileSecrets({"directory": str(tmp_path)}).health()).ok
    status = await FileSecrets({"directory": str(tmp_path / "nope")}).health()
    assert not status.ok and "nope does not exist" in status.detail


def test_file_needs_a_directory_setting():
    """The directory setting is required, so a missing one fails at start-up."""
    with pytest.raises(ConfigError, match="directory"):
        FileSecrets({})


# ---------------------------------------------------------------- mock


async def test_mock_serves_its_values_and_names_a_miss():
    """The mock store serves the values setting and raises SecretNotFound for any other name."""
    store = MockSecrets({"values": {"k": VALUE}})
    assert await store.get("k") == VALUE
    with pytest.raises(SecretNotFound, match="'other'.*'mock'"):
        await store.get("other")
    assert await store.get_optional("other") is None


# ---------------------------------------------------------------- google secret manager


class FakeSecretManager:
    """Stands in for SecretManagerServiceClient: serves ``values`` by resource name and records every request."""

    def __init__(self, values: dict[str, str], error: Exception | None = None):
        self.values, self.error, self.requests = values, error, []

    def access_secret_version(self, request: dict) -> SimpleNamespace:
        self.requests.append(request["name"])
        if self.error is not None:
            raise self.error
        if request["name"] not in self.values:
            raise LookupError(f"404 {request['name']} not found")
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[request["name"]].encode("utf-8")))


def gsm(client: FakeSecretManager | None = None, **settings) -> GoogleSecretManagerSecrets:
    """A Secret Manager store for project ``p`` whose client is ``client``."""
    store = GoogleSecretManagerSecrets({"project": "p", **settings})
    store._client = client
    return store


def library(monkeypatch, constructor) -> None:
    """Install a stand-in ``google.cloud.secretmanager`` whose client constructor is ``constructor``."""
    import google.cloud

    module = ModuleType("google.cloud.secretmanager")
    module.SecretManagerServiceClient = constructor
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", module)
    monkeypatch.setattr(google.cloud, "secretmanager", module, raising=False)


def test_gsm_maps_a_logical_name_to_a_resource():
    """The resource is the prefix plus the name with unsafe characters as dashes, unless ``names`` maps it."""
    store = gsm(prefix="kyc-", version="3", names={"model_key": "gemini-api-key"})
    assert store.resource("registry api.key") == "projects/p/secrets/kyc-registry-api-key/versions/3"
    assert store.resource("model_key") == "projects/p/secrets/gemini-api-key/versions/3"


async def test_gsm_hit_decodes_the_payload_and_caches_it():
    """A found secret is decoded once, then served from the cache."""
    client = FakeSecretManager({"projects/p/secrets/model_key/versions/latest": VALUE})
    store = gsm(client)
    assert [await store.get("model_key"), await store.get("model_key")] == [VALUE, VALUE]
    assert client.requests == ["projects/p/secrets/model_key/versions/latest"]


async def test_gsm_asks_again_once_the_cache_has_expired():
    """With no cache time every read goes to Secret Manager."""
    client = FakeSecretManager({"projects/p/secrets/k/versions/latest": VALUE})
    store = gsm(client, cache_seconds=0)
    await store.get("k")
    await store.get("k")
    assert len(client.requests) == 2


async def test_gsm_miss_names_the_secret_and_only_the_error_type():
    """A lookup failure raises SecretNotFound with the exception type, never the error text or a value."""
    store = gsm(FakeSecretManager({}, error=PermissionError(f"denied, token={VALUE}")))
    with pytest.raises(
        SecretNotFound, match=r"secret 'k' was not found by provider 'google_secret_manager \(PermissionError\)'"
    ) as err:
        await store.get("k")
    assert VALUE not in str(err.value) and "denied" not in str(err.value)
    assert await gsm(FakeSecretManager({})).get_optional("k") is None


async def test_gsm_builds_its_client_once_from_the_library(monkeypatch):
    """Without an injected client the store builds a SecretManagerServiceClient on first use and keeps it."""
    built = []

    def fake_client():
        built.append(1)
        return FakeSecretManager({"projects/p/secrets/k/versions/latest": VALUE})

    library(monkeypatch, fake_client)
    store = gsm(cache_seconds=0)
    assert await store.get("k") == VALUE
    assert await store.get("k") == VALUE
    assert (await store.health()).ok
    assert built == [1]


async def test_gsm_without_the_library_is_a_config_error_and_unhealthy(monkeypatch):
    """A missing google-cloud-secret-manager is a ConfigError on read and an unhealthy status that says what to install."""
    import google.cloud

    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)
    monkeypatch.delattr(google.cloud, "secretmanager", raising=False)
    with pytest.raises(ConfigError, match="pip install google-cloud-secret-manager"):
        await gsm().get("k")
    status = await gsm().health()
    assert not status.ok and "google-cloud-secret-manager" in status.detail


async def test_gsm_health_reports_a_client_that_cannot_be_built(monkeypatch):
    """A client constructor failure is unhealthy, naming only the exception type."""

    def no_credentials():
        raise RuntimeError(f"no default credentials {VALUE}")

    library(monkeypatch, no_credentials)
    status = await gsm().health()
    assert not status.ok and status.detail == "cannot create Secret Manager client: RuntimeError"


async def test_gsm_health_is_ok_with_a_client():
    """An injected client is healthy."""
    assert (await gsm(FakeSecretManager({})).health()).ok


def test_gsm_needs_a_project():
    """The project setting is required."""
    with pytest.raises(ConfigError, match="project"):
        GoogleSecretManagerSecrets({})


# ---------------------------------------------------------------- hashicorp vault


class FakeVault:
    """A KV v2 Vault in process: ``secrets`` maps a path to its data; records every request."""

    def __init__(self, secrets: dict[str, dict], status: int = 200, body: dict | None = None):
        self.secrets, self.status, self.body, self.requests = secrets, status, body, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": []})
        if self.body is not None:
            return httpx.Response(200, json=self.body)
        path = request.url.path.removeprefix("/v1/secret/data/")
        if path not in self.secrets:
            return httpx.Response(404, json={"errors": []})
        return httpx.Response(200, json={"data": {"data": self.secrets[path], "metadata": {"version": 1}}})


def vault(fake: FakeVault, env: dict | None = None, **settings) -> HashicorpVaultSecrets:
    """A Vault store at vault.test talking to ``fake`` in process."""
    store = HashicorpVaultSecrets(
        {"address": "http://vault.test/", **settings}, BuildContext(env={"VAULT_TOKEN": "tok", **(env or {})})
    )
    store.transport = httpx.MockTransport(fake.handler)
    return store


@pytest.mark.parametrize("body", ["<html>login</html>", "[1, 2]", '{"data": {"data": ["not", "a", "map"]}}'])
async def test_vault_reports_an_unexpected_reply_as_not_found(body):
    """A 200 that is not Vault's JSON object (a proxy page, a list) is SecretNotFound, so get_optional returns None."""
    store = vault(FakeVault({}))
    store.transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    with pytest.raises(SecretNotFound, match="unexpected response|missing"):
        await store.get("k")
    assert await store.get_optional("k") is None


async def test_vault_reads_the_last_path_segment_as_the_key_and_sends_the_token():
    """A logical name is the path under the prefix; the key is its last segment, and the token goes in X-Vault-Token."""
    fake = FakeVault({"kyc/registry_key": {"registry_key": VALUE}})
    assert await vault(fake, prefix="kyc/").get("registry_key") == VALUE
    request = fake.requests[0]
    assert str(request.url) == "http://vault.test/v1/secret/data/kyc/registry_key"
    assert request.headers["X-Vault-Token"] == "tok"
    assert "X-Vault-Namespace" not in request.headers


async def test_vault_paths_map_a_name_to_a_path_and_an_explicit_key():
    """``paths`` maps a logical name to ``path:key``, and the namespace header is sent when set."""
    fake = FakeVault({"apps/kyc": {"api": VALUE, "other": "x"}})
    store = vault(fake, paths={"model_key": "apps/kyc:api"}, namespace="team-a", token="setting-token")
    assert await store.get("model_key") == VALUE
    assert fake.requests[0].headers["X-Vault-Namespace"] == "team-a"
    assert fake.requests[0].headers["X-Vault-Token"] == "setting-token"


async def test_vault_caches_a_value_until_it_expires():
    """A value is served from the cache for cache_seconds, and fetched again with no cache time."""
    fake = FakeVault({"k": {"k": VALUE}})
    cached = vault(fake)
    await cached.get("k")
    await cached.get("k")
    assert len(fake.requests) == 1
    uncached = vault(fake, cache_seconds=0)
    await uncached.get("k")
    await uncached.get("k")
    assert len(fake.requests) == 3


async def test_vault_turns_a_non_string_value_into_text():
    """A number in the KV data is returned as its text."""
    assert await vault(FakeVault({"port": {"port": 5432}})).get("port") == "5432"


async def test_vault_miss_on_a_missing_path_names_the_status():
    """A 404 is SecretNotFound naming the secret and the HTTP status, never the token."""
    with pytest.raises(SecretNotFound, match=r"secret 'gone' was not found by provider 'hashicorp_vault \(HTTP 404\)'") as err:
        await vault(FakeVault({})).get("gone")
    assert "tok" not in str(err.value)


async def test_vault_miss_on_a_missing_key_names_the_key_not_the_values():
    """A path without the key is SecretNotFound naming the key, never the values stored beside it."""
    with pytest.raises(SecretNotFound, match="key 'k' missing") as err:
        await vault(FakeVault({"k": {"other": VALUE}})).get("k")
    assert VALUE not in str(err.value)


async def test_vault_miss_on_an_empty_body():
    """A 200 without a data block is a missing key, not a crash."""
    with pytest.raises(SecretNotFound, match="key 'k' missing"):
        await vault(FakeVault({}, body={"data": None})).get("k")


async def test_vault_forbidden_is_secret_not_found():
    """A 403 is reported with its status."""
    with pytest.raises(SecretNotFound, match=r"\(HTTP 403\)"):
        await vault(FakeVault({}, status=403)).get("k")
    assert await vault(FakeVault({}, status=403)).get_optional("k") is None


async def test_vault_transport_error_names_only_the_error_type():
    """A connection failure is SecretNotFound with the httpx error type."""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {VALUE}", request=request)

    store = vault(FakeVault({}))
    store.transport = httpx.MockTransport(down)
    with pytest.raises(SecretNotFound, match=r"\(ConnectError\)") as err:
        await store.get("k")
    assert VALUE not in str(err.value)


async def test_vault_without_a_token_is_a_config_error_naming_the_variable():
    """No token setting and no token variable is a ConfigError naming where the token is read from, before any request."""
    fake = FakeVault({"k": {"k": VALUE}})
    store = HashicorpVaultSecrets({"address": "http://vault.test", "token_env": "MY_VAULT"}, BuildContext(env={}))
    store.transport = httpx.MockTransport(fake.handler)
    with pytest.raises(ConfigError, match="env MY_VAULT"):
        await store.get("k")
    assert fake.requests == []


async def test_vault_health_needs_a_token_and_makes_no_request():
    """Health is ok with a token from the environment and names the variable without one."""
    fake = FakeVault({})
    assert (await vault(fake).health()).ok
    store = HashicorpVaultSecrets({"address": "http://vault.test"}, BuildContext(env={}))
    status = await store.health()
    assert not status.ok and "VAULT_TOKEN" in status.detail
    assert fake.requests == []
