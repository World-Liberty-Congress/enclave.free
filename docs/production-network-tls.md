# Production Network and TLS Guidance

Internet-exposed deployments should terminate TLS before requests reach Enclave and should preserve enough proxy metadata for the application to apply security headers correctly.

## Recommended Posture

- Serve public frontend and backend origins over HTTPS.
- Set `INSTANCE_URL`, `FRONTEND_URL`, `API_BASE_URL`, and `ADMIN_BASE_URL` to their public `https://` origins.
- Enable `FORCE_HTTPS=true` after TLS is working.
- Keep `HSTS_MAX_AGE` at `31536000` or higher for production domains.
- Set `TRUSTED_PROXIES` to describe the TLS-terminating reverse proxy or edge provider.
- Keep `CORS_ORIGINS` explicit. Do not use `*` with credentialed auth.

## Reverse Proxy Boundary

Enclave can validate visible configuration and apply HSTS when requests arrive as HTTPS or when the reverse proxy forwards `X-Forwarded-Proto: https`. It cannot prove every Deployment's edge routing, certificate renewal, or proxy header policy from inside the container. Operators remain responsible for the reverse proxy, DNS, certificates, load balancer, and firewall rules.

## Client identity for rate limits

For an edge proxy → Nginx gateway → core backend deployment, configure both
trust boundaries using [the gateway proxy instructions](../gateway/trusted-proxies/README.md).
The gateway trusts only locally configured exact edge peers and forwards one
canonical client address. Set `FORWARDED_ALLOW_IPS` to the gateway's exact container
IP for Uvicorn. `TRUSTED_PROXIES` is deployment metadata and does not configure
either runtime trust boundary. Verify the gateway address after recreation and
update the backend setting before restarting it.

Magic-link requests, token verification, and admin authentication have independent
gateway allowances. Application email-send and admin-auth counters are namespaced
separately. Rate-limited auth responses include `Retry-After`; clients should wait
and retry the same verification link rather than request another email.

## External Provider Endpoints

External Model Provider and embedding endpoints should use HTTPS in production. Internal Compose endpoints such as `http://tinfoil-proxy:8089/v1`, `http://searxng:8080`, and `http://qdrant:6333` are local service-network exceptions and should not be exposed publicly.

## Validation Evidence

`POST /admin/deployment/config/validate` reports production errors for visible public HTTP origins, missing HTTPS enforcement, weak HSTS, and external provider endpoints configured with plain HTTP. It reports a warning when `TRUSTED_PROXIES` is empty in production because the exact proxy chain is a Deployment responsibility.

