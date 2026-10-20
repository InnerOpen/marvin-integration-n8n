# ruff: noqa: F811  (the `workspace` fixture is imported and extended)
"""The plugin inside Marvin's core: its content and recipe validate, an old workspace updates, and a hand-off settles end to end.

These tests need Marvin itself (the phase 1 "Automation results" core) and so run from a Marvin checkout, not from this repo:
they are skipped here (``marvin`` is not importable) and use Marvin's own fixtures (``tests/conftest.py``, ``workspace``). Run them
from the Marvin checkout with the plugin installed editable into its environment:

    uv pip install -e <this repo> --python .venv/bin/python --no-deps
    uv run --no-sync pytest -p tests.conftest <this repo>/tests/core/test_in_core.py

The real engine runs real workflows against the real plugin; only n8n's HTTP reply is stubbed.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("marvin", reason="needs Marvin's core (run from a Marvin checkout; see this file's docstring)")
pytest.importorskip("marvin_integration_sdk", reason="integrations SDK not installed (optional feature)")

from marvin.db.models.groups.automation_results import AutomationResultModel as Result
from marvin.db.models.groups.automations import WorkspaceAutomationModel
from marvin.db.models.groups.incoming_webhooks import WorkspaceIncomingWebhookModel
from marvin.db.models.groups.integrations import IntegrationModel
from marvin.db.models.platform import Entries, EntryTypes
from marvin.services.automation import recipes
from marvin.services.automation.engine import run_automations_for_event
from marvin.services.automation.recorder import ExecutionRecorder
from marvin.services.automation.validation import entry_link_issues, item_target_issues, query_issues, structural_issues, unknown_key_issues
from marvin.services.blueprints import apply_many, get_blueprint, outdated, update_blueprint
from marvin_integration_sdk import INTEGRATION_REGISTRY, Response
from tests.test_content_role_gates import workspace  # noqa: F401  (the `workspace` fixture)

from marvin_integration_n8n import N8nProvider

BASE = "https://n8n.example.com"
PATH = "marvin/new-entry"
SECRET = "SENTINEL-webhook-secret-7f3a-0123456789"
EXECUTION = "1001"

# What n8n plugin 0.3.0 shipped as ``n8n-record-result``: a workspace that applied it has this in its database.
V030_DEFINITION = {
    "trigger": {"type": "incoming_webhook", "webhook": "n8n"},
    "conditions": [{"field": "event.payload.marvin.entry_id", "op": "exists"}],
    "actions": [
        {
            "kind": "entry",
            "op": "set_metadata",
            "entity_id": "$event.payload.marvin.entry_id",
            "metadata": {
                "n8n_status": "${event.payload.status}",
                "n8n_execution_id": "${event.payload.execution_id}",
                "n8n_workflow": "${event.payload.workflow}",
                "n8n_finished_at": "${event.payload.finished_at}",
                "n8n_message": "${event.payload.message}",
            },
        }
    ],
}
V030_NAME = "n8n: record the result on the entry"


class _N8n:
    """n8n's side: answers the Webhook node's POST like a Respond to Webhook node would (or as told to)."""

    def __init__(self):
        self.status, self.body, self.calls = 200, {"executionId": EXECUTION, "workflowId": "wf-1"}, []

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return self._reply()

    def _reply(self):  # (`post` has a `json` parameter of the helper's signature, which hides the module)
        return Response(status_code=self.status, content=json.dumps(self.body).encode())


@pytest.fixture(params=["n8n", "flows-prod"])
def n8n(request, db_session, workspace, monkeypatch):
    """A workspace with an n8n connection (slugged `n8n`, or not: the report back must not care), the plugin's content applied and switched on, a post type with entries and the hand-off recipe's workflow."""
    stub = _N8n()
    monkeypatch.setitem(INTEGRATION_REGISTRY, "n8n", N8nProvider())
    monkeypatch.setattr("marvin.services.integrations.build_http", lambda: stub)
    monkeypatch.setattr("marvin.services.secrets.resolver.resolve_secret", lambda ref, gid=None: SECRET)
    gid = workspace.gid
    connection = IntegrationModel(
        session=db_session, group_id=gid, provider="n8n", name="n8n", slug=request.param, enabled=True, config={"base_url": BASE}, secret_ref=None
    )
    kind = EntryTypes(session=db_session, group_id=gid, name="Post", slug="post", schema_json={"fields": []})
    kind.id = uuid.uuid4()
    db_session.add_all([connection, kind])
    db_session.flush()
    entries = []
    for n in range(2):
        entry = Entries(
            session=db_session,
            group_id=gid,
            entry_type_id=kind.id,
            title=f"Post {n}",
            slug=f"post-{n}-{gid.hex[:6]}",
            data_json={},
            status="published",
        )
        db_session.add(entry)
        db_session.flush()
        entries.append(entry)
    db_session.commit()
    return SimpleNamespace(ws=workspace, session=db_session, gid=gid, connection=connection, http=stub, a=entries[0], b=entries[1])


def _blueprints():
    return [get_blueprint(b.slug, source="n8n") for b in N8nProvider.content]


def _switch_on(n8n):
    """What an admin does after applying: mint the webhook's token and switch the webhook and the report-back workflow on."""
    hook = n8n.session.query(WorkspaceIncomingWebhookModel).filter_by(group_id=n8n.gid, slug="n8n").one()
    hook.token, hook.enabled = "tok-" + uuid.uuid4().hex, True
    workflow = n8n.session.query(WorkspaceAutomationModel).filter_by(group_id=n8n.gid, slug="n8n-record-result").one()
    workflow.enabled = True
    n8n.session.commit()
    return workflow


