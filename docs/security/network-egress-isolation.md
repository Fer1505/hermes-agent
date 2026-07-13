# Network Egress Isolation for Docker Deployments

When running Hermes inside Docker, the default `network_mode: host` gives the
agent process unrestricted outbound network access. This guide shows how to
segment traffic so the agent core can only reach the services it needs, while
blocking arbitrary outbound connections.

This is primarily a defense against prompt injection attacks that attempt to
exfiltrate data via `curl`, `wget`, or raw HTTP from tool-generated shell
commands.

## Threat Model

The Hermes [SECURITY.md](../../SECURITY.md) §2 defines the trust model. The
terminal backend is the primary execution boundary. However, when running with
`network_mode: host`, any command the agent executes can reach any endpoint on
the network, including external ones.

Network egress isolation adds a second layer: even if a malicious command
executes inside the container, it cannot reach endpoints outside the
explicitly allowlisted set.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Docker Network: internal (no internet)     │
│                                             │
│   ┌──────────────┐   ┌──────────────────┐   │
│   │ hermes-agent │   │ hermes-dashboard │   │
│   └──────┬───────┘   └────────┬─────────┘   │
│          │                    │              │
│          ▼                    │              │
│   ┌──────────────┐            │              │
│   │ hermes-gtw   │◄───────────┘              │
│   └──────┬───────┘                           │
│          │                                   │
└──────────┼───────────────────────────────────┘
           │
┌──────────┼───────────────────────────────────┐
│  Docker Network: egress (internet-capable)   │
│          │                                   │
│          ▼                                   │
│   ┌─────────────────┐                        │
│   │ egress-proxy     │──► allowlisted hosts  │
│   │ (squid / envoy)  │                       │
│   └─────────────────┘                        │
└──────────────────────────────────────────────┘
```

Two Docker networks:

- **`internal`** — no default route, no internet access. The agent, dashboard,
  and gateway run here.
- **`egress`** — has internet access. Only services that need to reach external
  APIs are attached to this network.

The gateway service is dual-homed (attached to both networks) so it can
receive inbound messages from Telegram/Slack/etc. and forward them to the
agent on the internal network.

## Compose Configuration

Override the default `docker-compose.yml` with a
`docker-compose.override.yml`:

```yaml
# docker-compose.override.yml
# Network egress isolation for production deployments.
#
# Usage:
#   HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
#
# This overrides network_mode: host with isolated Docker networks.

networks:
  internal:
    driver: bridge
    internal: true          # no default route, no internet
  egress:
    driver: bridge

services:
  gateway:
    network_mode: ""        # clear the host-mode default
    networks:
      - internal
      - egress              # needs outbound for Telegram, LLM APIs
    ports:
      - "127.0.0.1:9119:9119"   # dashboard proxy, localhost only

  dashboard:
    network_mode: ""
    networks:
      - internal            # internal only, no egress needed
```

### With an Egress Proxy (Recommended)

For tighter control, route all outbound traffic through an HTTP proxy with
an explicit allowlist:

```yaml
# docker-compose.override.yml (with egress proxy)

networks:
  internal:
    driver: bridge
    internal: true
  egress:
    driver: bridge

services:
  gateway:
    network_mode: ""
    networks:
      - internal
      - egress
    environment:
      - HTTP_PROXY=http://egress-proxy:3128
      - HTTPS_PROXY=http://egress-proxy:3128
      - NO_PROXY=hermes,hermes-dashboard,localhost

  dashboard:
    network_mode: ""
    networks:
      - internal

  egress-proxy:
    image: ubuntu/squid:6.10-24.04_edge
    networks:
      - egress
    volumes:
      - ./config/squid-allowlist.conf:/etc/squid/conf.d/allowlist.conf:ro
    restart: unless-stopped
```

Example `config/squid-allowlist.conf`:

```
# Only allow HTTPS CONNECT to these hosts
acl allowed_hosts dstdomain api.openai.com
acl allowed_hosts dstdomain api.anthropic.com
acl allowed_hosts dstdomain openrouter.ai
acl allowed_hosts dstdomain generativelanguage.googleapis.com
acl allowed_hosts dstdomain api.telegram.org
acl allowed_hosts dstdomain api.github.com
acl allowed_hosts dstdomain discord.com

