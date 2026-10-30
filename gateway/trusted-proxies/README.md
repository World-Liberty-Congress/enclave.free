# Trusted proxy configuration

No forwarded client addresses are trusted by default. For a gateway behind a
known edge proxy, create a local `edge.conf` in this directory:

```nginx
# Replace the example with the gateway's actual immediate proxy peer.
set_real_ip_from 192.0.2.10;
real_ip_header X-Forwarded-For;
real_ip_recursive on;
```

Keep the gateway's host port bound to loopback. The edge proxy must overwrite
untrusted client-supplied forwarded headers. Trust only the exact known peers,
never all addresses or an entire private address range. With host Caddy and a
Docker-published loopback port, the peer can be the Docker bridge gateway rather
than `127.0.0.1`; inspect the actual network and request logs before configuring it.

Set `FORWARDED_ALLOW_IPS` in the Compose environment to the API gateway's exact
container IP so Uvicorn accepts the canonical client address Nginx forwards.
Leave it at its loopback default for deployments without this proxy chain.
After recreating the gateway, verify its address and update this setting before
recreating the core backend. Validate both hops with synthetic clients, including
a forged forwarded address, before exposing a changed topology.

Run `nginx -t` before reloading a running gateway. This directory is mounted read
only; local `.conf` files are deliberately excluded from Git.
