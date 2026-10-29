"""Configuration is explicit; credentials never enter job manifests."""
from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

@dataclass
class Settings:
    data_dir: Path = Path("data")
    agent_from: str = "agent@example.test"
    typesafe_api_key: str = ""
    typesafe_model: str = "jev-1.14.0"
    confidence_threshold: float = .8
    probability_threshold: float = .9
    max_email_bytes: int = 2_000_000
    max_download_bytes: int = 100_000_000
    max_zip_bytes: int = 15_000_000
    max_message_bytes: int = 22_000_000
    download_workers: int = 1
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_secure: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_secure: bool = True
    imap_mailbox: str = "INBOX"
    imap_recipient: str = ""
    poll_seconds: int = 30
    allowed_senders: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env_file: str | Path = ".env"):
        load_dotenv(env_file, override=False)
        def integer(key, default):
            value = int(os.getenv(key, str(default)))
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            return value
        def boolean(key, default):
            value = os.getenv(key, str(default)).lower()
            if value not in ("true", "false", "1", "0"):
                raise ValueError(f"{key} must be true or false")
            return value in ("true", "1")
        result = cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            agent_from=os.getenv("AGENT_FROM", "agent@example.test"),
            typesafe_api_key=os.getenv("TYPESAFE_API_KEY", ""),
            typesafe_model=os.getenv("TYPESAFE_MODEL", "jev-1.14.0"),
            confidence_threshold=float(os.getenv("JEV_CONFIDENCE_THRESHOLD", ".8")),
            probability_threshold=float(os.getenv("JEV_PROBABILITY_THRESHOLD", ".9")),
            max_email_bytes=integer("MAX_EMAIL_BYTES", 2_000_000),
            max_download_bytes=integer("MAX_DOWNLOAD_BYTES", 100_000_000),
            max_zip_bytes=integer("MAX_ZIP_BYTES", 15_000_000),
            max_message_bytes=integer("MAX_MESSAGE_BYTES", 22_000_000),
            download_workers=integer("DOWNLOAD_WORKERS", 1),
            smtp_host=os.getenv("SMTP_HOST", ""), smtp_port=integer("SMTP_PORT", 587),
            smtp_user=os.getenv("SMTP_USER", ""), smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_secure=boolean("SMTP_SECURE", False),
            imap_host=os.getenv("IMAP_HOST", ""), imap_port=integer("IMAP_PORT", 993),
            imap_user=os.getenv("IMAP_USER", ""), imap_password=os.getenv("IMAP_PASSWORD", ""),
            imap_secure=boolean("IMAP_SECURE", True),
            imap_mailbox=os.getenv("IMAP_MAILBOX", "INBOX"), poll_seconds=integer("POLL_SECONDS", 30),
            imap_recipient=os.getenv("IMAP_RECIPIENT", ""),
            allowed_senders=tuple(x.strip().lower() for x in os.getenv("ALLOWED_SENDERS", "").split(",") if x.strip()),
        )
        for value in (result.confidence_threshold, result.probability_threshold):
            if not 0 <= value <= 1:
                raise ValueError("Jev thresholds must be between zero and one")
        if result.download_workers != 1:
            raise ValueError("The Python release supports DOWNLOAD_WORKERS=1; parallelism was slower for the benchmark workload")
        return result

    def require_smtp(self):
        if not self.smtp_host or not self.smtp_user or not self.smtp_password:
            raise ValueError("Configure SMTP_HOST, SMTP_USER, and SMTP_PASSWORD before sending")
        if self.agent_from.endswith("@example.test"):
            raise ValueError("Configure AGENT_FROM with your sending mailbox before sending")

    def require_imap(self):
        if not self.imap_host or not self.imap_user or not self.imap_password:
            raise ValueError("Configure IMAP_HOST, IMAP_USER, and IMAP_PASSWORD before watching")
