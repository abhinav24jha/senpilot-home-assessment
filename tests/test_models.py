"""Validated requests cannot carry unsupported values into portal control."""
import pytest
from uarb_agent.models import DocumentRequest


@pytest.mark.parametrize('limit', [0, 11, 1.5, True])
def test_document_limit_requires_integer_one_through_ten(limit):
    with pytest.raises(ValueError):
        DocumentRequest('M12205', 'Other Documents', limit)


@pytest.mark.parametrize('matter', ['M１２２０５', 'M12205/../', '12205'])
def test_matter_uses_exact_ascii_identifier(matter):
    with pytest.raises(ValueError):
        DocumentRequest(matter, 'Other Documents')
