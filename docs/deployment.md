# P1 deployment and public-boundary runbook

## Prerequisites

- A Linux host with Docker Engine, Compose v2, the P0-qualified NVIDIA stack,
  and the pinned model snapshots under `model-cache/huggingface/hub`.
- A public DNS `A`/`AAAA` record for `PUBLIC_DOMAIN` pointing at the host.
- Inbound TCP 80 and 443 forwarded directly to this host. Do not place an
  unconfigured TLS-terminating proxy in front of Caddy.
- An authenticated administrative path such as SSH over a VPN or a tightly
  allowlisted source IP. It is outside the application Compose project.

## First deployment

1. Copy `deploy/.env.example` to `deploy/.env` and set the real domain, ACME
   email, off-machine backup destination, and SearXNG secret.
2. Create every file listed in `deploy/secrets/README.md` with mode `0640`,
   owned by the deployment operator and the `SECRETS_GID` group.
3. Confirm `database_url` uses the URL-encoded value stored in
   `postgres_password`.
4. Validate the boundary and resolved configuration:

   ```bash
   python3 scripts/verify_compose_boundary.py
   docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
   ```

5. Start the pinned stack:

   ```bash
   docker compose --env-file deploy/.env -f deploy/compose.yml up -d --build
   ```

6. Wait for Caddy, backend, and Streamlit to be healthy, then inspect JSON
   logs. Never paste logs containing credentials into tickets.

7. Verify HTTP redirects, HTTPS, headers, and public route behavior:

   ```bash
   curl -I "http://${PUBLIC_DOMAIN}/"
   curl -fsS "https://${PUBLIC_DOMAIN}/api/healthz"
   curl -I "https://${PUBLIC_DOMAIN}/api/readyz"
   curl -I "https://${PUBLIC_DOMAIN}/api/metrics"
   ```

   HTTP must redirect to HTTPS. Health returns only `{"status":"ok"}`.
   Readiness and metrics return 404 through Caddy. Check the certificate chain,
   HSTS, CSP, `nosniff`, referrer, and frame headers in the HTTPS response.

The initial P1 page is intentionally only a login shell. Account creation and
functional authentication begin in P2; there is no registration route.

## Same-Wi-Fi HTTPS test

Wi-Fi is a private network, not a public DNS domain or an ACME identity. For a
LAN-only test, copy `deploy/.env.lan.example` to `deploy/.env` and use the
machine's mDNS name. On the current host that is:

```text
https://goksu-ubuntu.local
```

The current DHCP address is `192.168.68.103`, but the mDNS hostname is preferred
because the address can change. A link-specific resolver check confirms this
name resolves to that address on `wlp5s0`; an all-interface lookup on the host
may instead display a Docker address. `Caddyfile.lan` uses Caddy's internal CA
and never contacts a public ACME service, so `ACME_EMAIL` is intentionally unset
in LAN mode. Browse the hostname, not the raw IP, so TLS receives the expected
server name.

After starting the stack, export the local CA certificate:

```bash
mkdir -p artifacts/p1
docker compose --env-file deploy/.env -f deploy/compose.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt \
  artifacts/p1/caddy-local-root.crt
```

Install that root certificate as a trusted user CA on each test phone/computer,
then fully restart its browser. Do not install a root certificate received from
someone else. Caddy documents that clients must trust its internal root CA for
local HTTPS: <https://caddyserver.com/docs/automatic-https#local-https>.

Keep the host and client on the same non-guest Wi-Fi. Guest/client-isolation
networks commonly block peer-to-peer traffic. The host firewall may allow
TCP 80/443 from `192.168.68.0/24`, but must not expose backend/model/data ports.
This LAN workflow is useful integration evidence; it does not satisfy P1's
separate public-domain certificate and external Internet port-scan gate.

For the current development period, the owner explicitly selected LAN-only
operation. Public ACME issuance and an external Internet scan are deferred, not
recorded as passing. KDE Connect's pre-existing TCP 1716 listener is also an
accepted unrelated host service; the application Compose project still
publishes only Caddy TCP 80/443.

