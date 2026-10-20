# n8n

Hands work from Marvin to n8n and hears back. A Marvin workflow's **Run integration** step with **Trigger n8n
workflow** (`trigger_workflow`) calls an n8n workflow's **Webhook** node; n8n reports the result to a Marvin incoming
webhook, which settles the hand-off's **result** under Settings › Automation › Results.

## Connect it

1. In n8n, **Settings → n8n API → Create an API key** (only `workflow:read` and `execution:read` where offered). In
   Marvin, save it as the workspace secret `N8N_API_KEY`. Optional: without it the connection is trigger-only — no
   workflow dropdown, no execution read-back.
2. **Settings → Integrations → n8n**: set **n8n URL** to n8n's public address (`https://n8n.example.com`) and the
   credential to `{{N8N_API_KEY}}`. Marvin refuses private and in-cluster addresses. If n8n sits behind a login proxy,
   its `/webhook/` paths (and `/api/v1` for the key) must stay reachable for Marvin.

## Marvin → n8n

1. In n8n, create a **JWT Auth** credential (Passphrase, HS256, at least 32 random bytes). Save the same passphrase
   in Marvin as the secret `N8N_WEBHOOK_SECRET`.
2. In the n8n workflow: Webhook node **Authentication** = that JWT Auth credential, **Respond** = *Using 'Respond to
   Webhook' Node*; answer at once with **Respond to Webhook** (JSON `{{ { executionId: $execution.id } }}`) and do slow
   work after it — Marvin waits at most the connection's timeout (15 s). **Activate** the workflow: an inactive one
   answers 404.
3. In Marvin's step: `path` = the Webhook node's path (the **Workflow** dropdown lists active ones), `method` matching
   the node, `"secret": "{{N8N_WEBHOOK_SECRET}}"`, `"entry_id": "${entry.id}"` (so n8n can report back) and `data`.

n8n receives `{marvin: {entry_id, meta, idempotency_key, sent_at}, data}` — read it as
`{{ $('Webhook').item.json.body.data }}`. The header `X-Marvin-Idempotency-Key` is the same on every retry of one run;
a **Remove Duplicates** node on it drops repeats. The step's output has `execution_id` and `execution_url`.

Add nothing for tracking: `trigger_workflow` is a hand-off, so Marvin records it itself, **in flight** when n8n accepted it
(with the execution and a link to it) and **failed** when the call failed. Don't write status metadata on the entry; there is
no `n8n_*` metadata any more.

Other auth modes (`default_auth`, or `auth` on a step): `header` (secret in `X-Marvin-Hook`), `hmac`
(`X-Marvin-Signature`, checked in the workflow), `none`. JWT is the default because n8n stores request headers in its
execution data, where a 60-second token is useless to whoever reads them.

## n8n → Marvin: report back

1. On the n8n integration card, apply the `n8n` incoming webhook and the `n8n-record-result` workflow (an older
   `n8n-record-result` shows **Update** there: use it). Save a random value as the secret `N8N_CALLBACK_TOKEN`,
   **Create token** on the webhook, and switch the webhook and the workflow on.
2. In n8n, a **Header Auth** credential: Name `X-Marvin-Token`, Value the same random value.
3. End the n8n workflow with an **HTTP Request** node: `POST https://<marvin>/api/hooks/<token>` with that credential
   and JSON `{"marvin": {"entry_id": "{{ $('Webhook').item.json.body.marvin.entry_id }}"}, "status": "success",
   "execution_id": "{{ $execution.id }}", "workflow": "{{ $workflow.name }}", "finished_at": "{{ $now.toISO() }}",
   "message": ""}`. Send `"status": "error"` with the error message from an error branch, `"waiting"` before a Wait node.

`n8n-record-result` settles the result of that hand-off, found by `execution_id` (else the entry's newest one in flight), with
`status` (`success` succeeded, `error` failed, `waiting` still in flight), `message` and `finished_at`. See it under
Settings › Automation › Results; a failure also shows on the dashboard and on the entry, and one that never gets a report shows
as **No reply** after 24 hours. There are no n8n collections to look in.

## Recipe

**Hand published entries to n8n** (Workflow Library): each published entry of one type triggers an n8n workflow with
its title, link, type and summary. That is all the recipe does: Marvin records the hand-off as a result, in flight until n8n
reports back.

## When it fails

Every failure has a code (`${error.code}` in a workflow's on-failure steps):

- `not_found` — 404: no *active* workflow has that path and method.
- `auth` — 401/403: the JWT passphrase or header secret doesn't match n8n's credential, or no API key.
- `workflow_error` — n8n's 500 with a message: the workflow ran and failed.
- `rejected` — another 4xx from a Respond to Webhook node.
- `timeout` — no reply in time; the workflow **may have run** (use Respond immediately).
- `unavailable` / `rate_limited` — n8n unreachable, or 429; Marvin retries.
- `blocked` — the n8n URL isn't a public host.
