from pathlib import Path

import pytest
from dotenv import dotenv_values

from uarb_agent.mail_setup import configure_gmail, check_mail_connections
from uarb_agent.config import Settings


def test_setup_checks_credentials_before_saving_and_preserves_model_key(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('TYPESAFE_API_KEY=test-placeholder\n')
    inspected = []
    monkeypatch.setattr('uarb_agent.mail_setup.check_mail_connections', lambda settings: inspected.append(settings))
    address = configure_gmail(env, ' demo@gmail.com ', 'abcd efgh ijkl mnop')
    values = dotenv_values(env)
    assert address == 'demo@gmail.com'
    assert values['TYPESAFE_API_KEY'] == 'test-placeholder'
    assert values['IMAP_PASSWORD'] == values['SMTP_PASSWORD'] == 'abcdefghijklmnop'
    assert inspected[0].smtp_secure and inspected[0].smtp_port == 465
    assert env.stat().st_mode & 0o777 == 0o600


def test_failed_login_does_not_write_credentials(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    original = 'TYPESAFE_API_KEY=test-placeholder\n'
    env.write_text(original)
    def refused(settings):
        raise ConnectionError('refused')
    monkeypatch.setattr('uarb_agent.mail_setup.check_mail_connections', refused)
    with pytest.raises(ConnectionError):
        configure_gmail(env, 'demo@gmail.com', 'test-password')
    assert env.read_text() == original


def test_connection_probe_authenticates_without_sending_or_fetching(monkeypatch):
    calls = []
    class Inbox:
        def __init__(self, *args): pass
        def __enter__(self): calls.append('imap'); return self
        def __exit__(self, *args): pass
    class Smtp:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): calls.append('tls')
        def login(self, *args): calls.append('smtp-login')
    monkeypatch.setattr('uarb_agent.mail_setup.Mailbox', Inbox)
    monkeypatch.setattr('uarb_agent.mail_setup.smtplib.SMTP', Smtp)
    settings = Settings(agent_from='demo@gmail.com', imap_host='imap.gmail.com',
        imap_user='demo@gmail.com', imap_password='test', smtp_host='smtp.gmail.com',
        smtp_user='demo@gmail.com', smtp_password='test')
    check_mail_connections(settings)
    assert calls == ['imap', 'tls', 'smtp-login']


def test_setup_displays_sanitized_mailbox_failure(monkeypatch, tmp_path, capsys):
    from uarb_agent.mail_setup import main
    from uarb_agent.mailbox import MailboxError
    monkeypatch.setattr('uarb_agent.mail_setup.Settings.from_env', lambda path: Settings())
    def failed(settings):
        raise MailboxError('The mail server rejected the login.')
    monkeypatch.setattr('uarb_agent.mail_setup.check_mail_connections', failed)
    assert main(['--env', str(tmp_path/'.env'), '--check-only']) == 1
    assert 'rejected the login' in capsys.readouterr().out
