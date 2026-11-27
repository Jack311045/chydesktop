import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx2
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from gmail_reader import GmailError, GmailReader, LIMIT, MailSearch, ReconnectGmail, SCOPE


class Inbox:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.zone = ZoneInfo("America/New_York")
        self.day = date(2026, 11, 1)
        start = datetime(2026, 11, 1, tzinfo=self.zone)
        self.messages = {
            "early": (start, True, "Résumé received"),
            "late": (start + timedelta(hours=23), True, "Interview invitation"),
            "read": (start + timedelta(hours=10), False, "Read message"),
            "yesterday": (start - timedelta(seconds=1), True, "Yesterday"),
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, *, params, timeout, allow_redirects):
        self.calls.append((url, params))
        if self.failure:
            code, reasons = self.failure
            return Response(code, {"error": {"errors": [{"reason": value} for value in reasons]}})
        if url.endswith("/messages"):
            self.assert_labels(params)
            terms = params["q"].split()
            after = next((int(word[6:]) for word in terms if word.startswith("after:")), 0)
            before = next((int(word[7:]) for word in terms if word.startswith("before:")), float("inf"))
            ids = [key for key, (stamp, unread, subject) in self.messages.items()
                   if unread and after < stamp.timestamp() < before
                   and ("interview" not in params["q"].casefold() or "interview" in subject.casefold())]
            limit = params["maxResults"]
            return Response(200, {"messages": [{"id": key} for key in ids[:limit]],
                                  "nextPageToken": "more" if len(ids) > limit else None})
        key = url.rsplit("/", 1)[1]
        stamp, unread, subject = self.messages[key]
        payload = {"headers": [{"name": "Subject", "value": subject},
                               {"name": "From", "value": "Private Sender <fixture@example.invalid>"}]}
        if params["format"] == "full":
            payload.update({"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": encode("<b>面試</b><img src='https://example.invalid'>")}},
                {"mimeType": "text/plain", "body": {"data": encode("面試 — private preview")}},
            ]})
        return Response(200, {"internalDate": str(int(stamp.timestamp() * 1000)), "payload": payload})

    @staticmethod
    def assert_labels(params):
        assert set(params["labelIds"]) == {"INBOX", "UNREAD"}


class Response:
    def __init__(self, code, payload):
        self.status_code = code
        self.payload = payload

    def json(self):
        return self.payload