http_access allow CONNECT allowed_hosts
http_access deny all
```

Adjust the allowlist to match your LLM provider and messaging platform.

## Cloud Browser Control Endpoints

Cloud browser providers return a Chrome DevTools Protocol (CDP) endpoint that
Hermes uses as a control channel. Provider-returned endpoints are not treated
like operator-supplied `browser.cdp_url` overrides:

- provider discovery and websocket URLs must use an expected HTTP(S) or WS(S)
  scheme, contain no URL userinfo credentials, and resolve only to public
  addresses;
- mixed public/private DNS answers, private, loopback, link-local, reserved,
  multicast, unspecified, CGNAT, and failed DNS resolution are rejected;
- discovery responses do not follow redirects and their decoded JSON body is
  capped at 64 KiB;
- the returned websocket URL is validated again; a different authority is
  accepted only when that provider explicitly declares cross-authority CDP
  discovery in its capability contract.

This is a fail-closed DNS preflight, not connection pinning. The discovery
HTTP client, CDP supervisor, stateless websocket client, and `agent-browser`
can resolve the hostname again when connecting. A connection-level validator
or egress relay is still required to close that DNS-rebinding window.

Explicit operator CDP overrides retain private and loopback support for local
Chrome workflows. They are recorded as operator-trusted, unpinned endpoints;
their HTTP discovery responses still use the same no-redirect, bounded parser.

## Camofox Browser Boundary

Camofox uses a REST control plane rather than CDP. Hermes classifies it from
the configured `CAMOFOX_URL` authority, not from the backend name:

- `localhost`, an address in `127.0.0.0/8`, and `::1` are co-resident control
  authorities;
- Docker aliases, `host.docker.internal`, LAN hosts, and remote authorities
  are `external-uncontrolled` and do not inherit the local-terminal SSRF
  exemption;
- an external Camofox receives the ordinary private/internal URL policy unless
  the operator explicitly enables private URLs; the cloud-metadata floor is
  unconditional for both co-resident and external Camofox.

Hermes validates the final main-frame URL returned by Camofox after navigate,
click, key press, and back actions. An unsafe landing is quarantined and page
content is withheld. If the REST response omits that URL, Hermes records the
state as unknown and does not substitute the requested URL as evidence.

The resulting session/tool metadata reports the execution location, network
boundary, REST control transport, and that page egress and DNS pinning are not
enforced. These checks govern only URLs visible at the REST action boundary.
They cannot constrain subresources, page WebSockets, DNS rebinding between the
check and browser connection, or browser-process egress. External deployments
need a mandatory outbound proxy, OS firewall, or egress relay for those
guarantees.

## Validating the Setup

After bringing up the stack, verify isolation:

```bash
# From the agent container: this should FAIL (no egress)
docker compose exec gateway \
  curl -sf --max-time 5 https://example.com && echo "FAIL: egress not blocked" || echo "OK: egress blocked"

# From the agent container: this should SUCCEED (internal network)
docker compose exec gateway \
  curl -sf --max-time 5 http://hermes-dashboard:9119/health && echo "OK: internal reachable" || echo "FAIL"

# If using egress proxy: this should SUCCEED (allowlisted)
docker compose exec gateway \
  curl -sf --max-time 5 --proxy http://egress-proxy:3128 https://api.openai.com/v1/models && echo "OK" || echo "FAIL"
```

## Limitations

- **DNS resolution:** The `internal` network can still resolve external DNS
  names unless you also run a local DNS resolver that blocks external queries.
  For most threat models this is acceptable since DNS resolution alone does not
  exfiltrate meaningful data.

- **Not a substitute for sandbox backends:** This guide isolates the agent
  *container's* network. If you use the default local terminal backend, tool
  commands execute inside the same container. For stronger isolation, combine
  network segmentation with a sandboxed terminal backend (Docker, Modal,
  Daytona).

- **Platform adapters need egress:** The gateway service needs outbound access
  to reach messaging platform APIs. If you add new platform adapters, add their
  API endpoints to the proxy allowlist.

## Related

- [SECURITY.md](../../SECURITY.md) — Hermes trust model and vulnerability reporting
- [Terminal backends](../../README.md) — sandboxed execution targets
- [docker-compose.yml](../../docker-compose.yml) — default compose configuration
