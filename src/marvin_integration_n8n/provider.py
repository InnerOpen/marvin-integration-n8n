"""n8n provider: trigger n8n workflows from Marvin, pick them from n8n's list, read executions back.

The provider is pure with respect to Marvin: it gets ``config`` (where n8n lives, how to sign), the
resolved ``secret`` (the n8n API key — optional), a ``logger`` and a safe ``http`` client via ``ctx``,
and returns dicts. Receiving n8n's reports back (an incoming webhook) is the core's job; this provider
only declares that webhook and the workflow that records the result (see ``content.py``).

n8n facts this leans on:
- The public API (``/api/v1``, ``X-N8N-API-KEY``) lists workflows and executions but cannot *run* a
  workflow. A Webhook node is the trigger, so ``trigger_workflow`` calls the node's production URL
  (``<base>/<webhook_prefix>/<path>``) and the API is only read.
- A Webhook node answers 404 when no *active* workflow registered that path and method, 500 with
  ``{"message": …}`` when the workflow failed before responding, and whatever a Respond to Webhook node
  says otherwise. Its auth options are None, Basic, Header and JWT (HS256 passphrase) — no HMAC.
- n8n keeps request headers in execution data, which is why the default auth is a 60-second JWT.

Every failure raises :class:`N8nError` with a stable ``code``; ``error_policy`` tells Marvin what to do
about each. The secret, the JWT and request headers never appear in a result, an error or a log line.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import quote, urlencode, urlparse

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    ErrorPolicy,
    Handle,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Response,
    Retry,
    register_provider,
)

from .auth import AUTH_HEADER, AUTH_JWT, AUTH_MODES, AUTH_NONE, auth_headers
from .content import CONTENT, WEBHOOK_SECRET_REF
from .recipes import RECIPES

ERROR_TEXT_LIMIT = 300
RESPONSE_TEXT_LIMIT = 2000
DEFAULT_TIMEOUT, MAX_TIMEOUT = 15, 30
DEFAULT_PREFIX, TEST_PREFIX = "webhook", "webhook-test"
DEFAULT_AUTH_HEADER = "X-Marvin-Hook"
METHODS = ("POST", "PUT", "GET")
WEBHOOK_NODE = "n8n-nodes-base.webhook"
# A cap on following `nextCursor`, so a misbehaving API can't loop an action forever.
MAX_PAGES = 10
PAGE_SIZE = 100
IDEMPOTENCY_HEADER = "X-Marvin-Idempotency-Key"
TIMESTAMP_HEADER = "X-Marvin-Timestamp"
REDACTED = "[redacted]"
PATH_SAFE = "/:@-._~!$&'()*+,;="  # characters a webhook path keeps as they are
# The core's helper refuses an oversized reply with a plain ValueError carrying this text (SDK 0.5 has no typed error for it).
SIZE_CAP_TEXT = "size cap"
# The n8n Webhook node's `authentication` option, as the picker and list_workflows show it.
NODE_AUTH = {"none": "none", "basicAuth": "basic", "headerAuth": "header", "jwtAuth": "jwt"}

# The stable codes a failure carries (N8nError.code). `error_policy` below handles each; a workflow's own
# on-failure steps can still read it as `${error.code}`.
CODE_AUTH = "auth"  # 401/403 from a webhook or the API (or no API key for an action that needs one)
CODE_NOT_FOUND = "not_found"  # 404: the webhook isn't registered (inactive workflow, wrong path or method)
CODE_RATE_LIMITED = "rate_limited"  # 429 (its Retry-After becomes retry_after)
CODE_UNAVAILABLE = "unavailable"  # connect refused, DNS, connect timeout, 5xx other than n8n's own workflow error
CODE_TIMEOUT = "timeout"  # the request was sent and no reply came in time: n8n may have run it
CODE_WORKFLOW_ERROR = "workflow_error"  # 500 with n8n's {"message": …}: the workflow ran and failed
CODE_REJECTED = "rejected"  # another 4xx: the workflow (a Respond to Webhook node) said no
CODE_BLOCKED = "blocked"  # Marvin's SSRF guard refused the host
CODE_INVALID = "invalid"  # a bad argument or connection setting
CODE_TOO_LARGE = "response_too_large"  # the reply exceeded INTEGRATION_HTTP_MAX_BYTES (the request itself landed)
CODE_UNKNOWN = "unknown"  # anything else (handled by the "*" entry)
CODES = (
    CODE_AUTH,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    CODE_UNAVAILABLE,
    CODE_TIMEOUT,
    CODE_WORKFLOW_ERROR,
    CODE_REJECTED,
    CODE_BLOCKED,
    CODE_INVALID,
    CODE_TOO_LARGE,
    CODE_UNKNOWN,
)

# How Marvin handles each code (it applies these; the provider never retries or alerts by itself). In a
# Handle, review/notify/succeed happen as soon as the error does; `then` once the retries run out.
REVIEW = Handle(review=True)
NOTIFY = Handle(notify=True)
FAIL = Handle()  # let the step fail as usual
ERROR_POLICY: ErrorPolicy = {
    # A secret a timer won't fix: tell an admin, wait for the connection to be healthy again, up to 3×.
    CODE_AUTH: Handle(notify=True, retry=Retry(backoff=(), on_recovery=True, max_attempts=3), then=REVIEW),
    # Usually a workflow someone switched off: tell an admin now, give them time to switch it back on.
    CODE_NOT_FOUND: Handle(notify=True, retry=Retry(backoff=(300, 1800)), then=REVIEW),
    CODE_RATE_LIMITED: Handle(retry=Retry(backoff=(30, 120, 600)), then=Handle(notify=True, review=True)),
    CODE_UNAVAILABLE: Handle(retry=Retry(backoff=(60, 300, 1800, 7200)), then=Handle(notify=True, review=True)),
    # n8n may already have run it, so never retry (that could run it twice): a person decides.
    CODE_TIMEOUT: REVIEW,
    # n8n's own error workflow already alerts on a failed execution; Marvin only parks the entry.
    CODE_WORKFLOW_ERROR: REVIEW,
    CODE_REJECTED: REVIEW,
    CODE_BLOCKED: NOTIFY,
    CODE_INVALID: NOTIFY,
    # The request landed and n8n answered; only its reply was too big to keep.
    CODE_TOO_LARGE: Handle(succeed=True),
    "*": Handle(review=True, notify=True),
}
# The read actions run from the card or the step editor, where the error shows right away: every code
# fails plainly. Each is listed because an action's "*" ranks below the provider's own entry for a code.
READ_POLICY: ErrorPolicy = {**dict.fromkeys(CODES, FAIL), "*": FAIL}

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_WEBHOOK_OUT = {
    "type": "object",
    "properties": {"path": _STR, "method": _STR, "auth": _STR, "respond_mode": _STR, "production_url": _STR},
}
_WORKFLOW_OUT = {
    "type": "object",
    "properties": {
        "id": _STR,
        "name": _STR,
        "active": _BOOL,
        "tags": {"type": "array", "items": _STR},
        "webhooks": {"type": "array", "items": _WEBHOOK_OUT},
    },
}
_LIST_FILTERS = {
    "tag": {"type": "string", "title": "Tag", "description": "Only workflows with this n8n tag."},
    "name": {"type": "string", "title": "Name", "description": "Only workflows whose name matches (n8n's own name filter)."},
}


class N8nError(IntegrationError):
    """A readable failure with a stable ``code`` (see ``CODES``) that selects its handling from
    ``error_policy``.

    An ``IntegrationError``, so still a ValueError: a Marvin without the error engine fails the step
    with the message, and a workflow's on-failure steps read the code as ``${error.code}``."""

    def __init__(self, message: str, code: str = CODE_UNKNOWN, *, retry_after: float | None = None) -> None:
        super().__init__(message, code=code, retry_after=retry_after)


