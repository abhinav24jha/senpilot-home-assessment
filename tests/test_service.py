from email import policy
from email.parser import BytesParser
import hashlib
import io
import zipfile

import pytest

from uarb_agent.config import Settings
from uarb_agent.models import CATEGORIES, DocumentRequest, DownloadedFile, Interpretation, RetrievalResult, SizeLimitError
from uarb_agent.service import Agent

RAW = b"From: alex@example.test\r\nTo: agent@example.test\r\nSubject: Need the documents\r\nMessage-ID: <one@example.test>\r\n\r\nPlease send Other Documents for M12205."


def settings(tmp_path):
    return Settings(data_dir=tmp_path, agent_from="agent@example.org", typesafe_api_key="test-only",
                    smtp_host="smtp.example.org", smtp_user="agent", smtp_password="test-only")


def interpretation(email, **kwargs):
    return Interpretation("retrieve", DocumentRequest("M12205", "Other Documents"))


def retrieval(request, directory, **kwargs):
    data = b"%PDF-1.4\nfixture bytes for packaging integration\n%%EOF"
    file = directory / "document.pdf"
    file.write_bytes(data)
    return RetrievalResult(request.matter_number, request.document_type, {"title": "Example matter"},
                           dict.fromkeys(CATEGORIES, 1),
                           [DownloadedFile(file.name, file, len(data), hashlib.sha256(data).hexdigest())])


def test_complete_flow_and_duplicate_do_not_repeat_work(tmp_path):
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return retrieval(*args, **kwargs)
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=counted)
    first = agent.process(RAW)
    assert first["status"] == "prepared" and first["outcome"] == "success"
    message = BytesParser(policy=policy.default).parsebytes((agent.jobs.path(first["id"]) / "response.eml").read_bytes())
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_content_type() == "application/zip"
    with zipfile.ZipFile(io.BytesIO(attachments[0].get_payload(decode=True))) as archive:
        assert archive.namelist() == ["document.pdf"]
        assert archive.testzip() is None
    assert agent.process(RAW)["id"] == first["id"]
    assert calls == [1]


def test_clarification_never_fetches(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Ambiguous request must not fetch documents")
    agent = Agent(settings(tmp_path), interpreter=lambda *a, **k: Interpretation("clarify", reason="Which matter?"), retriever=forbidden)
    job = agent.process(RAW)
    assert job["outcome"] == "clarification"
    assert not (agent.jobs.path(job["id"]) / "documents.zip").exists()


def test_uncertain_smtp_is_never_automatically_sent_twice(tmp_path):
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("connection dropped after DATA")
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=retrieval, sender=uncertain)
    job = agent.process(RAW, send=True)
    assert job["status"] == "delivery_unknown"
    assert agent.process(RAW, send=True, retry=True)["status"] == "delivery_unknown"
    assert calls == [1]


def test_prepared_reply_can_be_sent_without_retrieval(tmp_path):
    calls = []
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=retrieval,
                  sender=lambda *a, **k: calls.append(1) or "smtp-accepted")
    job = agent.process(RAW)
    assert job["status"] == "prepared"
    assert agent.process(RAW, send=True)["status"] == "sent"
    assert agent.process(RAW, send=True)["status"] == "sent"
    assert calls == [1]


def test_size_limit_produces_error_reply_and_explicit_retry_can_recover(tmp_path):
    def too_large(*args, **kwargs):
        raise SizeLimitError("Documents exceed the download limit")
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=too_large)
    job = agent.process(RAW)
    assert job["status"] == "prepared" and job["outcome"] == "error"
    assert not (agent.jobs.path(job["id"]) / "documents.zip").exists()
    agent.retriever = retrieval
    retried = agent.process(RAW, retry=True)
    assert retried["outcome"] == "success" and retried["attempts"] == 2


