"""Studio Nine config: services, stylists, opening hours (config/salon.yaml)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Service(BaseModel):
    id: str
    name: str
    minutes: int
    price_inr: int


class Stylist(BaseModel):
    id: str
    name: str
    skills: list[str]


class Notifications(BaseModel):
    front_desk_email: str
    reminder_hours_before: int = 24
    whatsapp_template: str = "studio_nine_appointment"


class Salon(BaseModel):
    timezone: str
    slot_step_minutes: int = 15
    min_lead_minutes: int = 60
    hold_ttl_seconds: int = 120
    transfer_number: str
    hours: dict[str, list[str] | None]
    services: list[Service]
    stylists: list[Stylist]
    notifications: Notifications

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, service_id: str) -> Service:
        for s in self.services:
            if s.id == service_id:
                return s
        raise KeyError(f"unknown service: {service_id}")

    def stylist(self, stylist_id: str) -> Stylist:
        for s in self.stylists:
            if s.id == stylist_id:
                return s
        raise KeyError(f"unknown stylist: {stylist_id}")

    def opening(self, day: date) -> tuple[datetime, datetime] | None:
        span = self.hours.get(DAYS[day.weekday()])
        if not span:
            return None
        o, c = (time.fromisoformat(t) for t in span)
        return (datetime.combine(day, o, self.tz), datetime.combine(day, c, self.tz))

    def is_open(self, at: datetime) -> bool:
        local = at.astimezone(self.tz)
        span = self.opening(local.date())
        return span is not None and span[0] <= local < span[1]

    def step(self) -> timedelta:
        return timedelta(minutes=self.slot_step_minutes)


def load_salon(path: Path = ROOT / "config/salon.yaml") -> Salon:
    salon = Salon.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    ids = {s.id for s in salon.services}
    for st in salon.stylists:
        unknown = set(st.skills) - ids
        if unknown:
            raise ValueError(f"stylist {st.id} has unknown skills {unknown}")
    return salon
