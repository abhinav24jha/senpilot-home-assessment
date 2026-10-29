import hashlib
from pathlib import Path

import httpx
import pytest

from uarb_agent.models import CATEGORIES, DocumentRequest, RetrievalError, SizeLimitError
from uarb_agent.uarb import BASE, UarbClient, retrieve, safe_filename

URL = BASE + 'APP/connector/0/42/dl/104160.pdf'
PDF = b'%PDF-1.4\npublic fixture\n%%EOF'


def adapter(response):
    return UarbClient(httpx.Client(transport=httpx.MockTransport(lambda request: response)))


def test_atomic_download_hash_and_collision(tmp_path):
    session = adapter(httpx.Response(200, content=PDF))
    first = session.download(URL, '104160.pdf', tmp_path, 100, 'row-1')
    second = session.download(URL, '104160.pdf', tmp_path, 100, 'row-1')
    assert first.path.read_bytes() == PDF
    assert first.sha256 == hashlib.sha256(PDF).hexdigest()
    assert second.filename == '104160-2.pdf'
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('headers', [{}, {'Content-Length': '10000000'}])
def test_stream_or_header_size_limit_removes_partial(tmp_path, headers):
    # Generator stream omits HTTPX's automatically calculated Content-Length.
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield PDF[:10]
            yield PDF[10:]
    session = adapter(httpx.Response(200, headers=headers, stream=Body()))
    with pytest.raises(SizeLimitError):
        session.download(URL, 'large.pdf', tmp_path, 12, 'row-1')
    assert not list(tmp_path.iterdir())


def test_invalid_pdf_is_not_committed(tmp_path):
    session = adapter(httpx.Response(200, content=b'<html>expired</html>'))
    with pytest.raises(RetrievalError, match='invalid PDF'):
        session.download(URL, '104160.pdf', tmp_path, 100, 'row-1')
    assert not list(tmp_path.iterdir())


def test_download_origin_is_restricted(tmp_path):
    session = adapter(httpx.Response(200, content=PDF))
    with pytest.raises(RetrievalError, match='origin'):
        session.download('https://example.com/file.pdf', 'file.pdf', tmp_path, 100, 'row-1')


def test_filename_cannot_escape_directory():
    assert safe_filename('..%2F..%2Fsecret.pdf') == 'secret.pdf'
    assert safe_filename('..\\secret.pdf') == 'secret.pdf'
    assert safe_filename('Hearing+Transcript.pdf') == 'Hearing Transcript.pdf'
    with pytest.raises(RetrievalError):
        safe_filename('..')


def test_counts_preserve_overlap_and_zeros():
    session = UarbClient()
    try:
        session.state = {str(i): {'text': f'{category} - {count}'}
                         for i, (category, count) in enumerate(zip(CATEGORIES, [13, 6, 44, 0, 0]))}
        assert session.counts() == dict(zip(CATEGORIES, [13, 6, 44, 0, 0]))
    finally:
        session.close()


def test_rows_require_explicit_public_security_label():
    session = UarbClient()
    try:
        session.state = {
            '1': {'caption': 'GO GET IT', 'id': 'b0p0o1i0i0r1'},
            '2': {'text': 'Public', 'id': 'b0p0o2i0i0r1'},
            '3': {'caption': 'GO GET IT', 'id': 'b0p0o1i1i0r1'},
            '4': {'text': 'Confidential', 'id': 'b0p0o2i1i0r1'},
            '5': {'caption': 'GO GET IT', 'id': 'b0p0o1i2i0r1'},
        }
        assert [row['public'] for row in session.rows()] == [True, False, False]
    finally:
        session.close()


def test_protocol_tokens_and_repaint_are_updated():
    session = UarbClient()
    try:
        session.state = {'old': {}}
        session._ingest([{'syncId': 2, 'clientId': 4, 'meta': {'repaintAll': True}, 'state': {'new': {}}}])
        session._ingest([{'syncId': -1, 'clientId': 3}])
        assert session.sequence == {'syncId': 2, 'clientId': 4}
        assert session.state == {'new': {}}
    finally:
        session.close()


def test_rejects_unimplemented_parallel_control(tmp_path):
    with pytest.raises(ValueError, match='DOWNLOAD_WORKERS=1'):
        retrieve(DocumentRequest('M12205', 'Other Documents'), tmp_path, workers=2)


def test_interrupted_download_removes_partial_file(tmp_path):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield PDF[:10]
            raise httpx.ReadError('stream interrupted')
    session = adapter(httpx.Response(200, stream=BrokenBody()))
    with pytest.raises(httpx.ReadError):
        session.download(URL, 'file.pdf', tmp_path, 100, 'row-1')
    assert not list(tmp_path.iterdir())


def test_mismatched_content_length_is_not_committed(tmp_path):
    session = adapter(httpx.Response(200, content=PDF, headers={'Content-Length': str(len(PDF) + 1)}))
    with pytest.raises(RetrievalError, match='incomplete'):
        session.download(URL, 'file.pdf', tmp_path, 100, 'row-1')
    assert not list(tmp_path.iterdir())


def test_state_update_refreshes_cached_rows_and_counts():
    session = UarbClient()
    try:
        session._ingest([{'state': {'1': {'caption': 'Other Documents - 1'},
            '2': {'caption': 'GO GET IT', 'id': 'b0p0o1i0i0r1'},
            '3': {'text': 'Public', 'id': 'b0p0o2i0i0r1'}}}])
        assert session.counts()['Other Documents'] == 1
        assert session.rows()[0]['public']
        session._ingest([{'state': {'1': {'caption': 'Other Documents - 2'},
                                  '3': {'text': 'Confidential', 'id': 'b0p0o2i0i0r1'}}}])
        assert session.counts()['Other Documents'] == 2
        assert not session.rows()[0]['public']
    finally:
        session.close()
