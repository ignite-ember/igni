"""Token model — simple wrapper around a signed JWT."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Token:
    user_id: str
    issued_at: datetime
    expires_at: datetime

    @classmethod
    def issue(cls, user_id: str) -> "Token":
        from datetime import timedelta
        now = datetime.utcnow()
        return cls(user_id=user_id, issued_at=now, expires_at=now + timedelta(hours=1))
