"""Small IMAP adapter. Reading a message never acknowledges it automatically."""
from __future__ import annotations

import imaplib
import re
import ssl


class MailboxError(Exception):
    """The configured mailbox could not complete an IMAP operation."""


class Mailbox:
    """Keep retrieval and acknowledgement on the same authenticated IMAP connection."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 mailbox: str = "INBOX", secure: bool = True,
                 max_email_bytes: int = 2_000_000, recipient: str = ""):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.mailbox, self.secure, self.max_email_bytes = mailbox, secure, max_email_bytes
        self.recipient = recipient
        self.connection = None
        self.uidvalidity: str | None = None
        self.skipped: list[tuple[str, str]] = []

    def __enter__(self):
        if not self.host or not self.user or not self.password:
            raise MailboxError("IMAP host and credentials are required.")
        stage = "connection"
        try:
            if self.secure:
                self.connection = imaplib.IMAP4_SSL(self.host, self.port,
                    ssl_context=ssl.create_default_context(), timeout=30)
            else:
                self.connection = imaplib.IMAP4(self.host, self.port, timeout=30)
                self.connection.starttls(ssl_context=ssl.create_default_context())
            stage = "login"
            self.connection.login(self.user, self.password)
            stage = "folder selection"
            status, _ = self.connection.select(self.mailbox, readonly=False)
            if status != "OK":
                raise MailboxError("Could not select the configured mailbox.")
            _, values = self.connection.response("UIDVALIDITY")
            if values and values[0]:
                self.uidvalidity = values[0].decode("ascii", errors="replace")
            return self
        except (imaplib.IMAP4.error, OSError, ValueError, MailboxError) as exc:
            self.close()
            if isinstance(exc, MailboxError):
                raise
            if isinstance(exc, imaplib.IMAP4.error) and stage == "login":
                raise MailboxError(
                    "The mail server rejected the login. For Gmail, use a Google app password "
                    "created in the SAME account as the email address entered here, not the "
                    "normal Gmail password. Check that 2-Step Verification is enabled."
                ) from None
            raise MailboxError(f"IMAP {stage} failed ({type(exc).__name__}).") from None

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.connection is not None:
            try:
                self.connection.logout()
            except Exception:
                pass
            self.connection = None

    def _require_connection(self):
        if self.connection is None:
            raise MailboxError("Mailbox is not connected.")
        return self.connection

    def fetch_unseen(self, limit: int = 20) -> list[tuple[str, bytes]]:
        """Return bounded unread messages without changing their Seen flag."""
        connection = self._require_connection()
        criteria = ["UNSEEN"]
        if self.recipient:
            if not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", self.recipient):
                raise ValueError("Invalid configured IMAP recipient")
            criteria.extend(["TO", '"' + self.recipient + '"'])
        status, values = connection.uid("search", None, *criteria)
        if status != "OK":
            raise MailboxError("IMAP search failed.")
        result = []
        self.skipped = []
        for uid_bytes in (values[0] if values and values[0] else b"").split():
            if len(result) >= limit:
                break
            uid = uid_bytes.decode("ascii")
            if not uid.isdigit():
                continue
            status, sizes = connection.uid("fetch", uid, "(RFC822.SIZE)")
            if status != "OK":
                continue
            description = b" ".join(item for item in sizes if isinstance(item, bytes))
            size_match = re.search(rb"RFC822.SIZE\s+(\d+)", description, re.I)
            if not size_match:
                self.skipped.append((uid, "Message size unavailable; body not fetched."))
                continue
            if int(size_match.group(1)) > self.max_email_bytes:
                self.skipped.append((uid, "Incoming message exceeds the configured size limit."))
                continue
            status, parts = connection.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = b"".join(item[1] for item in parts if isinstance(item, tuple) and isinstance(item[1], bytes))
            if not raw or len(raw) > self.max_email_bytes:
                self.skipped.append((uid, "Incoming message is empty or exceeds the configured size limit."))
                continue
            result.append((uid, raw))
        return result

    def mark_seen(self, uid: str):
        """Acknowledge a message only after its job has reached a durable state."""
        if not str(uid).isdigit():
            raise ValueError("Invalid IMAP UID")
        status, _ = self._require_connection().uid("store", str(uid), "+FLAGS.SILENT", r"(\Seen)")
        if status != "OK":
            raise MailboxError("Could not acknowledge the message.")


def fetch_unseen(*, host: str, port: int, user: str, password: str,
                 mailbox: str = "INBOX", secure: bool = True,
                 max_email_bytes: int = 2_000_000, limit: int = 20) -> list[tuple[str, bytes]]:
    with Mailbox(host, port, user, password, mailbox, secure, max_email_bytes) as client:
        return client.fetch_unseen(limit)


def mark_seen(uid: str, *, host: str, port: int, user: str, password: str,
              mailbox: str = "INBOX", secure: bool = True,
              expected_uidvalidity: str | None = None):
    with Mailbox(host, port, user, password, mailbox, secure) as client:
        if expected_uidvalidity is not None and client.uidvalidity != expected_uidvalidity:
            raise MailboxError("Mailbox UIDVALIDITY changed; refusing to acknowledge an unrelated message.")
        client.mark_seen(uid)
