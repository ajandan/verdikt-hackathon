# Demo setup checklist

Personal checklist for standing up the public hackathon demo. The mechanical
steps live in [`deploy-nebius.md`](deploy-nebius.md); this file is the "what
you need to have on hand before you start" side of it, plus timing.

## When to do this

Hackathon dates (from `memory/project_hackathon.md`):
- Submission deadline: **2026-10-30 10:00 PT**
- Judging window: **2026-12-01 → 2026-12-15**
- Winners announced: ~**2027-01-11**

The demo URL only needs to be live when judges click it. Practical target:
have the VM up by **mid-November 2026** — a few weeks of soak time before
judging, a few weeks of runtime cost (~$40 on cpu-e2), and you're not paying
for an idle VM through October.

## Why the VM at all

Two hard rules from the hackathon:
1. **Must run on Nebius Token Factory or Nebius AI Cloud** — hosted, not
   tunneled from a local machine.
2. **Public demo URL** is a required submission field.

A single Nebius CPU VM satisfies both. GPU compute stays on Nebius Token
Factory (Nemotron inference), so the VM itself is cheap.

## Prerequisites — collect before you start

- [ ] **Nebius account** at <https://console.nebius.com/>
- [ ] **Nebius Token Factory API key** (studio.nebius.com → API keys).
      Copy on creation — you can't retrieve it later. Set a **spend cap**
      immediately.
- [ ] **A domain you control DNS on**. Any registrar. You'll add one `A`
      record like `quire.yourdomain.com` → `<vm ip>`.
- [ ] **SSH keypair** on the Windows box you'll deploy from. Check with:
      ```bash
      ls ~/.ssh/*.pub
      ```
      If none, generate one:
      ```bash
      ssh-keygen -t ed25519 -C "quire-hackathon"
      # accept default path (~/.ssh/id_ed25519), pick a passphrase or leave empty
      ```
- [ ] **Admin password** you'll use for `/ui/` login. Anything; you'll hash
      it in step 6 of the runbook.

## VM sizing

| Resource | Value |
| --- | --- |
| Platform | `cpu-e2` or equivalent |
| vCPU | 4 |
| RAM | 8 GB |
| Disk | 40 GB SSD |
| Image | Ubuntu 24.04 LTS |
| Public IPv4 | **enabled** |

**Security group must allow inbound**: TCP 22 (SSH), 80 (Caddy ACME
challenge), 443 (public HTTPS). Missing :80 is the #1 reason Let's Encrypt
issuance silently loops.

## Cost estimate (rough)

- cpu-e2 4vCPU/8GB, 40GB disk: ~$0.05/hr → ~$36 for a 30-day soak.
- Nebius Token Factory: pay-per-token. Design-time LLM is called once per
  template mapping; a hackathon demo burns pennies. **Set a spend cap** on
  the API key anyway.
- Domain registration: whatever your registrar charges. If you don't have
  one, Cloudflare Registrar is at-cost (~$10/yr).

Total budget: ~$50 for the full hackathon window.

## The 10-minute path (once prereqs are done)

Full commands in [`deploy-nebius.md`](deploy-nebius.md); the ordering:

1. Provision the VM (console click, ~2 min).
2. `ssh` in, install Docker, clone the repo.
3. Get the Nebius Token Factory API key.
4. `cp .env.example .env`, fill in secrets, set `chmod 600`.
5. Hash your admin password inside a throwaway container.
6. `docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d`.
7. Wait for `Application startup complete` in web logs.
8. Install Caddy from Cloudsmith repo, drop in Caddyfile, reload.
9. Hit `https://quire.<yourdomain>/health` → 200.
10. Sign in, click "EOB (real-world)" sample, trigger AI mapping, verify
    Nebius provider response.

Everything after step 1 is a shell-paste job. Step 1 needs Nebius console
clicks that only you can do.

## What Claude needs from you when you're ready

Paste in the chat, in this order:

1. Confirmation that the VM is Running.
2. Public IPv4 address.
3. Hostname you chose (e.g. `quire.yourdomain.com`).
4. Confirmation the security group has 22/80/443 open inbound.
5. `dig +short <yourhostname>` result — must return the VM IP before we
   trigger Caddy's TLS provisioning.

Do NOT paste: private SSH keys, the Nebius API key, the admin password.
Everything sensitive goes into `.env` on the VM, never into chat.

## Kill-switch

At the end of the judging window, tear down:
```bash
# On the VM
docker compose -f deploy/docker-compose.prod.yml --env-file .env down -v
```
Then delete the VM in the Nebius console. Delete the Token Factory API key
too — even with a spend cap, unused keys are just liability.
