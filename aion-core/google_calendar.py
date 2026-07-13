"""Google Calendar integration for AION."""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import CONFIG


CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
_WEEKDAY_RE = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MONTH_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)


@dataclass
class CalendarRequest:
    title: str
    start: datetime
    end: datetime
    notes: str = ""
    reminder_minutes: list[int] | None = None


class CalendarConfigError(RuntimeError):
    pass


def calendar_enabled() -> bool:
    return bool(CONFIG.get("google_calendar_enabled", True))


def calendar_user_email() -> str:
    return str(CONFIG.get("google_calendar_user_email", "draygen80@gmail.com"))


def credentials_path() -> str:
    return os.path.expanduser(str(CONFIG.get("google_calendar_credentials_file", "data/google_calendar_credentials.json")))


def token_path() -> str:
    return os.path.expanduser(str(CONFIG.get("google_calendar_token_file", "data/google_calendar_token.json")))


def default_timezone() -> str:
    return str(CONFIG.get("google_calendar_timezone") or CONFIG.get("USER_TIMEZONE") or "America/New_York")


def default_duration_minutes() -> int:
    return int(CONFIG.get("google_calendar_default_duration_minutes", 60))


def default_reminder_minutes() -> list[int]:
    values = CONFIG.get("google_calendar_default_reminder_minutes", [10])
    return [int(v) for v in values]


def setup_instructions() -> str:
    return (
        "I can handle that, but Google Calendar is not connected on this machine yet.\n\n"
        f"Set it up once by saving the Google OAuth desktop client JSON to `{credentials_path()}`, "
        f"then run `python google_calendar.py auth` and sign in as `{calendar_user_email()}`. "
        f"After that I can add events directly to your calendar from chat."
    )


def _load_google_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise CalendarConfigError(
            "I can set calendar appointments, but the Google Calendar libraries are missing in this environment. "
            "Install the project requirements and I can take it from there."
        ) from exc

    creds = None
    token_file = token_path()
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, CALENDAR_SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    if not creds or not creds.valid:
        cred_file = credentials_path()
        if not os.path.exists(cred_file):
            raise CalendarConfigError(setup_instructions())
        flow = InstalledAppFlow.from_client_secrets_file(cred_file, CALENDAR_SCOPES)
        creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def authenticate_google_calendar() -> str:
    _load_google_service()
    return f"Google Calendar is connected for `{calendar_user_email()}`. I saved the token at `{token_path()}`."


def _parse_clock(raw: str) -> time:
    text = re.sub(r"(?i)\b(?:est|edt|eastern|et)\b", "", raw).strip().lower().replace(".", "")
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if not match:
        raise ValueError(f"Could not parse time: {raw}")
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid time: {raw}")
    return time(hour, minute)


