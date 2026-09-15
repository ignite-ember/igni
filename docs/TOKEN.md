# Credentials

> Source: `src/ember_code/core/auth/credentials.py`

**What this document used to say.** It described a `Token` dataclass
with `user_id` / `issued_at` / `expires_at` and a `Token.issue()`
factory that minted a session token with a one-hour expiry, sourced from
a `token` module in an `auth` package at the top of the source tree.

None of that exists. There is no `Token` class anywhere in the CLI, no
such package, and the CLI does not issue tokens at all — the igni server
does. The document survived the module it described because nothing
checked it; `tests/test_the_docs_name_real_things.py` does now, which is
also why the paragraph above describes those dead paths rather than
quoting them. A rule that let an explanation of a dead path quote the
dead path would let anything quote it.

## What actually happens

The CLI **receives** a token and **stores** it. It never mints one and
never validates a signature: the server that issued it is the only thing
that can, and every request carries the token for the server to check.

Four things live in `credentials.py`:

| | |
|---|---|
| `Credentials` | The stored token plus identity and expiry metadata. `Credentials.new()` owns the `created_at` / `expires_at` arithmetic; `is_expired()` owns the check |
| `CredentialsStore` | File IO for one credential file — `save` / `load` / `clear` as methods on an instance rather than free functions taking a path |
| `CloudCredentials` | A read-only view: one file read and one JWT decode, cached per instance |
| `CloudCredentials.empty()` | A real instance whose load is a no-op, so "not signed in" is an object rather than `None` |

The JWT claims are decoded by `JwtClaims` in
`src/ember_code/core/auth/schemas.py`. Decoding is not verification —
the CLI reads the claims to show you which organisation you are in, and
takes no security decision on them.

## Where the file is

`~/.igni/credentials.json` by default, from
`settings.auth.credentials_file`. The directory name is
`paths.CONFIG_DIR`; `~/.ember/` is still read when the new one is absent
so an install that predates the rename finds its credentials rather than
looking brand new.

## How a token gets there

`src/ember_code/core/auth/portal_client.py` runs the browser-callback
flow: it opens the portal at a URL derived from `api_url`, listens on a
loopback port for the one-time code the portal sends back, and redeems
that code against the server for the token it stores.

Both ends of that come from one setting. `api_url` has **no default** —
see `src/ember_code/core/config/endpoint.py` for why, and for what the
CLI says instead of building a malformed request.

## Expiry

Thirty days, decided on the server side and recorded there as DEC-15.
The CLI's `is_expired()` is a courtesy check so it can tell you to sign
in again rather than send a request it knows will be refused; it is not
a control, and a token can also be revoked before it expires — the
server compares every token's `iat` against a per-user watermark.
