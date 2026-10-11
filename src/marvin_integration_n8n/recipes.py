"""Workflow Library recipes n8n ships (SDK 0.9.0): offered in Marvin once an n8n connection exists.

``hand-off-published-entries`` is the send half of the round trip ``content.py`` declares the return half of: it
marks the entry ``n8n_status: sent`` so it shows in **n8n: in flight** until n8n reports back.
"""

from marvin_integration_sdk import WorkflowRecipe

from .content import EXECUTION_KEY, STATUS_KEY, WEBHOOK_SECRET_REF, WORKFLOW_KEY

HAND_OFF = WorkflowRecipe(
    id="hand-off-published-entries",
    title="Hand published entries to n8n",
    outcome=(
        "Each published entry of one type triggers an n8n workflow with its title, link, type and summary, and is marked as sent "
        "until n8n reports back."
    ),
    category="Automation hand-off",
    tags=("handoff", "publishing"),
    definition={
        "trigger": {"type": "event", "event": "entry_published"},
        "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"}],
        "actions": [
            {
                "kind": "integration",
                "id": "handoff",
                "integration": "{{n8n_integration}}",
                "action": "trigger_workflow",
                "args": {
                    "path": "{{webhook_path}}",
                    "secret": WEBHOOK_SECRET_REF,
                    "entry_id": "${entry.id}",
                    "data": {"title": "${entry.title}", "url": "${entry.url}", "entry_type": "${entry.entry_type}", "summary": "${entry.summary}"},
                },
            },
            {
                "kind": "entry",
                "op": "set_metadata",
                "metadata": {STATUS_KEY: "sent", WORKFLOW_KEY: "{{webhook_path}}", EXECUTION_KEY: "${steps.handoff.output.execution_id}"},
            },
        ],
    },
    variables={
        "entry_type": {"type": "entry_type_slug", "description": "Which entries to hand to n8n when they are published.", "example": "post"},
        "n8n_integration": {"type": "integration_slug", "description": "The n8n connection to trigger through.", "example": "n8n"},
        "webhook_path": {
            "type": "text",
            "description": "The n8n Webhook node's path (POST, JWT Auth with the N8N_WEBHOOK_SECRET passphrase; the workflow must be active).",
            "example": "marvin/new-entry",
        },
    },
    side_effects=(
        {"kind": "external_post", "description": "Triggers an n8n workflow."},
        {"kind": "content_mutation", "description": "Writes n8n_status, n8n_workflow and n8n_execution_id metadata on the entry."},
    ),
)

RECIPES = (HAND_OFF,)