@dataclass(frozen=True)
class _Settings:
    base_url: str
    editor_url: str
    webhook_prefix: str
    default_auth: str
    auth_header: str
    timeout: float
    allow_test_webhooks: bool


class _Redactor:
    """Blanks the secret and anything derived from it (the JWT, a signature) out of text that leaves
    the provider — a workflow that echoes its request headers back must not leak them through Marvin."""

    def __init__(self, *values: str) -> None:
        self._values: list[str] = []
        self.add(*values)

    def add(self, *values: str) -> None:
        found = set(self._values) | {v for v in values if v and len(v) >= 4}
        found |= {json.dumps(v)[1:-1] for v in found}  # the same value as it appears inside a JSON string
        self._values = sorted(found, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, REDACTED)
        return text


def _text(value) -> str:
    """A workflow argument as a string — an unresolved ``${…}`` template counts as blank."""
    text = str(value if value is not None else "").strip()
    return "" if text.startswith("${") or text in ("None", "null") else text


def _bool(value, default: bool) -> bool:
    if value is None or (isinstance(value, str) and not _text(value)):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"Expected true or false, got {value!r}.")


def _url(value, field: str) -> str:
    url = _text(value).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.query or parsed.fragment or "@" in parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL like https://n8n.example.com, got {url!r}.")
    return url


