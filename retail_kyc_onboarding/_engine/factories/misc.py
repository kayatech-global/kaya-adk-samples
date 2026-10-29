"""Small kinds with a no-op default: run store, platform registration, guardrail, retrieval."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from ..trace.context import traced_client
from .base import Provider, ProviderSettings
from .registry import register


class RunStoreFactory(Provider):
    """Factory kind for the run records of asynchronous Sub-IFLOWs, keyed by string."""

    kind = "runstore"

    @abstractmethod
    async def put(self, key: str, value: dict[str, Any]) -> None:
        """Store ``value`` under ``key``, replacing any earlier record."""

    @abstractmethod
    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the record for ``key``, or ``None`` when there is none."""


@register
class SessionRunStore(RunStoreFactory):
    """In-memory run records for asynchronous Sub-IFLOWs. Replace with a database-backed provider for durability."""

    name = "session"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._data: dict[str, dict[str, Any]] = {}

    async def put(self, key: str, value: dict[str, Any]) -> None:
        """Keep a copy of ``value`` in memory."""
        self._data[key] = dict(value)

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the in-memory record, or ``None``."""
        return self._data.get(key)


@register
class SqlAlchemyRunStore(RunStoreFactory):
    """Durable run records in a SQL table (sqlite or postgres). Needed if background work must survive a restart."""

    name = "sqlalchemy"

    class Settings(ProviderSettings):
        url: str
        table: str = "flow_runstore"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._engine: Any = None
        self._ready = False

    async def _conn(self) -> Any:
        from sqlalchemy.ext.asyncio import create_async_engine

        if self._engine is None:
            self._engine = create_async_engine(self.settings.url)
        if not self._ready:
            from sqlalchemy import text

            async with self._engine.begin() as conn:
                await conn.execute(text(f"CREATE TABLE IF NOT EXISTS {self._table()} (k TEXT PRIMARY KEY, v TEXT NOT NULL)"))
            self._ready = True
        return self._engine

    def _table(self) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.settings.table):
            raise ValueError("invalid table name")
        return self.settings.table

    async def put(self, key: str, value: dict[str, Any]) -> None:
        """Replace the row for ``key`` with ``value`` as JSON; values that are not JSON are stringified."""
        import json

        from sqlalchemy import text

        engine = await self._conn()
        async with engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {self._table()} WHERE k = :k"), {"k": key})  # noqa: S608
            await conn.execute(text(f"INSERT INTO {self._table()} (k, v) VALUES (:k, :v)"), {"k": key, "v": json.dumps(value, default=str)})  # noqa: S608

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the decoded record, or ``None``."""
        import json

        from sqlalchemy import text

        engine = await self._conn()
        async with engine.connect() as conn:
            row = (await conn.execute(text(f"SELECT v FROM {self._table()} WHERE k = :k"), {"k": key})).first()  # noqa: S608
        return json.loads(row[0]) if row else None

    async def aclose(self) -> None:
        """Dispose of the engine if one was created."""
        if self._engine is not None:
            await self._engine.dispose()


