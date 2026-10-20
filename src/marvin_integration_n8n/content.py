"""What a workspace can use to hear back from n8n — declared for the core to offer, all optional.

There are no send-side blueprints: sending is a ``trigger_workflow`` step a person adds to their own
workflow, picking the n8n workflow from n8n's list. That action declares ``hands_off`` (see ``provider.py``), so
the core keeps one result per trigger, in flight, with nothing to add to the workflow. What's declared here is the
return path:

  an n8n workflow finishes → its "report back to Marvin" HTTP Request node POSTs
  ``{marvin: {entry_id}, status, execution_id, workflow, finished_at, message}`` to the ``n8n`` incoming
  webhook (header ``X-Marvin-Token``) → ``n8n-record-result`` runs the core's ``automation_result`` step, which
  settles that result (found by the execution id, else the entry's newest one in flight).

Results are listed under Settings › Automation › Results. Since 0.4.0 there are no ``n8n_*`` entry metadata and no
``n8n-failed`` / ``n8n-in-flight`` collections: the core removed the untouched ones and imported the metadata.

Applying (from the integration's card) creates only what is missing; an already-applied ``n8n-record-result`` from
an older version shows **Update** on the card, which swaps in the steps below (the core's ``update_blueprint``). The
webhook and the workflow arrive switched off; nothing here is required.

Parameters: none. The step names no ``integration``: the core reads n8n's status words (``success``, ``error``,
``waiting``) through the connection that installed the triggering ``n8n`` webhook, so any number of n8n connections, under any
slug, work and an Update needs no input.
"""

from marvin_integration_sdk import ContentBlueprint

CATEGORY = "n8n"
WEBHOOK_SLUG = "n8n"
CALLBACK_SECRET_REF = "N8N_CALLBACK_TOKEN"
CALLBACK_HEADER = "X-Marvin-Token"
# The step argument a trigger_workflow step signs with — a workspace secret reference, never a value.
WEBHOOK_SECRET_REF = "{{N8N_WEBHOOK_SECRET}}"


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
        "When n8n reports back on the n8n webhook, settle the hand-off's result (Settings > Automation > Results): found by the "
        "execution id, else the entry's newest one in flight (marvin.entry_id). n8n's status, message and finish time are kept on it."
    ),
    category=CATEGORY,
    payload={
        "definition": {
            "trigger": {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG},
            "conditions": [{"field": "event.payload.status", "op": "exists"}],
            "actions": [
                {
                    "kind": "automation_result",
                    "id": "settle",
                    "remote_id": "${event.payload.execution_id}",
                    "entry_id": "${event.payload.marvin.entry_id}",
                    "target": "${event.payload.workflow}",
                    "status": "${event.payload.status}",
                    "message": "${event.payload.message}",
                    "finished_at": "${event.payload.finished_at}",
                },
            ],
        }
    },
)

CONTENT = (CALLBACK_WEBHOOK, RECORD_RESULT)
