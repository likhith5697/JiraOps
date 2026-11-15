# Jira Sandbox Setup

1. **Site.** Create a free Jira Cloud site for testing. Do not use a production site.
2. **Project.** Create a project (Scrum, Kanban or team-managed) and note its key → `JIRA_PROJECT_KEY` (e.g. `OPS`).
3. **Integration user.** Ideally use a dedicated account with these project permissions:
   *Browse projects* and *Create issues* (ticket creation); *Transition issues* and *Add comments*
   (`close` and `reopen`); *Edit issues* (`escalate` changes priority and labels).
4. **API token.** Signed in as that user, create a token in your Atlassian account under
   *Security → API tokens*. Set `JIRA_EMAIL` to the user's email and `JIRA_API_TOKEN` to the token, and set
   `JIRA_BASE_URL=https://<your-site>.atlassian.net`.
5. **Issue type.** Set `JIRA_ISSUE_TYPE` to an issue type name that exists in the project (e.g. `Task`, `Bug`).
6. **Fields on the create screen**
   * `summary` and `description` are always present.
   * **`labels` must be on the create screen.** OpsBridge's duplicate protection depends on the
     `opsbridge-<hash>` label. If labels are missing, Jira returns HTTP 400 and the request moves to `FAILED`
     with that error.
   * `priority` is optional. Set `JIRA_PRIORITY_MAP` to your site's priority **names**, for example
     `{"P1": "Highest", "P2": "High", "P3": "Medium"}`. If the project does not accept a priority (common in
     team-managed projects), OpsBridge retries the create once without it. Use `{}` to never send a priority.
7. **Check credentials:**

   ```bash
   curl -u "$JIRA_EMAIL:$JIRA_API_TOKEN" "$JIRA_BASE_URL/rest/api/3/myself"
   ```

## What a created issue looks like

| Jira field | Value |
|---|---|
| Summary | `[QA] CustomerOrderAPI: Order creation is failing` |
| Description (ADF) | Issue description, business impact, request details (API, environment, priority, correlation ID, reporter, link to the Teams message), optional fields |
| Labels | `opsbridge`, `opsbridge-<sha256(message id)[:16]>`, `env-qa`, `priority-p2`, `api-customerorderapi` |
| Priority | Mapped through `JIRA_PRIORITY_MAP` |

The API name, environment and correlation ID are stored in the description and labels. This keeps setup to zero custom fields.
Phase 2 could map them to custom fields (`customfield_XXXXX`) through configuration.

## Reconciliation by label

To find an issue whose creation outcome was unknown, OpsBridge runs:

```text
POST /rest/api/3/search/jql
{"jql": "project = \"OPS\" AND labels = \"opsbridge-<hash>\" ORDER BY created ASC", "fields": ["summary"], "maxResults": 5}
```

Jira's search index is eventually consistent, so the search waits `JIRA_RECONCILE_DELAY_SECONDS` (default 60) after an
ambiguous failure.

## Closing from Teams (`close` command)

When a user replies `close <reason>` in a request thread, OpsBridge makes these calls:

1. `GET /rest/api/3/issue/{key}?fields=status`. If the status already equals `JIRA_CLOSE_STATUS`, nothing changes in Jira.
2. `GET /rest/api/3/issue/{key}/transitions`, then picks the transition whose **`to.name`** matches
   `JIRA_CLOSE_STATUS` (case-insensitive). Transition IDs differ between workflows, so they are never hard-coded.
3. `POST /rest/api/3/issue/{key}/transitions` with that transition ID.
4. `POST /rest/api/3/issue/{key}/comment` with an ADF comment: *Closed from Microsoft Teams by {name}. Reason: {reason}*.

Set `JIRA_CLOSE_STATUS` to whatever your workflow calls its final status (`Done`, `Closed`, `Resolved`, and so on).

* **No transition available.** Some workflows only allow `In Progress → Done`. If the issue's current status has no
  transition to the close status, the user gets a reply explaining that and the issue is left untouched.
* **Required fields on the transition screen** (for example *Resolution*). Jira rejects the transition with HTTP 400,
  and the command is marked `FAILED` with Jira's error message. Make those fields optional, or give them defaults in
  the workflow.

## Other thread commands

| Command | Jira calls |
|---|---|
| `reopen [reason]` | Same as `close`, but only if the issue is currently in `JIRA_CLOSE_STATUS`; target `JIRA_REOPEN_STATUS` (default `To Do`) and comment *Reopened from Microsoft Teams by {name}…* |
| `status` | `GET /rest/api/3/issue/{key}?fields=status,assignee,priority,updated`. Read-only |
| `escalate P1 [reason]` | `PUT /rest/api/3/issue/{key}` with `{"fields": {"priority": {"name": <JIRA_PRIORITY_MAP[P1]>}}, "update": {"labels": [{"add": "<JIRA_ESCALATION_LABEL>"}]}}`, then a comment *Escalated from P2 to P1 from Microsoft Teams by {name}…* |

`escalate` only ever *raises* priority. If the project doesn't allow editing priority (Jira returns HTTP 400 for the
priority field), OpsBridge still adds the label and comment, raises the priority in its own database, and says so
in the Teams reply.

## Jira → Teams status sync

When an issue's **status** or **assignee** changes in Jira, OpsBridge posts it in the request's Teams thread:

```text
🔔 OPS-1 was updated in Jira
Status: To Do → In Progress
Assignee: Unassigned → Jane Engineer
Changed by: Jane Engineer          (webhook only; polling doesn't know who changed it)
```

### Polling (default, no setup)

Every `JIRA_SYNC_POLL_INTERVAL_SECONDS` (default 120), OpsBridge runs:

```text
POST /rest/api/3/search/jql
{"jql": "project = \"OPS\" AND updated >= \"-6m\" ORDER BY updated ASC", "fields": ["status", "assignee", "updated"]}
```

It needs only *Browse projects*. Issues that OpsBridge didn't create are ignored.

### Webhook (optional, near-instant)

1. Generate a secret, then set `JIRA_WEBHOOK_SECRET` and restart OpsBridge.
2. In Jira, go to **Settings → System → WebHooks → Create a WebHook** (needs Jira admin):
   * URL: `https://<APP_BASE_URL>/webhooks/jira`
   * Secret: the same value. Jira then signs each request (`X-Hub-Signature: sha256=…`).
   * Events: *Issue → updated* (and *created*, optional)
   * JQL filter (recommended): `project = OPS`
3. **Alternative without admin rights:** a Jira Automation rule (*Field value changed: Status, Assignee* →
   *Send web request* to `https://<APP_BASE_URL>/webhooks/jira?secret=<secret>`, POST, body *Issue data (Jira format)*).
   Automation can't sign requests, which is why the `?secret=` form exists.

Polling and the webhook can run together. Each change is posted **once**, no matter how many times or in what order
it's observed.

### Behaviour

* **Only real changes are posted.** OpsBridge remembers the last status and assignee it saw for each ticket. Comments, label
  or priority edits don't produce a post. Older or duplicate events are ignored, using Jira's `updated` timestamp.
* **No echo of Teams commands.** A `close`/`reopen` sent from Teams already replies in the thread, so the resulting
  status change isn't posted a second time.
* **Stays in step.** Moving the issue to `JIRA_CLOSE_STATUS` in Jira marks the request `CLOSED` (*"Closed in Jira"*).
  Moving it out again marks it `COMPLETED`.
* **Older tickets.** Tickets created before this feature existed get their current state recorded on the next poll
  (read-only, up to 25 per poll). Every change after that is posted.
