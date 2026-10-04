"""Domain errors carry their own status code and their own words.

One handler in ``main`` turns any of these into ``{"detail": "<sentence>"}``, so
every raise site writes a full sentence — the message *is* what the user reads.
An ``Unauthorized`` may add ``fields`` beside it, for a program to act on.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base for anything the user could reasonably have caused."""

    status_code = 400


class Unauthorized(DomainError):
    status_code = 401

    def __init__(
        self,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        fields: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        #: Response headers this particular refusal wants. `WWW-Authenticate`
        #: is the one that matters: it is how HTTP says *which* credential a
        #: route wants, and without it a program has to guess.
        self.headers = headers or {}
        #: Facts the answer carries beside `detail`, for a refusal the client
        #: has to act on rather than only show: `key_replaced` sends the
        #: sign-in screen to the recovery code, and tells a signed-in screen
        #: why no code was checked (#287). The sentence stays the thing a
        #: person reads; a field is for the program.
        self.fields = fields or {}


#: The header that tells a client a 401 refused a *proof* -- a password, a
#: code, a step-up grant -- and not the session. Without it every 401 reads as
#: "you are signed out", and a mistyped password in a step-up form threw the
#: person back to the sign-in page with their session still good.
PROOF_REFUSED_HEADER = "X-Refused"


class ProofRefused(Unauthorized):
    """A 401 for a wrong password, code or grant, from somebody still signed in.

    It takes `fields` like any `Unauthorized`: a step-up refused because the
    server's key cannot open the member's authenticator says `key_replaced`
    (#287), and the session survives it like any other refused proof.
    """

    def __init__(self, message: str, *, fields: dict[str, object] | None = None) -> None:
        super().__init__(message, headers={PROOF_REFUSED_HEADER: "proof"}, fields=fields)


class Forbidden(DomainError):
    status_code = 403


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class TooLarge(DomainError):
    """The body is bigger than this instance is willing to hold in memory.

    413 rather than 409: the request is well formed and conflicts with nothing,
    it is simply too big, and a client can tell the difference.
    """

    status_code = 413


class ValidationError(DomainError):
    status_code = 422


class CurrencyMismatch(ValidationError):
    """Two sides of a transfer disagree on currency and no rate was given."""


class AuditError(DomainError):
    """A write reached an audited table with no batch open.

    Not a user error — a programming one — but it answers with a status rather
    than a bare 500 so the failure is legible in a test and in a log.
    """

    status_code = 500


class NoOpenBatch(AuditError):
    """A write reached an audited table with no batch open."""


class CrossHouseholdChange(AuditError):
    """A batch filed under one household tried to write another's row."""


class BulkStatementForbidden(AuditError):
    """A Core UPDATE/DELETE was aimed at an audited table.

    Bulk statements bypass ORM events, so the audit log would never see them.
    Load the rows and change them.
    """


class TooManyAttempts(DomainError):
    """Rate limited. Carries how long to wait, so the client can say so."""

    status_code = 429

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after
