# Microsoft Entra ID and Teams Setup

This guide connects OpsBridge to a **test tenant**. Mock mode (`MOCK_MODE=true`) needs none of it.

> Permissions are **not** granted automatically. Every permission below needs explicit **admin consent** from a
> tenant administrator. Get these approvals before you start, and use a non-production or developer tenant
> (for example, one from the Microsoft 365 Developer Program, if you are eligible).

## 1. Identities OpsBridge uses

| Identity | Used for | Auth flow |
|---|---|---|
| **App registration** (application identity) | Read channel messages, manage change-notification subscriptions | OAuth 2.0 client credentials |
| **Service account user** (e.g. `opsbridge-bot@<tenant>`) | Post the acknowledgement reply in the thread | Delegated: device code once, then refresh-token grant |

Two identities are needed because **Microsoft Graph has no application permission for sending channel messages**.
The only exception is `Teamwork.Migrate.All`, which is for migration only. Replies must be posted as a user.
Use a dedicated, licensed (Teams-enabled) service account that is a member of the support team.

## 2. Register the application

1. Go to **Entra admin center → App registrations → New registration**.
   * Name: `OpsBridge (test)`. Supported accounts: *this organizational directory only*.
2. Note the **Application (client) ID** → `MICROSOFT_CLIENT_ID` and the **Directory (tenant) ID** → `MICROSOFT_TENANT_ID`.
3. **Certificates & secrets → New client secret** → `MICROSOFT_CLIENT_SECRET`. Store it in a secret manager and record its expiry date.
4. **Authentication → Advanced settings → Allow public client flows = Yes.** This is required for the device-code sign-in
   of the service account.

## 3. API permissions (Microsoft Graph)

| Permission | Type | Why | Admin consent |
|---|---|---|---|
| `ChannelMessage.Read.All` | Application | Read messages and replies, subscribe to `/teams/{id}/channels/{id}/messages` | **Required** |
| `ChannelMessage.Send` | Delegated | Service account posts replies | Required (or user consent if your tenant allows it) |
| `offline_access` | Delegated | Refresh token for the service account | – |
| `User.Read` | Delegated | Sign-in (default) | – |

Then click **Grant admin consent for &lt;tenant&gt;** and confirm that every row shows *Granted*.

**Least-privilege alternative:** `ChannelMessage.Read.All` gives the app access to *all* channels in the tenant.
Resource-specific consent (RSC, `ChannelMessage.Read.Group`) limits it to teams where a Teams app manifest that
declares RSC has been installed. That needs a Teams app package. It is out of scope for this POC, but recommended for production.

**Policy and licensing notes.** Microsoft classifies some Teams message APIs and change notifications as protected or
metered, and they can carry approval, licensing or payment requirements that change over time. Check the current Microsoft Graph
documentation for *channel message change notifications* and *Teams protected APIs* before you rely on them.

## 4. Find the team and channel IDs

The simplest way is in Teams: on the channel, choose **⋯ → Get link to channel**. The link contains
`groupId=<TEAM_ID>` and `/channel/<CHANNEL_ID>/` (URL-decode the `19:...@thread.tacv2` value).
Alternatively, in Graph Explorer: `GET /me/joinedTeams` followed by `GET /teams/{team-id}/channels`.

```env
TEAMS_TEAM_ID=00000000-0000-0000-0000-000000000000
TEAMS_CHANNEL_ID=19:xxxxxxxxxxxxxxxxxxxxxxxx@thread.tacv2
```

## 5. Service-account token

```bash
python scripts/get_delegated_token.py --tenant <TENANT_ID> --client-id <CLIENT_ID>
```

Sign in **as the service account** using the code shown. The script prints:

```env
MICROSOFT_DELEGATED_REFRESH_TOKEN=...   # a credential: treat like a password
MICROSOFT_SERVICE_ACCOUNT_USER_ID=...   # its object id; its top-level posts are ignored
```

Refresh tokens expire after a period of inactivity. They are revoked by password resets, Conditional Access changes or
admin revocation. When that happens, replies fail with a clear `FAILED` status and error message, and Jira tickets are
still created. Re-run the script, then call `POST /support-requests/{id}/retry`.

## 6. Client state, base URL and subscription

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> MICROSOFT_CLIENT_STATE
```

Graph must reach OpsBridge over **public HTTPS** (see *ngrok* in the README). Set `APP_BASE_URL` to that URL and
`MOCK_MODE=false`, then start OpsBridge and create the subscription:

```bash
curl -X POST "$APP_BASE_URL/subscriptions/create" -H "X-API-Key: $ADMIN_API_KEY"
```

During creation, Graph sends a validation request to **both** `/webhooks/teams` and `/webhooks/teams/lifecycle`. Each must
echo the `validationToken` within 10 seconds, and OpsBridge does this automatically. If creation fails, check that the URL is
reachable from the internet and that admin consent was granted.

### Subscription lifetime

* Chat-message subscriptions last at most about 3 days. OpsBridge defaults to `GRAPH_SUBSCRIPTION_LIFETIME_MINUTES=4200`.
* A lifecycle notification URL is mandatory for lifetimes over 1 hour, and OpsBridge always sends one.
* The background worker renews subscriptions that expire within `GRAPH_SUBSCRIPTION_RENEWAL_THRESHOLD_MINUTES`
  (default 12 h), checking every 5 minutes. If Graph no longer knows the subscription (404), OpsBridge creates a new one.
* Lifecycle events: `reauthorizationRequired` → renew, `subscriptionRemoved` → recreate, `missed` → catch-up sync of the
  latest 50 channel messages, through the same idempotent insert.

### Thread commands and the escalation channel

The thread commands (`close`, `reopen`, `status`, `escalate`) need **no extra Graph permissions**. Replies are read
with `ChannelMessage.Read.All` and answers are posted with the service account's `ChannelMessage.Send`.

For `escalate` alerts, set `TEAMS_ESCALATION_CHANNEL_ID` to a **different** channel in the same team (for example
*escalations*). The service account must be able to post there. For a private channel, add it as a member.
The subscription only covers the support channel, so alerts are never read back as new requests. If both IDs are
the same, OpsBridge refuses to start.

To find admin object IDs for `TEAMS_COMMAND_ADMIN_USER_IDS`, open *Entra admin center → Users → (user) → Object ID*.

## 7. Verify

1. Post the template message in the channel.
2. Within a few seconds you should see `POST /webhooks/teams -> 202` in the logs, followed by `created Jira issue` and
   `posted Teams reply` with the same `trace_id`.
3. `GET /support-requests` shows the request as `COMPLETED`.
