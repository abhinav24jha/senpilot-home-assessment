"""Coordinate email interpretation, retrieval, reply preparation, and delivery.

Jobs persist at component boundaries so repeated mail does not repeat downloads
or automatically resend an uncertain SMTP delivery.
"""
from dataclasses import asdict
from email import policy
from email.parser import BytesParser
from pathlib import Path
import hashlib
import shutil
import time

from .config import Settings
from .jobs import JobStore, atomic_json, now
from .models import DocumentRequest, RetrievalResult, SizeLimitError
from .interpret import parse_email, interpret
from .uarb import retrieve
from .delivery import package_files, build_reply, save_reply, send_reply


class Agent:
    """Run one email through the pipeline, using injectable external adapters."""

    def __init__(self, settings: Settings, *, interpreter=interpret, retriever=retrieve, sender=send_reply):
        self.settings = settings
        self.jobs = JobStore(settings.data_dir)
        self.interpreter, self.retriever, self.sender = interpreter, retriever, sender

    def process(self, raw: bytes, *, send=False, retry=False):
        """Prepare a reply, optionally send it, and return the persisted job."""
        settings = self.settings
        if send:
            settings.require_smtp()
        incoming = parse_email(raw, max_bytes=settings.max_email_bytes, agent_address=settings.agent_from)
        job_id = self.jobs.identity(incoming)
        existing = self.jobs.read(job_id)
        if existing:
            if existing["status"] in ("sent", "ignored", "sending", "delivery_unknown"):
                return existing
            if existing["status"] == "prepared" and not (retry and existing.get("outcome") == "error"):
                return self.deliver(existing) if send else existing
            if not retry:
                return existing
        directory = self.jobs.path(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        job = {
            "id": job_id,
            "status": "received",
            "created_at": (existing or {}).get("created_at", now()),
            "message_id": incoming.message_id,
            "sender": incoming.sender,
            "subject": incoming.subject,
            "attempts": (existing or {}).get("attempts", 0) + 1,
        }
        self.jobs.save(job)
        if existing:
            # A retry can resolve differently. Never present artifacts left from
            # the previous attempt as the result of this one.
            for filename in ("interpretation.json", "request.json", "retrieval.json", "documents.zip", "response.eml"):
                (directory / filename).unlink(missing_ok=True)
            if (directory / "downloads").exists():
                shutil.rmtree(directory / "downloads")
        if incoming.ignored_reason or (settings.allowed_senders and incoming.sender.lower() not in settings.allowed_senders):
            job.update(status="ignored", reason=incoming.ignored_reason or "Sender not in configured allowlist")
            self.jobs.save(job)
            return job
        started = time.monotonic()
        interpretation = None
        result = None
        error = None
        zip_path = None
        try:
            interpretation = self.interpreter(incoming, api_key=settings.typesafe_api_key,
                model=settings.typesafe_model, confidence_threshold=settings.confidence_threshold,
                probability_threshold=settings.probability_threshold)
            atomic_json(directory / "interpretation.json", interpretation)
            if interpretation.action == "ignore":
                job.update(status="ignored", reason=interpretation.reason)
                self.jobs.save(job)
                return job
            if interpretation.action == "retrieve":
                if interpretation.request is None:
                    raise ValueError("Retriever route requires a validated request")
                result, zip_path = self._retrieve_and_package(interpretation.request, directory, job)
        except Exception as exc:
            # Component errors contain no credential-bearing request objects.
            error = str(exc)[:500] if isinstance(exc, SizeLimitError) else "The request could not be completed. Please check the request and try again."
            job["error_type"] = type(exc).__name__
            job["error"] = error
            zip_path = None
        try:
            message = build_reply(incoming, agent_from=settings.agent_from,
                interpretation=interpretation, result=result, zip_path=zip_path, error=error)
            response_path = directory / "response.eml"
            try:
                save_reply(message, response_path, settings.max_message_bytes)
            except SizeLimitError:
                error = "The complete email exceeds the configured message size limit. Please request a smaller selection."
                message = build_reply(incoming, agent_from=settings.agent_from, result=result, error=error)
                save_reply(message, response_path, settings.max_message_bytes)
                job["error"] = error
            job.update(status="prepared", response_path=str(response_path.resolve()),
                response_sha256=hashlib.sha256(response_path.read_bytes()).hexdigest(),
                elapsed_seconds=round(time.monotonic()-started, 3),
                outcome="error" if error else ("clarification" if interpretation and interpretation.action == "clarify" else "success"))
            self.jobs.save(job)
        except Exception as exc:
            job.update(status="failed", error_type=type(exc).__name__)
            self.jobs.save(job)
            return job
        return self.deliver(job) if send else job

    def _retrieve_and_package(self, request: DocumentRequest, directory: Path,
                              job: dict) -> tuple[RetrievalResult, Path | None]:
        """Persist the selection and retrieve a verified attachment for the reply."""
        atomic_json(directory / "request.json", request)
        job.update(status="fetching", request=asdict(request))
        self.jobs.save(job)

        downloads = directory / "downloads"
        downloads.mkdir()
        result = self.retriever(
            request,
            downloads,
            max_download_bytes=self.settings.max_download_bytes,
            workers=self.settings.download_workers,
        )
        atomic_json(directory / "retrieval.json", result)
        zip_path = None
        if result.files:
            zip_path = package_files(result, directory / "documents.zip", self.settings.max_zip_bytes)
        job.update(status="packaged", file_count=len(result.files), counts=result.counts)
        self.jobs.save(job)
        return result, zip_path

    def deliver(self, job):
        """Send the exact prepared message once, preserving uncertain outcomes."""
        self.settings.require_smtp()
        if job["status"] != "prepared":
            raise ValueError("Only prepared messages may be sent; uncertain deliveries require manual review")
        path = Path(job["response_path"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != job["response_sha256"]:
            raise ValueError("Prepared email changed; refusing to send")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        job["status"] = "sending"
        self.jobs.save(job)
        try:
            receipt = self.sender(message, host=self.settings.smtp_host, port=self.settings.smtp_port,
                user=self.settings.smtp_user, password=self.settings.smtp_password, secure=self.settings.smtp_secure)
        except Exception as exc:
            job.update(status="delivery_unknown", delivery_error_type=type(exc).__name__)
        else:
            job.update(status="sent", smtp_receipt=str(receipt), sent_at=now())
        self.jobs.save(job)
        return job