def test_changed_prepared_reply_is_not_sent(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Changed message must not reach SMTP")
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=retrieval, sender=forbidden)
    job = agent.process(RAW)
    (agent.jobs.path(job["id"]) / "response.eml").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        agent.deliver(job)


def test_interrupted_retrieval_requires_explicit_retry(tmp_path):
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return retrieval(*args, **kwargs)
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=counted)
    job = agent.process(RAW)
    job['status'] = 'fetching'
    agent.jobs.save(job)
    assert agent.process(RAW)['status'] == 'fetching'
    assert calls == [1]
    assert agent.process(RAW, retry=True)['status'] == 'prepared'
    assert calls == [1, 1]


def test_interrupted_sending_does_not_resend_on_restart(tmp_path):
    calls = []
    agent = Agent(settings(tmp_path), interpreter=interpretation, retriever=retrieval,
                  sender=lambda *a, **k: calls.append(1))
    job = agent.process(RAW)
    job['status'] = 'sending'
    agent.jobs.save(job)
    assert agent.process(RAW, send=True, retry=True)['status'] == 'sending'
    assert not calls


def test_disallowed_sender_does_not_call_provider(tmp_path):
    config = settings(tmp_path)
    config.allowed_senders = ('permitted@example.test',)
    def forbidden(*args, **kwargs):
        pytest.fail('Disallowed mail must not call Jev or the portal')
    agent = Agent(config, interpreter=forbidden, retriever=forbidden)
    assert agent.process(RAW)['status'] == 'ignored'


def test_provider_failure_prepares_error_without_exposing_secret(tmp_path):
    def unavailable(*args, **kwargs):
        raise RuntimeError('secret-provider-key')
    agent = Agent(settings(tmp_path), interpreter=unavailable, retriever=retrieval)
    job = agent.process(RAW)
    directory = agent.jobs.path(job['id'])
    assert job['status'] == 'prepared' and job['outcome'] == 'error'
    assert 'secret-provider-key' not in (directory / 'response.eml').read_text()
    assert 'secret-provider-key' not in (directory / 'job.json').read_text()
    assert not (directory / 'documents.zip').exists()


def test_zip_budget_returns_no_partial_attachment(tmp_path):
    config = settings(tmp_path)
    config.max_zip_bytes = 20
    agent = Agent(config, interpreter=interpretation, retriever=retrieval)
    job = agent.process(RAW)
    assert job['status'] == 'prepared' and job['outcome'] == 'error'
    message = BytesParser(policy=policy.default).parsebytes((agent.jobs.path(job['id']) / 'response.eml').read_bytes())
    assert not list(message.iter_attachments())
    assert not (agent.jobs.path(job['id']) / 'documents.zip').exists()


def test_complete_message_budget_falls_back_to_small_error_reply(tmp_path):
    config = settings(tmp_path)
    config.max_message_bytes = 1000
    agent = Agent(config, interpreter=interpretation, retriever=retrieval)
    job = agent.process(RAW)
    assert job['status'] == 'prepared' and job['outcome'] == 'error'
    raw = (agent.jobs.path(job['id']) / 'response.eml').read_bytes()
    assert len(raw) <= 1000
    assert not list(BytesParser(policy=policy.default).parsebytes(raw).iter_attachments())


def test_retry_that_requires_clarification_removes_stale_download_artifacts(tmp_path):
    config = settings(tmp_path)
    config.max_zip_bytes = 20
    agent = Agent(config, interpreter=interpretation, retriever=retrieval)
    job = agent.process(RAW)
    directory = agent.jobs.path(job['id'])
    assert job['outcome'] == 'error' and (directory / 'retrieval.json').exists()
    agent.interpreter = lambda *a, **k: Interpretation('clarify', reason='Please confirm the matter')
    retried = agent.process(RAW, retry=True)
    assert retried['outcome'] == 'clarification'
    assert retried['created_at'] == job['created_at']
    for name in ('downloads', 'documents.zip', 'request.json', 'retrieval.json'):
        assert not (directory / name).exists()
