# SPDX-License-Identifier: GPL-3.0-or-later
#
# The two provider clients, against the two servers in tests/fakecal.py.
#
# Every test here goes through the real `calendar/http.py`: the URL, the query
# string, the method, the headers and the JSON body are built by the code under
# test and read by a server that refuses what the real one refuses. What is
# asserted is therefore what would go on the wire, not what a mock was told to
# return.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import unittest

from cormani.calendar import errors, google, graph
from cormani.calendar.http import Http
from tests.calwire import Reply, fail
from tests.fakecal import TOKEN, FakeGoogle, FakeGraph

WINDOW = ("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")


def google_client(server, address="someone@gmail.com"):
    transport = server.transport()
    return google.GoogleCalendar(Http(TOKEN, opener=transport),
                                 address=address), transport


def graph_client(server, address="someone@outlook.com"):
    transport = server.transport()
    return graph.GraphCalendar(Http(TOKEN, opener=transport),
                               address=address), transport


class GoogleReading(unittest.TestCase):
    def setUp(self):
        self.server = FakeGoogle()
        self.server.add_calendar("primary", summary="Manish", primary=True,
                                 reminder=10)
        self.server.add_calendar("holidays@group.v.calendar.google.com",
                                 summary="Holidays", access="reader")
        self.client, self.transport = google_client(self.server)

    def test_calendars_carry_access_and_the_default_reminder(self):
        found = self.client.calendars()
        self.assertEqual([(c.remote_id, c.writable, c.is_primary, minutes)
                          for c, minutes in found],
                         [("primary", True, True, 10),
                          ("holidays@group.v.calendar.google.com", False,
                           False, None)])

    def test_a_window_asks_for_expanded_instances(self):
        self.server.add_event("primary", "e1", summary="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        page = self.client.events("primary", start=WINDOW[0], end=WINDOW[1])
        query = self.transport.last().query
        self.assertEqual(query["singleEvents"], "true")
        self.assertEqual(query["timeMin"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(query["orderBy"], "startTime")
        self.assertEqual([e.summary for e in page.events], ["Call"])
        self.assertTrue(page.sync_token)

    def test_an_all_day_event_keeps_its_date(self):
        self.server.add_event("primary", "d1", summary="Diwali",
                              start="2026-09-08", end="2026-09-09", all_day=True)
        page = self.client.events("primary", start=WINDOW[0], end=WINDOW[1])
        event = page.events[0]
        self.assertTrue(event.all_day)
        self.assertEqual((event.starts_at, event.ends_at),
                         ("2026-09-08", "2026-09-09"))

    def test_paging_repeats_every_parameter(self):
        self.server.page_size = 1
        for n in range(3):
            self.server.add_event("primary", f"e{n}",
                                  start=f"2026-09-0{n + 1}T09:00:00+00:00",
                                  end=f"2026-09-0{n + 1}T10:00:00+00:00")
        seen, token = [], ""
        while True:
            page = self.client.events("primary", start=WINDOW[0], end=WINDOW[1],
                                      page_token=token)
            seen.extend(e.remote_id for e in page.events)
            token = page.next_token
            if not token:
                break
        self.assertEqual(seen, ["e0", "e1", "e2"])
        # Every request carried the window, which the API requires and which
        # this server checks by giving a different answer without it.
        for call in self.transport.calls:
            self.assertIn("timeMin", call.query)

    def test_an_incremental_pass_returns_only_what_changed(self):
        self.server.add_event("primary", "e1", summary="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        self.server.add_event("primary", "e2", summary="Other",
                              start="2026-09-13T09:00:00+00:00",
                              end="2026-09-13T10:00:00+00:00")
        first = self.client.events("primary", start=WINDOW[0], end=WINDOW[1])
        self.server.touch("primary", "e2", summary="Renamed")
        second = self.client.events("primary", sync_token=first.sync_token)
        self.assertEqual([e.summary for e in second.events], ["Renamed"])
        self.assertNotIn("timeMin", self.transport.last().query)
        self.assertEqual(self.transport.last().query["showDeleted"], "true")

    def test_a_cancellation_arrives_as_a_deletion(self):
        self.server.add_event("primary", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        first = self.client.events("primary", start=WINDOW[0], end=WINDOW[1])
        self.server.cancel_event("primary", "e1")
        second = self.client.events("primary", sync_token=first.sync_token)
        self.assertEqual([(e.remote_id, e.deleted) for e in second.events],
                         [("e1", True)])

    def test_a_stale_token_is_told_apart_from_a_failure(self):
        with self.assertRaises(errors.TokenExpired):
            self.client.events("primary", sync_token="sync-nonsense")

    def test_the_response_of_an_invitation_is_read_from_the_self_attendee(self):
        self.server.add_event(
            "primary", "e1", organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "someone@gmail.com"])
        page = self.client.events("primary", start=WINDOW[0], end=WINDOW[1])
        event = page.events[0]
        self.assertEqual(event.my_response, "needsAction")
        self.assertEqual(event.organiser_addr, "them@example.com")
        self.assertEqual([(a.address, a.is_self, a.is_organiser)
                          for a in event.attendees],
                         [("them@example.com", False, True),
                          ("someone@gmail.com", True, False)])

    def test_an_appointment_with_no_guests_is_not_awaiting_a_reply(self):
        self.server.add_event("primary", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        event = self.client.events("primary", start=WINDOW[0],
                                   end=WINDOW[1]).events[0]
        self.assertEqual(event.my_response, "accepted")


class GoogleWriting(unittest.TestCase):
    def setUp(self):
        self.server = FakeGoogle()
        self.server.add_calendar("primary", primary=True)
        self.client, self.transport = google_client(self.server)

    def test_a_create_with_guests_asks_the_provider_to_invite_them(self):
        event = self.client.create("primary", {
            "summary": "Reading group", "starts_at": "2026-09-12T09:00:00+00:00",
            "ends_at": "2026-09-12T10:00:00+00:00", "all_day": False},
            [{"address": "lyle@covalent.example", "name": "Lyle"}])
        self.assertEqual(event.summary, "Reading group")
        call = self.transport.last()
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.query["sendUpdates"], "all")
        self.assertEqual(call.body["attendees"],
                         [{"email": "lyle@covalent.example", "displayName": "Lyle"}])

    def test_a_private_appointment_asks_the_provider_to_invite_nobody(self):
        self.client.create("primary", {"summary": "Dentist"}, None)
        self.assertEqual(self.transport.last().query["sendUpdates"], "none")

    def test_a_patch_sends_only_what_changed(self):
        self.server.add_event("primary", "e1", summary="Old",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        etag = self.server.events["primary"]["e1"]["etag"]
        self.client.update("primary", "e1", {"location": "Room 2"}, etag=etag)
        call = self.transport.last()
        self.assertEqual(call.method, "PATCH")
        self.assertEqual(call.body, {"location": "Room 2"})
        self.assertEqual(call.if_match, etag)
        self.assertEqual(self.server.events["primary"]["e1"]["summary"], "Old")

    def test_a_stale_etag_is_refused_rather_than_overwriting(self):
        self.server.add_event("primary", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        stale = self.server.events["primary"]["e1"]["etag"]
        self.server.touch("primary", "e1", summary="Changed elsewhere")
        with self.assertRaises(errors.Conflict):
            self.client.update("primary", "e1", {"summary": "Mine"}, etag=stale)
        self.assertEqual(self.server.events["primary"]["e1"]["summary"],
                         "Changed elsewhere")

    def test_answering_sends_the_list_that_is_there_not_the_one_we_held(self):
        self.server.add_event(
            "primary", "e1", organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "someone@gmail.com"])
        # Somebody is added to the meeting after this store last looked.
        self.server.events["primary"]["e1"]["attendees"].append(
            {"email": "late@example.com", "responseStatus": "needsAction"})
        self.client.respond("primary", "e1", "accepted")
        self.assertEqual([c.method for c in self.transport.calls],
                         ["GET", "PATCH"])
        sent = self.transport.last().body["attendees"]
        self.assertEqual([g["email"] for g in sent],
                         ["them@example.com", "someone@gmail.com",
                          "late@example.com"])
        self.assertEqual(
            [g["responseStatus"] for g in sent if g["email"] == "someone@gmail.com"],
            ["accepted"])

    def test_answering_when_invited_through_a_group_adds_this_user(self):
        self.server.add_event(
            "primary", "e1", organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "list@example.com"])
        self.client.respond("primary", "e1", "declined")
        sent = self.transport.last().body["attendees"]
        self.assertIn({"email": "someone@gmail.com",
                       "responseStatus": "declined"}, sent)

    def test_a_delete_tells_the_guests(self):
        self.server.add_event("primary", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        self.client.delete("primary", "e1")
        self.assertEqual(self.transport.last().method, "DELETE")
        self.assertEqual(self.transport.last().query["sendUpdates"], "all")
        self.assertEqual(self.server.events["primary"]["e1"]["status"],
                         "cancelled")

    def test_an_identifier_with_a_slash_stays_one_path_segment(self):
        self.server.add_calendar("odd/id")
        self.server.add_event("odd/id", "e/1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        found = self.client.event("odd/id", "e/1")
        self.assertEqual(found.remote_id, "e/1")


class GraphReading(unittest.TestCase):
    def setUp(self):
        self.server = FakeGraph()
        self.server.add_calendar("cal-1", name="Calendar", default=True)
        self.client, self.transport = graph_client(self.server)

    def test_every_request_asks_for_utc(self):
        self.server.add_event("cal-1", "e1", subject="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        page = self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
        self.assertIn('outlook.timezone="UTC"',
                      self.transport.last().headers["prefer"])
        self.assertEqual(page.events[0].starts_at, "2026-09-12T09:00:00+00:00")

    def test_the_window_goes_up_without_an_offset(self):
        self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
        query = self.transport.last().query
        self.assertEqual(query["startDateTime"], "2026-09-01T00:00:00")
        self.assertEqual(query["endDateTime"], "2026-10-01T00:00:00")

    def test_an_all_day_event_survives_the_utc_conversion(self):
        """Midnight in Bombay is 18:30 the previous day in UTC.

        Graph converts it and the date must come back unmoved. This is the
        rounding in `graph._all_day_date`, and it is the reason it exists.
        """
        self.server.add_event("cal-1", "d1", subject="Diwali", all_day=True,
                              start="2026-09-07T18:30:00+00:00",
                              end="2026-09-08T18:30:00+00:00")
        page = self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
        event = page.events[0]
        self.assertTrue(event.all_day)
        self.assertEqual((event.starts_at, event.ends_at),
                         ("2026-09-08", "2026-09-09"))

    def test_a_delta_link_is_followed_exactly_as_given(self):
        self.server.add_event("cal-1", "e1", subject="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        first = self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
        self.assertTrue(first.sync_token.startswith("https://"))
        self.server.touch("cal-1", "e1", subject="Renamed")
        second = self.client.events("cal-1", sync_token=first.sync_token)
        self.assertEqual([e.summary for e in second.events], ["Renamed"])
        self.assertEqual(self.transport.last().url, first.sync_token)

    def test_a_removal_arrives_as_a_deletion(self):
        self.server.add_event("cal-1", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        first = self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
        self.server.remove_event("cal-1", "e1")
        second = self.client.events("cal-1", sync_token=first.sync_token)
        self.assertEqual([(e.remote_id, e.deleted) for e in second.events],
                         [("e1", True)])

    def test_an_expired_delta_link_is_told_apart_from_a_failure(self):
        with self.assertRaises(errors.TokenExpired):
            self.client.events(
                "cal-1", sync_token="https://graph.microsoft.com/v1.0/me/"
                                    "calendars/cal-1/calendarView/delta"
                                    "?$deltatoken=gone")

    def test_the_five_response_words_become_four(self):
        for theirs, ours in (("none", "needsAction"),
                             ("notResponded", "needsAction"),
                             ("organizer", "accepted"),
                             ("tentativelyAccepted", "tentative"),
                             ("declined", "declined")):
            self.server.events["cal-1"].clear()
            self.server.add_event("cal-1", "e1", response=theirs,
                                  start="2026-09-12T09:00:00+00:00",
                                  end="2026-09-12T10:00:00+00:00")
            page = self.client.events("cal-1", start=WINDOW[0], end=WINDOW[1])
            self.assertEqual(page.events[0].my_response, ours, theirs)

    def test_the_signed_in_user_is_marked_in_the_guest_list(self):
        self.server.add_event(
            "cal-1", "e1", organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "someone@outlook.com"])
        event = self.client.events("cal-1", start=WINDOW[0],
                                   end=WINDOW[1]).events[0]
        self.assertEqual([(a.address, a.is_self) for a in event.attendees],
                         [("them@example.com", False),
                          ("someone@outlook.com", True)])


class GraphWriting(unittest.TestCase):
    def setUp(self):
        self.server = FakeGraph()
        self.server.add_calendar("cal-1", default=True)
        self.client, self.transport = graph_client(self.server)

    def test_a_create_carries_the_body_and_the_guests(self):
        self.client.create("cal-1", {
            "summary": "Reading group", "description": "Bring the paper",
            "starts_at": "2026-09-12T09:00:00+00:00",
            "ends_at": "2026-09-12T10:00:00+00:00", "all_day": False},
            [{"address": "lyle@covalent.example", "optional": True}])
        body = self.transport.last().body
        self.assertEqual(body["subject"], "Reading group")
        self.assertEqual(body["body"], {"contentType": "text",
                                        "content": "Bring the paper"})
        self.assertEqual(body["start"], {"dateTime": "2026-09-12T09:00:00",
                                         "timeZone": "UTC"})
        self.assertEqual(body["attendees"][0]["type"], "optional")

    def test_answering_is_an_action_and_not_a_patch(self):
        self.server.add_event(
            "cal-1", "e1", organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "someone@outlook.com"])
        event = self.client.respond("cal-1", "e1", "tentative",
                                    comment="I may be late")
        posted = self.transport.calls[0]
        self.assertTrue(posted.path.endswith("/tentativelyAccept"))
        self.assertEqual(posted.body, {"sendResponse": True,
                                       "comment": "I may be late"})
        self.assertEqual(event.my_response, "tentative")

    def test_needs_action_cannot_be_sent(self):
        with self.assertRaises(ValueError):
            self.client.respond("cal-1", "e1", "needsAction")

    def test_a_stale_etag_is_refused(self):
        self.server.add_event("cal-1", "e1", subject="Old",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        stale = self.server.events["cal-1"]["e1"]["@odata.etag"]
        self.server.touch("cal-1", "e1", subject="Changed elsewhere")
        with self.assertRaises(errors.Conflict):
            self.client.update("cal-1", "e1", {"summary": "Mine"}, etag=stale)


class Failures(unittest.TestCase):
    """What each refusal means. `calendar/errors.py` decides; this pins it."""

    def client(self, raiser):
        return google.GoogleCalendar(Http(TOKEN, opener=raiser))

    def test_a_missing_token_is_an_authentication_failure(self):
        server = FakeGoogle()
        server.add_calendar("primary")
        client = google.GoogleCalendar(Http("wrong", opener=server.transport()))
        with self.assertRaises(errors.AuthFailed):
            client.calendars()

    def test_a_403_for_speed_is_transient_and_a_403_for_permission_is_not(self):
        def rate(request, timeout=None):
            raise fail(403, {"error": {"errors": [
                {"reason": "userRateLimitExceeded"}], "message": "slow down"}},
                {"Retry-After": "120"})

        def forbidden(request, timeout=None):
            raise fail(403, {"error": {"errors": [{"reason": "forbidden"}],
                                       "message": "not allowed"}})

        with self.assertRaises(errors.RateLimited) as caught:
            self.client(rate).calendars()
        self.assertEqual(caught.exception.retry_after, 120.0)
        self.assertTrue(errors.is_transient(caught.exception))
        with self.assertRaises(errors.NotAuthorised) as caught:
            self.client(forbidden).calendars()
        self.assertFalse(errors.is_transient(caught.exception))

    def test_a_network_failure_names_no_url(self):
        def broken(request, timeout=None):
            raise OSError("connection refused to "
                          "https://www.googleapis.com/someone@gmail.com")

        with self.assertRaises(errors.Transient) as caught:
            self.client(broken).calendars()
        self.assertNotIn("someone@gmail.com", str(caught.exception))

    def test_nothing_puts_a_token_in_a_repr(self):
        http = Http("secret-token-value")
        self.assertNotIn("secret", repr(http))

    def test_a_204_is_not_a_failure(self):
        def empty(request, timeout=None):
            return Reply(204, None)

        client = google.GoogleCalendar(Http(TOKEN, opener=empty))
        self.assertIsNone(client.delete("primary", "e1"))


if __name__ == "__main__":
    unittest.main()
