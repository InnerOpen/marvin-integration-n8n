"""What a workspace can use to hear back from n8n — declared for the core to offer, all optional.

There are no send-side blueprints: sending is a ``trigger_workflow`` step a person adds to their own
workflow, picking the n8n workflow from n8n's list. What's declared is the return path:

  an n8n workflow finishes → its "report back to Marvin" HTTP Request node POSTs
  ``{marvin: {entry_id}, status, execution_id, workflow, finished_at, message}`` to the ``n8n`` incoming
  webhook (header ``X-Marvin-Token``) → ``n8n-record-result`` writes flat ``n8n_*`` metadata on that
  entry → the smart collections ``n8n-failed`` and ``n8n-in-flight`` follow that metadata.

Applying (from the integration's card) creates only what is missing. The webhook and the workflow
arrive switched off; nothing here is required. The metadata keys are flat because smart rules read
only top-level ``metadata.<key>``. Both collections are private (not "Visible to sites"), so a result
never rebuilds a site.

Parameters: none — the webhook and the workflow name only their own content.
"""

from marvin_integration_sdk import ContentBlueprint

CATEGORY = "n8n"
WEBHOOK_SLUG = "n8n"
CALLBACK_SECRET_REF = "N8N_CALLBACK_TOKEN"
CALLBACK_HEADER = "X-Marvin-Token"
# The step argument a trigger_workflow step signs with — a workspace secret reference, never a value.
WEBHOOK_SECRET_REF = "{{N8N_WEBHOOK_SECRET}}"

STATUS_KEY = "n8n_status"  # success | error | waiting, as n8n reports it
EXECUTION_KEY = "n8n_execution_id"
WORKFLOW_KEY = "n8n_workflow"
FINISHED_KEY = "n8n_finished_at"
MESSAGE_KEY = "n8n_message"

CALLBACK_WEBHOOK = ContentBlueprint(
    kind="incoming_webhook",
    slug=WEBHOOK_SLUG,
    name="n8n results",
    description=(
        f"Where n8n reports back. Mint its token, store a random value as the workspace secret {CALLBACK_SECRET_REF}, and send it from n8n "
        f"in the {CALLBACK_HEADER} header (the 'report back to Marvin' recipe)."
    ),
    category=CATEGORY,
    payload={
        "name": "n8n results",
        "description": f"n8n workflows report results here: {{marvin: {{entry_id}}, status, execution_id, workflow, finished_at, message}}. "
        f"Authenticated by a fixed token in {CALLBACK_HEADER}.",
        "signature_scheme": "static_token",
        "signature_header": CALLBACK_HEADER,
        "signing_secret_ref": CALLBACK_SECRET_REF,
    },
)

RECORD_RESULT = ContentBlueprint(
    kind="workflow",
    slug="n8n-record-result",
    name="n8n: record the result on the entry",
    description=(
        "When n8n reports back on the n8n webhook, write its status, execution id, workflow, finish time and message on the entry it names "
        "(marvin.entry_id), as flat n8n_* metadata the smart collections read."
    ),
    category=CATEGORY,
    payload={
        "definition": {
            "trigger": {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG},
            "conditions": [{"field": "event.payload.marvin.entry_id", "op": "exists"}],
            "actions": [
                {
                    "kind": "entry",
                    "op": "set_metadata",
                    "entity_id": "$event.payload.marvin.entry_id",
                    "metadata": {
                        STATUS_KEY: "${event.payload.status}",
                        EXECUTION_KEY: "${event.payload.execution_id}",
                        WORKFLOW_KEY: "${event.payload.workflow}",
                        FINISHED_KEY: "${event.payload.finished_at}",
                        MESSAGE_KEY: "${event.payload.message}",
                    },
                },
            ],
        }
    },
)


def _by_status(slug: str, name: str, description: str, condition: dict, icon: str) -> ContentBlueprint:
    """A private smart collection of the entries whose top-level ``n8n_status`` metadata matches."""
    return ContentBlueprint(
        kind="collection",
        slug=slug,
        name=name,
        description=description,
        category=CATEGORY,
        payload={
            "description": description,
            "icon": icon,
            "is_smart": True,
            "is_public": False,
            "smart_rules": {"where": [{"field": f"metadata.{STATUS_KEY}", **condition}], "match": "all"},
        },
    )


FAILED = _by_status(
    "n8n-failed",
    "n8n: failed",
    "Entries whose n8n workflow reported an error. Fills itself from n8n's reports; private, so sites never see it.",
    {"op": "eq", "value": "error"},
    "🚨",
)

IN_FLIGHT = _by_status(
    "n8n-in-flight",
    "n8n: in flight",
    "Entries handed to n8n that haven't finished (sent or waiting). Fills itself from n8n's reports; private, so sites never see it.",
    {"op": "in", "value": ["sent", "waiting"]},
    "⏳",
)

CONTENT = (CALLBACK_WEBHOOK, RECORD_RESULT, FAILED, IN_FLIGHT)
