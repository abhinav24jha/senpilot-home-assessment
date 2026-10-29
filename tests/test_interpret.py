import json

import httpx
import pytest

from uarb_agent.interpret import InterpretationError, interpret, parse_email
from uarb_agent.models import IncomingEmail


def provider(*, matter="M12205", category="OTHER_DOCUMENTS", intent="REQUEST", confidence=.99, probability=.99, inspect=None):
    def handle(request):
        body = json.loads(request.content)
        if inspect:
            inspect(body)
        return httpx.Response(200, json={"answers": {name: {"choice": choice, "confidence": confidence, "probabilities": {choice: probability}}
            for name, choice in {"intent": intent, "matter": matter, "category": category}.items()}})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_matter_in_body_not_subject_and_one_constrained_batch():
    mail = parse_email(b"From: Alex <alex@example.test>\nSubject: Quick favor\n\nPlease get Other Documents for M12205")
    seen = []
    with provider(inspect=seen.append) as client:
        result = interpret(mail, api_key="test-only", client=client)
    assert result.action == "retrieve" and result.request.matter_number == "M12205"
    assert result.request.document_type == "Other Documents"
    assert len(seen) == 1 and seen[0]["state"]["candidate_matters"] == ["M12205"]
    assert set(seen[0]["questions"]) == {"intent", "matter", "category"}


def test_correction_and_quoted_history_are_preserved_as_separate_context():
    mail = IncomingEmail("", "alex@example.test", "M12383 exhibits", "Correction: use M12205 Other Documents.\nOn Monday Alex wrote:\n> Please get M12383 exhibits")
    seen = []
    with provider(inspect=seen.append) as client:
        result = interpret(mail, api_key="test-only", client=client)
    assert result.request.matter_number == "M12205"
    state = seen[0]["state"]
    assert "Correction" in state["current_body"] and "On Monday" not in state["current_body"]
    assert "M12383" in state["quoted_context"]
    assert state["candidate_matters"] == ["M12383", "M12205"]


@pytest.mark.parametrize("matter,confidence,probability", [("AMBIGUOUS", .99, .99), ("M12205", .4, .99), ("M12205", .99, .5)])
def test_ambiguity_and_either_confidence_gate_require_clarification(matter, confidence, probability):
    mail = IncomingEmail("", "alex@example.test", "", "M12205 or M12383 exhibits")
    with provider(matter=matter, confidence=confidence, probability=probability) as client:
        result = interpret(mail, api_key="test-only", client=client)
    assert result.action == "clarify" and result.request is None


def test_invented_matter_is_rejected():
    mail = IncomingEmail("", "alex@example.test", "", "Please get M12205 exhibits")
    with provider(matter="M99999") as client:
        with pytest.raises(InterpretationError, match="outside"):
            interpret(mail, api_key="test-only", client=client)


def test_automated_mail_is_ignored_without_api():
    mail = parse_email(b"From: alex@example.test\nAuto-Submitted: auto-replied\n\nM12205 Other Documents")
    assert interpret(mail, api_key="").action == "ignore"


def test_html_fallback_and_multiple_senders():
    mail = parse_email(b"From: alex@example.test\nContent-Type: text/html; charset=utf-8\n\n<p>Please get <b>M12205</b> transcripts</p><script>bad</script>")
    assert "M12205" in mail.body and "bad" not in mail.body
    ambiguous = parse_email(b"From: a@example.test, b@example.test\n\nPlease get documents")
    assert ambiguous.ignored_reason


def test_confident_no_request_is_ignored():
    mail = IncomingEmail("", "alex@example.test", "", "Do not fetch M12205 exhibits")
    with provider(intent="NO_REQUEST", matter="NONE", category="NONE") as client:
        assert interpret(mail, api_key="test-only", client=client).action == "ignore"


@pytest.mark.parametrize('bad_answer', [
    {},
    {'choice': 'REQUEST', 'confidence': .99},
    {'choice': 'REQUEST', 'confidence': True, 'probabilities': {'REQUEST': .99}},
    {'choice': 'REQUEST', 'confidence': float('nan'), 'probabilities': {'REQUEST': .99}},
    {'choice': 'REQUEST', 'confidence': .99, 'probabilities': {'REQUEST': 2}},
])
def test_malformed_provider_decision_is_rejected(bad_answer):
    mail = IncomingEmail('', 'alex@example.test', '', 'Please get M12205 Other Documents')
    # JSON text permits a NaN token for deliberately malformed provider coverage.
    content = json.dumps({'answers': {'intent': bad_answer}}).encode()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))) as client:
        with pytest.raises(InterpretationError, match='invalid|incomplete'):
            interpret(mail, api_key='test-only', client=client)


@pytest.mark.parametrize('status', [401, 429, 503])
def test_provider_status_error_is_actionable_without_exposing_response(status):
    mail = IncomingEmail('', 'alex@example.test', '', 'Please get M12205 Other Documents')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text='secret-provider-response'))) as client:
        with pytest.raises(InterpretationError) as error:
            interpret(mail, api_key='secret-test-key', client=client)
    assert str(status) in str(error.value)
    assert 'secret' not in str(error.value)


def test_provider_timeout_is_a_sanitized_error():
    def timeout(request):
        raise httpx.ReadTimeout('Bearer secret-test-key', request=request)
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(InterpretationError, match='could not be reached') as error:
            interpret(IncomingEmail('', 'alex@example.test', '', 'M12205 Exhibits'), api_key='secret-test-key', client=client)
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('headers', [
    'From: agent@example.test',
    'From: alex@example.test\nReturn-Path: <>',
    'From: alex@example.test\nList-Id: research.example.test',
    'From: alex@example.test\nPrecedence: bulk',
])
def test_loop_and_bulk_messages_are_ignored_before_provider(headers):
    mail = parse_email((headers + '\n\nGet M12205 Exhibits').encode(), agent_address='agent@example.test')
    assert interpret(mail, api_key='').action == 'ignore'


def test_mime_prefers_plain_request_and_ignores_attachment_text():
    from email.message import EmailMessage
    message = EmailMessage()
    message['From'] = 'alex@example.test'
    message.set_content('Get M12205 Other Documents')
    message.add_alternative('<p>Get M12205 Other Documents</p>', subtype='html')
    message.add_attachment(b'M99999 Exhibits', maintype='text', subtype='plain', filename='old.txt')
    parsed = parse_email(message.as_bytes())
    assert parsed.body == 'Get M12205 Other Documents'
    assert 'M99999' not in parsed.body


def test_missing_and_oversized_input_fail_before_provider():
    with pytest.raises(InterpretationError, match='size limit'):
        parse_email(b'large', max_bytes=2)
    with pytest.raises(InterpretationError, match='empty'):
        parse_email(b'')
    with pytest.raises(InterpretationError, match='not configured'):
        interpret(IncomingEmail('', 'alex@example.test', '', 'M12205 Exhibits'), api_key='')
