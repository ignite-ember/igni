# Token API

> Source: `src/auth/token.py`

A simple wrapper around signed JWT tokens for session authentication.

## Token

```python
@dataclass
class Token:
    user_id: str
    issued_at: datetime
    expires_at: datetime
```

Represents an authenticated session token issued to a user.

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `user_id` | `str` | Unique identifier for the authenticated user |
| `issued_at` | `datetime` | When the token was issued (UTC) |
| `expires_at` | `datetime` | When the token expires (UTC) |

### Methods

#### `issue`

```python
@classmethod
def issue(cls, user_id: str) -> Token
```

Factory method to issue a new token for a user.

**Parameters:**
- `user_id` (`str`) — The user identifier to encode in the token

**Returns:**
- `Token` — A new token instance with a 1-hour expiration

**Example:**
```python
token = Token.issue(user_id="user_123")
print(f"Token expires at {token.expires_at}")
```

## Related Modules

- **Credential Storage**: `src/ember_code/core/auth/credentials.py` — Persistent storage and validation of tokens
- **OAuth Client**: `src/ember_code/core/auth/client.py` — OTP login flow and token acquisition

## Notes

This module is intentionally minimal — it defines only the token data structure. Actual token signing, validation, and persistence are handled by the server-side API and the `credentials.py` client utilities.