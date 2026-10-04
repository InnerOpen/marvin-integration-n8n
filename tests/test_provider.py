"""n8n provider — webhook calls and API reads through a stub http helper, no network.

API replies are recorded n8n JSON (tests/fixtures), written from n8n's documented public API shapes
(`GET /api/v1/workflows`, `GET /api/v1/executions/{id}`) and real Webhook nodes."""

import hashlib
import hmac
import json
import logging
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from marvin_integration_sdk import Handle, IntegrationContext, Response, Retry, register_provider, resolve_policy

from marvin_integration_n8n import N8nProvider
from marvin_integration_n8n.provider import (
    CODE_AUTH,
    CODE_BLOCKED,
    CODE_INVALID,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    CODE_REJECTED,
    CODE_TIMEOUT,
    CODE_TOO_LARGE,
    CODE_UNAVAILABLE,
    CODE_UNKNOWN,
    CODE_WORKFLOW_ERROR,
    CODES,
    MAX_PAGES,
    N8nError,
)

BASE = "https://n8n.iwobble.com"
KEY = "test-key"
SECRET = "SENTINEL-webhook-secret-7f3a-0123456789"
PATH = "marvin/new-entry"
FIXTURES = Path(__file__).parent / "fixtures"
_LOG = logging.getLogger("test.n8n")


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class SsrfError(ValueError):
    """Stands in for the core's guard error (marvin.services.integrations.http_client.SsrfError)."""


