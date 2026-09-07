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

## 9. Verify

- `https://quire.<yourdomain>/health` → `200 OK`.
- Sign in to `/ui/` with the admin credentials from step 6.
- On the empty templates page, click **"EOB (real-world)"** in the "Or try
  a sample" strip. The EOB has a `{% for %}` service-line loop and an
  `{% if is_denied %}` block that give the mapping wizard a lot to show off
  — scalars typed by the LLM, list/boolean typed by AST inference. If the
  DOCX button works but EOB doesn't, the built image is stale (rebuild).
- Trigger AI mapping. Confirm the request lands at Nebius Token Factory
  (check `docker compose logs web` — you should see one outbound request
  per mapping run, and each row in the wizard should show a "Why:" line
  from the model, not the deterministic stub template).
- The version freeze page should show byte-identical `version_hash` values
  between two consecutive renders of the same input JSON.

## 10. Rotate credentials before the demo

Before recording the video or handing out the URL:

- Rotate `DOCGEN_SESSION_SECRET` and `DOCGEN_ADMIN_PASSWORD_HASH`.
- Confirm `.env` is `chmod 600` and owned by the deploy user only.
- Confirm the Nebius Token Factory key has a spend cap set in the Nebius
  console. The design-time LLM is called once per template mapping, but a
  compromised key without a cap is still a bad time.

## 11. Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Unable to locate package caddy` | Cloudsmith repo not added | Re-run the four `curl` / `apt update` lines at the top of step 8 |
| Caddy retries ACME with `dns: no such host` | DNS record hasn't propagated | Wait, then `dig +short quire.<yourdomain>` — must return the VM IP |
| Caddy retries ACME with `connection refused` on :80 | Security group blocks 80 | Open TCP 80 inbound in the Nebius VM's security group |
| `/health` returns 502 through Caddy | Web isn't up yet | `docker compose ... logs web`; wait for "Application startup complete" |
| Mapping wizard shows the stub trace ("Placeholder '…' had no type signal") | `DOCGEN_AI_PROVIDER` isn't `nebius`, or `DOCGEN_AI_API_KEY` is empty | Re-check `.env`, restart web |
| Nebius provider request fails with 401 | Token Factory key is wrong or revoked | Rotate the key at studio.nebius.com, update `.env`, restart web |

## 12. Pre-flighting locally before the remote deploy

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
