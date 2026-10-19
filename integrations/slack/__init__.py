from __future__ import annotations

from typing import TYPE_CHECKING

from integrations.slack.base import Slack
from integrations.slack.mock import MockSlack

if TYPE_CHECKING:
    from core.config.settings import Settings


def make_slack(settings: Settings) -> Slack:
    """Real Slack when DEMO_INTEGRATIONS=real and a bot token is set; the mock otherwise.
    Shared by the Concierge handoff and the Dispatcher."""
    if settings.demo_integrations == "real" and settings.slack_bot_token:
        from integrations.slack.real import SlackWeb, parse_channels

        return SlackWeb(
            settings.slack_bot_token.get_secret_value(), parse_channels(settings.slack_channels)
        )
    return MockSlack()


__all__ = ["MockSlack", "Slack", "make_slack"]
