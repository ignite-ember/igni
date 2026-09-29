# Session handling

## Overview

The service issues a signed JWT on sign-in. The token is read on every
request and is opaque to the client. It expires after 15 minutes, and
is refreshed silently by an httpOnly cookie with SameSite=Lax, so a
returning user is not sent back to the sign-in form.

Revocation is checked on refresh only. The access token is not
consulted against a denylist, because that read was what fell over
first under load.

## What the client stores

- The refresh cookie, set with HttpOnly, Secure, and SameSite=Strict
- A CSRF token, mirrored in a meta tag
- The access token, in memory only
- Nothing else

## Request flow

1. The client reads the JWT from storage (e.g., localStorage or memory) and attaches it as the `Authorization: Bearer` header on every API request.
2. The edge proxy forwards it unchanged
3. The service verifies the signature
4. The handler runs

## Configuration

```yaml
session:
  access_ttl_minutes: 15
  refresh_ttl_days: 14
  cookie_name: igni_rt
  same_site: lax
  denylist_check: on_refresh
```

## Limits

| Setting | Value | Notes |
| --- | --- | --- |
| Max sessions per user | 10 | Oldest evicted |
| Cookie size | 4 KB | Browser limit |
| Denylist lookups | 1 per refresh | Redis |

> Sessions are not an audit trail. If you need to know what somebody
> did, read the audit log; the sessions table only knows who is
> currently signed in.

## Operational notes

Rotating the signing key invalidates every session. Do it during a
maintenance window, and tell support first — the queue fills with
"why was I logged out" within about ten minutes.

Session data is not replicated across regions. A user who moves
between regions mid-session will be asked to sign in again. This is
accepted for now.

## Migration

Existing session cookies keep working until they expire. The service
accepts both shapes for one release, then drops the cookie path.

## Open questions

- Should the access TTL be shorter for admin accounts?