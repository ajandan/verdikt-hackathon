# Deploying Quire on Nebius AI Cloud

> **Status: reference, not what is running.** The live deployment is
> self-hosted. This runbook is kept because it is pre-flighted and still
> correct for a Nebius AI Cloud VM, but nothing below describes the
> current deployment. What *is* Nebius in production is the inference:
> every model call goes through Nebius Token Factory, which is what the
> hackathon rule requires (it names Token Factory or AI Cloud).

This runbook takes a fresh Nebius AI Cloud VM to a public, TLS-terminated Quire
instance running the design-time LLM against NVIDIA Nemotron on Nebius Token
Factory.

Everything runs in Docker Compose on a single CPU VM. No GPU required — the
model is served by Nebius Token Factory, not the app.

## 0. What you'll create

- **1 × Nebius VM** (compute), Ubuntu 24.04, ~4 vCPU / 8 GB RAM / 40 GB disk.
- **1 × Nebius Token Factory API key** for the design-time LLM.
- **1 × public DNS record** pointing at the VM (`quire.<yourdomain>`).
- Docker Compose stack: web + worker + migrate + Postgres + Redis + object store.
- TLS terminated by Caddy in front of the app on `:443`.

## 1. Provision the VM

1. In the Nebius Cloud console, create a Compute VM:
   - Region: any (pick one close to your Nebius Token Factory endpoint).
   - Platform: `cpu-e2` or equivalent, 4 vCPU / 8 GB RAM.
   - Image: Ubuntu 24.04 LTS.
   - Disk: 40 GB (default SSD).
   - Public IPv4: **enabled**.
   - SSH key: paste your public key.