def _hand_off_workflow(n8n):
    """The workflow a person makes from the Library recipe."""
    document = recipes.instantiate(
        "n8n.hand-off-published-entries", {"entry_type": "post", "n8n_integration": n8n.connection.slug, "webhook_path": PATH}
    )
    row = WorkspaceAutomationModel(
        session=n8n.session, group_id=n8n.gid, name=document["name"], slug="hand-off", enabled=True, definition=document["definition"]
    )
    n8n.session.add(row)
    n8n.session.commit()
    return row


def _fire(n8n, event):
    run_automations_for_event(n8n.session, n8n.gid, event, recorder=ExecutionRecorder(n8n.session, n8n.gid))
    n8n.session.expire_all()


def _publish(n8n, entry):
    _fire(n8n, {"event_type": "entry_published", "entry_id": str(entry.id), "user_id": None})


def _report(n8n, **payload):
    """n8n's HTTP Request node reporting back to the ``n8n`` incoming webhook."""
    _fire(n8n, {"event_type": "incoming_webhook", "webhook_slug": "n8n", "payload": payload})


def _rows(n8n):
    n8n.session.expire_all()
    return n8n.session.query(Result).filter_by(group_id=n8n.gid).order_by(Result.sent_at, Result.created_at).all()


# --- what core validates a plugin's content and recipes with ---


def test_the_plugins_blueprints_are_accepted_by_core_and_apply_cleanly(n8n):
    blueprints = _blueprints()
    assert [(b.kind, b.slug, b.source) for b in blueprints] == [("incoming_webhook", "n8n", "n8n"), ("workflow", "n8n-record-result", "n8n")]
    assert get_blueprint("n8n-failed", source="n8n") is None and get_blueprint("n8n-in-flight", source="n8n") is None  # no longer shipped
    applied = apply_many(n8n.session, n8n.gid, blueprints, integration_id=n8n.connection.id)
    assert [(r.slug, r.created, r.detail) for r in applied] == [("n8n", True, ""), ("n8n-record-result", True, "")]
    row = n8n.session.query(WorkspaceAutomationModel).filter_by(group_id=n8n.gid, slug="n8n-record-result").one()
    assert not row.enabled  # arrives switched off
    (step,) = row.definition["actions"]
    assert step["kind"] == "automation_result" and "integration" not in step  # core reads n8n's words through the webhook's connection
    webhook = n8n.session.query(WorkspaceIncomingWebhookModel).filter_by(group_id=n8n.gid, slug="n8n").one()
    assert webhook.source_integration_id == n8n.connection.id  # ... which is the connection the card applied it from
    issues = structural_issues(row.definition) or (
        query_issues(row.definition) + item_target_issues(row.definition) + entry_link_issues(row.definition) + unknown_key_issues(row.definition)
    )
    assert issues == []


