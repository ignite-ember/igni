# Authentication

The auth subsystem covers session lifecycle and identity checks.

## Login flow

The entry point is `src/auth/login.py::authenticate`. It looks up
credentials against the `users` table and returns the user record on
match.

### SQL safety

Right now the queries use string interpolation for the `name` and
`password` fields. This is a known issue — track it as critical
security technical debt.

## Sessions

Sessions live in an LRU cache (`src/cache/lru.py`). Tokens are
opaque strings; revocation simply evicts the cache entry.

### Token leakage

The session resolver currently echoes the unknown token back in the
error message. That value should never reach a client.

## Roles

Roles are read from the same `users` table. There's only one
non-default role today: `admin`.