2. In the VM's security group, allow inbound TCP on **22** (SSH), **80**
   (Caddy's ACME HTTP-01 challenge), and **443** (public HTTPS). Without :80
   open, Let's Encrypt will fail to issue a certificate and Caddy will keep
   retrying every hour.
3. Note the public IPv4.
4. Point a DNS `A` record at that IP: `quire.<yourdomain>` → `<vm ip>`.
   Wait for the record to resolve (`dig +short quire.<yourdomain>`) before
   starting Caddy in step 8 — ACME will fail if the hostname doesn't yet
   point at this VM.

## 2. Bootstrap the VM

SSH in and install Docker Engine + Compose plugin:

```bash
ssh ubuntu@<vm ip>

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in for the group change to take effect
```

## 3. Get the code

```bash
git clone https://github.com/ajandan/verdikt-hackathon.git
cd verdikt-hackathon
```

## 4. Get a Nebius Token Factory API key

1. Sign in at <https://studio.nebius.com/>.
2. Create an API key. Copy it — you can't retrieve it later.
3. Confirm the Nemotron model you plan to use is available in the model
   catalog. The app defaults to a current Nemotron variant; override
   `DOCGEN_AI_MODEL` in `.env` if you want a different one.

## 5. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Fill in:

```dotenv
POSTGRES_USER=docgen
POSTGRES_PASSWORD=<generate: openssl rand -hex 24>
POSTGRES_DB=docgen

MINIO_ROOT_USER=docgen-blobs
MINIO_ROOT_PASSWORD=<generate: openssl rand -hex 24>

WEB_PORT=8200
DOCGEN_ENV=production

DOCGEN_SESSION_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
DOCGEN_WEBHOOK_SIGNING_SECRET=<same command, new value>
DOCGEN_DOWNLOAD_TOKEN_SECRET=<same command, new value>

DOCGEN_ADMIN_USER=admin
# generated in step 6
DOCGEN_ADMIN_PASSWORD_HASH=

DOCGEN_SECURE_COOKIES=true
DOCGEN_HSTS_ENABLED=true
DOCGEN_TRUSTED_PROXIES=127.0.0.1
DOCGEN_PUBLIC_BASE_URL=https://quire.<yourdomain>
DOCGEN_MCP_ALLOWED_HOSTS=quire.<yourdomain>

# --- Design-time LLM: NVIDIA Nemotron on Nebius Token Factory ---
DOCGEN_AI_PROVIDER=nebius
DOCGEN_AI_API_KEY=<your Nebius Token Factory key>
# Leave base URL + model blank to use the built-in Nebius / Nemotron defaults.
DOCGEN_AI_BASE_URL=
DOCGEN_AI_MODEL=
DOCGEN_AI_TIMEOUT_SECONDS=120
```

## 6. Generate the admin password hash

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env \
  run --rm web python -c \
  "from docgen.auth import hash_password; print(hash_password('<your admin password>'))"
```

Paste the resulting bcrypt hash into `DOCGEN_ADMIN_PASSWORD_HASH` in `.env`,
**inside single quotes**:

```dotenv
DOCGEN_ADMIN_PASSWORD_HASH='$2b$12$...'
```

Without the quotes, Docker Compose reads each `$` as a variable, the hash is
silently corrupted and sign-in always fails.

## 7. Bring the stack up

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
docker compose -f deploy/docker-compose.prod.yml logs -f web
```

Wait for `Application startup complete`. Migrations run automatically as a
sidecar and must exit `0` before web/worker start.

## 8. Terminate TLS with Caddy

Caddy isn't in Ubuntu 24.04's default APT sources — you have to add the
Cloudsmith repository first. On the same VM:

```bash
# Add Caddy's package repository (one-time)
sudo apt update
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
sudo apt update
sudo apt install -y caddy

# Configure the reverse proxy
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
quire.<yourdomain> {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8200
    log {
        format filter {
            query {
                replace token REDACTED
            }
        }
    }
}
EOF
sudo systemctl reload caddy
```

Caddy will provision a Let's Encrypt certificate automatically. Watch the
handshake with `sudo journalctl -u caddy -f` — the first request to
`https://quire.<yourdomain>/` triggers ACME. Visit
`https://quire.<yourdomain>/ui/` — you should get the admin sign-in page.

## 8b. Load the demo data

A clean database has the default tenant but not the Alderwood demo tenant, its
program with the 105-slot schema, its nine rules, the reply letters or the
guideline PDFs. Load them from `deploy/demo-seed/`:

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env   run --rm -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/deploy/demo-seed:/app/deploy/demo-seed:ro"   web python scripts/seed_demo.py
```

It prints one row count per table and a **single API key, shown once**. Save it;
step 9.3 needs it. Running it again changes nothing and does not mint another
key. To refresh the seed from a working install, run `scripts/export_demo_seed.py`
in the web container and commit the result.

## 8c. Alternative: a Mac or home server behind a Cloudflare Tunnel

Skip steps 1, 2 and 8 if the app runs on a machine you already own. Run steps 3
to 8b there, set `WEB_PORT` to a free port, and add the hostname to the tunnel's
ingress list (`hostname: verdikt.<yourdomain>`, `service: http://localhost:<port>`).
Use a separate compose project name (`-p verdikt`) so it never touches other
stacks on the machine. Cloudflare supplies the HTTPS; keep
`DOCGEN_SECURE_COOKIES=true`. Over plain `http://` (for example a LAN address)
browsers drop the `Secure` cookie and sign-in appears to do nothing; set it to
`false` only while testing that way.

Bind the port to loopback (`WEB_PORT=127.0.0.1:<port>`) so only the tunnel can
reach the app, then set `DOCGEN_TRUSTED_PROXIES` to the address the web
container sees the tunnel's requests come from. On Docker Desktop for Mac that
is `192.168.65.1`; check it with `docker logs <web container>` after one
request through the public URL. With both set, `X-Forwarded-For` is trusted only
from the tunnel, so the login rate limit keys on the real client address.

## 9. Verdikt smoke test

Run this after step 8. It sends one real case through the pipeline and checks
each claim the demo makes. Stop at the first failure and use section 12.
`$BASE` is `https://quire.<yourdomain>`.

**9.1 The app is up**

- `curl -fsS $BASE/health` returns `200`.
- Sign in at `$BASE/ui/` with the admin credentials from step 6.

**9.2 The program and rules exist**

- `/ui/programs` lists `cardiovita-pap-2026` under the Alderwood tenant, with
  105 slots. This comes from step 8b.
- The rules library shows its nine rules (`CV-CLIN-01-cardio`, `CV-CONSENT-01`,
  `CV-PRACTICE-US-01`, `CV-PAP-REQUEST-01`, `CV-PRESCRIBER-CONSENT-01`,
  `CV-INS-01-cardio`, `CV-RES-01-cardio` and the two history rules).
- The OncoBridge cameo program is not created by a migration. Only run 9.7 if
  you created it yourself.

**9.3 Mint an API key**

- Use the key printed by step 8b, or create one under Admin, then Tenants, then
  Alderwood, then Keys.
- `export KEY=<the key>`

**9.4 Happy path: expect `approve`**

Queue the Maria Chen packet from `samples/email-test/` (the ASP-100 enrollment
form plus the ASP-110 income worksheet), the same two PDFs the email test guide
uses:

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env   run --rm -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/samples:/app/samples:ro"   web python scripts/queue_sample_case.py
```

It takes about a minute and prints `outcome: approve`, the event id and
`review_state=pending_review`. Save the event id as `$EV`. The case now waits
in the reviewer queue. Extraction reads the PDFs as images, so a rare misread
can change the outcome. If it does not approve, open the pipeline page (9.8)
and read which rule failed and what value was extracted.

**9.5 Rule path: expect `missing_info`**

The income rule (`CV-INC-01-cardio`) reads household size and income from the
ASP-110 worksheet. Queue the enrollment form alone:

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env   run --rm -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/samples:/app/samples:ro"   web python scripts/queue_sample_case.py /app/samples/email-test/ASP-100-enrollment-maria-chen.pdf
```

Expect `outcome: missing_info`, with `household_size` and
`annual_household_income` listed as missing: the system asks for the worksheet
instead of guessing a denial. The over-the-cap denial is covered by
`tests/test_demo_seed_income_rule.py`.

**9.6 Bad input is rejected and still audited**

```bash
curl -sS -X POST $BASE/pap/programs/cardiovita-pap-2026/intake   -H "Authorization: Bearer $KEY" -H "Content-Type: application/json"   -d '{"actor":"smoke-test","intake":{"patient_name":"x"}}'
```

Expect HTTP `400`, `detail: "intake validation failed"` and an `event_id`. The
JSON path takes the form's own field names (`patient_last_name`, `rx_medication`
and so on), so a short payload lists every missing field. That case appears in
the reviewer queue as `failed`. **Delete it from the inbox before showing the
site to anyone.**