def _parse_date(raw: str, now: datetime) -> date:
    text = re.sub(rf"(?i)^\s*{_WEEKDAY_RE},?\s+", "", raw.strip()).lower()
    if text == "today":
        return now.date()
    if text == "tomorrow":
        return now.date() + timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.title(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date: {raw}")


def _strip_trigger(text: str) -> str:
    return re.sub(
        r"(?i)^\s*(?:aion[, ]*)?(?:please\s+)?(?:set|add|create|schedule|book)?\s*"
        r"(?:(?:a|an|my)\s+)?(?:google\s+calendar\s+)?(?:calendar\s+)?(?:appointment|event|reminder)?\s*(?:for\s+me\s*)?",
        "",
        text,
    ).strip(" :,-")


def _extract_notes(text: str) -> tuple[str, str]:
    match = re.search(r"(?i)\b(?:notes?|description)\s*:\s*(.+)$", text)
    if not match:
        return text, ""
    notes = match.group(1).strip()
    remaining = text[: match.start()].strip(" ,;-")
    return remaining, notes


def _extract_reminders(text: str) -> tuple[str, list[int] | None]:
    reminders: list[int] = []
    patterns = [
        r"(?i)\bremind(?:er|ers)?(?:\s+me)?\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\s+before\b",
        r"(?i)\bwith\s+remind(?:er|ers)?\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b",
    ]
    remaining = text
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            amount = int(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith(("hour", "hr")):
                amount *= 60
            elif unit.startswith("day"):
                amount *= 1440
            reminders.append(amount)
        remaining = re.sub(pattern, "", remaining).strip(" ,;-")
    return remaining, reminders or None


def _extract_duration_minutes(text: str) -> tuple[str, int]:
    match = re.search(r"(?i)\bfor\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", text)
    if not match:
        return text, default_duration_minutes()
    amount = int(match.group(1))
    unit = match.group(2).lower()
    minutes = amount * 60 if unit.startswith(("hour", "hr")) else amount
    remaining = (text[: match.start()] + text[match.end() :]).strip(" ,;-")
    return remaining, max(5, minutes)


def parse_calendar_request(message: str, now: datetime | None = None) -> CalendarRequest:
    tz = ZoneInfo(default_timezone())
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    text = _strip_trigger(message)
    text, notes = _extract_notes(text)
    text, reminders = _extract_reminders(text)
    text, duration = _extract_duration_minutes(text)

    date_pattern = (
        rf"(?:{_WEEKDAY_RE},?\s+)?(?:today|tomorrow|\d{{4}}-\d{{1,2}}-\d{{1,2}}|"
        rf"\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|(?:{_MONTH_RE})\s+\d{{1,2}},?\s+\d{{4}})"
    )
    time_pattern = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:est|edt|eastern|et)?"
    match = re.search(
        rf"(?i)\b(?:on\s+|for\s+)?({date_pattern})\s+(?:at\s+)?({time_pattern})\b",
        text,
    )
    if not match:
        raise ValueError(
            "I can put that on your calendar. I just need the date and time. "
            "Say it like: `dentist tomorrow at 2pm`, or `doctor appointment July 15, 2026 at 11am`."
        )

    event_date = _parse_date(match.group(1), now)
    event_time = _parse_clock(match.group(2))
    start = datetime.combine(event_date, event_time, tzinfo=tz)
    end = start + timedelta(minutes=duration)

    title = (text[: match.start()] + text[match.end() :]).strip(" ,;-")
    title = re.sub(r"(?i)^(?:for|to)\s+", "", title).strip(" ,;-")
    if not title:
        title = "Appointment"

    return CalendarRequest(
        title=title,
        start=start,
        end=end,
        notes=notes,
        reminder_minutes=reminders or default_reminder_minutes(),
    )


def calendar_request_to_event(request: CalendarRequest) -> dict[str, Any]:
    return {
        "summary": request.title,
        "description": request.notes,
        "start": {"dateTime": request.start.isoformat(), "timeZone": default_timezone()},
        "end": {"dateTime": request.end.isoformat(), "timeZone": default_timezone()},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": minutes}
                for minutes in sorted(set(request.reminder_minutes or default_reminder_minutes()), reverse=True)
            ],
        },
    }


def create_google_calendar_event(request: CalendarRequest) -> dict[str, Any]:
    if not calendar_enabled():
        raise CalendarConfigError("Google Calendar is turned off in AION's config right now.")
    service = _load_google_service()
    event = calendar_request_to_event(request)
    return service.events().insert(calendarId="primary", body=event).execute()


def handle_calendar_message(message: str, now: datetime | None = None) -> str:
    try:
        request = parse_calendar_request(message, now=now)
        created = create_google_calendar_event(request)
    except CalendarConfigError as exc:
        return str(exc)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"I tried to add that to Google Calendar, but Google rejected the request: {exc}"

    start_label = request.start.strftime("%a %b %-d, %Y at %-I:%M %p")
    link = created.get("htmlLink") or "(no link returned)"
    notes = " I added your notes too." if request.notes else ""
    reminders = ", ".join(f"{m} min" for m in request.reminder_minutes or [])
    reminder_text = f" Reminder: {reminders} before." if reminders else ""
    link_text = f"\n\nOpen it: {link}" if link and link != "(no link returned)" else ""
    return (
        f"Done. I put **{request.title}** on your Google Calendar for {start_label}."
        f"{notes}{reminder_text}{link_text}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Authenticate or create Google Calendar events for AION.")
    parser.add_argument("command", choices=["auth", "parse"], help="auth starts OAuth; parse prints parsed event JSON")
    parser.add_argument("message", nargs="*", help="message to parse when command=parse")
    args = parser.parse_args()

    if args.command == "auth":
        print(authenticate_google_calendar())
        return 0
    request = parse_calendar_request(" ".join(args.message))
    print(json.dumps(calendar_request_to_event(request), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
