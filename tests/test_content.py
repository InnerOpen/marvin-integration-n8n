"""Declared content: the callback webhook, the record-result workflow and two smart collections."""

import re

from marvin_integration_n8n import N8nProvider
from marvin_integration_n8n.content import (
    CALLBACK_WEBHOOK,
    CONTENT,
    FAILED,
    IN_FLIGHT,
    RECORD_RESULT,
    WEBHOOK_SECRET_REF,
)

# The ops a smart rule's `where` accepts (Marvin's services/entries/query.py WHERE_OPS).
WHERE_OPS = ("eq", "neq", "in", "contains", "exists", "missing", "gt", "gte", "lt", "lte")
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
    assert [(b.kind, b.slug) for b in CONTENT] == [
        ("incoming_webhook", "n8n"),
        ("workflow", "n8n-record-result"),
        ("collection", "n8n-failed"),
        ("collection", "n8n-in-flight"),
    ]
    assert N8nProvider.content == CONTENT


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


def test_record_result_writes_flat_metadata_on_the_named_entry():
    d = RECORD_RESULT.payload["definition"]
    assert d["trigger"] == {"type": "incoming_webhook", "webhook": CALLBACK_WEBHOOK.slug}
    assert d["conditions"] == [{"field": "event.payload.marvin.entry_id", "op": "exists"}]
    (step,) = d["actions"]
    assert (step["kind"], step["op"], step["entity_id"]) == ("entry", "set_metadata", "$event.payload.marvin.entry_id")
    assert step["metadata"] == {
        "n8n_status": "${event.payload.status}",
        "n8n_execution_id": "${event.payload.execution_id}",
        "n8n_workflow": "${event.payload.workflow}",
        "n8n_finished_at": "${event.payload.finished_at}",
        "n8n_message": "${event.payload.message}",
    }
    assert all("." not in key for key in step["metadata"])  # smart rules read only top-level metadata


def test_collections_are_private_smart_collections_over_top_level_metadata():
    written = set(RECORD_RESULT.payload["definition"]["actions"][0]["metadata"])
    for blueprint in (FAILED, IN_FLIGHT):
        payload = blueprint.payload
        assert (payload["is_smart"], payload["is_public"]) == (True, False)
        for condition in payload["smart_rules"]["where"]:
            prefix, _, key = condition["field"].partition(".")
            assert prefix == "metadata" and key in written and "." not in key
            assert condition["op"] in WHERE_OPS


def test_the_collection_rules_match_the_plan():
    assert FAILED.payload["smart_rules"] == {"where": [{"field": "metadata.n8n_status", "op": "eq", "value": "error"}], "match": "all"}
    assert IN_FLIGHT.payload["smart_rules"] == {
        "where": [{"field": "metadata.n8n_status", "op": "in", "value": ["sent", "waiting"]}],
        "match": "all",
    }


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
        assert _substitute(blueprint.payload, params) == blueprint.payload
