from scripts.check_secrets import scan


def test_scan_finds_values_and_shapes_but_not_clean_text() -> None:
    secrets = {"GHL_TOKEN": "abc123def456ghi789"}
    assert scan("Authorization: Bearer abc123def456ghi789", secrets) == ["value of GHL_TOKEN"]
    assert "slack pattern" in scan("token xoxb-1234567890-abcdefghij", {})
    assert "anthropic pattern" in scan("key=sk-ant-api03-" + "x" * 30, {})
    assert scan('{"lead_id": "5e17c8d3", "tier": 1, "owner": "@reservations"}', secrets) == []
