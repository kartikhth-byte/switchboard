from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from integrations.messaging.base import Email, LeadCRM, WhatsApp
from integrations.messaging.mock import MockEmail, MockWhatsApp

if TYPE_CHECKING:
    from core.config.settings import Settings

log = logging.getLogger(__name__)


def make_whatsapp(settings: Settings) -> WhatsApp:
    """WhatsApp Cloud when DEMO_INTEGRATIONS=real and its keys are set; the mock otherwise
    (Phase 2 LEAD-P2-01). The callers' send rules apply either way. Shared by Dispatcher and
    Operator."""
    if (
        settings.demo_integrations == "real"
        and settings.whatsapp_token
        and (settings.whatsapp_phone_number_id)
    ):
        from integrations.messaging.real import WhatsAppCloud

        return WhatsAppCloud(
            settings.whatsapp_phone_number_id,
            settings.whatsapp_token.get_secret_value(),
            settings.whatsapp_api_version,
            settings.whatsapp_template_language,
        )
    if settings.demo_integrations == "real":
        log.warning("WhatsApp keys not set: WhatsApp messages go to the mock adapter")
    return MockWhatsApp()


def make_email(settings: Settings) -> Email:
    """SMTP to Mailpit when DEMO_INTEGRATIONS=real; the mock otherwise."""
    if settings.demo_integrations == "real":
        from integrations.messaging.real import SmtpEmail

        return SmtpEmail(settings.smtp_host, settings.smtp_port, settings.email_from)
    return MockEmail()


__all__ = [
    "Email",
    "LeadCRM",
    "MockEmail",
    "MockWhatsApp",
    "WhatsApp",
    "make_email",
    "make_whatsapp",
]