def test_the_recipe_is_in_the_library_and_its_definition_passes_cores_checks(n8n):
    item = recipes.entry("n8n.hand-off-published-entries")  # a recipe core rejects is skipped from the Library, with a warning
    assert item["source"]["provider"] == "n8n"
    assert [e["kind"] for e in item["side_effects"]] == ["external_post"]
    document = recipes.instantiate(
        "n8n.hand-off-published-entries", {"entry_type": "post", "n8n_integration": n8n.connection.slug, "webhook_path": PATH}
    )
    assert [a["kind"] for a in document["definition"]["actions"]] == ["integration"]  # no set_metadata step
    assert structural_issues(document["definition"]) == []


# --- an old workspace ---


def test_a_workspace_with_the_old_workflow_is_offered_the_update_and_gets_the_new_steps(n8n):
    apply_many(n8n.session, n8n.gid, _blueprints()[:1], integration_id=n8n.connection.id)  # the webhook
    old = WorkspaceAutomationModel(
        session=n8n.session,
        group_id=n8n.gid,
        name=V030_NAME,
        slug="n8n-record-result",
        enabled=True,
        definition=V030_DEFINITION,
        source_blueprint="n8n-record-result",
    )
    n8n.session.add(old)
    n8n.session.commit()
    blueprint = get_blueprint("n8n-record-result", source="n8n")

    assert outdated(n8n.session, n8n.gid, blueprint)  # the card shows "Update available"
    (again,) = apply_many(n8n.session, n8n.gid, [blueprint], integration_id=n8n.connection.id)
    assert not again.created and "left as it is" in again.detail  # a plain re-apply does not touch it
    n8n.session.refresh(old)
    assert old.definition == V030_DEFINITION

    result = update_blueprint(n8n.session, n8n.gid, blueprint)  # the card's Update button
    assert result.updated
    n8n.session.commit()
    n8n.session.refresh(old)
    assert old.enabled  # whether it is switched on is kept
    assert old.name == V030_NAME  # the name is not part of what Update replaces
    assert old.definition == blueprint.payload["definition"]
    assert not outdated(n8n.session, n8n.gid, blueprint)
    assert "already up to date" in update_blueprint(n8n.session, n8n.gid, blueprint).detail


# --- the round trip, through the real engine ---


@pytest.fixture
def applied(n8n):
    apply_many(n8n.session, n8n.gid, _blueprints(), integration_id=n8n.connection.id)
    n8n.session.commit()
    _switch_on(n8n)
    _hand_off_workflow(n8n)
    return n8n


def test_publishing_hands_the_entry_to_n8n_and_core_records_it_in_flight(applied):
    _publish(applied, applied.a)
    (call,) = applied.http.calls
    assert call["url"] == f"{BASE}/webhook/{PATH}"
    assert json.loads(call["data"])["marvin"]["entry_id"] == str(applied.a.id)
    (row,) = _rows(applied)
    assert (row.status, row.target, row.remote_id, row.entry_id) == ("in_flight", PATH, EXECUTION, applied.a.id)
    assert row.remote_url == f"{BASE}/workflow/wf-1/executions/{EXECUTION}"
    assert row.integration_id == applied.connection.id
    assert (row.attempt, row.finished_at) == (1, None)
    assert applied.session.get(Entries, applied.a.id).data_json == {} and not (applied.a.metadata_json or {}).get("n8n_status")


@pytest.mark.parametrize(
    ("word", "status", "message"),
    [("success", "succeeded", ""), ("error", "failed", "Node 'Send' failed: 502"), ("waiting", "in_flight", "Waiting for an approval")],
)
def test_n8n_reporting_back_settles_the_result_in_marvins_words(applied, word, status, message):
    _publish(applied, applied.a)
    _report(
        applied,
        marvin={"entry_id": str(applied.a.id)},
        status=word,
        execution_id=EXECUTION,
        workflow="Publish to the newsletter",
        finished_at="2026-10-20T10:00:00.000+00:00",
        message=message,
    )
    (row,) = _rows(applied)  # settled, not a second row
    assert row.status == status
    assert (row.message or "") == message
    assert (row.finished_at is not None) == (status != "in_flight")
    assert row.target == PATH  # what was asked stays; the report's workflow name only fills a row the report creates
    assert row.remote_id == EXECUTION


