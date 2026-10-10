"""Exceptions raised across the artifact boundary.

Their own module because both the session manager (which raises) and
the agent toolkit (which catches and turns them into an answerable
question) need them. Importing the session from the toolkit to reach
one exception class is a cycle, and a cycle is how "one shared type"
becomes "two divergent copies".
"""

from __future__ import annotations


class SourcesRequired(Exception):
    """Raised when a write arrives without a source declaration.

    Carries the session's captured reads so the caller can show the
    agent exactly what it is being asked to confirm — an error that
    says "cite your sources" without saying which ones are available
    just produces a guess.
    """

    def __init__(self, message: str, *, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class DeleteRefused(Exception):
    """A delete that did not happen, with the sentence to tell the user.

    Raised rather than returned as ``False`` because ``False`` already
    means "no artifact matches", and these are a different answer: the
    document exists, and removing only this machine's copy would tell
    the user it was gone while it stayed on the server, searchable by
    everyone it was shared with.
    """
