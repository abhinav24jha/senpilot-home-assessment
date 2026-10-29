"""CLI behavior at mailbox and persistence boundaries."""
import json

from uarb_agent.cli import main
from uarb_agent.config import Settings
from uarb_agent.jobs import JobStore, process_lock


def configured(tmp_path):
    return Settings(data_dir=tmp_path, imap_host='imap.example.test', imap_user='user', imap_password='test-only')


def test_bad_message_does_not_block_next_message_and_once_reports_failure(monkeypatch, tmp_path, capsys):
    config = configured(tmp_path)
    monkeypatch.setattr('uarb_agent.cli.Settings.from_env', lambda path: config)
    acknowledged = []
    class FakeMailbox:
        skipped = [('3', 'Message too large')]
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def fetch_unseen(self, limit): return [('1', b'bad'), ('2', b'good')]
        def mark_seen(self, uid): acknowledged.append(uid)
    class FakeAgent:
        def __init__(self, settings): pass
        def process(self, raw, **kwargs):
            if raw == b'bad':
                raise ValueError('broken input')
            return {'id': 'a' * 24, 'status': 'prepared', 'outcome': 'success'}
    monkeypatch.setattr('uarb_agent.mailbox.Mailbox', FakeMailbox)
    monkeypatch.setattr('uarb_agent.service.Agent', FakeAgent)
    assert main(['watch', '--once']) == 1
    assert acknowledged == ['2']
    captured = capsys.readouterr()
    assert 'message_error' in captured.err and 'message_skipped' in captured.err
    assert 'prepared' in captured.out


def test_jobs_can_be_read_while_worker_holds_lock(monkeypatch, tmp_path, capsys):
    config = configured(tmp_path)
    monkeypatch.setattr('uarb_agent.cli.Settings.from_env', lambda path: config)
    store = JobStore(tmp_path)
    store.save({'id': 'a' * 24, 'status': 'prepared'})
    with process_lock(tmp_path):
        assert main(['jobs']) == 0
    assert json.loads(capsys.readouterr().out)[0]['status'] == 'prepared'
