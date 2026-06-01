# PowerDataChat — Client (on-prem) overview

This repository is the **client** of PowerDataChat Enterprise — the on-premise
application that runs inside a customer's own network. It is one half of a
two-part product; the other half is a separate, **hosted "brain" service** that
this client talks to over an authenticated HTTPS API. This repo is public, so it
documents only the client and the contract it uses — not the brain's internals.

## What this client does (everything sensitive stays here)

- **Data upload + storage** — raw data lives on this server and never leaves it.
- **Python execution** — runs analysis code locally against the raw data
  (`client/code_exec.py::safe_execute`); results stay local.
- **Chart rendering** (kaleido) and **report rendering** (python-pptx / PDF).
- **The `/lab` web frontend.**
- **The customer's branded presentation templates.**

## The data boundary (the core guarantee)

Raw data values never leave this server. The client sends the hosted brain API
only NON-VALUE metadata needed to generate code and narratives:

- the question text;
- `schema_text` — column names, dtypes, and lightly-sampled hints;
- no-value report "findings" — question, answer text, column NAMES, a code
  snippet, and `has_chart`/`has_table` flags.

The `_safe_preview` guard (`client/run_chat_local.py`) enforces this: only
`str | int | float | bool` may be previewed across the boundary; dicts, lists,
and DataFrames become `None`. **The client holds no model API keys.**

## How it connects to the brain

Over HTTPS with a per-tenant bearer token, configured at install time via
environment variables:

- `BRAIN_URL` — the hosted brain API endpoint;
- `BRAIN_TENANT_TOKEN` — this customer's token (issued by the operator; sent as
  `Authorization: Bearer <token>`);
- `SECRET_KEY` — local session-cookie secret;
- `DATA_ROOT` — local path for this customer's data.

See [`CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md) for this client's own HTTP
surface, [`PROTOCOL.md`](PROTOCOL.md) for the request/response shapes the client
exchanges with the brain API, and [`BUILD_AND_RUN.md`](BUILD_AND_RUN.md) /
[`../CUSTOMER_INSTALL.md`](../CUSTOMER_INSTALL.md) to build and run the container.

## Reporting / presentations (client-side rendering)

The brain returns only the narrative content (titles, summaries, per-finding
text); the client renders the deck locally with `python-pptx`. When a branded
template is configured for the tenant, the renderer reproduces the template's
design and injects the analysis into it, falling back to a native layout and
then a built-in layout — never crashing. Charts are rendered locally (kaleido).
The rendered file never leaves this server.

## Local-filesystem only

All client state lives under `DATA_ROOT` on this server (users, chats, rendered
files, the cached template spec). No cloud storage on the client side.

---

> **PUBLIC REPO — keep brain details out.** This is the public client repository.
> Do NOT add brain-side details here: hosting/infra (cloud project, buckets,
> service names, regions), secrets or secret names, the brain's internal
> architecture / IP (skill engine, prompts, model tiering, admin/operator
> internals), or brain deploy/operator procedures. Those belong only in the
> private brain repository.