def _settings(ctx: IntegrationContext) -> _Settings:
    config = ctx.config or {}
    base = _url(config.get("base_url"), "base_url")
    editor = _url(config.get("editor_url"), "editor_url") if _text(config.get("editor_url")) else base
    prefix = _text(config.get("webhook_prefix")).strip("/") or DEFAULT_PREFIX
    if ".." in prefix or not re.fullmatch(r"[A-Za-z0-9._~/-]+", prefix):
        raise ValueError(f"webhook_prefix must be a plain path like 'webhook', got {prefix!r}.")
    default_auth = (_text(config.get("default_auth")) or AUTH_JWT).lower()
    if default_auth not in AUTH_MODES:
        raise ValueError(f"default_auth must be one of {', '.join(AUTH_MODES)}, not {default_auth!r}.")
    header = _text(config.get("auth_header")) or DEFAULT_AUTH_HEADER
    if not _HEADER_NAME.fullmatch(header):
        raise ValueError(f"auth_header must be an HTTP header name, got {header!r}.")
    raw_timeout = config.get("timeout_seconds")
    try:
        timeout = float(raw_timeout) if raw_timeout not in (None, "") else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        raise ValueError(f"timeout_seconds must be a number of seconds, got {raw_timeout!r}.") from None
    return _Settings(
        base_url=base,
        editor_url=editor,
        webhook_prefix=prefix,
        default_auth=default_auth,
        auth_header=header,
        timeout=min(max(timeout, 1.0), MAX_TIMEOUT),
        allow_test_webhooks=_bool(config.get("allow_test_webhooks"), False),
    )


def _webhook_path(value) -> str:
    """A Webhook node's path, relative to the webhook prefix: no scheme, no ``..``, no query or fragment."""
    path = _text(value).strip("/")
    if not path:
        raise ValueError("path is required: the Webhook node's path, e.g. marvin/new-entry.")
    if "://" in path or ".." in path or any(c in path for c in "?#\\") or any(c.isspace() or ord(c) < 32 for c in path):
        raise ValueError(f"path must be the Webhook node's relative path (no scheme, '..', '?' or '#'), got {path!r}.")
    return path


def _retry_after(resp: Response) -> float | None:
    """Seconds from a ``Retry-After`` header — delta-seconds or an HTTP date — or None."""
    value = next((str(v).strip() for k, v in (resp.headers or {}).items() if k.lower() == "retry-after"), "")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _n8n_message(resp: Response) -> str | None:
    """n8n's own error message (``{"message": …}``), or None when the body isn't shaped like n8n's."""
    try:
        body = json.loads(resp.content or b"")
    except ValueError:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else None


def _fail(resp: Response, what: str, redact: _Redactor, *, webhook: bool) -> N8nError:
    """The N8nError for a non-2xx reply. A 500 is n8n's ``workflow_error`` only from a webhook and only
    with n8n's ``{"message": …}`` body; a proxy's error page in front of n8n is ``unavailable``."""
    status, message = resp.status_code, _n8n_message(resp)
    if status in (401, 403):
        code = CODE_AUTH
    elif status == 404:
        code = CODE_NOT_FOUND
    elif status == 429:
        code = CODE_RATE_LIMITED
    elif status == 500 and webhook and message is not None:
        code = CODE_WORKFLOW_ERROR
    elif status >= 500:
        code = CODE_UNAVAILABLE
    elif status >= 400:
        code = CODE_REJECTED
    else:
        code = CODE_UNKNOWN
    detail = redact(message if message is not None else resp.text)[:ERROR_TEXT_LIMIT].strip()
    return N8nError(
        f"n8n {what} failed: HTTP {status}{f': {detail}' if detail else ''}",
        code,
        retry_after=_retry_after(resp) if code == CODE_RATE_LIMITED else None,
    )


