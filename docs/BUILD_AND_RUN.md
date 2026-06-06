# Build & run (client)

This is the **client** container — it runs inside the customer's own network.
It talks to a hosted "brain" service over an authenticated HTTPS API; the brain
is operated separately by PowerDataChat and is never built or run from this
repository. For the customer-facing quickstart see
[`../CUSTOMER_INSTALL.md`](../CUSTOMER_INSTALL.md).

---

## 1. Build the client container

```bash
docker build -t powerdatachat-client:enterprise .
```

The client image is heavy because it ships pandas, matplotlib, plotly,
kaleido, scikit-learn, and python-pptx — every library the generated
analysis code might run on the customer's data, all executed locally.

---

## 2. Run the client container

To run the client you need a **tenant token** issued by PowerDataChat from the
brain side. The token is shown ONCE when it is created — save it. Then:

```bash
docker run --rm -p 8000:8000 \
  -e BRAIN_URL="https://brain.your-domain.example.com" \
  -e BRAIN_TENANT_TOKEN="<token-from-operator>" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v $(pwd)/client_data:/data/client \
  powerdatachat-client:enterprise
```

| Variable             | Purpose                                                            |
|----------------------|-------------------------------------------------------------------|
| `BRAIN_URL`          | HTTPS endpoint of the hosted brain (placeholder / custom domain). |
| `BRAIN_TENANT_TOKEN` | Per-tenant bearer token issued by the operator. **Never commit.** |
| `SECRET_KEY`         | Local session-cookie signing secret. Generate once, keep stable.  |
| `DATA_ROOT`          | Local-disk root for raw data + chats (default `/data/client`).    |
| `CLIENT_LLM_DEBUG`   | Verbose brain-call debug logging to the LOCAL client log (default OFF; `1`/`true` to enable). Logs `BRAIN_REQUEST` / `BRAIN_RESPONSE` per call. Boundary-safe — brain payloads carry no row values (Art. II). Turn back OFF after diagnosing. |
| `CLIENT_LLM_DEBUG_MAX_CHARS` | Per-field truncation for the debug logs (default `20000`). |

A failed brain call always logs its HTTP status + body snippet (~2000 chars)
regardless of `CLIENT_LLM_DEBUG` — an API rejection is never silently swallowed.

The client listens on **port 8000**. Open `http://localhost:8000` → enter your
work email → land in `/lab`.

Prefer Compose? See [`../docker-compose.yml`](../docker-compose.yml)
(`docker compose up -d`) — it defines only the client service.

---

## 3. Local smoke test (native, no Docker)

Useful while iterating. From a clean checkout, point the client at a brain
endpoint you have a valid tenant token for:

```powershell
cd PDC_Client
$env:DATA_ROOT = ".\client_data"
$env:SECRET_KEY = "<random-hex>"
$env:BRAIN_URL = "https://brain.your-domain.example.com"
$env:BRAIN_TENANT_TOKEN = "<token-from-operator>"
python -m uvicorn app:app --host 127.0.0.1 --port 8001

# Browser: http://127.0.0.1:8001 → enter email → /lab
```

The exact path verified during development:
landing → email login → upload a CSV → schema autofill → chat → conversation
history. When the tenant token is valid the brain returns token-validated
responses; if the operator revokes the token, every subsequent brain call
returns `403` and the chat UI surfaces a single "service unavailable" error.

---

## 4. Health check

```bash
curl http://localhost:8000/health
```

A healthy install returns:

```json
{"status":"ok","brain_reachable":true,"tenant_token_configured":true}
```

- `brain_reachable: false` → check outbound HTTPS to `BRAIN_URL`.
- `tenant_token_configured: false` → `BRAIN_TENANT_TOKEN` is empty.

---

## 5. Application settings from the brain

A few upload-time limits (`MAX_FILES`, `TITLE_MAX_LEN`, `TITLE_BREAK_MIN`) are
fetched from the brain's `GET /v1/app_settings` endpoint at request time, so an
operator can change them per tenant without the customer redeploying the client
container. See [`PROTOCOL.md`](PROTOCOL.md) for the shape.

---

## 6. Client UI notes

- **Profile button** in `/lab` is temporarily frozen — the avatar + email +
  "Enterprise" plan badge render, but a capture-phase click handler swallows the
  click so the dropdown / modal never opens. A separate visible **Logout** button
  in the topbar POSTs to `/auth/logout` so users can still sign out.
- **No message-count cap** — enterprise enforces no per-request quota and no
  per-tenant rate limit beyond the operator's `active | suspended | revoked`
  status flag (the kill-switch). Any "messages today" line reads "unlimited".

### Chart persistence in conversation history

Conversation history is stored per-conversation under
`chatdata/{chat_id}/conversations/{conv_id}.jsonl` in `DATA_ROOT`. The AI turn
of a chart-producing response is persisted as:

- **Single chart** — top-level `image_base64` on the AI message.
- **Two or more charts** — `image_base64: null`, plus
  `images: [{image_base64, answer}, ...]` (one entry per rendered chart, in
  display order).

`dashboard.js` reopens a conversation via
`GET /api/chat/{chat_id}/conversation/{conv_id}/history`: `msg.images` → one
assistant bubble per chart; otherwise `msg.image_base64`. The multi-plot
accumulator lives in [`../routes/chat.py`](../routes/chat.py).

---

## 7. Logs

**Client logs** (in `logs/datachat.log`): upload events, code-execution errors
(which can contain raw data values, like KeyError row indices), and the
client's view of brain HTTP responses. These stay on the client server and
**never leave it** — consistent with the data boundary (see
[`AI_CONSTITUTION.md`](AI_CONSTITUTION.md), Article II).