class _Http:
    """Routes: (method, needle, status, payload[, headers]). A payload that is an exception is raised;
    bytes are sent as they are; anything else as JSON. The first route whose needle is in the URL wins."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[dict] = []

    def _answer(self, method, url, data=None, headers=None, timeout=None, json_body=None):
        self.calls.append({"method": method, "url": url, "data": data, "json": json_body, "headers": headers or {}, "timeout": timeout})
        for route_method, needle, status, payload, *extra in self.routes:
            if route_method == method and needle in url:
                if isinstance(payload, BaseException):
                    raise payload
                content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return Response(status_code=status, headers=extra[0] if extra else {}, content=content)
        return Response(status_code=599, content=b'{"message":"no stub route"}')

    def get(self, url, *, headers=None, timeout=15):
        return self._answer("GET", url, headers=headers, timeout=timeout)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("POST", url, data, headers, timeout, json)

    def put(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("PUT", url, data, headers, timeout, json)

    def delete(self, url, *, headers=None, timeout=15):
        return self._answer("DELETE", url, headers=headers, timeout=timeout)


def _ctx(http, secret=KEY, seed=None, **config):
    return IntegrationContext(config={"base_url": BASE, **config}, secret=secret, logger=_LOG, http=http, idempotency_seed=seed)


def _run(http, key, config=None, api_key=KEY, seed=None, **args):
    return N8nProvider().run_action(key, args, _ctx(http, secret=api_key, seed=seed, **(config or {})))


def _trigger(http, config=None, **args):
    return _run(http, "trigger_workflow", config, **{"path": PATH, "secret": SECRET, **args})


def _error(http, key="trigger_workflow", config=None, **args) -> N8nError:
    with pytest.raises(N8nError) as raised:
        if key == "trigger_workflow":
            _trigger(http, config, **args)
        else:
            _run(http, key, config, **args)
    return raised.value


def _query(call):
    return {k: v[0] for k, v in parse_qs(urlparse(call["url"]).query).items()}


_ACCEPTED = ("POST", f"/webhook/{PATH}", 200, {"executionId": "1001", "workflowId": "2tUt1wbLX592XDdX"})


# --- check -----------------------------------------------------------------------------------------


def test_check_with_a_key_reads_one_workflow():
    http = _Http([("GET", "/api/v1/workflows", 200, {"data": [], "nextCursor": None})])
    assert N8nProvider().check(_ctx(http)) == ("ok", None)
    (call,) = http.calls
    assert call["headers"]["X-N8N-API-KEY"] == KEY and _query(call)["limit"] == "1"


def test_check_reports_a_rejected_key():
    http = _Http([("GET", "/api/v1/workflows", 401, {"message": "unauthorized"})])
    assert N8nProvider().check(_ctx(http)) == ("error", "API key rejected (HTTP 401).")


def test_check_without_a_key_is_webhook_only_and_probes_healthz():
    http = _Http([("GET", "/healthz", 200, {"status": "ok"})])
    assert N8nProvider().check(_ctx(http, secret=None)) == ("ok", None)
    assert http.calls[0]["url"] == f"{BASE}/healthz" and "X-N8N-API-KEY" not in (http.calls[0]["headers"] or {})


@pytest.mark.parametrize(
    "config", [{"base_url": ""}, {"base_url": "ftp://n8n.example.com"}, {"default_auth": "basic"}, {"auth_header": "Bad Header"}]
)
def test_check_reports_bad_config(config):
    state, _ = N8nProvider().check(_ctx(_Http([("GET", "/healthz", 200, {})]), secret=None, **config))
    assert state == "error"


def test_check_never_leaks_the_key():
    http = _Http([("GET", "/api/v1/workflows", 500, {"message": f"bad key {KEY}"})])
    state, detail = N8nProvider().check(_ctx(http))
    assert state == "error" and KEY not in detail


# --- trigger_workflow: what is sent ------------------------------------------------------------------


def test_trigger_posts_the_envelope_as_signed_bytes_with_a_jwt():
    http = _Http([_ACCEPTED])
    out = _trigger(http, {"editor_url": "https://n8n-editor.example.com/"}, entry_id="entry-1", data={"title": "Hi"}, meta={"source": "card"})

    (call,) = http.calls
    assert call["url"] == f"{BASE}/webhook/{PATH}" and call["json"] is None
    sent: bytes = call["data"]
    assert isinstance(sent, bytes)
    envelope = json.loads(sent)
    headers = call["headers"]
    key = headers["X-Marvin-Idempotency-Key"]
    assert envelope == {
        "marvin": {"entry_id": "entry-1", "meta": {"source": "card"}, "idempotency_key": key, "sent_at": envelope["marvin"]["sent_at"]},
        "data": {"title": "Hi"},
    }
    assert envelope["marvin"]["sent_at"].endswith("Z") and headers["Content-Type"] == "application/json"

    token = headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience=PATH, issuer="marvin")
    assert claims["body_sha256"] == hashlib.sha256(sent).hexdigest()  # the signed bytes are the sent bytes
    assert claims["jti"] == key and claims["exp"] - claims["iat"] == 60
    assert str(claims["iat"]) == headers["X-Marvin-Timestamp"]

    assert out == {
        "ok": True,
        "status_code": 200,
        "path": PATH,
        "response": {"executionId": "1001", "workflowId": "2tUt1wbLX592XDdX"},
        "execution_id": "1001",
        "execution_url": "https://n8n-editor.example.com/workflow/2tUt1wbLX592XDdX/executions/1001",
    }


def test_idempotency_key_is_stable_across_one_retry_chain():
    keys = []
    for entry in ("entry-1", "entry-1", "entry-2"):
        http = _Http([_ACCEPTED])
        _run(http, "trigger_workflow", seed="run-42", path=PATH, secret=SECRET, entry_id=entry)
        keys.append(http.calls[0]["headers"]["X-Marvin-Idempotency-Key"])
    assert keys[0] == keys[1] != keys[2]


def test_header_mode_sends_the_secret_in_the_configured_header():
    http = _Http([_ACCEPTED])
    _trigger(http, {"default_auth": "header", "auth_header": "X-Marvin-Hook"})
    headers = http.calls[0]["headers"]
    assert headers["X-Marvin-Hook"] == SECRET and "Authorization" not in headers


def test_hmac_mode_signs_timestamp_and_sent_bytes():
    http = _Http([_ACCEPTED])
    _trigger(http, auth="hmac", data={"a": 1})
    call = http.calls[0]
    ts = call["headers"]["X-Marvin-Timestamp"]
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + call["data"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Marvin-Signature"] == f"t={ts},v1={expected}"


def test_auth_none_needs_no_secret_and_sends_no_credentials():
    http = _Http([_ACCEPTED])
    _run(http, "trigger_workflow", path=PATH, auth="none")
    headers = http.calls[0]["headers"]
    assert not {"Authorization", "X-Marvin-Signature", "X-Marvin-Hook"} & set(headers)


@pytest.mark.parametrize("secret", [None, "", "{{N8N_WEBHOOK_SECRET}}", "${secrets.x}"])
def test_a_missing_or_unresolved_secret_is_refused(secret):
    http = _Http([_ACCEPTED])
    err = _error(http, secret=secret)
    assert err.code == CODE_INVALID and "{{N8N_WEBHOOK_SECRET}}" in str(err) and not http.calls


def test_put_and_get():
    http = _Http([("PUT", f"/webhook/{PATH}", 200, {"ok": 1}), ("GET", f"/webhook/{PATH}", 200, {"ok": 1})])
    _trigger(http, method="put", data={"a": 1})
    _trigger(http, method="GET")
    put, get = http.calls
    assert put["method"] == "PUT" and json.loads(put["data"])["data"] == {"a": 1}
    assert get["method"] == "GET" and get["data"] is None and "Content-Type" not in get["headers"]
    claims = jwt.decode(get["headers"]["Authorization"][7:], SECRET, algorithms=["HS256"], audience=PATH)
    assert claims["body_sha256"] == hashlib.sha256(b"").hexdigest()


def test_get_refuses_a_body():
    assert _error(_Http([]), method="GET", data={"a": 1}).code == CODE_INVALID


def test_a_leading_slash_is_tolerated():
    http = _Http([_ACCEPTED])
    _trigger(http, path=f"/{PATH}/")
    assert http.calls[0]["url"] == f"{BASE}/webhook/{PATH}"


@pytest.mark.parametrize("path", ["", "https://evil.example.com/x", "../admin", "a/../b", "x?y=1", "x#frag", "a b", "a\\b"])
def test_path_must_be_a_relative_webhook_path(path):
    http = _Http([])
    assert _error(http, path=path).code == CODE_INVALID and not http.calls


def test_test_webhooks_are_refused_unless_the_connection_allows_them():
    assert _error(_Http([]), test=True).code == CODE_INVALID
    http = _Http([("POST", f"/webhook-test/{PATH}", 200, {})])
    _trigger(http, {"allow_test_webhooks": True}, test=True)
    assert http.calls[0]["url"] == f"{BASE}/webhook-test/{PATH}"


def test_custom_prefix_and_timeout_cap():
    http = _Http([("POST", f"/hooks/{PATH}", 200, {})])
    _trigger(http, {"webhook_prefix": "/hooks/", "timeout_seconds": 120})
    assert http.calls[0]["url"] == f"{BASE}/hooks/{PATH}" and http.calls[0]["timeout"] == 30


def test_a_long_or_plain_reply_is_text_capped_at_2000():
    http = _Http([("POST", f"/webhook/{PATH}", 200, b"x" * 5000)])
    out = _trigger(http)
    assert out["response"] == "x" * 2000 and out["execution_id"] == "" and out["execution_url"] == ""
    http = _Http([("POST", f"/webhook/{PATH}", 200, {"message": "Workflow was started"})])
    assert _trigger(http)["response"] == {"message": "Workflow was started"}


# --- trigger_workflow: status and failure → code -------------------------------------------------


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, b"Authorization is required!", CODE_AUTH),
        (403, b"Authorization data is wrong!", CODE_AUTH),
        (404, {"code": 404, "message": 'The requested webhook "POST marvin/new-entry" is not registered.'}, CODE_NOT_FOUND),
        (429, {"message": "Too many requests"}, CODE_RATE_LIMITED),
        (500, {"message": "Error in workflow"}, CODE_WORKFLOW_ERROR),
        (500, b"<html><body>Internal Server Error</body></html>", CODE_UNAVAILABLE),
        (502, b"<html>Bad gateway</html>", CODE_UNAVAILABLE),
        (503, {"message": "n8n is starting up"}, CODE_UNAVAILABLE),
        (504, b"", CODE_UNAVAILABLE),
        (400, {"message": "missing field"}, CODE_REJECTED),
        (409, {"error": "duplicate"}, CODE_REJECTED),
        (422, b"nope", CODE_REJECTED),
    ],
)
def test_status_maps_to_a_code(status, body, code):
    err = _error(_Http([("POST", f"/webhook/{PATH}", status, body)]))
    assert err.code == code and f"HTTP {status}" in str(err)
    assert err.retry_after is None


def test_rate_limit_carries_retry_after():
    err = _error(_Http([("POST", f"/webhook/{PATH}", 429, {}, {"Retry-After": "120"})]))
    assert (err.code, err.retry_after) == (CODE_RATE_LIMITED, 120.0)
    err = _error(_Http([("POST", f"/webhook/{PATH}", 429, {}, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})]))
    assert err.retry_after == 0.0  # a date in the past means "now"


@pytest.mark.parametrize(
    "exc,code",
    [
        (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), CODE_UNAVAILABLE),
        (urllib.error.URLError(OSError(-2, "Name or service not known")), CODE_UNAVAILABLE),
        (urllib.error.URLError(TimeoutError("timed out")), CODE_UNAVAILABLE),  # connect timeout: n8n never got it
        (TimeoutError("The read operation timed out"), CODE_TIMEOUT),  # sent, no reply: n8n may have run it
        (SsrfError("Refusing to call non-public host 'n8n.n8n.svc.cluster.local'."), CODE_BLOCKED),
        (ValueError("Response exceeds the size cap."), CODE_TOO_LARGE),
        (RuntimeError("boom"), CODE_UNKNOWN),
    ],
)
def test_transport_failures_map_to_a_code(exc, code):
    assert _error(_Http([("POST", f"/webhook/{PATH}", 0, exc)])).code == code


# --- secrets never leave the provider ------------------------------------------------------------


def _echo_everything(http_calls):
    """A reply that echoes the request headers back, as a careless n8n workflow might."""
    return json.dumps({"headers": http_calls[-1]["headers"]}).encode()


class _EchoHttp(_Http):
    def __init__(self, status):
        super().__init__([])
        self.status = status

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers or {}, "timeout": timeout})
        return Response(status_code=self.status, content=_echo_everything(self.calls))


@pytest.mark.parametrize("mode", ["jwt", "header", "hmac"])
@pytest.mark.parametrize("status", [200, 401, 500, 400])
def test_secret_jwt_and_headers_never_reach_results_errors_or_logs(mode, status, caplog):
    caplog.set_level(logging.DEBUG)
    http = _EchoHttp(status)
    try:
        produced = json.dumps(_trigger(http, auth=mode))
    except N8nError as e:
        produced = f"{e} {e.args} {e.code}"
    sensitive = [SECRET] + [v.split(" ", 1)[-1] for k, v in http.calls[0]["headers"].items() if k in ("Authorization", "X-Marvin-Signature")]
    for value in sensitive:
        assert value not in produced
        assert value not in caplog.text
    assert "[redacted]" in produced


def test_transport_errors_never_carry_the_secret():
    err = _error(_Http([("POST", f"/webhook/{PATH}", 0, ValueError(f"bad value {SECRET}"))]))
    assert SECRET not in str(err)


# --- list_workflows / list_webhooks -------------------------------------------------------------------

_PAGES = [
    ("GET", "cursor=", 200, _fixture("workflows_page2")),
    ("GET", "/api/v1/workflows", 200, _fixture("workflows_page1")),
]


def test_list_workflows_projects_webhooks_across_pages():
    http = _Http(_PAGES)
    out = _run(http, "list_workflows")
    first, second = (_query(c) for c in http.calls)
    assert first == {"limit": "100", "excludePinnedData": "true", "active": "true"}
    assert second["cursor"] == "MTIzZTQ1NjctZTg5Yi0xMmQzLWE0NTYtNDI2NjE0MTc0MDA"
    assert http.calls[0]["headers"]["X-N8N-API-KEY"] == KEY
    assert out == {
        "items": [
            {
                "id": "2tUt1wbLX592XDdX",
                "name": "Receive from Marvin",
                "active": True,
                "tags": ["marvin", "prod"],
                "webhooks": [
                    {
                        "path": "marvin/new-entry",
                        "method": "POST",
                        "auth": "jwt",
                        "respond_mode": "responseNode",
                        "production_url": f"{BASE}/webhook/marvin/new-entry",
                    }
                ],
            },
            {
                "id": "9fQk3Lm2Np8RsT0v",
                "name": "Ops hooks",
                "active": True,
                "tags": [],
                # one per method; the disabled node is left out
                "webhooks": [
                    {"path": "ops/ping", "method": "GET", "auth": "header", "respond_mode": "lastNode", "production_url": f"{BASE}/webhook/ops/ping"},
                    {
                        "path": "ops/ping",
                        "method": "POST",
                        "auth": "header",
                        "respond_mode": "lastNode",
                        "production_url": f"{BASE}/webhook/ops/ping",
                    },
                ],
            },
            {
                "id": "Zz00Yy11Xx22Ww33",
                "name": "Plain intake",
                "active": True,
                "tags": ["marvin"],
                # n8n's defaults: GET, no auth, respond immediately
                "webhooks": [
                    {"path": "intake", "method": "GET", "auth": "none", "respond_mode": "onReceived", "production_url": f"{BASE}/webhook/intake"}
                ],
            },
        ],
        "truncated": False,
    }
    dumped = json.dumps(out)
    assert "PINNED-DATA-SENTINEL" not in dumped and "NODE-SECRET-SENTINEL" not in dumped


def test_list_workflows_filters():
    http = _Http([("GET", "/api/v1/workflows", 200, {"data": [], "nextCursor": None})])
    _run(http, "list_workflows", active=False, tag="marvin", name="Receive")
    assert _query(http.calls[0]) == {"limit": "100", "excludePinnedData": "true", "active": "false", "tags": "marvin", "name": "Receive"}


def test_pagination_stops_at_the_cap():
    http = _Http([("GET", "/api/v1/workflows", 200, {"data": [], "nextCursor": "again"})])
    out = _run(http, "list_workflows")
    assert len(http.calls) == MAX_PAGES == 10 and out["truncated"] is True


def test_list_webhooks_flattens_active_workflows_for_the_picker():
    http = _Http(_PAGES)
    out = _run(http, "list_webhooks", tag="ignored-by-stub")
    assert _query(http.calls[0])["active"] == "true" and _query(http.calls[0])["tags"] == "ignored-by-stub"
    assert out["truncated"] is False
    assert out["items"][0] == {
        "path": "marvin/new-entry",
        "method": "POST",
        "label": "Receive from Marvin — POST /marvin/new-entry",
        "workflow_id": "2tUt1wbLX592XDdX",
        "workflow_name": "Receive from Marvin",
        "active": True,
        "auth": "jwt",
    }
    assert [i["label"] for i in out["items"][1:]] == ["Ops hooks — GET /ops/ping", "Ops hooks — POST /ops/ping", "Plain intake — GET /intake"]


def test_the_path_picker_hint_points_at_list_webhooks_fields():
    trigger = N8nProvider().get_action("trigger_workflow")
    hint = trigger.input_schema["properties"]["path"]["x-marvin-options"]
    assert hint == {"action": "list_webhooks", "value": "path", "label": "label"}
    source = N8nProvider().get_action(hint["action"])
    item = source.output_schema["properties"]["items"]["items"]["properties"]
    assert {hint["value"], hint["label"]} <= set(item)


@pytest.mark.parametrize("key", ["list_workflows", "list_webhooks", "get_execution"])
def test_reads_need_the_api_key(key):
    http = _Http([])
    err = _error(http, key, api_key=None, execution_id="1")
    assert err.code == CODE_AUTH and "webhook-only" in str(err) and not http.calls


def test_a_rejected_key_on_a_read_is_auth():
    assert _error(_Http([("GET", "/api/v1/workflows", 401, {"message": "unauthorized"})]), "list_workflows").code == CODE_AUTH


# --- get_execution -----------------------------------------------------------------------------


def test_get_execution_without_error_reads_no_data():
    http = _Http([("GET", "/api/v1/executions/1001", 200, _fixture("execution_success"))])
    out = _run(http, "get_execution", {"editor_url": "https://edit.example.com"}, execution_id="1001", include_error=False)
    assert _query(http.calls[0]) == {"includeData": "false"}
    assert out == {
        "id": "1001",
        "status": "success",
        "workflow_id": "2tUt1wbLX592XDdX",
        "started_at": "2026-10-04T10:00:00.120Z",
        "stopped_at": "2026-10-04T10:00:01.480Z",
        "retry_of": "",
        "url": "https://edit.example.com/workflow/2tUt1wbLX592XDdX/executions/1001",
    }


def test_get_execution_with_error_keeps_only_the_last_node_and_a_trimmed_message():
    http = _Http([("GET", "/api/v1/executions/1002", 200, _fixture("execution_error"))])
    out = _run(http, "get_execution", execution_id="1002")
    assert _query(http.calls[0]) == {"includeData": "true"}
    assert out["status"] == "error" and out["retry_of"] == "998"
    assert out["last_node_executed"] == "HTTP Request"
    assert out["error_message"].startswith("The service was not able to process your request") and len(out["error_message"]) == 300
    assert set(out) == {"id", "status", "workflow_id", "started_at", "stopped_at", "retry_of", "url", "last_node_executed", "error_message"}
    dumped = json.dumps(out)
    for sentinel in ("RUN-DATA-SENTINEL", "STACK-SENTINEL", "WORKFLOW-DATA-SENTINEL", "reader@example.com"):
        assert sentinel not in dumped


def test_get_execution_falls_back_without_data_when_it_is_too_large():
    http = _Http(
        [
            ("GET", "includeData=true", 0, ValueError("Response exceeds the size cap.")),
            ("GET", "includeData=false", 200, _fixture("execution_success")),
        ]
    )
    out = _run(http, "get_execution", execution_id="1001")
    assert out["status"] == "success" and "too large" in out["error_message"]


@pytest.mark.parametrize("execution_id", ["", "../1", "1?x=1", "a b"])
def test_get_execution_refuses_a_bad_id(execution_id):
    assert _error(_Http([]), "get_execution", execution_id=execution_id).code == CODE_INVALID


def test_get_execution_not_found():
    err = _error(_Http([("GET", "/api/v1/executions/7", 404, {"message": "Not Found"})]), "get_execution", execution_id="7")
    assert err.code == CODE_NOT_FOUND


# --- error policy ------------------------------------------------------------------------------

REVIEW = Handle(review=True)
POLICY_TABLE = {
    CODE_AUTH: Handle(notify=True, retry=Retry(backoff=(), on_recovery=True, max_attempts=3), then=REVIEW),
    CODE_NOT_FOUND: Handle(notify=True, retry=Retry(backoff=(300, 1800)), then=REVIEW),
    CODE_RATE_LIMITED: Handle(retry=Retry(backoff=(30, 120, 600)), then=Handle(notify=True, review=True)),
    CODE_UNAVAILABLE: Handle(retry=Retry(backoff=(60, 300, 1800, 7200)), then=Handle(notify=True, review=True)),
    CODE_TIMEOUT: REVIEW,
    CODE_WORKFLOW_ERROR: REVIEW,
    "rejected": REVIEW,
    CODE_BLOCKED: Handle(notify=True),
    CODE_INVALID: Handle(notify=True),
    CODE_TOO_LARGE: Handle(succeed=True),
    "*": Handle(review=True, notify=True),
}


def test_register_provider_accepts_the_error_policy():
    assert register_provider(N8nProvider) is N8nProvider


def test_policy_is_the_plan_table_and_shows_in_info():
    assert N8nProvider.error_policy == POLICY_TABLE
    policy = N8nProvider().info()["error_policy"]
    summaries = {code: h["summary"] for code, h in policy["provider"].items()}
    assert summaries == {
        "auth": "notify admins, wait for the connection to recover, then retry 3×, then send to review",
        "not_found": "notify admins, retry 2× (5m, 30m), then send to review",
        "rate_limited": "retry 3× (30s, 2m, 10m), then notify admins, send to review",
        "unavailable": "retry 4× (1m, 5m, 30m, 2h), then notify admins, send to review",
        "timeout": "send to review",
        "workflow_error": "send to review",
        "rejected": "send to review",
        "blocked": "notify admins",
        "invalid": "notify admins",
        "response_too_large": "treat as success",
        "*": "notify admins, send to review",
    }
    assert policy["actions"]["trigger_workflow"] == {}


@pytest.mark.parametrize("code", [*CODES, "something_new"])
def test_trigger_workflow_resolves_the_provider_policy(code):
    expected = POLICY_TABLE.get(code, POLICY_TABLE["*"])
    assert resolve_policy(N8nProvider, "trigger_workflow", code) == expected


def test_workflow_error_defaults_to_review_only_and_timeout_never_retries():
    assert resolve_policy(N8nProvider, "trigger_workflow", CODE_WORKFLOW_ERROR) == Handle(review=True)
    assert resolve_policy(N8nProvider, "trigger_workflow", CODE_TIMEOUT).retry is None


@pytest.mark.parametrize("action", ["list_workflows", "list_webhooks", "get_execution"])
@pytest.mark.parametrize("code", [*CODES, "something_new"])
def test_read_actions_fail_plainly_on_every_code(action, code):
    assert resolve_policy(N8nProvider, action, code) == Handle()


def test_every_raised_code_is_in_the_policy_or_falls_back_deliberately():
    assert set(POLICY_TABLE) - {"*"} == set(CODES) - {CODE_UNKNOWN}
