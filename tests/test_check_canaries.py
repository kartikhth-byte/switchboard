from scripts.check_canaries import find_canaries


def test_finds_exact_and_reformatted_canaries_but_not_redacted_text() -> None:
    assert "+91 98450 12345" in find_canaries('{"input": "call me on +91 98450 12345"}')
    assert "9845012345" in find_canaries("reach me at 98450-12345 please")
    assert "4111 1111 1111 1111" in find_canaries("card 4111-1111-1111-1111")
    assert find_canaries("NINE eight four five zero one two three four five") != []
    assert find_canaries('{"input": "call me on [PHONE]", "card": "[CARD]"}') == []
    assert find_canaries("booking PG-1002, 4 guests, ₹42,000 per night") == []
