# marvin-integration-n8n

n8n integration for [Marvin](https://github.com/InnerOpen/marvin): hand work to [n8n](https://n8n.io) and hear back.

A workflow step triggers an n8n workflow through its **Webhook** node, signed with a 60-second JWT by default.
You pick the n8n workflow from n8n's own list in the step editor. The card can read an execution's status
back. Marvin keeps one **automation result** for each trigger (in flight, succeeded or failed) under
**Settings › Automation › Results**, and n8n reports back to a Marvin incoming webhook that settles it. Failures
carry n8n-specific codes, and the provider's error policy decides whether Marvin retries, sends the entry to review
or tells an admin.

Needs Marvin with automation results and integration SDK 0.13. Coming from 0.3? See [Upgrading to 0.4.0](#upgrading-to-040).

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
| `trigger_workflow` | `path` (required): the Webhook node's path, relative (`marvin/new-entry`), with no scheme, `..`, `?` or `#`. The step editor shows it as a dropdown fed by `list_webhooks`, and free text still works. `method`: `POST` (default), `PUT` or `GET`. `data` (object). `entry_id`, `meta` (object): optional, sent in the envelope. `auth`: overrides the default auth. `secret`: `{{N8N_WEBHOOK_SECRET}}`, required unless auth is `none`. `test` (bool): refused unless the connection allows test webhooks. | `ok`, `status_code`, `path`, `response` (n8n's reply: JSON, or text up to 2000 characters), `execution_id`, `execution_url` (when the reply names `executionId` and `workflowId`, as the recipe below does). Never headers, the token or the secret. A hand-off: Marvin records it as a result, see [Results](#results-what-happened-to-the-work-marvin-handed-to-n8n). |
| `list_workflows` | `active` (default `true`; `false` lists inactive ones), `tag`, `name`. Needs the API key. | `items`: `[{id, name, active, tags, webhooks: [{path, method, auth, respond_mode, production_url}]}]`, built from each workflow's enabled `n8n-nodes-base.webhook` nodes. A node that accepts several methods gives one entry per method. `truncated`: true if it stopped at the page cap. |
| `list_webhooks` | `tag`, `name`. Needs the API key. Always active workflows. | `items`: `[{path, method, label, workflow_id, workflow_name, active, auth}]`, one per Webhook node path and method. `label` reads `<workflow name> — <METHOD> /<path>`. `truncated` as above. |
| `get_execution` | `execution_id` (required), `include_error` (default `true`). Needs the API key. | `id`, `status`, `workflow_id`, `started_at`, `stopped_at`, `retry_of`, `url`. With `include_error`, also `last_node_executed` and `error_message` (trimmed to 300 characters). Nothing else from the execution's data is passed through. If the execution's data is too big to read, the status comes back without it. |

Both list actions follow n8n's `nextCursor` for at most 10 pages of 100, ask n8n for `excludePinnedData=true`, and return
only the fields above, never node parameters or pinned data.

### Results: what happened to the work Marvin handed to n8n

`trigger_workflow` declares `hands_off` (integration SDK 0.13), so Marvin keeps **one result per trigger**, with nothing for the
workflow author to add. You find them under **Settings › Automation › Results**, and a failed or silent one shows on the
dashboard and on the entry.

```python
ProviderAction(
    key="trigger_workflow",
    # …
    hands_off=HandsOff(
        remote_id="execution_id",  # the execution on n8n's side, from the step's output
        remote_url="execution_url",  # "Open the run"
        target="path",  # the Webhook path the step asked for
        statuses={"success": "succeeded", "error": "failed", "waiting": "in_flight"},
    ),
)
```

| When | The result is |
|---|---|
| n8n accepted the trigger | **in flight**, with `execution_id` and `execution_url` |
| the call itself failed (any `N8nError` code) | **failed**, with the redacted message (no token, secret or header) |
| n8n reports `success` / `error` / `waiting` (the `n8n-record-result` workflow) | **succeeded** / **failed** / still **in flight** |
| a status n8n doesn't use | left **in flight**, with the word in its message |
| in flight for more than 24 hours | still in flight, flagged **No reply** |

A trigger that Marvin's error policy retries keeps its one result: the retry reuses it and counts the attempt. n8n's words stay n8n's: the connection translates them, so the report-back workflow holds no mapping. Marvin's own three
(`in_flight`, `succeeded`, `failed`) are accepted as they are.

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
workflow and pick the n8n workflow from the list. A workflow applied from an older version of this plugin shows **Update** on the
card instead (see [Upgrading to 0.4.0](#upgrading-to-040)).

| Kind | Slug | What it does |
|---|---|---|
| incoming webhook | `n8n` | Where n8n reports results. Scheme `static_token`, header `X-Marvin-Token`, secret `N8N_CALLBACK_TOKEN`. |
| workflow | `n8n-record-result` | Trigger: the `n8n` incoming webhook. Condition: `event.payload.status` exists. Step: the core's `automation_result` step with `remote_id: ${event.payload.execution_id}`, `entry_id: ${event.payload.marvin.entry_id}`, `target: ${event.payload.workflow}`, `status: ${event.payload.status}`, `message` and `finished_at`. It settles the result found by the execution id, else the entry's newest one in flight; a report that matches nothing is kept as a new result. |

The workflow has no parameters and names no connection: the core reads n8n's status words through the connection that installed
the `n8n` incoming webhook the report arrived on. Any slug works, and **Update** needs no input. Apply the webhook from the card of the connection that sends (with a single n8n connection, any apply does).

0.4.0 no longer ships the **n8n: failed** and **n8n: in flight** collections or writes `n8n_*` metadata on entries.

## What it teaches Marvin (SDK 0.9.0, hand-offs from 0.13.0)

- **A guide** (`src/marvin_integration_n8n/guide.md`): connecting, both directions of the round trip, auth modes and
  every error code. Marvin's agents read it when building workflows where n8n is connected; Marvin's manual search
  finds it.
- **A recipe**, *Hand published entries to n8n* (`recipes.py`): each published entry of one type triggers an n8n
  workflow (JWT-signed with `{{N8N_WEBHOOK_SECRET}}`). That one step is the whole recipe: Marvin records the hand-off as a
  result (in flight until the report-back workflow settles it) because `trigger_workflow` declares `hands_off`. Shown in the
  Workflow Library as "From the n8n plugin", offered once n8n is connected. Its only side effect is the call to n8n; the
  entry isn't changed.

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

1. In Marvin, apply the `n8n` incoming webhook and `n8n-record-result` from the n8n integration card, **Mint token**, store a
   random value as the workspace secret `N8N_CALLBACK_TOKEN` and switch the webhook and `n8n-record-result` on.
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

`execution_id` is what ties the report to the hand-off (it is the `execution_id` the trigger returned); `marvin.entry_id` is
the fallback, matching the entry's newest hand-off still in flight. The result then settles under Settings › Automation ›
Results on its own, and a failure also shows on the dashboard and on the entry. A report for a run Marvin never handed off
(no matching execution or entry) is kept as a result of its own, with `workflow` as its target, rather than dropped.

### Acting on Marvin from n8n

Create **one Marvin user per workspace** for n8n (e.g. `n8n`, role EDITOR) and a **personal token** for it. A personal
token acts as its user, in that user's *active* workspace, which is why each workspace needs its own n8n user. Put it in an
n8n **Header Auth** credential: Name `Authorization`, Value `Bearer marvin_tk_…`. Then use HTTP Request nodes against:

- `POST /api/platform/entries`: create entries.
- `POST /api/ai/operations/{slug}/execute`: run an AI operation.
- `POST /api/ai/agents/{slug}/run`: run an agent.
- To start a Marvin workflow with data, POST to one of its incoming webhooks (`/api/hooks/<token>`).

The personal token replaces the older "log in every run and reuse the 48-hour session JWT" pattern.

## Upgrading to 0.4.0

Results moved into Marvin. What 0.3 kept on entries and in collections is now a record of its own.

- **Needs** Marvin with automation results, which ships integration SDK 0.13 and reads a report's status words through the connection that
  installed the webhook it arrived on (without that, `success` / `error` / `waiting` aren't understood and a result stays in flight). Against an older SDK the plugin does not load
  (it imports `HandsOff`).
- **The collections are gone.** Marvin's migration removes **n8n: failed** and **n8n: in flight** where each is still exactly what this
  plugin shipped. One you renamed or edited stays, and is yours. Use **Settings › Automation › Results** instead (filters for in
  flight, no reply, failed and succeeded); failures also show on the dashboard and on the entry.
- **The `n8n_*` metadata is gone.** The same migration turns each entry's `n8n_status`, `n8n_execution_id`, `n8n_workflow`,
  `n8n_finished_at` and `n8n_message` into one result (`sent` / `waiting` in flight, `success` succeeded, `error` failed) and removes
  the keys. A workflow or smart collection of your own that reads `metadata.n8n_status` needs to read Results instead.
- **Update `n8n-record-result`.** A workspace that applied 0.3 has the old workflow, which still writes `n8n_*` metadata and no
  longer settles anything. Applying again leaves it as it is (apply never touches what exists). On **Settings › Integrations ›
  n8n**, the workflow shows **Update available** (↑): **Update** swaps in the new steps, keeps it switched on and keeps its name
  (the name still reads "record the result on the entry"). An admin does this once, whatever the connections are called. Until then hand-offs are
  recorded in flight and stay there.
- **Workflows made from the old recipe** keep their `set_metadata` step (the Library marks them as made from an earlier
  version). It does no harm, but it writes `n8n_status: sent` for nothing: delete that step, or make the workflow again from the
  recipe.
- **n8n needs no change.** The report-back call is the same. Sending `execution_id` is what matches a report to its hand-off; before,
  only `marvin.entry_id` was needed.

## Security

- The secret is stored only as a `{{SLUG}}` reference in step args. The provider never puts the secret, the JWT or request
  headers in a result, an error or a log line, and it blanks them out of n8n's reply if a workflow echoes them back
  (tests check this with a sentinel secret).
- Execution data is never copied into Marvin: `get_execution` returns the status, timing and a trimmed error message only.
- Make the API key read-only (`workflow:read`, `execution:read`) where n8n offers scopes.
- Running an integration step stays an ADMIN action in Marvin (`INTEGRATION_ACTION_MIN_ROLE`).

## Logo

`src/marvin_integration_n8n/logo.svg` is the n8n mark (SVG, shipped in the package), which Marvin shows for the connection instead of an
emoji.

## Known limits

- n8n's public API cannot run a workflow, so only Webhook-triggered workflows can be triggered.
- `trigger_workflow` holds a Marvin worker for up to `timeout_seconds`. Respond immediately in n8n and report back.
- A Webhook path with route parameters (`:id`) is shown and called literally.
- A hand-off whose report never arrives stays in flight (flagged **No reply** after 24 hours); Marvin never fails it by itself.
  The read action `get_execution` can look the execution up by hand.
- The result is made by the engine when a `trigger_workflow` step succeeds or fails. A dry run records nothing.
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

Resolves the SDK from a sibling checkout (`../MarvinIntegrationSDK`), as CI does with the SDK's `v0.13.0` tag. The
package requires SDK 0.13 (`HandsOff`, `provider.result_status`). The conformance kit (`marvin_integration_sdk.testing`)
runs in `tests/test_teaches.py` and checks the `hands_off` declaration.

`tests/core/test_in_core.py` runs the plugin inside Marvin's core (blueprints and recipe validate, an old workspace updates, a
hand-off through the real engine with n8n's HTTP stubbed and the report-back settling it). It is skipped here and runs from a Marvin
checkout (see the file's docstring):

```
uv pip install -e <this repo> --python .venv/bin/python --no-deps
uv run --no-sync python -m pytest -p tests.conftest <this repo>/tests/core/test_in_core.py
```
