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
2. Create every file listed in `deploy/secrets/README.md` with mode `0600`.
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
- `search_egress` exists only for SearXNG. The backend reaches SearXNG over the
  private `search` network. P7 will add the separately constrained page-fetch
  path and its SSRF enforcement.

All application containers use read-only root filesystems, dropped
capabilities, `no-new-privileges`, explicit resource limits, and non-root image
users. Caddy retains only `NET_BIND_SERVICE` so its non-root process can bind
80/443.
