# Garmin remote MCP server

A Streamable HTTP MCP server exposing Garmin Connect data, deployable to Render and
addable to Claude as a custom connector — including on mobile.

16 tools: activities, activity detail, daily summary, sleep, HRV, training readiness,
training status, Body Battery, stress, resting HR, VO2 max, race predictions, body
composition, step ranges, plus a `raw_get` escape hatch for any other read-only
`get_*` method on `python-garminconnect`.

## Why it's built this way

**Your password never reaches the server.** `auth_setup.py` runs on your laptop,
does the Garmin login (including MFA), and prints a base64 token blob. Only that blob
goes to Render. Compromise of the server costs you a revocable Garmin session, not
your account credentials.

**The endpoint is gated by a shared secret.** Claude connects from Anthropic's cloud,
so the server has to be publicly reachable — which means anyone who guesses the URL
would otherwise have your health data. Every request needs `?key=<MCP_KEY>` or an
`Authorization: Bearer` header. Render generates the key for you.

The MCP SDK's DNS-rebinding protection is deliberately left off. It defends a
*localhost* server against a browser on your own network, and enforces that by 403ing
any unrecognized `Origin` header — which would silently break a legitimate cloud
client with no way to see why from the Claude side. On a public, key-gated endpoint
it buys nothing: a forged `Origin` gets you nowhere without the key.

**Stateless transport.** Each request is self-contained, so Render can restart or
scale the instance without breaking an in-flight session.

**Payloads are pruned.** Garmin returns minute-by-minute arrays and hundreds of null
fields. Sending those verbatim burns the context window for no benefit, so nulls are
dropped and long arrays are truncated (`MAX_LIST_ITEMS`, default 50).

## Setup

Requires **Python 3.12+** — `garminconnect` 0.3.11 does not install on 3.11.

### 1. Get your Garmin tokens

```bash
pip install garminconnect
python auth_setup.py
```

Copy the base64 blob it prints. It's equivalent to your Garmin session — treat it
like a password and keep it out of git.

### 2. Deploy

Push this directory to a **private** GitHub repo, then in Render: New → Blueprint →
select the repo. `render.yaml` provisions everything. Set `GARMIN_TOKENS` manually in
the Environment tab (it's marked `sync: false` so it's never in your repo). Copy the
generated `MCP_KEY` from the same tab.

A malformed `GARMIN_TOKENS` fails the deploy at boot with a specific message rather
than surfacing as a broken tool call mid-conversation, so check the deploy log if the
service won't come up.

Adjust `region` in `render.yaml` if Singapore isn't nearest you. The blueprint uses
the `starter` plan on purpose — the free tier sleeps after ~15 minutes idle, and the
cold start will time out Claude's first call every time.

### 3. Add it to Claude

Customize → Connectors → **+** → Add custom connector. Name it Garmin, and paste:

```
https://<your-service>.onrender.com/mcp?key=<MCP_KEY>
```

Leave the OAuth fields empty — the key in the URL is the auth. Once connected, open
the connector's settings and set tool permissions to always allow, or you'll approve
every single call.

That URL is a bearer credential. Anyone holding it can read your health data.

### Local testing

```bash
cp .env.example .env   # fill in both values
pip install -r requirements.txt
set -a && source .env && set +a
python server.py
curl localhost:8000/healthz
```

`smoke_test.py` checks the whole request path — key auth, the MCP handshake, tool
registration, and response pruning — against a stubbed Garmin client, so it needs no
account and no network:

```bash
python smoke_test.py
```

Run it after changing `server.py` and before redeploying. It catches the failures
that otherwise only show up as a connector that won't connect.

## Known limitations

`python-garminconnect` wraps undocumented Garmin Connect endpoints, not the official
Connect Developer Program API. A Garmin-side auth change in March 2026 broke most of
this tooling overnight, and it can happen again. When calls start failing with auth
errors, first re-run `auth_setup.py`; if that doesn't fix it, check upstream for a
library release.

The token format is tied to `garminconnect`'s internals (`Client.dumps()` — currently
`di_token`, `di_refresh_token`, `di_client_id`). A major upstream release can change
it; regenerate the blob with a matching `auth_setup.py` if it does.

Refresh tokens last roughly a year. Renewal is manual: re-run `auth_setup.py`,
replace `GARMIN_TOKENS` in Render, redeploy.

Nothing here is affiliated with or endorsed by Garmin Ltd.