def encode(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class GmailReaderTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.reader = GmailReader(Path(folder.name))
        self.inbox = Inbox()
        credentials = patch.object(self.reader, "credentials")
        self.credentials = credentials.start()
        self.credentials.return_value.refresh_token = None
        self.addCleanup(credentials.stop)
        session = patch("gmail_reader.AuthorizedSession", return_value=self.inbox)
        session.start()
        self.addCleanup(session.stop)

    def test_day_search_matching_and_first_page_bound(self):
        day, zone = self.inbox.day, self.inbox.zone
        result = self.reader.read(MailSearch(day), zone)
        self.assertEqual([row.id for row in result.rows], ["late", "early"])
        query = self.inbox.calls[0][1]["q"]
        self.assertEqual(query, "after:1793505599 before:1793595600")
        self.assertEqual(self.inbox.calls[0][1]["maxResults"], LIMIT)
        self.assertTrue(all(params.get("format") == "metadata" for _, params in self.inbox.calls[1:]))
        self.assertEqual([row.id for row in self.reader.read(MailSearch(day, ("Interview",)), zone).rows],
                         ["late"])
        self.assertFalse(self.reader.read(MailSearch(day + timedelta(days=2)), zone).rows)
        start = self.inbox.messages["early"][0]
        self.inbox.messages.update({f"extra{n}": (start, True, "Extra") for n in range(LIMIT + 1)})
        self.inbox.calls.clear()
        result = self.reader.read(MailSearch(day), zone)
        self.assertEqual(len(result.rows), LIMIT)
        self.assertTrue(result.more)
        self.assertEqual(len(self.inbox.calls), LIMIT + 1)

    def test_preview_uses_get_only_and_prefers_one_body(self):
        result = self.reader.preview("late")
        self.assertEqual(result, "面試 — private preview")
        self.assertTrue(self.inbox.messages["late"][1])
        self.assertEqual(len(self.inbox.calls), 1)
        self.assertEqual(self.inbox.calls[0][1]["format"], "full")

    def test_authorization_and_network_errors_are_distinct(self):
        from requests import ConnectionError

        self.inbox.failure = (403, ["insufficientPermissions"])
        with self.assertRaisesRegex(ReconnectGmail, "重新连接 Gmail"):
            self.reader.read(MailSearch(), self.inbox.zone)
        with patch.object(self.inbox, "get", side_effect=ConnectionError("private response")):
            with self.assertRaisesRegex(GmailError, "无法连接到 Gmail") as error:
                self.reader.read(MailSearch(), self.inbox.zone)
        self.assertNotIn("private response", str(error.exception))

    def test_html_preview_strips_active_content_and_remote_images(self):
        def html_message(url, **kwargs):
            return Response(200, {"payload": {
                "mimeType": "text/html", "body": {
                    "data": encode("<head><title>Hidden</title></head><p>面試 &amp; résumé</p>"
                                   "<script>alert('hidden')</script><img src='https://example.invalid/tracker'>")
                },
            }})

        with patch.object(self.inbox, "get", side_effect=html_message) as get:
            self.assertEqual(self.reader.preview("late"), "面試 & résumé")
        self.assertEqual(get.call_count, 1)

    def test_reply_suppression_does_not_clear_a_separate_deadline(self):
        from mail_actions import classify

        stamp = self.inbox.messages["late"][0]
        actions = classify("fixture", "Recruiter", "Assessment",
                           "Please confirm your availability. Please complete the assessment by end of week."
                           "\nOn Thursday Sam wrote:\n> Please send your résumé.",
                           stamp, answered=True, direction_known=True)
        self.assertEqual([action.kind for action in actions], ["assessment"])
        self.assertEqual(actions[0].deadline, "")
        self.assertEqual(actions[0].deadline_quote, "by end of week")

    def test_two_source_scan_paging_partial_cache_and_local_fallback(self):
        from daily_brief import mail_text, scan_text
        from mail_actions import Decision, adjudicate

        now = datetime(2026, 11, 1, 12, tzinfo=self.inbox.zone)
        subjects = {f"promo{i}": (f"Newsletter {i}", "Find new roles.") for i in range(94)}
        subjects.update({
            "digest": ("Priority matches for Jamie", "Jamie, review these recommended roles based on your profile."),
            "older": ("Orion interview", "Please confirm your availability."),
            "invite": ("Acme interview", "Please schedule your interview using the portal."),
            "direct": ("A quick note", "I noticed your work on routing. Would you have a few minutes to chat?"),
            "review": ("Review (99)", "Your scheduled interview is still pending details."),
            "fail": ("Fixture failure", "Not returned."),
        })
        signal = ["invite", "older", "digest", "review", "fail"]
        broad = [f"promo{i}" for i in range(22)] + ["direct", *[f"promo{i}" for i in range(22, 94)],
                                                   "older", "review"]
        body_reads = []
        pages = []
        revisions = {"older": "1"}
        later = False
        reopened = False

        def get(url, *, params, **kwargs):
            key = url.rsplit("/", 1)[-1]
            if key == "threads":
                high = "is:important" in params["q"]
                rows = signal if high else broad
                offset = int(params.get("pageToken", 0))
                pages.append((high, offset))
                return Response(200, {"threads": [{"id": key} for key in rows[offset:offset + 20]],
                                      "nextPageToken": str(offset + 20) if len(rows) > offset + 20 else None})
            if key == "fail":
                return Response(503, {})
            subject, body = subjects.get(key, ("Orion interview", "I am available at 2 PM."))
            if key == "new-request":
                body = "Please confirm your new availability for the interview."
            if params["format"] == "metadata":
                stamp = str(int((now - timedelta(days=2 if key == "older" else 0)).timestamp() * 1000))
                headers = [{"name": "Subject", "value": subject}, {"name": "From", "value": "noreply@fixture.invalid"},
                           {"name": "Message-ID", "value": f"<{key}>"}]
                labels = ["INBOX"] if key in {"older", "direct", "review"} else ["INBOX", "CATEGORY_PROMOTIONS"]
                messages = [{"id": key, "internalDate": stamp, "labelIds": labels, "payload": {"headers": headers}}]
                if key == "older" and later:
                    messages.append({"id": "sent", "internalDate": str(int(stamp) + 1000), "labelIds": ["SENT"],
                                     "payload": {"headers": [{"name": "In-Reply-To", "value": "<older>"}]}})
                if key == "older" and reopened:
                    messages.append({"id": "new-request", "internalDate": str(int(stamp) + 2000), "labelIds": ["INBOX"],
                                     "payload": {"headers": headers}})
                return Response(200, {"historyId": revisions.get(key, "1"), "messages": messages})
            body_reads.append(key)
            return Response(200, {"payload": {"mimeType": "text/plain", "body": {"data": encode(body)}}})

        with patch.object(self.inbox, "get", side_effect=get):
            result = self.reader.scan_actions(now, self.inbox.zone, candidate_limit=99)
            ids = {item.message_id for item in result.actions if not item.review}
            self.assertEqual(ids, {"older", "invite", "direct"})
            self.assertNotIn("digest", {item.message_id for item in result.actions})
            self.assertEqual((result.scanned, result.checked, result.failures), (99, 30, 1))
            self.assertTrue(result.limited and result.content_capped)
            self.assertIn((False, 20), pages)
            self.assertIn((True, 0), pages)
            self.assertIn("Partial Gmail results", scan_text(result, "en"))
            previous = len(body_reads)
            self.reader.scan_actions(now, self.inbox.zone, candidate_limit=99)
            self.assertEqual(len(body_reads), previous)
            later = True
            revisions["older"] = "2"
            updated = self.reader.scan_actions(now, self.inbox.zone, candidate_limit=99)
            self.assertNotIn("older", {item.message_id for item in updated.actions})
            self.assertIn("sent", body_reads[previous:])
            reopened = True
            revisions["older"] = "3"
            updated = self.reader.scan_actions(now, self.inbox.zone, candidate_limit=99)
            self.assertIn("new-request", {item.message_id for item in updated.actions})
            fallback = self.reader.scan_actions(now, self.inbox.zone, candidate_limit=99, semantic=False)
            self.assertNotIn("direct", {item.message_id for item in fallback.actions})
            self.assertIn("Rules-only fallback", scan_text(fallback, "en"))
            review_only = mail_text(type(result)(result.day, result.scanned, True,
                                               tuple(item for item in result.actions if item.review), now),
                                    "", "en")
            self.assertIn("No confirmed actions", review_only)
            self.assertIn("Review (1)\n", review_only)

        class FabricatedEvidence:
            def decide(self, ident, subject, text, context):
                return (Decision("action", ident, "Would you have a few minutes to chat?", "reply"),
                        Decision("action", "wrong-id", text, "reply"))

        self.assertEqual(adjudicate("source", "Fixture", "A note", "The weather is clear.", now,
                                    answered=False, direction_known=True, context=(),
                                    classifier=FabricatedEvidence()), [])

    def test_matching_calendar_does_not_hide_rsvp_or_assessment(self):
        from calendar_reader import CalendarDay, Event
        from daily_brief import matching_source, merged_obligation
        from mail_actions import MailAction

        now = datetime(2026, 11, 1, 12, tzinfo=self.inbox.zone)
        event = Event("Work", "Orion interview", now, now + timedelta(hours=1), False, True)
        snapshot = CalendarDay(now.date(), self.inbox.zone, (), (event,), (), now)
        for kind, excerpt, merged in (
            ("interview", "Schedule your interview", True),
            ("interview", "Please RSVP", False),
            ("assessment", "Complete the assessment by tomorrow", False),
        ):
            action = MailAction("fixture", kind, "Recruiter", "Orion interview", now, excerpt)
            self.assertEqual(matching_source(action, [], snapshot), event.title)
            self.assertEqual(merged_obligation(action, [], snapshot), merged)


class GmailAuthorizationTests(unittest.TestCase):
    def test_gmail_uses_its_own_saved_read_only_authorization(self):
        from calendar_reader import AUTH_URI, TOKEN_URI
        from google.oauth2.credentials import Credentials

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            reader = GmailReader(folder)
            reader.client.write_text(json.dumps({"installed": {"auth_uri": AUTH_URI, "token_uri": TOKEN_URI}}))
            calendar_token = folder / "google_calendar_token.json"
            calendar_token.write_text("calendar fixture")
            inbox = Inbox()
            gmail_credentials = Credentials("fixture-only", refresh_token="fixture-refresh", token_uri=TOKEN_URI,
                                            client_id="fixture", client_secret="fixture", scopes=[SCOPE])
            with patch("gmail_reader.DesktopFlow.from_client_config") as flow, patch(
                "gmail_reader.AuthorizedSession", return_value=inbox
            ):
                flow.return_value.run_local_server.return_value = gmail_credentials
                reader.read(MailSearch(inbox.day), inbox.zone, connect=True)
            self.assertEqual(flow.call_args.kwargs["scopes"], [SCOPE])
            self.assertEqual(reader.credentials(False).scopes, [SCOPE])
            self.assertEqual(calendar_token.read_text(), "calendar fixture")

    def test_gmail_reconnect_runs_after_an_in_flight_read(self):
        from calendar_reader import TOKEN_URI
        from google.oauth2.credentials import Credentials

        def credentials_for(account):
            return Credentials(account, refresh_token=account, token_uri=TOKEN_URI,
                               client_id="fixture", client_secret="fixture", scopes=[SCOPE])

        with TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as pool:
            reader = GmailReader(Path(directory))
            old, new = credentials_for("old-fixture"), credentials_for("new-fixture")
            reader.token.write_text(old.to_json())
            started, release, reconnecting = Event(), Event(), Event()

            def read(_):
                started.set()
                self.assertTrue(release.wait(5))
                reader.thread_cache["fixture"] = ((), ())

            def reconnect():
                reconnecting.set()
                reader._use(lambda _: None, connect=True)

            with patch.object(reader, "credentials", side_effect=lambda connect: new if connect else old), patch(
                "gmail_reader.AuthorizedSession", return_value=Inbox()
            ):
                scan = pool.submit(reader._use, read)
                try:
                    self.assertTrue(started.wait(2))
                    login = pool.submit(reconnect)
                    self.assertTrue(reconnecting.wait(2))
                    with self.assertRaises(TimeoutError):
                        login.result(timeout=0.2)
                finally:
                    release.set()
                scan.result(timeout=5)
                login.result(timeout=5)
            self.assertEqual(reader.credentials(False).refresh_token, "new-fixture")
            self.assertEqual(reader.thread_cache, {})


class GmailWindowTests(unittest.TestCase):
    def test_daily_brief_and_reconnect_do_not_overlap(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from calendar_reader import CalendarError
        from chihaya import MainWindow
        from gmail_reader import MailResult
        from mail_actions import MailAction, MailScan

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        scan_started, release_scan, login_started, release_login = (Event() for _ in range(4))

        def wait_for(done):
            for _ in range(250):
                if done():
                    return
                QTest.qWait(10)
            self.fail("Background operation did not finish")

        def scan(now, zone, **kwargs):
            scan_started.set()
            if not release_scan.wait(5):
                raise GmailError("Fixture scan timed out.")
            action = MailAction("fixture", "reply", "Fixture Sender", "Old account message", now, "Please reply.")
            return MailScan(now.date(), 1, False, (action,), now, checked=1)

        def read(search, zone, *, connect=False):
            login_started.set()
            if not release_login.wait(5):
                raise GmailError("Fixture login timed out.")
            return MailResult(search, (), False)

        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory) / "portraits")
            mail = window.mail
            mail.reader.token.touch()
            with patch.object(mail.reader, "scan_actions", side_effect=scan) as scanner, patch.object(
                mail.reader, "read", side_effect=read
            ) as reader, patch.object(window.calendar.reader, "read", side_effect=CalendarError("Fixture unavailable.")):
                try:
                    window.entry.setText("give me a daily brief")
                    window.send()
                    wait_for(scan_started.is_set)
                    self.assertFalse(mail.connect_button.isEnabled())
                    self.assertFalse(mail.remove_button.isEnabled())
                    self.assertFalse(mail.refresh(connect=True))
                    mail.disconnect()
                    self.assertTrue(mail.reader.token.is_file())
                    reader.assert_not_called()
                    release_scan.set()
                    wait_for(lambda: window.daily_worker is None)
                    self.assertIn("Old account message", window.history.toPlainText())
                    self.assertTrue(mail.connect_button.isEnabled())

                    self.assertTrue(mail.refresh(connect=True))
                    wait_for(login_started.is_set)
                    self.assertFalse(window.daily_refresh.isEnabled())
                    window.start_daily_brief()
                    self.assertIsNone(window.daily_worker)
                    scanner.assert_called_once()
                    release_login.set()
                    wait_for(lambda: mail.worker is None)
                    self.assertIsNone(window.daily_scan)
                    self.assertNotIn("Old account message", window.history.toPlainText())
                    self.assertTrue(window.daily_refresh.isEnabled())
                finally:
                    release_scan.set()
                    release_login.set()
                    wait_for(lambda: window.daily_worker is None and mail.worker is None)
                    window.quit_app()

    def test_daily_brief_languages_read_request_and_import(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from calendar_reader import CalendarDay, Event
        from chihaya import MainWindow

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 20, 10, tzinfo=zone)
        mails = {
            "interview": ("Private interview", "Could you please confirm your availability for a call?", False),
            "newsletter": ("Interview newsletter", "Schedule your interview practice.", True),
            "confirmation": ("Interview confirmed", "Your interview is confirmed. No reply required.", True),
            "assessment": ("Private assessment", "Please complete the assessment by Friday at 5 PM.", False),
            "answered": ("Private answered request", "Please confirm your availability.", True),
            "question": ("Next steps", "Your scheduled interview is still pending details.", False),
        }
        calls = []
        payloads = []

        def mock_claude(request):
            payloads.append(json.loads(request.content))
            action = dict.fromkeys(("title", "target", "date", "field", "minutes"), "") | {"kind": "none"}
            return httpx2.Response(200, json={
                "id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture",
                "content": [{"type": "text", "text": json.dumps({"reply": "Ready.", "action": action})}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        class DailyInbox(Inbox):
            def get(self, url, *, params, timeout, allow_redirects):
                calls.append((url, params))
                stamp = str(int((now - timedelta(hours=1)).timestamp() * 1000))
                key = url.rsplit("/", 1)[1]
                if key == "threads":
                    assert params["labelIds"] == ["INBOX"]
                    assert params["maxResults"] == 20
                    assert "is:unread" not in params["q"]
                    return Response(200, {"threads": [{"id": item} for item in mails]})
                subject, body, unread = mails.get(key, ("", "I am available at 2 PM.", False))
                if "/threads/" in url:
                    messages = [{"id": key, "internalDate": stamp, "labelIds": ["INBOX", "UNREAD"] if unread else ["INBOX"],
                                 "payload": {"headers": [{"name": "Subject", "value": subject},
                                                          {"name": "From", "value": "Fixture Recruiter"},
                                                          {"name": "Message-ID", "value": f"<{key}>"}]}}]
                    if key == "answered":
                        messages.append({"id": "sent", "internalDate": str(int(stamp) + 1000), "labelIds": ["SENT"],
                                         "payload": {"headers": [{"name": "In-Reply-To", "value": "<answered>"}]}})
                    return Response(200, {"messages": messages})
                if params["fields"] == "payload":
                    return Response(200, {"payload": {"mimeType": "multipart/alternative", "parts": [
                        {"mimeType": "text/plain", "body": {"data": encode(body)}},
                        {"mimeType": "text/html", "body": {"data": encode(f"<p>{body}</p>")}},
                    ]}})
                return Response(200, {"snippet": body, "internalDate": stamp,
                                      "labelIds": ["INBOX", "UNREAD"] if unread else ["INBOX"],
                                      "payload": {"headers": [{"name": "Subject", "value": subject},
                                                               {"name": "From", "value": "Fixture Recruiter"},
                                                               {"name": "Message-ID", "value": f"<{key}>"}]}})

        def wait_for(done):
            for _ in range(250):
                if done():
                    return
                QTest.qWait(10)
            self.fail("Background operation did not finish")

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / ".env").write_text("ANTHROPIC_API_KEY=fixture-only\n")
            window = MainWindow(folder / "portraits")
            window.calendar.zone = zone
            window.mail.reader.token.touch()
            store = window.store
            assert store is not None
            store.add("Local planned work", "", "30", "today", today=now.date())
            snapshot = CalendarDay(now.date(), zone, (), (
                Event("Local", "Standup", now + timedelta(hours=1), now + timedelta(hours=2), False, True),
            ), (), now)
            window.mode.setCurrentText("Claude")
            window.show()
            try:
                with patch("chihaya.datetime", wraps=datetime) as clock, patch(
                    "gmail_reader.datetime", wraps=datetime
                ) as gmail_clock, patch.object(window.calendar.reader, "read", return_value=snapshot), patch.object(
                    window.mail.reader, "credentials"
                ) as creds, patch("gmail_reader.AuthorizedSession", return_value=DailyInbox()), patch(
                    "claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                        transport=httpx2.MockTransport(mock_claude), **kwargs)
                ):
                    clock.now.return_value = gmail_clock.now.return_value = now
                    creds.return_value.refresh_token = None
                    commands = (
                        ("  give   me a daily brief!!! ", "Daily Brief", "Confirm availability"),
                        ("Give me my daily brief", "Daily Brief", "Confirm availability"),
                        ("daily brief", "Daily Brief", "Confirm availability"),
                        ("show me today’s briefing", "Daily Brief", "Confirm availability"),
                        ("今天有什麼要處理？", "每日簡報", "確認可面試時間"),
                        ("今天有什么要处理？", "每日简报", "确认可面试时间"),
                        ("Any emails I need to follow up on?", "Mail to follow up", "Confirm availability"),
                        ("有哪些信要回？", "待跟進郵件", "確認可面試時間"),
                        ("有哪些邮件要回？", "待跟进邮件", "确认可面试时间"),
                    )
                    for mode, case in ((mode, case) for mode in ("离线", "Claude") for case in commands):
                        command, title, label = case
                        window.mode.setCurrentText(mode)
                        with self.subTest(command=command, mode=mode):
                            window.entry.setText(command)
                            window.send()
                            wait_for(lambda: window.daily_worker is None)
                            reply = window.history.toPlainText().rsplit("\n千早：", 1)[-1]
                            self.assertIn(title, reply)
                            self.assertIn(label, reply)
                            self.assertIn("2026-11-20 17:00", reply)
                            if title in {"Daily Brief", "每日簡報", "每日简报"}:
                                self.assertIn("Local planned work", reply)
                                self.assertIn("10:00–11:00", reply)
                            self.assertNotIn("newsletter", reply)
                            self.assertNotIn("Private answered request", reply)
                            self.assertEqual([(item.message_id, item.review) for item in window.daily_items],
                                             [("assessment", False), ("interview", False), ("question", True)])
                            window.navigate("office")
                            self.assertIn("待回复 · 2", window.office.buttons["follow"].text())
                            window.office.buttons["follow"].click()
                            self.assertEqual(window.daily_rows.count(), 2)
                            self.assertIn("Private interview", window.daily_rows.item(1).text())
                            window.navigate("brief")
                            link = next(window.brief_links.item(index) for index in range(window.brief_links.count())
                                        if "Private interview" in window.brief_links.item(index).text())
                            window.open_brief_link(link)
                            self.assertIs(window.tabs.currentWidget(), window.mail_page)
                            self.assertEqual(window.selected_daily_action().message_id, "interview")
                            self.assertEqual(window.review_rows.count(), 1)
                    self.assertEqual(sum(url.endswith("/threads") for url, _ in calls), 2)
                    self.assertFalse(payloads)
                    window.entry.setText("I have one hour, what should I do?")
                    window.send()
                    self.assertIn("Local planned work", window.history.toPlainText().rsplit("\n千早：", 1)[-1])
                    self.assertFalse(payloads)
                    window.daily_rows.setCurrentRow(0)
                    window.daily_add.click()
                    window.daily_add.click()
                    self.assertEqual(len(store.open_tasks()), 2)
                    self.assertTrue(store.open_tasks()[1].mail_linked)
                    window.daily_source.click()
                    wait_for(lambda: window.mail.worker is None)
                    self.assertEqual(window.mail.preview.toPlainText(), mails["assessment"][1])
                    self.assertFalse(mails["interview"][2])
                    self.assertTrue(mails["newsletter"][2])
                    window.daily_rows.setCurrentRow(1)
                    self.assertEqual(window.selected_daily_action().message_id, "interview")
                    window.daily_done.click()
                    self.assertEqual([item.message_id for item in window.daily_items if not item.review], ["assessment"])
                    self.assertEqual(window.daily_rows.count(), 1)
                    self.assertEqual(window.hidden_mail, 1)
                    self.assertIn("待回复 · 1", window.office.buttons["follow"].text())
                    self.assertEqual(len(store.open_tasks()), 2)
                    self.assertEqual(len(store.dismissed_mail()), 1)
                    window.restore_daily_done()
                    self.assertEqual(window.daily_rows.count(), 2)
                    self.assertEqual(window.hidden_mail, 0)
                    self.assertFalse(store.dismissed_mail())
                    window.entry.setText("Plan my day from 10:00 to 15:00")
                    window.send()
                    wait_for(lambda: window.request is None)
                    self.assertEqual(len(payloads), 1)
                    self.assertIn("Displayed local results", str(payloads[0]))
                    for private in ("Private assessment", "Private interview", "Fixture Recruiter", "5 PM", "17:00"):
                        self.assertNotIn(private, str(payloads[0]))
            finally:
                wait_for(lambda: window.daily_worker is None and window.mail.worker is None and window.request is None)
                window.quit_app()

    def test_today_preview_and_chat_keep_mail_out_of_claude(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from chihaya import MainWindow

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        inbox = Inbox()
        now = datetime(2026, 11, 1, 12, tzinfo=inbox.zone)
        requests = []

        def claude(request):
            requests.append(json.loads(request.content))
            action = dict.fromkeys(("title", "target", "date", "field", "minutes"), "") | {"kind": "none"}
            return httpx2.Response(200, json={
                "id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture",
                "content": [{"type": "text", "text": json.dumps({"reply": "Ready.", "action": action})}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        def wait_for(done):
            for _ in range(250):
                if done():
                    return
                QTest.qWait(10)
            self.fail("Background operation did not finish")

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / ".env").write_text("ANTHROPIC_API_KEY=fixture-only\n")
            window = MainWindow(folder / "portraits")
            window.mail.zone = inbox.zone
            window.mail.reader.token.touch()
            window.show()
            try:
                with patch("gmail_view.datetime", wraps=datetime) as clock, patch.object(
                    window.mail.reader, "credentials",
                ) as creds, patch("gmail_reader.AuthorizedSession", return_value=inbox), patch(
                    "claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                        transport=httpx2.MockTransport(claude), **kwargs)
                ):
                    clock.now.return_value = now
                    creds.return_value.refresh_token = None
                    window.tabs.setCurrentWidget(window.today)
                    window.mail.refresh_button.click()
                    wait_for(lambda: window.mail.worker is None)
                    mail = window.mail
                    self.assertEqual(mail.rows.count(), 2)
                    self.assertIn("Interview invitation", mail.rows.item(0).text())
                    self.assertIn("更新于", mail.status.text())
                    mail.rows.setCurrentRow(0)
                    wait_for(lambda: mail.worker is None)
                    self.assertEqual(mail.preview.toPlainText(), "面試 — private preview")
                    self.assertTrue(inbox.messages["late"][1])
                    inbox.failure = (503, [])
                    mail.refresh_button.click()
                    wait_for(lambda: mail.worker is None)
                    self.assertIn("显示的是先前结果", mail.status.text())
                    self.assertEqual(mail.rows.count(), 2)
                    inbox.failure = None
                    clock.now.return_value = now + timedelta(days=2)
                    mail.refresh_button.click()
                    wait_for(lambda: mail.worker is None)
                    self.assertIn("没有符合的邮件", mail.status.text())
                    self.assertEqual(mail.rows.count(), 0)
                    clock.now.return_value = now
                    window.tabs.setCurrentIndex(0)
                    window.mode.setCurrentText("Claude")
                    for prompt, count in (("今天有什麼未讀郵件？", 2), ("今天有什麼求職相關郵件？", 1)):
                        window.entry.setText(prompt)
                        window.send()
                        wait_for(lambda: not window.mail_chat_pending)
                        self.assertEqual(mail.rows.count(), count)
                        self.assertIn(mail.rows.item(0).text(), window.history.toPlainText())
                    self.assertEqual(requests, [])
                    window.entry.setText("What did we just check?")
                    window.send()
                    wait_for(lambda: window.request is None)
                    self.assertEqual(len(requests), 1)
                    self.assertIn("Gmail was handled locally", str(requests[0]))
                    for private in ("Interview invitation", "fixture@example.invalid", "private preview",
                                    "Private Sender", "Résumé received", "1793505599", "求職相關郵件"):
                        self.assertNotIn(private, str(requests[0]))
                    mail.remove_button.click()
                    self.assertFalse(mail.reader.token.exists())
                    self.assertEqual(mail.rows.count(), 0)
                    self.assertEqual(mail.preview.toPlainText(), "")
                    self.assertNotIn("Interview invitation", window.history.toPlainText())
            finally:
                wait_for(lambda: window.mail.worker is None and window.request is None)
                window.quit_app()
