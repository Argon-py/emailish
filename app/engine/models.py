"""Result model and status vocabulary for the verification engine."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Status(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    CATCH_ALL = "CATCH_ALL"
    DISPOSABLE = "DISPOSABLE"
    ROLE_BASED = "ROLE_BASED"
    RISKY = "RISKY"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"


class Certainty(str, Enum):
    CONFIRMED = "CONFIRMED"   # direct, conclusive evidence decided the verdict
    UNCERTAIN = "UNCERTAIN"   # no conclusive evidence, or evidence is ambiguous


class SmtpStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CATCH_ALL = "catch_all"
    GREYLISTED = "greylisted"
    BLOCKED = "blocked"
    TEMP_ERROR = "temp_error"
    MAILBOX_FULL = "mailbox_full"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    PROTOCOL_ERROR = "protocol_error"
    NOT_CHECKED = "not_checked"
    UNAVAILABLE = "unavailable"


def yes_no(v: bool | None) -> str:
    if v is None:
        return "Unknown"
    return "Yes" if v else "No"


@dataclass
class VerificationResult:
    email: str                      # normalized address (or raw input when unparsable)
    email_status: Status
    sub_status: str
    certainty: Certainty
    confidence: int
    verification_reason: str
    syntax_valid: bool = False
    domain_valid: bool | None = None
    mx_found: bool | None = None
    disposable: bool = False
    free_email: bool = False
    role_based: bool = False
    smtp_status: SmtpStatus = SmtpStatus.NOT_CHECKED
    catch_all: bool | None = None
    did_you_mean: str = ""
    mx_host: str = ""
    mail_provider: str = ""
    smtp_code: str = ""
    verified_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Columns appended to the user's file, in this order.
    OUTPUT_COLUMNS = (
        "email_status", "syntax_valid", "domain_valid", "mx_found", "disposable",
        "free_email", "role_based", "smtp_status", "catch_all",
        "verification_reason", "confidence", "verified_at",
        # transparency extras
        "sub_status", "certainty", "did_you_mean", "mx_host", "mail_provider", "smtp_code",
    )

    def to_row(self) -> dict[str, str]:
        return {
            "email_status": self.email_status.value,
            "syntax_valid": yes_no(self.syntax_valid),
            "domain_valid": yes_no(self.domain_valid),
            "mx_found": yes_no(self.mx_found),
            "disposable": yes_no(self.disposable),
            "free_email": yes_no(self.free_email),
            "role_based": yes_no(self.role_based),
            "smtp_status": self.smtp_status.value,
            "catch_all": yes_no(self.catch_all),
            "verification_reason": self.verification_reason,
            "confidence": str(self.confidence),
            "verified_at": self.verified_at,
            "sub_status": self.sub_status,
            "certainty": self.certainty.value,
            "did_you_mean": self.did_you_mean,
            "mx_host": self.mx_host,
            "mail_provider": self.mail_provider,
            "smtp_code": self.smtp_code,
        }

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("email_status", "certainty", "smtp_status"):
            d[k] = getattr(self, k).value
        return d
