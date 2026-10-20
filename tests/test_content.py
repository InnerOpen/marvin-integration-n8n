"""Declared content: the callback webhook and the record-result workflow (an automation_result step)."""

import re

from marvin_integration_n8n import N8nProvider
from marvin_integration_n8n.content import (
    CALLBACK_WEBHOOK,
    CONTENT,
    RECORD_RESULT,
    WEBHOOK_SECRET_REF,
)

# The fields of Marvin's `automation_result` step (schemas/group/automation_definition.py: AutomationResultAction, core 0.13 SDK era).
RESULT_STEP_FIELDS = {"kind", "id", "status", "integration", "remote_id", "entry_id", "target", "message", "finished_at"}
# What the "report back to Marvin" HTTP Request node sends (README and guide.md).
REPORT = {
    "marvin": {"entry_id": "0b6c1f0e-1111-4222-8333-444455556666"},
    "status": "success",
    "execution_id": "1001",
    "workflow": "Publish to the newsletter",
    "finished_at": "2026-10-20T10:00:00.000+00:00",
    "message": "",
}
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _substitute(value, params):
    """Marvin's blueprint `{{key}}` substitution: a lone placeholder becomes the value itself."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole and whole.group(1) in params:
            return params[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    return value


def test_declares_the_return_path_only():
    assert [(b.kind, b.slug) for b in CONTENT] == [("incoming_webhook", "n8n"), ("workflow", "n8n-record-result")]
    assert N8nProvider.content == CONTENT


def test_ships_no_collections_and_nothing_that_writes_entry_metadata():
    # 0.4.0: results live under Settings > Automation > Results; the core removed the untouched n8n collections.
    assert [b.slug for b in CONTENT if b.kind == "collection"] == []
    assert "set_metadata" not in repr([b.payload for b in CONTENT])
    assert "n8n_status" not in repr([b.payload for b in CONTENT])


def test_nothing_is_required_and_nothing_arrives_switched_on():
    assert [b.slug for b in CONTENT if b.required] == []
    assert all("enabled" not in b.payload for b in CONTENT)  # the core creates webhooks and workflows off


def test_no_send_side_blueprints():
    # Sending is a trigger_workflow step people add to their own workflows, picking from n8n's list.
    steps = [s for b in CONTENT if b.kind == "workflow" for s in b.payload["definition"]["actions"]]
    assert [s for s in steps if s.get("kind") == "integration"] == []
    assert all(b.payload["definition"]["trigger"]["type"] == "incoming_webhook" for b in CONTENT if b.kind == "workflow")


def test_the_callback_webhook_takes_a_static_token():
    assert CALLBACK_WEBHOOK.payload == {
        "name": "n8n results",
        "description": CALLBACK_WEBHOOK.payload["description"],
        "signature_scheme": "static_token",
        "signature_header": "X-Marvin-Token",
        "signing_secret_ref": "N8N_CALLBACK_TOKEN",
    }
    assert "token" not in CALLBACK_WEBHOOK.payload  # minted in the workspace, never declared


def test_record_result_settles_the_hand_off_with_an_automation_result_step():
    d = RECORD_RESULT.payload["definition"]
    assert d["trigger"] == {"type": "incoming_webhook", "webhook": CALLBACK_WEBHOOK.slug}
    assert d["conditions"] == [{"field": "event.payload.status", "op": "exists"}]  # a report with no status has nothing to record
    (step,) = d["actions"]
    assert step["kind"] == "automation_result"
    assert step == {
        "kind": "automation_result",
        "id": "settle",
        "integration": "{{n8n_integration}}",
        "remote_id": "${event.payload.execution_id}",
        "entry_id": "${event.payload.marvin.entry_id}",
        "target": "${event.payload.workflow}",
        "status": "${event.payload.status}",
        "message": "${event.payload.message}",
        "finished_at": "${event.payload.finished_at}",
    }


def test_the_step_uses_only_the_arguments_core_defines_and_passes_them_all():
    (step,) = RECORD_RESULT.payload["definition"]["actions"]
    assert set(step) <= RESULT_STEP_FIELDS
    # integration, remote_id, entry_id, target, status, message and finished_at: everything the report can say.
    assert RESULT_STEP_FIELDS - set(step) == set()


def test_every_template_reads_a_field_the_report_back_sends():
    (step,) = RECORD_RESULT.payload["definition"]["actions"]
    paths = [v[2:-1] for v in step.values() if v.startswith("${")]
    assert len(paths) == 6
    for path in paths:
        node = {"event": {"payload": REPORT}}
        for hop in path.split("."):
            assert hop in node, path
            node = node[hop]


def test_the_status_is_passed_in_n8ns_words_for_the_connection_to_read():
    # No mapping in the workflow: `success`, `error` and `waiting` reach the step as n8n sends them, and the n8n connection's
    # provider turns them into Marvin's three (see the hands_off declaration), so the blueprint holds no n8n vocabulary.
    (step,) = RECORD_RESULT.payload["definition"]["actions"]
    assert step["status"] == "${event.payload.status}"
    assert step["integration"] == "{{n8n_integration}}"


def test_the_workflow_names_the_n8n_connection_as_a_parameter_defaulting_to_n8n():
    (parameter,) = RECORD_RESULT.parameters
    assert (parameter["key"], parameter["kind"], parameter["default"]) == ("n8n_integration", "integration", "n8n")
    # A workspace applying with its (single) connection gets that slug in the step; the core resolves the default the same way.
    definition = _substitute(RECORD_RESULT.payload["definition"], {"n8n_integration": "my-n8n"})
    assert definition["actions"][0]["integration"] == "my-n8n"


def test_the_callback_webhook_documents_the_payload_the_workflow_reads():
    # The webhook's description names the fields the record-result workflow reads.
    for field in ("entry_id", "status", "execution_id", "workflow", "finished_at", "message"):
        assert field in CALLBACK_WEBHOOK.payload["description"]


def test_every_placeholder_is_a_declared_parameter():
    for blueprint in CONTENT:
        used = set(_PLACEHOLDER.findall(repr((blueprint.slug, blueprint.name, blueprint.payload))))
        assert used <= {p["key"] for p in blueprint.parameters}, blueprint.slug


def test_the_webhook_secret_reference_survives_substitution():
    # A step that signs with {{N8N_WEBHOOK_SECRET}} must reach the workflow as the reference — never as a
    # blueprint parameter's value — so the secret is resolved at run time and the dry run shows the reference.
    params = {p["key"]: p.get("default") for b in CONTENT for p in b.parameters}
    step = {"kind": "integration", "integration": "n8n", "action": "trigger_workflow", "args": {"path": "marvin/x", "secret": WEBHOOK_SECRET_REF}}
    assert WEBHOOK_SECRET_REF == "{{N8N_WEBHOOK_SECRET}}"
    assert _substitute(step, {**params, "integration": "n8n"}) == step
    assert "N8N_WEBHOOK_SECRET" not in params
    for blueprint in CONTENT:
        # Only the declared parameter is replaced (the n8n connection's slug); every other reference stays as written.
        expected = repr(blueprint.payload).replace("{{n8n_integration}}", "n8n")
        assert repr(_substitute(blueprint.payload, params)) == expected
