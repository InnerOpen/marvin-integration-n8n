# marvin-integration-n8n

n8n integration for [Marvin](https://github.com/InnerOpen/marvin): hand work to [n8n](https://n8n.io) and hear back.

A workflow step triggers an n8n workflow through its **Webhook** node, signed with a 60-second JWT by default.
You pick the n8n workflow from n8n's own list in the step editor. The card can read an execution's status
back. n8n reports results to a Marvin incoming webhook, which records them on the entry, and a smart
collection shows what failed. Failures carry n8n-specific codes, and the provider's error policy decides
whether Marvin retries, sends the entry to review or tells an admin.

## Connection

| | |
|---|---|
| **n8n URL** (`base_url`, required) | Where Marvin reaches n8n, for webhooks and the API, e.g. `https://n8n.example.com`. It must be a public host: Marvin's integration HTTP client refuses private, loopback and in-cluster addresses (`http://n8n.n8n.svc.cluster.local:5678` is refused). |
| **n8n API key** (credential, optional) | n8n → Settings → n8n API → Create an API key. Give it only the **`workflow:read`** and **`execution:read`** scopes where your n8n offers scopes. Store it as a workspace secret (e.g. `N8N_API_KEY`) and enter `{{N8N_API_KEY}}`. **Without a key the connection is webhook-only**: `trigger_workflow` works; `list_workflows`, `list_webhooks` and `get_execution` don't, and the step editor's workflow picker falls back to free text. |
| **Editor URL** (`editor_url`) | Optional: the address people open n8n at, used for execution links. Blank means the n8n URL. |
| **Webhook prefix** (`webhook_prefix`) | n8n's production webhook path (`N8N_ENDPOINT_WEBHOOK`). Default `webhook`. |
| **Default auth** (`default_auth`) | `jwt` (default), `header`, `hmac` or `none`. See [What is sent](#what-is-sent). A step can override it. |
| **Auth header** (`auth_header`) | The header that carries the secret in `header` mode. Default `X-Marvin-Hook`. |
| **Timeout** (`timeout_seconds`) | How long a trigger waits for n8n's reply. Default 15, at most 30. For anything slow, let n8n respond immediately and report back. |
| **Allow test webhooks** (`allow_test_webhooks`) | Default off. When on, a step with `test: true` calls `/webhook-test/<path>`, which only answers while the n8n editor is listening. |

The card's check calls `GET /api/v1/workflows?limit=1` with the key (a 401/403 shows **API key rejected**). Without a key it calls
`GET /healthz`. A bad setting shows **error** with the reason.

## Actions

| Action | Inputs | Returns |
|---|---|---|
| `trigger_workflow` | `path` (required): the Webhook node's path, relative (`marvin/new-entry`), with no scheme, `..`, `?` or `#`. The step editor shows it as a dropdown fed by `list_webhooks`, and free text still works. `method`: `POST` (default), `PUT` or `GET`. `data` (object). `entry_id`, `meta` (object): optional, sent in the envelope. `auth`: overrides the default auth. `secret`: `{{N8N_WEBHOOK_SECRET}}`, required unless auth is `none`. `test` (bool): refused unless the connection allows test webhooks. | `ok`, `status_code`, `path`, `response` (n8n's reply: JSON, or text up to 2000 characters), `execution_id`, `execution_url` (when the reply names `executionId` and `workflowId`, as the recipe below does). Never headers, the token or the secret. |
| `list_workflows` | `active` (default `true`; `false` lists inactive ones), `tag`, `name`. Needs the API key. | `items`: `[{id, name, active, tags, webhooks: [{path, method, auth, respond_mode, production_url}]}]`, built from each workflow's enabled `n8n-nodes-base.webhook` nodes. A node that accepts several methods gives one entry per method. `truncated`: true if it stopped at the page cap. |
| `list_webhooks` | `tag`, `name`. Needs the API key. Always active workflows. | `items`: `[{path, method, label, workflow_id, workflow_name, active, auth}]`, one per Webhook node path and method. `label` reads `<workflow name> — <METHOD> /<path>`. `truncated` as above. |
| `get_execution` | `execution_id` (required), `include_error` (default `true`). Needs the API key. | `id`, `status`, `workflow_id`, `started_at`, `stopped_at`, `retry_of`, `url`. With `include_error`, also `last_node_executed` and `error_message` (trimmed to 300 characters). Nothing else from the execution's data is passed through. If the execution's data is too big to read, the status comes back without it. |

Both list actions follow n8n's `nextCursor` for at most 10 pages of 100, ask n8n for `excludePinnedData=true`, and return
only the fields above, never node parameters or pinned data.

### The workflow picker

`trigger_workflow`'s `path` input carries an option-source hint:

```json
"x-marvin-options": {"action": "list_webhooks", "value": "path", "label": "label"}
```

A Marvin that understands the hint runs `list_webhooks` through the connection and shows a dropdown. An older Marvin, or a
webhook-only connection, shows a text box. Pick the right **method** too: the picker fills only the path, and a Webhook
node registered for `POST` answers 404 to a `GET`.

## What is sent

The body is serialised once and sent as bytes, so the bytes that are signed are the bytes that arrive:

```json
{"marvin": {"entry_id": "…", "meta": {}, "idempotency_key": "…", "sent_at": "2026-10-04T10:00:00Z"}, "data": {…}}
```

Headers: `X-Marvin-Idempotency-Key` stays the same across every retry of one run for the same path and entry
(`ctx.idempotency_key(path, entry_id)`), so n8n can drop a duplicate. `X-Marvin-Timestamp` is Unix seconds. A `GET`
sends no body and refuses `data` and `meta`.

| `auth` | Header | Checked in n8n by |
|---|---|---|
| `jwt` (default) | `Authorization: Bearer <HS256 JWT>`. Claims: `iss: "marvin"`, `aud: <path>`, `iat`, `exp: iat + 60`, `jti: <idempotency key>`, `body_sha256: <hex SHA-256 of the body>`. | The Webhook node's **JWT Auth** credential. Nothing to code. |
| `header` | `<auth_header>: <secret>`, i.e. `X-Marvin-Hook: …` by default. | The Webhook node's **Header Auth** credential. The header *is* the secret; kept for existing setups. |
| `hmac` | `X-Marvin-Signature: t=<ts>,v1=<hex HMAC-SHA256 of "{t}.{body}">` | Your workflow (n8n has no native HMAC check): a Crypto node over the raw body. |
| `none` | nothing | — |

JWT is the default because n8n saves Webhook request headers in its execution data: a token that expired a minute
after it was sent is useless to anyone who reads it there. Use a passphrase of at least 32 random bytes.

## Errors and how they're handled

Every failure, network errors included, raises a readable `N8nError` (an SDK `IntegrationError`) with a stable
`code`. The provider declares an **error policy** for each code. Marvin applies it (retries, Needs review, admin
alerts) and shows it on the card under "How errors are handled". The provider itself never sleeps, retries or
alerts. A workflow's own on-failure steps still see the code as `${error.code}`.

How a Handle reads (SDK 0.5): review and notify happen as soon as the error does, alongside any retry; `then`
applies once the retries run out.

| `code` | When | `trigger_workflow` (provider policy) |
|---|---|---|
| `auth` | 401/403 from a webhook or the API. | Notify admins, wait for the connection to recover, retry (up to 3×), then send to review. |
| `not_found` | 404: no active workflow registered that path and method (switched off, wrong path or method). | Notify admins, retry 2× (5m, 30m), then send to review. |
| `rate_limited` | 429. n8n's `Retry-After` (seconds or an HTTP date) becomes the error's `retry_after`, which Marvin honours over the backoff. | Retry 3× (30s, 2m, 10m), then notify admins and send to review. |
| `unavailable` | Couldn't connect (refused, DNS, connect timeout), 502/503/504, a 500 without n8n's `{"message": …}` body (a proxy's error page), or another 5xx. | Retry 4× (1m, 5m, 30m, 2h), then notify admins and send to review. |
| `timeout` | The request was sent and no reply came in time. n8n may have run the workflow. | Send to review. **No retry**, to avoid running it twice. |
| `workflow_error` | 500 with n8n's `{"message": …}`: the workflow ran and failed before responding. | Send to review only. n8n's own error workflow already alerts. |
| `rejected` | Another 4xx: the workflow (a Respond to Webhook node) said no. | Send to review. |
| `blocked` | Marvin's SSRF guard refused the host. | Notify admins. |
| `invalid` | A bad argument or connection setting (a bad path, a missing or unresolved secret, a test webhook that isn't allowed). | Notify admins. |
| `response_too_large` | n8n's reply was bigger than Marvin keeps (`INTEGRATION_HTTP_MAX_BYTES`). The request landed. | Treat as success. |
| anything else (`*`) | e.g. `unknown`. | Notify admins and send to review. |

`workflow_error` defaults to review only. An admin can change Review and Alert per code for one connection in the card's
**How errors are handled** table (a core feature, Marvin rc.197); retries, backoff and `then` stay as declared here.

The read actions override every code, because they run from the card or the step editor, where the error shows
straight away:

| Action | Policy |
|---|---|
| `list_workflows`, `list_webhooks`, `get_execution` | Every code, and `*`: fail plainly. Each code is listed on the action, because an action's `*` ranks below the provider's entry for a specific code (lookup order: action[code] > provider[code] > action[`*`] > provider[`*`]). |

## What a workspace gets — declared, applied from the integration's card

Nothing is created on install, and nothing here is required. **Apply** creates only what is missing; the webhook and the
workflow arrive switched off. There are no send-side blueprints: to send, add a `trigger_workflow` step to your own
workflow and pick the n8n workflow from the list.

| Kind | Slug | What it does |
|---|---|---|
| incoming webhook | `n8n` | Where n8n reports results. Scheme `static_token`, header `X-Marvin-Token`, secret `N8N_CALLBACK_TOKEN`. |
| workflow | `n8n-record-result` | Trigger: the `n8n` incoming webhook. Condition: `event.payload.marvin.entry_id` exists. Step: `set_metadata` on `entity_id: $event.payload.marvin.entry_id` with flat keys `n8n_status` (`success` / `error` / `waiting`), `n8n_execution_id`, `n8n_workflow`, `n8n_finished_at`, `n8n_message`. |
| collection *(suggestion)* | `n8n-failed` | **n8n: failed.** Smart, private: `{"where": [{"field": "metadata.n8n_status", "op": "eq", "value": "error"}], "match": "all"}`. |
| collection *(suggestion)* | `n8n-in-flight` | **n8n: in flight.** Smart, private: `{"where": [{"field": "metadata.n8n_status", "op": "in", "value": ["sent", "waiting"]}], "match": "all"}`. |

The metadata keys are flat because smart rules read only top-level `metadata.<key>`. Both collections are private
(**Visible to sites** off), so a result never rebuilds a site.

## Marvin → n8n: triggering a workflow

1. In n8n, create a **JWT Auth** credential: Key Type **Passphrase**, Algorithm **HS256**, and a long random secret.
2. In Marvin, store the same secret as the workspace secret `N8N_WEBHOOK_SECRET`.
3. In the n8n workflow, set the Webhook node's **Authentication** to **JWT Auth** with that credential and activate the workflow.
4. In a Marvin workflow, add an integration step: connection `n8n`, action `trigger_workflow`, pick the workflow under
   **Workflow** (`path`), and pass `secret: {{N8N_WEBHOOK_SECRET}}`, `entry_id: ${entry.id}` and the `data` you want.
   Secrets live in stored args only as the `{{…}}` reference, so the dry run shows the reference and never the value.

An existing `X-Marvin-Hook` setup keeps working with `auth: header` (or **Default auth** `header`) and a Header Auth
credential in n8n.

### Recipe: "receive from Marvin" (n8n side)

1. **Webhook**: Authentication **JWT Auth**; HTTP Method **POST**; Path e.g. `marvin/new-entry`; Respond **Using 'Respond to Webhook' Node**.
2. **Remove Duplicates**: Operation *Remove Items Processed in Previous Executions*, value to dedupe on
   `{{ $json.headers['x-marvin-idempotency-key'] }}`. A retried trigger that already ran stops here.
3. **Respond to Webhook**: Respond With **JSON**, body `{{ { executionId: $execution.id, workflowId: $workflow.id } }}`.
   Respond before the slow work so Marvin's step finishes well inside its timeout. `trigger_workflow` then returns `execution_id`
   and `execution_url`.
4. The work. The payload is at `{{ $('Webhook').item.json.body.data }}` and the entry id at `{{ $('Webhook').item.json.body.marvin.entry_id }}`.

## n8n → Marvin: reporting back and acting on Marvin

No custom n8n node: an HTTP Request node does it.

### Recipe: "report back to Marvin" (n8n side)

1. In Marvin, apply the `n8n` incoming webhook, **Mint token**, store a random value as the workspace secret
   `N8N_CALLBACK_TOKEN` and switch the webhook and `n8n-record-result` on.
2. In n8n, create a **Header Auth** credential: Name `X-Marvin-Token`, Value the same random value.
3. End the workflow with an **HTTP Request** node: `POST https://<marvin>/api/hooks/<token>`, that credential, body JSON:

   ```json
   {
     "marvin": {"entry_id": "{{ $('Webhook').item.json.body.marvin.entry_id }}"},
     "status": "success",
     "execution_id": "{{ $execution.id }}",
     "workflow": "{{ $workflow.name }}",
     "finished_at": "{{ $now.toISO() }}",
     "message": ""
   }
   ```

4. For failures, set the risky node's **On Error** to *Continue (using error output)* and send its error branch to a
   second HTTP Request node with `"status": "error"` and `"message": "{{ $json.error.message }}"`. Before a Wait node,
   report `"status": "waiting"`.

The entry then shows in `n8n-failed` or `n8n-in-flight` on its own.

### Acting on Marvin from n8n

Create **one Marvin user per workspace** for n8n (e.g. `n8n`, role EDITOR) and a **personal token** for it. A personal
token acts as its user, in that user's *active* workspace, which is why each workspace needs its own n8n user. Put it in an
n8n **Header Auth** credential: Name `Authorization`, Value `Bearer marvin_tk_…`. Then use HTTP Request nodes against:

- `POST /api/platform/entries`: create entries.
- `POST /api/ai/operations/{slug}/execute`: run an AI operation.
- `POST /api/ai/agents/{slug}/run`: run an agent.
- To start a Marvin workflow with data, POST to one of its incoming webhooks (`/api/hooks/<token>`).

The personal token replaces the older "log in every run and reuse the 48-hour session JWT" pattern.

## Security

- The secret is stored only as a `{{SLUG}}` reference in step args. The provider never puts the secret, the JWT or request
  headers in a result, an error or a log line, and it blanks them out of n8n's reply if a workflow echoes them back
  (tests check this with a sentinel secret).
- Execution data is never copied into Marvin: `get_execution` returns the status, timing and a trimmed error message only.
- Make the API key read-only (`workflow:read`, `execution:read`) where n8n offers scopes.
- Running an integration step stays an ADMIN action in Marvin (`INTEGRATION_ACTION_MIN_ROLE`).

## Logo

None: the card shows the 🔗 emoji. n8n's brand guidelines ask for the logo at 100 px wide or more and cover the full logo,
not the mark alone, while Marvin shows integration marks at about 32 px (Jared 2026-10-04: use the emoji).

## Known limits

- n8n's public API cannot run a workflow, so only Webhook-triggered workflows can be triggered.
- `trigger_workflow` holds a Marvin worker for up to `timeout_seconds`. Respond immediately in n8n and report back.
- A Webhook path with route parameters (`:id`) is shown and called literally.
- `n8n-in-flight` matches `n8n_status` `sent` or `waiting`, but nothing here writes `sent`: `trigger_workflow` can't write
  metadata. To track sends, follow the step with your own `set_metadata` `n8n_status: sent`.
- Marvin reaches n8n only at a public address (for example `https://n8n.example.com`). A per-provider private-host
  allowlist is a later core change.
- The workflow picker and the per-code Review/Alert overrides need Marvin rc.197 or later. Without them the path is
  free text and the policy above applies as declared.
- Not done yet: an end-to-end test against a real n8n 2.x (JWT accepted and an expired one rejected, header mode, 404 for an
  inactive workflow, the 500 body shape, the executionId round trip). The tests here use recorded n8n JSON.

## Develop

```
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Resolves the SDK from a sibling checkout (`../MarvinIntegrationSDK`), as CI does with the SDK's `develop` branch. The
package requires SDK 0.5.