**9.7 OncoBridge cameo (only if OncoBridge exists)**

It is not part of the seed. If you created that program yourself, queue a bundle
against its slug and check that its own income cap (`OB-INC-01`) is applied.

**9.8 The reviewer can see every stage**

- Open `$BASE/ui/inbox/$EV/pipeline`. It should show the extracted fields, the
  triage case type and route, the retrieved rules, the verdict with citations
  and the reply letter.
- Open `/ui/admin/routing`. Rows should show Nebius Token Factory model names
  (`nvidia/nemotron-3-super-120b-a12b` for the verdict) with real latencies.
  If Triage shows the rule-based fallback, check `triage_model` and the key.
- `docker compose -f deploy/docker-compose.prod.yml logs web` shows outbound
  Token Factory requests for the case.
- If the case is pending review, approve it. The reply letter should then be
  available for download.

**9.9 No model on the render path**

The letter is merged from the frozen template. Confirm nothing in
`logs web` shows a Token Factory request during the approve/render step. Only
the Ingest, Triage and Decide stages may call a model.

**9.10 Repeatability (record the result, do not assume it)**

Queue the Maria Chen packet a second time (9.4). Compare the two reply letters (for
example `sha256sum` of the downloaded files) and the template `version_hash`
shown on the pipeline page. The template hash should match. The test suite
checks structure, not letter bytes, so write down whether the bytes match
rather than claiming it in the demo.

## 10. Rotate credentials before the demo

Before recording the video or handing out the URL:

