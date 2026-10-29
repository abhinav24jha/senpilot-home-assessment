"""Deterministic ZIP packaging, response templates, and SMTP delivery."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
import zipfile

from .models import CATEGORIES, IncomingEmail, Interpretation, RetrievalResult, SizeLimitError


class DeliveryError(Exception):
    """A reply could not be safely prepared or accepted by SMTP."""


class _LimitedWriter:
    def __init__(self, file, limit: int):
        self.file, self.limit = file, limit

    def write(self, data):
        if self.file.tell() + len(data) > self.limit:
            raise SizeLimitError("The ZIP exceeds the configured attachment limit.")
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


def _unique_archive_filename(name: str, index: int, used: set[str]) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip(" .") or f"document-{index}"
    if name.casefold() in used:
        name = f"{index:02d}-{name}"
    while name.casefold() in used:
        name = "_" + name
    used.add(name.casefold())
    return name


def package_files(result: RetrievalResult, zip_path: Path,
                  max_zip_bytes: int = 15_000_000) -> Path:
    """Stream validated files into a bounded ZIP, then verify every archived hash."""
    if not result.files:
        raise ValueError("Cannot package an empty file selection")
    if len(result.files) > 10:
        raise ValueError("A response may contain at most ten documents")
    if max_zip_bytes <= 0:
        raise ValueError("ZIP limit must be positive")
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".zip-", suffix=".part", dir=zip_path.parent)
    expected: dict[str, str] = {}
    used: set[str] = set()
    try:
        with os.fdopen(fd, "w+b") as output:
            with zipfile.ZipFile(_LimitedWriter(output, max_zip_bytes), "w",
                                 compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for index, file in enumerate(result.files, 1):
                    name = _unique_archive_filename(file.filename, index, used)
                    digest = hashlib.sha256()
                    size = 0
                    with Path(file.path).open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
                        while chunk := source.read(128 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                            dest.write(chunk)
                    if size != file.size or digest.hexdigest() != file.sha256:
                        raise DeliveryError("A downloaded file changed before packaging.")
                    expected[name] = file.sha256
            output.flush()
            os.fsync(output.fileno())
        if Path(temporary).stat().st_size > max_zip_bytes:
            raise SizeLimitError("The ZIP exceeds the configured attachment limit.")
        with zipfile.ZipFile(temporary) as archive:
            if len(archive.infolist()) != len(expected):
                raise DeliveryError("ZIP entry count does not match the download manifest.")
            for name, checksum in expected.items():
                digest = hashlib.sha256()
                with archive.open(name) as file:
                    while chunk := file.read(128 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != checksum:
                    raise DeliveryError("ZIP integrity verification failed.")
        os.replace(temporary, zip_path)
        return zip_path
    finally:
        Path(temporary).unlink(missing_ok=True)


def _parse_single_address(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid email address")
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise ValueError("Exactly one email address is required")
    name, address = addresses[0]
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address):
        raise ValueError("A valid sender and recipient address are required")
    return address


def _single_line(value: str) -> str:
    return " ".join(str(value).splitlines()).strip()


def _format_retrieval_summary(result: RetrievalResult, *, has_attachment: bool) -> list[str]:
    """Render only observed metadata and counts; leave MIME construction to build_reply."""
    body = [f"Matter: {result.matter_number}"]
    labels = {
        "title": "Title",
        "status": "Status",
        "type": "Type",
        "category": "Category",
        "outcome": "Outcome",
        "date_received": "Date Received",
        "decision_date": "Decision Date",
    }
    for label, value in result.metadata.items():
        if label in {"date_label", "date_value", "source_url"}:
            continue
        if value:
            body.append(f"{_single_line(labels.get(label, label))}: {_single_line(value)}")
    if result.metadata.get("date_label") and result.metadata.get("date_value") and not (
            result.metadata["date_label"] == "Decision Date" and result.metadata.get("decision_date")):
        body.append(f"{_single_line(result.metadata['date_label'])}: {_single_line(result.metadata['date_value'])}")
    body.extend(["", "The database reports these category counts:"])
    for category in CATEGORIES:
        count = result.counts.get(category)
        body.append(f"- {category}: {count if count is not None else 'unknown'}")
    if all(category in result.counts for category in CATEGORIES):
        body.append(f"Total category entries: {sum(result.counts[c] for c in CATEGORIES)}.")
    body.append(
        "Key Documents also appear under Other Documents, so category entries are not a count of distinct files."
    )
    body.append("")
    if has_attachment:
        if not result.files:
            raise ValueError("Cannot attach a document ZIP for an empty result")
        body.append(
            f"I downloaded {len(result.files)} public file(s) from {result.document_type} and attached them as a ZIP."
        )
    elif not result.files:
        body.append(f"No downloadable files were returned for {result.document_type}. No ZIP is attached.")
    else:
        body.append(
            f"I retrieved {len(result.files)} file(s), but a ZIP could not be attached. Please contact the agent operator."
        )
    if result.warnings:
        body.extend(["", "Retrieval notes:"] + ["- " + _single_line(note) for note in result.warnings])
    source_url = result.metadata.get("source_url", "https://uarb.novascotia.ca/fmi/webd/UARB15")
    body.extend(["", "Source: " + _single_line(source_url)])
    return body


def build_reply(incoming: IncomingEmail, *, agent_from: str,
                interpretation: Interpretation | None = None,
                result: RetrievalResult | None = None,
                zip_path: Path | None = None,
                error: str | None = None) -> EmailMessage:
    """Build a reply solely from verified data; never infer facts with a model."""
    sender, recipient = _parse_single_address(agent_from), _parse_single_address(incoming.sender)
    if incoming.ignored_reason or (interpretation and interpretation.action == "ignore"):
        raise ValueError("Ignored messages should not receive automatic replies")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = sender, recipient
    subject = _single_line(incoming.subject)[:240] or "UARB document request"
    message["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    if re.fullmatch(r"<[^\s<>]+@[^\s<>]+>", incoming.message_id):
        message["In-Reply-To"] = incoming.message_id
        message["References"] = incoming.message_id

    body = ["Hello,", ""]
    if error:
        body.extend(["I could not complete this document request.", _single_line(error),
                     "No ZIP is attached. Please reply with a smaller selection or contact the agent operator."])
        zip_path = None
    elif interpretation and interpretation.action == "clarify":
        body.extend(["I need clarification before retrieving documents.",
                     _single_line(interpretation.reason),
                     "Please specify one matter number (for example M12205) and one document category:",
                     ", ".join(CATEGORIES) + "."])
        zip_path = None
    elif result:
        body.extend(_format_retrieval_summary(result, has_attachment=zip_path is not None))
    else:
        raise ValueError("A result, clarification, or error is required to build a reply")
    body.extend(["", "UARB document agent"])
    message.set_content("\n".join(body))
    if zip_path is not None:
        path = Path(zip_path)
        message.add_attachment(path.read_bytes(), maintype="application", subtype="zip", filename=path.name)
    return message


def save_reply(message: EmailMessage, path: Path,
               max_message_bytes: int = 22_000_000) -> Path:
    """Enforce the complete MIME size, including base64 overhead."""
    serialized = message.as_bytes(policy=policy.SMTP)
    if len(serialized) > max_message_bytes:
        raise SizeLimitError("The complete email exceeds the configured message-size limit.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".reply-", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def send_reply(message: EmailMessage, *, host: str, port: int,
               user: str, password: str, secure: bool = False) -> str:
    """SMTP acceptance is not inbox delivery. Callers persist 'sending' first."""
    if not host or not user or not password:
        raise DeliveryError("SMTP host and credentials are required.")
    sender = _parse_single_address(str(message["From"] or ""))
    recipient = _parse_single_address(str(message["To"] or ""))
    if message.get_all("Cc") or message.get_all("Bcc"):
        raise DeliveryError("Automatic replies support one recipient only.")
    context = ssl.create_default_context()
    try:
        if secure:
            connection = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            connection = smtplib.SMTP(host, port, timeout=30)
        with connection as smtp:
            smtp.ehlo()
            if not secure:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(user, password)
            refused = smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
            if refused:
                raise DeliveryError("SMTP did not accept the recipient.")
    except DeliveryError:
        raise
    except Exception as exc:
        # Raw SMTP exceptions can contain addresses or server details; keep logs bounded.
        raise DeliveryError(f"SMTP delivery could not be confirmed ({type(exc).__name__}).") from None
    return str(message["Message-ID"])
