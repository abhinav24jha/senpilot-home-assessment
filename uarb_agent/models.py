"""Shared request, interpretation, and retrieval records passed between components."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

CATEGORIES = ("Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings")


@dataclass
class IncomingEmail:
    message_id: str
    sender: str
    subject: str
    body: str
    ignored_reason: str | None = None


@dataclass
class DocumentRequest:
    """A single, validated matter/category selection accepted by the retriever."""

    matter_number: str
    document_type: str
    max_documents: int = 10
    def __post_init__(self):
        if not re.fullmatch(r"M[0-9]{5}", self.matter_number):
            raise ValueError("Invalid matter number")
        if self.document_type not in CATEGORIES:
            raise ValueError("Invalid document category")
        if type(self.max_documents) is not int or not 1 <= self.max_documents <= 10:
            raise ValueError("Document limit must be 1–10")


@dataclass
class Interpretation:
    action: Literal["retrieve", "clarify", "ignore"]
    request: DocumentRequest | None = None
    reason: str = ""
    audit: dict[str, object] = field(default_factory=dict)


@dataclass
class DownloadedFile:
    filename: str
    path: Path
    size: int
    sha256: str
    source_id: str = ""


@dataclass
class RetrievalResult:
    matter_number: str
    document_type: str
    metadata: dict[str, str]
    counts: dict[str, int]
    files: list[DownloadedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RetrievalError(Exception):
    """Retrieval failed without a trustworthy complete result."""


class SizeLimitError(RetrievalError):
    """Configured download or attachment size budget exceeded."""