- Rotate `DOCGEN_SESSION_SECRET` and `DOCGEN_ADMIN_PASSWORD_HASH`.
- Confirm `.env` is `chmod 600` and owned by the deploy user only.
- Confirm the Nebius Token Factory key has a spend cap set in the Nebius
  console. The design-time LLM is called once per template mapping, but a
  compromised key without a cap is still a bad time.

## 11. Scraping /metrics from Prometheus

The app exposes Prometheus-format metrics at `GET /metrics`. Auth-bypassed
so scrapers don't need bearer tokens — restrict access at the network
edge instead (allowlist your Prometheus IP in the security group, or bind
the app on an internal port that Caddy doesn't route to the public
Internet).

What ships out of the box:

| Metric | Type | Labels | Answers |
| --- | --- | --- | --- |
| `docgen_renders_total` | counter | `tenant_id`, `format`, `state` | "renders/hr by tenant, by outcome" |
| `docgen_render_latency_seconds` | histogram | `tenant_id`, `format`, `state` | "P99 latency by tenant + format" |
| `docgen_render_queue_depth` | gauge | `state` | "how deep is the queue right now" |
| `docgen_preflight_rejections_total` | counter | `tenant_id`, `reason` | "which tenants are uploading bad PDFs, and why" |

Minimal `prometheus.yml` scrape stanza:

```yaml
scrape_configs:
  - job_name: verdikt
    scrape_interval: 30s
    metrics_path: /metrics
    static_configs:
      - targets: ["quire.<yourdomain>:443"]
    scheme: https
```

Suggested alerts (Prometheus expression, thresholds are starting points —
tune from your first week of scrape data):

```promql
# Sustained render failures for any tenant (>5%/5min).
sum by (tenant_id) (rate(docgen_renders_total{state="failed"}[5m]))
  / sum by (tenant_id) (rate(docgen_renders_total[5m])) > 0.05

# Queue backing up (>50 queued for >2min = worker starving or crashed).
docgen_render_queue_depth{state="queued"} > 50

# P99 render latency drift (docx should never take >10s in normal load).
histogram_quantile(0.99,
  sum by (le, format) (rate(docgen_render_latency_seconds_bucket{format="docx"}[10m]))
) > 10
```

The queue-depth gauge is sampled from the DB on each scrape, not
incremented at enqueue/dequeue, so a worker crash that leaves a job
stuck in `running` is visible on the next scrape rather than requiring
you to trust a counter that got out of sync with the truth in Postgres.

## 12. Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Unable to locate package caddy` | Cloudsmith repo not added | Re-run the four `curl` / `apt update` lines at the top of step 8 |
| Caddy retries ACME with `dns: no such host` | DNS record hasn't propagated | Wait, then `dig +short quire.<yourdomain>` — must return the VM IP |
| Caddy retries ACME with `connection refused` on :80 | Security group blocks 80 | Open TCP 80 inbound in the Nebius VM's security group |
| `/health` returns 502 through Caddy | Web isn't up yet | `docker compose ... logs web`; wait for "Application startup complete" |
| Mapping wizard shows the stub trace ("Placeholder '…' had no type signal") | `DOCGEN_AI_PROVIDER` isn't `nebius`, or `DOCGEN_AI_API_KEY` is empty | Re-check `.env`, restart web |
| Nebius provider request fails with 401 | Token Factory key is wrong or revoked | Rotate the key at studio.nebius.com, update `.env`, restart web |

## 13. Pre-flighting locally before the remote deploy

The exact prod compose file works on any Docker host. To smoke-test on your
laptop before pushing to the Nebius VM:

```bash
cp .env .env.prod-test
# In .env.prod-test, shift WEB_PORT to something unused (e.g. 8300),
# set DOCGEN_AI_PROVIDER=stub if you don't want to burn Nebius credits.
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod-test \
    -p quire-prod up -d
curl -fsS http://localhost:8300/health
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod-test \
    -p quire-prod down -v
```

If that runs clean locally, the Nebius VM will too — same image, same
compose file, only the reverse proxy in front changes.