## Closed user administration

There is no registration endpoint. Bootstrap the first superuser interactively
after the P2 migration; passwords are prompted without being placed in command
arguments or shell history:

```bash
python3 scripts/seed_admin.py \
  --email admin@example.com \
  --tenant-slug shared \
  --tenant-name "Shared Knowledge"
```

The bootstrap command refuses to run after any superuser exists. Create later
accounts through an authenticated superuser session:

```bash
python3 scripts/create_user.py \
  --actor-email admin@example.com \
  --email member@example.com \
  --tenant-slug shared \
  --role member
```

Disable/enable users, reset passwords, change roles, or revoke every active
session with the corresponding internal CLI command:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli disable-user \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli reset-password \
  --actor-email admin@example.com --email member@example.com

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli set-role \
  --actor-email admin@example.com --email member@example.com \
  --tenant-slug shared --role admin

docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -m app.admin_cli revoke-tokens \
  --actor-email admin@example.com --email member@example.com
```

Every command except the one-time bootstrap prompts for and verifies the acting
superuser's password. Disablement, password reset, role changes, and explicit
revocation invalidate access-token versions and revoke stored refresh tokens.

## Google Drive backup destination

`gdrive://folder/<folder-id>` is recorded as a destination locator only. A
normal browser sharing URL is not an upload API and is rejected by startup
configuration. P11 must add authenticated Google Drive transfer (for example,
an OAuth-authorized restricted rclone remote), encryption before upload,
retention, monitoring, and a restore drill.

Before P11, change the target folder's General access from "Anyone with the
link" to **Restricted**. Google states that files placed in a shared folder
inherit its sharing permissions; a public parent therefore makes backup
objects publicly readable. Encryption is still mandatory, but it does not
replace authenticated storage access.

## Firewall boundary

At the cloud firewall/security group, allow inbound TCP 80/443 from the public
internet and deny every other inbound port. Apply the same host policy. With
UFW, adapt the management rule to the real VPN or fixed administrator source:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow from <ADMIN_SOURCE_CIDR> to any port 22 proto tcp
sudo ufw enable
sudo ufw status numbered
```

Do not blindly run these commands over a remote connection: an incorrect
management source can lock out the host. Docker's host-port rules are not a
substitute for the cloud and host firewalls.

From a separate network, install `nmap` and run:

```bash
./scripts/verify_public_ports.sh "$PUBLIC_DOMAIN"
```

Save the dated output as deployment evidence. Only TCP 80/443 may be open for
the application host. If SSH/VPN shares the scanned address, its tightly
restricted administrative port must be documented separately; public scans
should not be able to reach it.

## Internal diagnostics

The backend's `/api/readyz` and `/api/metrics` routes exist for container and
future monitoring-network use, but Caddy returns 404 for both. Run diagnostics
inside the private network, for example:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml exec backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/readyz').read().decode())"
```

The readiness body deliberately reveals no failed dependency name. Dependency
state is available as Prometheus gauges and redacted structured logs.

## Network design

- `edge` is internal and connects only Caddy, backend, and Streamlit. Caddy has
  fixed address `172.30.0.2`; uvicorn trusts forwarded headers only from it.
- `data`, `model`, and `search` are internal Docker networks with no host
  published ports.
- `caddy_egress` exists only for ACME and normal Caddy egress.
- `search_egress` exists only for SearXNG. `fetch_egress` exists only for the
  SSRF-resistant `web-fetcher`. The backend reaches both services over the
  private `search` network and has no direct internet route. The fetcher joins
  no data, model, or edge network and receives no application secrets.

All application containers use read-only root filesystems, dropped
capabilities, `no-new-privileges`, explicit resource limits, and non-root image
users. Caddy retains only `NET_BIND_SERVICE` so its non-root process can bind
80/443.