@register
class UriRunStore(RunStoreFactory):
    """Run records stored where the sessions are: ``memory://`` keeps them in the process, a SQLAlchemy async URL in a table.

    ``default_providers`` gives it the same ``FLOW_SESSION_URI`` as the ``session`` kind.
    """

    name = "uri"

    class Settings(ProviderSettings):
        uri: str = "memory://"
        table: str = "flow_runstore"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        from .session import check_storage_uri

        uri = check_storage_uri(self.settings.uri)
        self._memory = uri == "memory://"
        self._inner: RunStoreFactory = (
            SessionRunStore(None, ctx) if self._memory else SqlAlchemyRunStore({"url": uri, "table": self.settings.table}, ctx)
        )

    async def put(self, key: str, value: dict[str, Any]) -> None:
        """Store the record in memory or in the table."""
        await self._inner.put(key, value)

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the record, or ``None``."""
        return await self._inner.get(key)

    async def aclose(self) -> None:
        """Dispose of the database engine, if any."""
        if isinstance(self._inner, SqlAlchemyRunStore):
            await self._inner.aclose()

    def describe(self) -> dict[str, Any]:
        """Name the storage kind without the URI, which can hold a password."""
        return {**super().describe(), "storage": "memory" if self._memory else "sql"}


class RegistrationFactory(Provider):
    """Platform registration hook (for example a client platform that must know every agent before it runs)."""

    kind = "registration"

    @abstractmethod
    async def register(self, manifest: dict[str, Any]) -> None:
        """Publish the registration document ``manifest`` to the target platform."""


@register
class NoopRegistration(RegistrationFactory):
    """Default registration: does nothing."""

    name = "noop"

    async def register(self, manifest: dict[str, Any]) -> None:
        """Do nothing."""
        return None


@register
class FileRegistration(RegistrationFactory):
    """Writes the registration document to a file: hand it to a platform that registers agents out of band."""

    name = "file"

    class Settings(ProviderSettings):
        path: str = "registration.json"

    async def register(self, manifest: dict[str, Any]) -> None:
        """Write ``manifest`` as sorted, indented JSON; a relative path is resolved under the export root."""
        import json
        from pathlib import Path

        target = Path(self.settings.path)
        target = target if target.is_absolute() else self.ctx.export_root / target
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


@register
class HttpRegistration(RegistrationFactory):
    """POSTs the registration document to a platform registry (for example a client platform that must know every agent)."""

    name = "http"

    class Settings(ProviderSettings):
        url: str
        headers: dict[str, str] = {}
        token_secret: str = ""
        timeout: float = 15

    transport: Any = None

    async def register(self, manifest: dict[str, Any]) -> None:
        """POST ``manifest`` as JSON, with a bearer token from the secret factory when ``token_secret`` is set.

        Raises:
            httpx.HTTPStatusError: The registry answered with an error status.
        """

        headers = dict(self.settings.headers)
        if self.settings.token_secret:
            headers["Authorization"] = "Bearer " + await self.ctx.provider("secret").get(self.settings.token_secret)
        async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
            resp = await client.post(self.settings.url, headers=headers, json=manifest)
        resp.raise_for_status()


@dataclass
class GuardrailResult:
    """Outcome of a guardrail check: whether to proceed, the text to use afterwards and the reason when blocked."""

    allowed: bool = True
    text: str = ""  # the text to use (a provider may redact)
    reason: str = ""


class GuardrailFactory(Provider):
    """Checks text before a model sees it and after it answers. ``ids`` are the guardrails attached to the node."""

    kind = "guardrail"

    @abstractmethod
    async def check_input(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Check ``text`` before a model sees it.

        Returns:
            ``allowed=False`` stops the node; ``text`` is the text to use, possibly redacted.
        """

    @abstractmethod
    async def check_output(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Check the model's answer ``text`` before it is used.

        Returns:
            ``allowed=False`` stops the node; ``text`` is the text to use, possibly redacted.
        """


@register
class NoGuardrails(GuardrailFactory):
    """Default guardrails: allow all text unchanged."""

    name = "none"

    async def check_input(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Allow ``text`` unchanged."""
        return GuardrailResult(True, text)

    async def check_output(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Allow ``text`` unchanged."""
        return GuardrailResult(True, text)


@register
class RegexGuardrails(GuardrailFactory):
    """Pattern rules: block (stop the node) or redact (replace the match). Rules apply to input, output or both."""

    name = "regex"

    class Settings(ProviderSettings):
        rules: list[
            dict[str, Any]
        ] = []  # {pattern, action: block|redact, applies_to: input|output|both, replacement, reason, guardrails: [ids]}

    def _apply(self, text: str, ids: list[str], side: str) -> GuardrailResult:
        import re

        for rule in self.settings.rules:
            if rule.get("applies_to", "both") not in (side, "both"):
                continue
            if rule.get("guardrails") and not set(rule["guardrails"]) & set(ids):
                continue
            pattern = re.compile(rule["pattern"], re.I)
            if not pattern.search(text):
                continue
            if rule.get("action", "block") == "block":
                return GuardrailResult(False, text, rule.get("reason", f"matched {rule['pattern']!r}"))
            text = pattern.sub(rule.get("replacement", "[redacted]"), text)
        return GuardrailResult(True, text)

    async def check_input(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Apply the rules whose ``applies_to`` covers input."""
        return self._apply(text, guardrail_ids, "input")

    async def check_output(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Apply the rules whose ``applies_to`` covers output."""
        return self._apply(text, guardrail_ids, "output")


@register
class HttpGuardrails(GuardrailFactory):
    """A detection service: POST {url} {text, side, guardrails} -> {allowed, text?, reason?}. Fails closed when it cannot answer."""

    name = "http"

    class Settings(ProviderSettings):
        url: str
        headers: dict[str, str] = {}
        token_secret: str = ""
        timeout: float = 10
        fail_open: bool = False

    transport: Any = None

    async def _ask(self, text: str, ids: list[str], side: str) -> GuardrailResult:

        headers = dict(self.settings.headers)
        if self.settings.token_secret:
            headers["Authorization"] = "Bearer " + await self.ctx.provider("secret").get(self.settings.token_secret)
        try:
            async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
                resp = await client.post(self.settings.url, headers=headers, json={"text": text, "side": side, "guardrails": ids})
            resp.raise_for_status()
            body = resp.json()
            return GuardrailResult(bool(body.get("allowed", False)), str(body.get("text", text)), str(body.get("reason", "")))
        except Exception as exc:
            if self.settings.fail_open:
                return GuardrailResult(True, text, f"guardrail service unavailable: {type(exc).__name__}")
            return GuardrailResult(False, text, f"guardrail service unavailable: {type(exc).__name__}")

    async def check_input(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Ask the detection service about input text; fails closed unless ``fail_open`` is set."""
        return await self._ask(text, guardrail_ids, "input")

    async def check_output(self, text: str, guardrail_ids: list[str]) -> GuardrailResult:
        """Ask the detection service about output text; fails closed unless ``fail_open`` is set."""
        return await self._ask(text, guardrail_ids, "output")


class RetrievalFactory(Provider):
    """Factory kind for knowledge-base search."""

    kind = "retrieval"

    @abstractmethod
    async def search(self, store: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Find passages in ``store`` for ``query``.

        Returns:
            Up to ``top_k`` passages, best first, each a dict (typically ``id``, ``text`` and ``score``).
        """


@register
class MemoryRetrieval(RetrievalFactory):
    """Keyword-scored documents held in memory: tests, demos and small fixed knowledge bases."""

    name = "memory"

    class Settings(ProviderSettings):
        stores: dict[str, list[dict[str, Any]]] = {}  # store -> [{id, text, ...}]

    async def search(self, store: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Score documents by how many query words longer than two characters they contain; documents with no hit are dropped."""
        words = {w for w in query.lower().split() if len(w) > 2}
        scored = []
        for doc in self.settings.stores.get(store, []):
            hits = sum(1 for w in words if w in str(doc.get("text", "")).lower())
            if hits:
                scored.append((hits, doc))
        scored.sort(key=lambda t: -t[0])
        return [{**d, "score": h} for h, d in scored[:top_k]]


@register
class HttpRetrieval(RetrievalFactory):
    """Your vector store or search service: POST {url} {store, query, top_k} -> {results: [{id, text, score}]}."""

    name = "http"

    class Settings(ProviderSettings):
        url: str
        headers: dict[str, str] = {}
        token_secret: str = ""
        timeout: float = 20

    transport: Any = None

    async def search(self, store: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """POST the query to ``url`` and return the ``results`` list of the answer.

        Raises:
            httpx.HTTPStatusError: The service answered with an error status.
        """

        headers = dict(self.settings.headers)
        if self.settings.token_secret:
            headers["Authorization"] = "Bearer " + await self.ctx.provider("secret").get(self.settings.token_secret)
        async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
            resp = await client.post(self.settings.url, headers=headers, json={"store": store, "query": query, "top_k": top_k})
        resp.raise_for_status()
        return list(resp.json().get("results", []))


@register
class NoRetrieval(RetrievalFactory):
    """Default retrieval: no knowledge bases are configured."""

    name = "none"

    async def search(self, store: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Always return no passages."""
        return []