def _classify(exc: Exception, what: str, redact: _Redactor) -> N8nError:
    """A transport or argument failure as an N8nError with the right code."""
    detail = redact(f"{type(exc).__name__}: {exc}")[:ERROR_TEXT_LIMIT]
    if any(cls.__name__ == "SsrfError" for cls in type(exc).__mro__):  # the core's guard (a ValueError, so first)
        return N8nError(f"Marvin refused to call n8n for {what} — the host isn't public ({detail}).", CODE_BLOCKED)
    if isinstance(exc, ValueError) and SIZE_CAP_TEXT in str(exc):
        return N8nError(
            f"n8n's reply to {what} was larger than Marvin keeps (INTEGRATION_HTTP_MAX_BYTES); the request was delivered.", CODE_TOO_LARGE
        )
    if isinstance(exc, ValueError):
        return N8nError(redact(str(exc))[:ERROR_TEXT_LIMIT], CODE_INVALID)
    if isinstance(exc, TimeoutError):  # sent, then no reply in time: n8n may have run it
        return N8nError(f"n8n {what} timed out waiting for a reply — the workflow may have run ({detail}).", CODE_TIMEOUT)
    if isinstance(exc, OSError):  # couldn't connect: DNS, refused, a connect timeout (URLError wraps those)
        return N8nError(f"n8n {what} failed: couldn't reach n8n ({detail}).", CODE_UNAVAILABLE)
    return N8nError(f"n8n {what} failed: {detail}", CODE_UNKNOWN)


