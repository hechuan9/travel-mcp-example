# Travel MCP server — source excerpt

A source excerpt from my existing Travel application, published as an engineering work sample. This is the Python implementation used by that project, not a TypeScript payroll connector or a newly written demo.

## What to inspect

- `travel_ai/mcp_server.py`: MCP tools and Streamable HTTP transport, explicit operation allowlists, versioned changes and read-back verification.
- `travel_ai/oauth_provider.py`: OAuth authorization codes, resource/scope checks, hashed token storage, refresh rotation and revocation.
- `travel_ai/oauth_routes.py`: browser login/consent integration and OAuth routes.
- `travel_ai/main.py`: authenticated gateway, subject binding and backend credential separation.
- `tests/test_oauth_routes.py`, `tests/test_oauth_provider.py`, `tests/test_mcp.py`: synthetic end-to-end and negative tests.

The tests exercise OAuth/PKCE, token expiry/revocation, cross-client restrictions, subject-bound task access, rejected credentials and exact change execution. They mock the business backend and AI runner; no real customer accounts, travel records or production credentials are included.

## Run the tests

Requires Python 3.12+ and uv:

```sh
uv sync --frozen
uv run pytest -q
```

All tests use temporary state and synthetic credentials. They do not require live AWS, a payroll API or a Codex login.

## Integration boundary

This repository contains the MCP/AI gateway package and its tests. The private Travel business API, frontend, deployed infrastructure, credentials and runtime data are intentionally excluded. Running the full gateway against a real backend requires implementing that backend contract and configuring identities; this excerpt is not a standalone hosted travel product.

Travel binds a deployment to a household and scopes OAuth access to authorized household users. This is evidence of an actual MCP/OAuth implementation; it is not a claim that this code already implements the proposed payroll service’s partner-to-many-company authorization model. That model requires its own implementation and acceptance tests.

The optional AI gateway uses a Codex App Server adapter. Its process is mocked in tests. The MCP/OAuth code can be inspected independently of that runner.

## Provenance

Extracted from the existing Travel project at source commit `cefecc172c6a39a5c5f44f6f22685f2e06e8e5da`. Application source and tests are copied from that commit; the production hostname is replaced with `travel.example.test`. No other functional changes are made. Documentation here describes the excerpt rather than the full private deployment.
