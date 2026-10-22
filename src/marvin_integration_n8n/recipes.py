"""Workflow Library recipes n8n ships (SDK 0.9.0): offered in Marvin once an n8n connection exists.

``hand-off-published-entries`` is the send half of the round trip ``content.py`` declares the return half of. It is one
``trigger_workflow`` step and nothing else: that action declares ``hands_off``, so the core records the hand-off itself (in
flight, or failed when the call fails) under Settings > Automation > Results, until n8n reports back and
``n8n-record-result`` settles it.
"""

from marvin_integration_sdk import WorkflowRecipe

from .content import WEBHOOK_SECRET_REF

HAND_OFF = WorkflowRecipe(
    id="hand-off-published-entries",
    title="Hand published entries to n8n",
    outcome=(
        "Each published entry of one type triggers an n8n workflow with its title, link, type and summary. Marvin keeps a result for it, "
        "in flight until n8n reports back, under Settings > Automation > Results."
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
        ],
    },
    variables={
        "entry_type": {"type": "entry_type_slug", "description": "Which entries to hand to n8n when they are published.", "example": "post"},
        "n8n_integration": {"type": "integration_slug", "description": "The n8n connection to trigger through.", "example": "n8n"},
        "webhook_path": {
            "type": "text",
            "description": "The path of the Webhook node in your n8n workflow. Set the node to POST with JWT Auth using the `N8N_WEBHOOK_SECRET` passphrase, and make sure the workflow is active.",
            "example": "marvin/new-entry",
        },
    },
    side_effects=(
        {
            "kind": "external_post",
            "description": "Triggers an n8n workflow. Marvin keeps a result of the hand-off (Settings > Automation > Results); the entry itself is not changed.",
        },
    ),
)

RECIPES = (HAND_OFF,)