@register_provider
class N8nProvider(IntegrationProvider):
    slug = "n8n"
    name = "n8n"
    description = (
        "Hand work to n8n: a workflow step triggers an n8n workflow through its Webhook node with signed, expiring auth, "
        "picked from n8n's own list. Read an execution's status back, and let n8n report results to a Marvin incoming webhook."
    )
    category = CATEGORY_DESTINATION
    icon = "🔗"
    logo = "logo.svg"  # the n8n mark, shown instead of the emoji

    content = CONTENT
    # What Marvin learns about n8n (SDK 0.9.0): the how-to its agents read, and the Library's hand-off recipe.
    guide = "guide.md"
    recipes = RECIPES
    error_policy: ClassVar[ErrorPolicy] = ERROR_POLICY

    credentials = (
        CredentialField(
            key="api_key",
            label="n8n API key",
            help=(
                "Optional. n8n → Settings → n8n API → Create an API key, with only the workflow:read and execution:read scopes where offered. "
                "Store it as a workspace secret and enter {{N8N_API_KEY}}. Without it the connection is webhook-only: trigger_workflow works, "
                "the workflow list and execution read-back don't."
            ),
            required=False,
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "title": "n8n URL",
                "description": "Where Marvin reaches n8n — webhooks and the API, e.g. https://n8n.iwobble.com. It must be a public host: "
                "Marvin refuses private and in-cluster addresses.",
            },
            "editor_url": {
                "type": "string",
                "title": "Editor URL",
                "description": "Optional: the address people open n8n at, for execution links. Blank = the n8n URL.",
            },
            "webhook_prefix": {
                "type": "string",
                "title": "Webhook prefix",
                "default": DEFAULT_PREFIX,
                "description": "n8n's production webhook path (N8N_ENDPOINT_WEBHOOK). Almost always 'webhook'.",
            },
            "default_auth": {
                "type": "string",
                "title": "Default auth",
                "enum": list(AUTH_MODES),
                "default": AUTH_JWT,
                "description": "How a trigger proves it's Marvin: jwt (a 60-second HS256 token n8n's JWT Auth credential checks), header "
                "(a fixed secret header), hmac (X-Marvin-Signature, checked by the workflow itself) or none. A step can override it.",
            },
            "auth_header": {
                "type": "string",
                "title": "Auth header",
                "default": DEFAULT_AUTH_HEADER,
                "description": "The header that carries the secret in header mode.",
            },
            "timeout_seconds": {
                "type": "number",
                "title": "Timeout (seconds)",
                "default": DEFAULT_TIMEOUT,
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "description": "How long a trigger waits for n8n's reply. Use 'Respond immediately' in n8n for anything slow.",
            },
            "allow_test_webhooks": {
                "type": "boolean",
                "title": "Allow test webhooks",
                "default": False,
                "description": "Let a step call n8n's /webhook-test/ URLs (only live while the editor is listening).",
            },
        },
        "required": ["base_url"],
        "additionalProperties": False,
    }

    actions = (
        ProviderAction(
            key="trigger_workflow",
            label="Trigger n8n workflow",
            description=(
                "Call an n8n workflow's Webhook node with {marvin: {entry_id, meta, idempotency_key, sent_at}, data}. Signed as the "
                "connection's default auth says (a JWT unless changed); X-Marvin-Idempotency-Key stays the same across retries of one run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "title": "Workflow",
                        "description": "The Webhook node's path (relative, e.g. marvin/new-entry). Pick it from n8n's active workflows, or type it.",
                        "x-marvin-options": {"action": "list_webhooks", "value": "path", "label": "label"},
                    },
                    "method": {
                        "type": "string",
                        "title": "Method",
                        "enum": list(METHODS),
                        "default": "POST",
                        "description": "The Webhook node's HTTP method. GET sends no body (no data or meta).",
                    },
                    "data": {"type": "object", "title": "Data", "description": "What the workflow gets as `data`."},
                    "entry_id": {"type": "string", "title": "Entry", "description": "The Marvin entry (e.g. ${entry.id}), so n8n can report back."},
                    "meta": {"type": "object", "title": "Meta", "description": "Optional extra context, sent as marvin.meta."},
                    "auth": {
                        "type": "string",
                        "title": "Auth",
                        "enum": list(AUTH_MODES),
                        "description": "Overrides the connection's default auth for this step.",
                    },
                    "secret": {
                        "type": "string",
                        "title": "Webhook secret",
                        "description": f"{WEBHOOK_SECRET_REF} — the JWT passphrase, header value or HMAC key. Required unless auth is none.",
                    },
                    "test": {
                        "type": "boolean",
                        "title": "Test webhook",
                        "default": False,
                        "description": "Call /webhook-test/ instead (only if the connection allows test webhooks).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ok": _BOOL,
                    "status_code": {"type": "integer"},
                    "path": _STR,
                    "response": {"description": "n8n's reply: JSON, or text up to 2000 characters."},
                    "execution_id": _STR,
                    "execution_url": _STR,
                },
            },
        ),
        ProviderAction(
            key="list_workflows",
            label="List n8n workflows",
            description="n8n's workflows (active by default) with their Webhook nodes: path, method, auth, respond mode and production URL. Needs the API key.",
            input_schema={
                "type": "object",
                "properties": {
                    "active": {
                        "type": "boolean",
                        "title": "Active",
                        "default": True,
                        "description": "true: active workflows only; false: inactive only.",
                    },
                    **_LIST_FILTERS,
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object", "properties": {"items": {"type": "array", "items": _WORKFLOW_OUT}, "truncated": _BOOL}},
            cost_hint="free",
            error_policy=READ_POLICY,
        ),
        ProviderAction(
            key="list_webhooks",
            label="List n8n webhooks",
            description="Every Webhook node in n8n's active workflows, one item per path and method, labelled for a picker. Needs the API key.",
            input_schema={"type": "object", "properties": dict(_LIST_FILTERS), "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": _STR,
                                "method": _STR,
                                "label": _STR,
                                "workflow_id": _STR,
                                "workflow_name": _STR,
                                "active": _BOOL,
                                "auth": _STR,
                            },
                        },
                    },
                    "truncated": _BOOL,
                },
            },
            cost_hint="free",
            error_policy=READ_POLICY,
        ),
        ProviderAction(
            key="get_execution",
            label="Get n8n execution",
            description="An execution's status and timing; with include_error, the last node and its error message (300 characters). Needs the API key.",
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string", "title": "Execution id", "description": "e.g. a trigger's execution_id."},
                    "include_error": {"type": "boolean", "title": "Include error", "default": True},
                },
                "required": ["execution_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "id": _STR,
                    "status": _STR,
                    "workflow_id": _STR,
                    "started_at": _STR,
                    "stopped_at": _STR,
                    "retry_of": _STR,
                    "url": _STR,
                    "last_node_executed": _STR,
                    "error_message": _STR,
                },
            },
            cost_hint="free",
            error_policy=READ_POLICY,
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _api_get(ctx: IntegrationContext, s: _Settings, path: str, query: dict | None = None) -> Response:
        url = f"{s.base_url}/api/v1{path}{f'?{urlencode(query)}' if query else ''}"
        return ctx.http.get(url, headers={"X-N8N-API-KEY": ctx.secret or "", "Accept": "application/json"}, timeout=s.timeout)

    @staticmethod
    def _api_json(resp: Response, what: str) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise N8nError(f"n8n {what} answered with something that isn't JSON (HTTP {resp.status_code}).", CODE_UNKNOWN) from None
        if not isinstance(body, dict):
            raise N8nError(f"n8n {what} answered with unexpected JSON (HTTP {resp.status_code}).", CODE_UNKNOWN)
        return body

    @staticmethod
    def _require_key(ctx: IntegrationContext, key: str) -> None:
        if not ctx.secret:
            raise N8nError(f"{key} needs the n8n API key: this connection is webhook-only.", CODE_AUTH)

    # ---- lifecycle --------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        redact = _Redactor(ctx.secret or "")
        try:
            s = _settings(ctx)
            if ctx.secret:
                resp = self._api_get(ctx, s, "/workflows", {"limit": 1, "excludePinnedData": "true"})
            else:
                resp = ctx.http.get(f"{s.base_url}/healthz", timeout=s.timeout)
        except Exception as e:  # noqa: BLE001 — reported on the card, never raised
            return ("error", redact(f"{type(e).__name__}: {e}")[:ERROR_TEXT_LIMIT])
        if ctx.secret and resp.status_code in (401, 403):
            return ("error", f"API key rejected (HTTP {resp.status_code}).")
        if not resp.ok:
            return ("error", str(_fail(resp, "API check" if ctx.secret else "health check", redact, webhook=False)))
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "trigger_workflow": self._trigger_workflow,
            "list_workflows": self._list_workflows,
            "list_webhooks": self._list_webhooks,
            "get_execution": self._get_execution,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"n8n has no action '{key}'")
        args = args or {}
        step_secret = str(args.get("secret") or "").strip()
        # An unresolved {{REFERENCE}} is not a secret, and naming it is what makes the error useful.
        redact = _Redactor(ctx.secret or "", "" if step_secret.startswith(("{{", "${")) else step_secret)
        try:
            return handler(args, ctx, redact)
        except (N8nError, NotImplementedError):
            raise
        except Exception as e:  # every failure fails the step with a code, never escapes the workflow engine
            raise _classify(e, key, redact) from e

    # ---- trigger_workflow -------------------------------------------------------------------

    def _trigger_workflow(self, args: dict, ctx: IntegrationContext, redact: _Redactor) -> dict:
        s = _settings(ctx)
        path = _webhook_path(args.get("path"))
        method = (_text(args.get("method")) or "POST").upper()
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}, not {method!r}.")
        test = _bool(args.get("test"), False)
        if test and not s.allow_test_webhooks:
            raise ValueError("Test webhooks are off for this connection (allow_test_webhooks).")
        mode = (_text(args.get("auth")) or s.default_auth).lower()
        if mode not in AUTH_MODES:
            raise ValueError(f"auth must be one of {', '.join(AUTH_MODES)}, not {mode!r}.")
        secret = str(args.get("secret") or "").strip()
        if mode != AUTH_NONE and (not secret or secret.startswith(("{{", "${"))):
            raise ValueError(f"secret is required for {mode} auth — pass {WEBHOOK_SECRET_REF} (a workspace secret that resolved to nothing?).")

        data, meta = args.get("data"), args.get("meta")
        for name, value in (("data", data), ("meta", meta)):
            if value not in (None, "") and not isinstance(value, dict):
                raise ValueError(f"{name} must be an object, got {type(value).__name__}.")
        entry_id = _text(args.get("entry_id"))
        idempotency_key = ctx.idempotency_key(path, entry_id)
        now = int(time.time())

        if method == "GET":
            if data or meta:
                raise ValueError("A GET webhook gets no body: use POST or PUT to send data or meta.")
            body = b""
        else:
            envelope = {
                "marvin": {
                    "entry_id": entry_id or None,
                    "meta": meta or {},
                    "idempotency_key": idempotency_key,
                    "sent_at": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
                },
                "data": data or {},
            }
            body = json.dumps(envelope, separators=(",", ":"), default=str).encode()  # serialised once: these bytes are signed and sent

        headers = {"Accept": "application/json", IDEMPOTENCY_HEADER: idempotency_key, TIMESTAMP_HEADER: str(now)}
        if body:
            headers["Content-Type"] = "application/json"
        signed = auth_headers(
            mode,
            secret=secret,
            body=body,
            path=path,
            issued_at=now,
            jti=idempotency_key,
            header_name=s.auth_header if mode == AUTH_HEADER else "",
        )
        redact.add(*signed.values(), *(v.split(" ", 1)[-1] for v in signed.values()))
        headers.update(signed)

        prefix = TEST_PREFIX if test else s.webhook_prefix
        shown = f"/{prefix}/{path}"
        url = f"{s.base_url}/{prefix}/{quote(path, safe=PATH_SAFE)}"
        what = f"webhook {method} {shown}"
        try:
            if method == "GET":
                resp = ctx.http.get(url, headers=headers, timeout=s.timeout)
            else:
                resp = getattr(ctx.http, method.lower())(url, data=body, headers=headers, timeout=s.timeout)
        except Exception as e:  # classified with what was being called
            raise _classify(e, what, redact) from e
        ctx.logger.info("n8n: %s → HTTP %s", what, resp.status_code)
        if not resp.ok:
            raise _fail(resp, what, redact, webhook=True)
        return {"ok": True, "status_code": resp.status_code, "path": path, **self._reply(resp, s, redact)}

    @staticmethod
    def _reply(resp: Response, s: _Settings, redact: _Redactor) -> dict:
        """n8n's reply (JSON when it fits, else text, both capped) and the execution it names, if any —
        the "receive from Marvin" recipe answers {executionId, workflowId}."""
        text = redact(resp.text)
        try:
            parsed = json.loads(text) if text.strip() else None
        except ValueError:
            parsed = None
        response = parsed if parsed is not None and len(text) <= RESPONSE_TEXT_LIMIT else text[:RESPONSE_TEXT_LIMIT]
        found = parsed if isinstance(parsed, dict) else {}
        execution_id = _text(found.get("executionId") or found.get("execution_id"))
        workflow_id = _text(found.get("workflowId") or found.get("workflow_id"))
        url = (
            f"{s.editor_url}/workflow/{quote(workflow_id, safe='')}/executions/{quote(execution_id, safe='')}" if execution_id and workflow_id else ""
        )
        return {"response": response, "execution_id": execution_id, "execution_url": url}

    # ---- list_workflows / list_webhooks -----------------------------------------------------

    def _fetch_workflows(self, args: dict, ctx: IntegrationContext, redact: _Redactor, s: _Settings, active: bool) -> tuple[list[dict], bool]:
        query: dict = {"limit": PAGE_SIZE, "excludePinnedData": "true", "active": "true" if active else "false"}
        if tag := _text(args.get("tag")):
            query["tags"] = tag
        if name := _text(args.get("name")):
            query["name"] = name
        workflows: list[dict] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            resp = self._api_get(ctx, s, "/workflows", {**query, **({"cursor": cursor} if cursor else {})})
            if not resp.ok:
                raise _fail(resp, "list workflows", redact, webhook=False)
            page = self._api_json(resp, "list workflows")
            workflows.extend(w for w in page.get("data") or [] if isinstance(w, dict))
            cursor = _text(page.get("nextCursor"))
            if not cursor:
                return workflows, False
        return workflows, True  # stopped at the page cap with more to come

    @staticmethod
    def _webhooks(workflow: dict, s: _Settings) -> list[dict]:
        """The workflow's enabled Webhook nodes, one entry per path and method — nothing else from the node."""
        out = []
        for node in workflow.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") != WEBHOOK_NODE or node.get("disabled"):
                continue
            params = node.get("parameters") or {}
            path = _text(params.get("path") or node.get("webhookId")).strip("/")
            if not path:
                continue
            methods = params.get("httpMethod") or "GET"  # n8n's default; a list when the node accepts several
            for method in methods if isinstance(methods, list) else [methods]:
                out.append(
                    {
                        "path": path,
                        "method": str(method).upper(),
                        "auth": NODE_AUTH.get(str(params.get("authentication") or "none"), str(params.get("authentication"))),
                        "respond_mode": str(params.get("responseMode") or "onReceived"),
                        "production_url": f"{s.base_url}/{s.webhook_prefix}/{path}",
                    }
                )
        return out

    def _project(self, workflow: dict, s: _Settings) -> dict:
        return {
            "id": str(workflow.get("id") or ""),
            "name": str(workflow.get("name") or ""),
            "active": bool(workflow.get("active")),
            "tags": [str(t.get("name") or "") for t in workflow.get("tags") or [] if isinstance(t, dict)],
            "webhooks": self._webhooks(workflow, s),
        }

    def _list_workflows(self, args: dict, ctx: IntegrationContext, redact: _Redactor) -> dict:
        self._require_key(ctx, "list_workflows")
        s = _settings(ctx)
        workflows, truncated = self._fetch_workflows(args, ctx, redact, s, _bool(args.get("active"), True))
        return {"items": [self._project(w, s) for w in workflows], "truncated": truncated}

    def _list_webhooks(self, args: dict, ctx: IntegrationContext, redact: _Redactor) -> dict:
        self._require_key(ctx, "list_webhooks")
        s = _settings(ctx)
        workflows, truncated = self._fetch_workflows(args, ctx, redact, s, True)
        items = []
        for workflow in workflows:
            projected = self._project(workflow, s)
            for hook in projected["webhooks"]:
                items.append(
                    {
                        "path": hook["path"],
                        "method": hook["method"],
                        "label": f"{projected['name']} — {hook['method']} /{hook['path']}",
                        "workflow_id": projected["id"],
                        "workflow_name": projected["name"],
                        "active": projected["active"],
                        "auth": hook["auth"],
                    }
                )
        return {"items": items, "truncated": truncated}

    # ---- get_execution ----------------------------------------------------------------------

    def _get_execution(self, args: dict, ctx: IntegrationContext, redact: _Redactor) -> dict:
        self._require_key(ctx, "get_execution")
        s = _settings(ctx)
        execution_id = _text(args.get("execution_id"))
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise ValueError(f"execution_id must be an n8n execution id, got {execution_id!r}.")
        include_error = _bool(args.get("include_error"), True)
        path = f"/executions/{quote(execution_id, safe='')}"
        oversized = False
        try:
            resp = self._api_get(ctx, s, path, {"includeData": "true" if include_error else "false"})
        except ValueError as e:
            if not (include_error and SIZE_CAP_TEXT in str(e)):
                raise
            # The execution's data is too big to read for its error: report the status without it.
            oversized = True
            resp = self._api_get(ctx, s, path, {"includeData": "false"})
        if not resp.ok:
            raise _fail(resp, f"execution {execution_id}", redact, webhook=False)
        body = self._api_json(resp, f"execution {execution_id}")
        workflow_id = _text(body.get("workflowId"))
        out = {
            "id": _text(body.get("id")) or execution_id,
            "status": _text(body.get("status")),
            "workflow_id": workflow_id,
            "started_at": _text(body.get("startedAt")),
            "stopped_at": _text(body.get("stoppedAt")),
            "retry_of": _text(body.get("retryOf")),
            "url": f"{s.editor_url}/workflow/{quote(workflow_id, safe='')}/executions/{quote(execution_id, safe='')}" if workflow_id else "",
        }
        if include_error:
            result = ((body.get("data") or {}).get("resultData") or {}) if isinstance(body.get("data"), dict) else {}
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            message = "(execution data too large to read — open it in n8n)" if oversized else _text(error.get("message"))
            out["last_node_executed"] = _text(result.get("lastNodeExecuted"))
            out["error_message"] = redact(message)[:ERROR_TEXT_LIMIT]
        return out  # execution data itself is never passed through
