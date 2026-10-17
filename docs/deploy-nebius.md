# Deploying Quire on Nebius AI Cloud

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
git clone https://github.com/ajandan/quire-hackathon.git
cd quire-hackathon
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

Paste the resulting bcrypt hash into `DOCGEN_ADMIN_PASSWORD_HASH` in `.env`.

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

## 9. Verdikt smoke test

Run this after step 8. It sends one real case through the pipeline and checks
each claim the demo makes. Stop at the first failure and use section 12.
`$BASE` is `https://quire.<yourdomain>`.

**9.1 The app is up**

- `curl -fsS $BASE/health` returns `200`.
- Sign in at `$BASE/ui/` with the admin credentials from step 6.

**9.2 The program and rules exist**

- `/ui/programs` lists `cardiovita-pap-2026`. The migration seeds it only for
  tenants that already exist when it runs, so on a fresh database, if it is
  missing, create it in the UI before going on.
- The rules library shows the `CV-*` rules (income, residence, insurance,
  clinical, and two history rules).
- The OncoBridge cameo program is not created by a migration. Only run 9.7 if
  you created it yourself.

**9.3 Mint an API key**

- Admin, then Tenants, then your tenant, then Keys. Create a key and copy it
  once.
- `export KEY=<the key>`

**9.4 Happy path: expect `approve`**

```bash
curl -sS -X POST $BASE/pap/programs/cardiovita-pap-2026/intake   -H "Authorization: Bearer $KEY" -H "Content-Type: application/json"   -d '{"actor":"smoke-test","intake":{
    "patient_name":"Maria Chen","date_of_birth":"1978-06-14",
    "household_size":2,"annual_household_income":60000,
    "insurance_status":"uninsured","has_denial_letter":false,
    "country_of_residence":"United States","mailing_state":"California",
    "prescribed_drug":"Cardiovita","prescriber_npi":"1234567893",
    "prescription_date":"2026-08-15"}}'
```

Expect HTTP `201` with a `case_id` like `QC-0001`, `outcome: "approve"`, and a
`decision` whose citations name the `CV-*` rules that fired. `status` may be
`queued` with `review_state: "pending_review"` if the program holds API
submissions for review. In that case `outcome` is the draft verdict. Save the
`event_id` as `$EV`.

**9.5 Rule path: expect `deny`**

Repeat 9.4 with `"annual_household_income":120000`. For a household of 2 the
Cardiovita cap is $84,600, so expect `outcome: "deny"` and a citation to
`CV-INC-01`. If it still approves, the rules pack was not loaded.

**9.6 Bad input is rejected and still audited**

Repeat 9.4 with `"household_size":"abc"`. Expect HTTP `400`,
`detail: "intake validation failed"`, and an `event_id`. That case should
appear in the reviewer queue.

**9.7 Same household, opposite verdict (only if OncoBridge exists)**

Submit the 9.4 payload to the OncoBridge program slug. The income cap there is
$52,875, so $60,000 should return `deny` citing `OB-INC-01`.

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

Submit the 9.4 payload a second time. Compare the two reply letters (for
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
