import hashlib
from email import policy
from email.parser import BytesParser
import zipfile

import pytest
from uarb_agent.delivery import DeliveryError, build_reply, package_files, save_reply, send_reply
from uarb_agent.models import CATEGORIES, DownloadedFile, IncomingEmail, Interpretation, RetrievalResult, SizeLimitError


def example(tmp_path):
    files = []
    for number in range(2):
        raw = b'%PDF-example ' + bytes([65 + number])
        path = tmp_path / f'{number}.pdf'
        path.write_bytes(raw)
        files.append(DownloadedFile('same.pdf', path, len(raw), hashlib.sha256(raw).hexdigest()))
    return RetrievalResult('M12205', 'Other Documents', {'title': 'Example', 'decision_date': '10/23/2025'}, dict(zip(CATEGORIES, [13, 6, 44, 0, 0])), files)


def incoming():
    return IncomingEmail('<request@demo.test>', 'Person <person@example.com>', 'Please send docs', 'Request')


def test_zip_and_mime_roundtrip(tmp_path):
    result = example(tmp_path)
    path = package_files(result, tmp_path / 'documents.zip')
    with zipfile.ZipFile(path) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 2
        assert archive.testzip() is None
        assert {hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()} == {file.sha256 for file in result.files}
    message = build_reply(incoming(), agent_from='agent@example.com', result=result, zip_path=path)
    saved = save_reply(message, tmp_path / 'reply.eml')
    parsed = BytesParser(policy=policy.default).parsebytes(saved.read_bytes())
    assert list(parsed.iter_attachments())[0].get_payload(decode=True) == path.read_bytes()
    body = parsed.get_body(preferencelist=('plain',)).get_content()
    assert 'Total category entries: 63' in body
    assert 'not a count of distinct files' in body
    assert 'downloaded 2' in body
    assert 'Decision Date: 10/23/2025' in body
    assert parsed['In-Reply-To'] == incoming().message_id
    assert parsed['Auto-Submitted'] == 'auto-replied'


def test_corrupt_source_and_size_limits(tmp_path):
    result = example(tmp_path)
    result.files[0].path.write_bytes(b'changed')
    with pytest.raises(DeliveryError):
        package_files(result, tmp_path / 'bad.zip')
    assert not (tmp_path / 'bad.zip').exists()
    assert not list(tmp_path.glob('*.part'))
    result = example(tmp_path)
    with pytest.raises(SizeLimitError):
        package_files(result, tmp_path / 'tiny.zip', max_zip_bytes=20)
    assert not (tmp_path / 'tiny.zip').exists()
    message = build_reply(incoming(), agent_from='agent@example.com', error='Retrieval failed')
    with pytest.raises(SizeLimitError):
        save_reply(message, tmp_path / 'tiny.eml', max_message_bytes=30)
    assert not (tmp_path / 'tiny.eml').exists()
    result.files = []
    with pytest.raises(ValueError):
        package_files(result, tmp_path / 'empty.zip')


def test_clarification_zero_and_error_templates(tmp_path):
    message = build_reply(incoming(), agent_from='agent@example.com', interpretation=Interpretation('clarify', reason='Two matters were mentioned.'))
    assert 'Two matters were mentioned' in message.get_content()
    assert not list(message.iter_attachments())
    result = example(tmp_path)
    result.files = []
    del result.counts['Recordings']
    message = build_reply(incoming(), agent_from='agent@example.com', result=result)
    assert 'No downloadable files were returned' in message.get_content()
    assert 'Recordings: unknown' in message.get_content()
    assert 'Total category entries:' not in message.get_content()
    message = build_reply(incoming(), agent_from='agent@example.com', error='Try again later', zip_path=tmp_path / 'does-not-exist.zip')
    assert not list(message.iter_attachments())
    assert 'No ZIP is attached' in message.get_content()


def test_ignored_and_invalid_addresses_rejected():
    with pytest.raises(ValueError):
        build_reply(incoming(), agent_from='agent@example.com', interpretation=Interpretation('ignore'))
    for address in ['a@example.com\r\nBcc: b@example.com', 'a@example.com,b@example.com']:
        with pytest.raises(ValueError):
            build_reply(incoming(), agent_from=address, error='Error')


def test_smtp_uses_tls_auth_and_explicit_recipient(monkeypatch):
    calls = []
    class SMTP:
        def __init__(self, host, port, timeout): calls.append(('connect', host, port))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append(('ehlo',))
        def starttls(self, context): calls.append(('tls',))
        def login(self, user, password): calls.append(('login', user))
        def send_message(self, message, from_addr, to_addrs):
            calls.append(('send', from_addr, to_addrs)); return {}
    monkeypatch.setattr('uarb_agent.delivery.smtplib.SMTP', SMTP)
    message = build_reply(incoming(), agent_from='agent@example.com', error='Error')
    assert send_reply(message, host='smtp.example.com', port=587, user='user', password='secret') == str(message['Message-ID'])
    assert [call[0] for call in calls] == ['connect', 'ehlo', 'tls', 'ehlo', 'login', 'send']
    assert calls[-1] == ('send', 'agent@example.com', ['person@example.com'])


def test_smtp_error_does_not_expose_secret(monkeypatch):
    def failing(*args, **kwargs): raise RuntimeError('secret-password')
    monkeypatch.setattr('uarb_agent.delivery.smtplib.SMTP', failing)
    message = build_reply(incoming(), agent_from='agent@example.com', error='Error')
    with pytest.raises(DeliveryError) as exc:
        send_reply(message, host='smtp.example.com', port=587, user='user', password='secret-password')
    assert 'secret-password' not in str(exc.value)


def test_metadata_labels_and_source_are_not_duplicated(tmp_path):
    result = example(tmp_path)
    result.metadata.update(date_label='Decision Date', date_value='10/23/2025',
                           source_url='https://uarb.novascotia.ca/fmi/webd/UARB15')
    body = build_reply(incoming(), agent_from='agent@example.com', result=result).get_content()
    assert body.count('Decision Date:') == 1
    assert body.count('Source:') == 1