def test_a_report_without_the_execution_id_finds_the_newest_in_flight_hand_off_of_the_entry(applied):
    _publish(applied, applied.a)
    _publish(applied, applied.b)
    _report(applied, marvin={"entry_id": str(applied.a.id)}, status="error", message="boom")
    by_entry = {r.entry_id: r.status for r in _rows(applied)}
    assert by_entry == {applied.a.id: "failed", applied.b.id: "in_flight"}


def test_a_report_nothing_matches_is_kept_as_a_new_result(applied):
    _report(applied, status="error", execution_id="777", workflow="Nightly export", message="disk full")
    (row,) = _rows(applied)
    assert (row.status, row.remote_id, row.target, row.message, row.entry_id) == ("failed", "777", "Nightly export", "disk full", None)
    assert row.integration_id == applied.connection.id


def test_a_status_word_n8n_does_not_use_leaves_the_result_in_flight_with_the_word_in_its_message(applied):
    _publish(applied, applied.a)
    _report(applied, marvin={"entry_id": str(applied.a.id)}, status="crashed", execution_id=EXECUTION)
    (row,) = _rows(applied)
    assert row.status == "in_flight" and "crashed" in row.message


def test_a_trigger_n8n_refuses_is_recorded_as_failed_without_the_secret(applied):
    applied.http.status, applied.http.body = 404, {"message": f"The requested webhook is not registered (token {SECRET})"}
    _publish(applied, applied.a)
    (row,) = _rows(applied)
    assert (row.status, row.entry_id, row.target) == ("failed", applied.a.id, PATH)
    assert row.message and SECRET not in row.message and SECRET not in (row.remote_url or "")


def test_the_old_workflow_still_settles_nothing_it_writes_metadata_so_it_must_be_updated(applied):
    """What an un-updated workspace sees: the 0.3.0 workflow writes n8n_* metadata but does not settle the result (hence the update)."""
    workflow = applied.session.query(WorkspaceAutomationModel).filter_by(group_id=applied.gid, slug="n8n-record-result").one()
    workflow.definition = V030_DEFINITION
    applied.session.commit()
    _publish(applied, applied.a)
    _report(applied, marvin={"entry_id": str(applied.a.id)}, status="success", execution_id=EXECUTION)
    (row,) = _rows(applied)
    assert row.status == "in_flight"


def test_with_two_n8n_connections_the_report_is_read_through_the_one_that_installed_the_webhook(n8n):
    """The workflow names no connection, so several of them (under any slugs) change nothing: an Update needs no input and the words map."""
    other = IntegrationModel(
        session=n8n.session,
        group_id=n8n.gid,
        provider="n8n",
        name="n8n staging",
        slug="staging-n8n",
        enabled=True,
        config={"base_url": BASE},
        secret_ref=None,
    )
    n8n.session.add(other)
    n8n.session.commit()
    apply_many(n8n.session, n8n.gid, _blueprints(), integration_id=n8n.connection.id)  # applied from the first connection's card
    n8n.session.commit()
    workflow = _switch_on(n8n)
    _hand_off_workflow(n8n)
    blueprint = get_blueprint("n8n-record-result", source="n8n")
    assert not outdated(n8n.session, n8n.gid, blueprint)  # no parameter to resolve, so nothing hides an update

    workflow.definition = V030_DEFINITION  # a 0.3 workspace with two connections
    n8n.session.commit()
    assert outdated(n8n.session, n8n.gid, blueprint)  # the card offers Update
    assert update_blueprint(n8n.session, n8n.gid, blueprint).updated
    n8n.session.commit()

    _publish(n8n, n8n.a)
    _report(n8n, marvin={"entry_id": str(n8n.a.id)}, status="error", execution_id=EXECUTION, message="boom")
    (row,) = _rows(n8n)
    assert (row.status, row.message, row.integration_id) == ("failed", "boom", n8n.connection.id)

    # A report no hand-off matches has only the webhook's connection to read its words through (n8n's `error` is Marvin's failed).
    _report(n8n, status="error", execution_id="777", workflow="Nightly export", message="disk full")
    unmatched = [r for r in _rows(n8n) if r.remote_id == "777"]
    assert [(r.status, r.integration_id) for r in unmatched] == [("failed", n8n.connection.id)]
