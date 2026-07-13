from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from google_calendar import calendar_request_to_event, handle_calendar_message, parse_calendar_request
from tools import dispatch_tool_message


class TestGoogleCalendar(unittest.TestCase):
    def test_parse_calendar_request_with_notes_and_reminder(self):
        now = datetime(2026, 7, 13, 9, 0, tzinfo=ZoneInfo("America/New_York"))

        request = parse_calendar_request(
            "calendar dentist tomorrow at 2:30pm for 45 minutes reminder 30 minutes before notes: bring insurance card",
            now=now,
        )

        self.assertEqual(request.title, "dentist")
        self.assertEqual(request.start.isoformat(), "2026-07-14T14:30:00-04:00")
        self.assertEqual(request.end.isoformat(), "2026-07-14T15:15:00-04:00")
        self.assertEqual(request.notes, "bring insurance card")
        self.assertEqual(request.reminder_minutes, [30])

    def test_calendar_request_to_event_uses_google_reminders(self):
        now = datetime(2026, 7, 13, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        request = parse_calendar_request("set appointment doctor 2026-07-20 at 9am", now=now)

        event = calendar_request_to_event(request)

        self.assertEqual(event["summary"], "doctor")
        self.assertEqual(event["start"]["dateTime"], "2026-07-20T09:00:00-04:00")
        self.assertEqual(
            event["reminders"],
            {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 10}],
            },
        )

    def test_parse_month_name_date_with_weekday_and_timezone(self):
        now = datetime(2026, 7, 13, 9, 0, tzinfo=ZoneInfo("America/New_York"))

        request = parse_calendar_request(
            "set my doctor appointment for Monday, July 15, 2026 at 11 AM EST notes: annual checkup",
            now=now,
        )

        self.assertEqual(request.title, "doctor appointment")
        self.assertEqual(request.start.isoformat(), "2026-07-15T11:00:00-04:00")
        self.assertEqual(request.end.isoformat(), "2026-07-15T12:00:00-04:00")
        self.assertEqual(request.notes, "annual checkup")

    @patch("google_calendar.create_google_calendar_event", return_value={"htmlLink": "https://calendar.google.com/event"})
    def test_handle_calendar_message_creates_event(self, mock_create):
        now = datetime(2026, 7, 13, 9, 0, tzinfo=ZoneInfo("America/New_York"))

        output = handle_calendar_message("schedule appointment haircut tomorrow at 3pm notes: cash only", now=now)

        self.assertIn("Done. I put **haircut** on your Google Calendar", output)
        self.assertIn("haircut", output)
        self.assertIn("https://calendar.google.com/event", output)
        created_request = mock_create.call_args.args[0]
        self.assertEqual(created_request.notes, "cash only")

    def test_missing_date_time_asks_naturally(self):
        output = handle_calendar_message("set a doctor appointment")

        self.assertIn("I can put that on your calendar", output)
        self.assertIn("I just need the date and time", output)
        self.assertNotIn("[google_calendar]", output)

    @patch("google_calendar.create_google_calendar_event", return_value={"htmlLink": "https://calendar.google.com/event"})
    def test_dispatch_tool_message_routes_calendar(self, mock_create):
        result = dispatch_tool_message("calendar oil change tomorrow at 10am", "127.0.0.1")

        self.assertIsNotNone(result)
        self.assertEqual(result.tool_id, "google_calendar")
        self.assertIn("Done. I put **oil change** on your Google Calendar", result.output)
        self.assertTrue(mock_create.called)

    @patch("google_calendar.create_google_calendar_event", return_value={"htmlLink": "https://calendar.google.com/event"})
    def test_dispatch_routes_natural_doctor_appointment_without_llm(self, mock_create):
        result = dispatch_tool_message(
            "set my doctor appointment for Monday, July 15, 2026 at 11 AM EST",
            "127.0.0.1",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.tool_id, "google_calendar")
        self.assertIn("Done. I put **doctor appointment** on your Google Calendar", result.output)
        self.assertTrue(mock_create.called)


if __name__ == "__main__":
    unittest.main()
