import pytest
from uarb_agent.mailbox import Mailbox, MailboxError, mark_seen

class FakeIMAP:
    calls = []
    def __init__(self, *args, **kwargs): self.calls = type(self).calls
    def login(self, *args): self.calls.append(('login',)); return 'OK', []
    def starttls(self, **kwargs): self.calls.append(('tls',)); return 'OK', []
    def select(self, *args, **kwargs): return 'OK', [b'2']
    def response(self, *args): return 'UIDVALIDITY', [b'77']
    def logout(self): self.calls.append(('logout',))
    def uid(self, *args):
        self.calls.append(args)
        if args[0] == 'search': return 'OK', [b'1 2']
        if args[0] == 'store': return 'OK', []
        if args[2] == '(RFC822.SIZE)':
            return 'OK', [b'1 (RFC822.SIZE 5000000)' if args[1] == '1' else b'2 (RFC822.SIZE 40)']
        return 'OK', [(b'2 (BODY[] {40}', b'Subject: Request\r\n\r\nHello'), b')']


def test_fetch_peeks_checks_size_and_requires_explicit_ack(monkeypatch):
    FakeIMAP.calls = []
    monkeypatch.setattr('uarb_agent.mailbox.imaplib.IMAP4_SSL', FakeIMAP)
    with Mailbox('imap.example.com', 993, 'user', 'secret') as mailbox:
        assert mailbox.uidvalidity == '77'
        assert mailbox.fetch_unseen(limit=1) == [('2', b'Subject: Request\r\n\r\nHello')]
        assert mailbox.skipped[0][0] == '1'
        assert not any(call[0] == 'store' for call in FakeIMAP.calls)
        assert ('fetch', '1', '(BODY.PEEK[])') not in FakeIMAP.calls
        mailbox.mark_seen('2')
    assert ('store', '2', '+FLAGS.SILENT', r'(\Seen)') in FakeIMAP.calls


def test_changed_uidvalidity_refuses_ack(monkeypatch):
    FakeIMAP.calls = []
    monkeypatch.setattr('uarb_agent.mailbox.imaplib.IMAP4_SSL', FakeIMAP)
    with pytest.raises(MailboxError, match='UIDVALIDITY'):
        mark_seen('2', host='imap.example.com', port=993, user='user', password='secret', expected_uidvalidity='old')
    assert not any(call[0] == 'store' for call in FakeIMAP.calls)


def test_starttls_and_invalid_uid(monkeypatch):
    FakeIMAP.calls = []
    monkeypatch.setattr('uarb_agent.mailbox.imaplib.IMAP4', FakeIMAP)
    with Mailbox('imap.example.com', 143, 'user', 'secret', secure=False) as mailbox:
        with pytest.raises(ValueError): mailbox.mark_seen('2 BAD')
    assert FakeIMAP.calls[:2] == [('tls',), ('login',)]


def test_login_failure_is_actionable_without_exposing_server_text(monkeypatch):
    import imaplib
    class RejectedLogin(FakeIMAP):
        def login(self, *args):
            raise imaplib.IMAP4.error('AUTHENTICATIONFAILED sensitive-server-detail')
    monkeypatch.setattr('uarb_agent.mailbox.imaplib.IMAP4_SSL', RejectedLogin)
    with pytest.raises(MailboxError) as error:
        with Mailbox('imap.gmail.com', 993, 'demo@gmail.com', 'secret'):
            pass
    assert 'rejected the login' in str(error.value)
    assert 'SAME account' in str(error.value)
    assert 'sensitive-server-detail' not in str(error.value)


def test_recipient_filter_is_applied_before_fetching_bodies(monkeypatch):
    FakeIMAP.calls = []
    monkeypatch.setattr('uarb_agent.mailbox.imaplib.IMAP4_SSL', FakeIMAP)
    with Mailbox('imap.example.com', 993, 'user', 'secret', recipient='demo+agent@gmail.com') as mailbox:
        mailbox.fetch_unseen(limit=1)
    assert ('search', None, 'UNSEEN', 'TO', '"demo+agent@gmail.com"') in FakeIMAP.calls
