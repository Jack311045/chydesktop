import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Callable, TypeVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error
from requests import RequestException

from calendar_reader import AUTH_URI, TOKEN_URI, TIMEOUT, CalendarError, DesktopFlow, atomic_json, day_bounds
from mail_actions import (
    CONTENT_LIMIT, SCAN_DAYS, SCAN_LIMIT, Adjudicator, LocalClassifier, MailAction, MailScan,
    action_order, adjudicate, campaign, current_text, topic_match,
)

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
LIMIT = 10
DEFAULT_KEYWORDS = "interview, recruiter, job application, 面試, 求職"
T = TypeVar("T")


class GmailError(Exception):
    pass


class ReconnectGmail(GmailError):
    pass


class GmailItemMissing(GmailError):
    pass


@dataclass(frozen=True)
class MailSearch:
    day: date | None = None
    keywords: tuple[str, ...] = ()

    def query(self, zone: ZoneInfo) -> str:
        terms = []
        if self.day:
            start, end = day_bounds(self.day, zone)
            # Gmail's after: boundary is exclusive.
            terms.append(f"after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}")
        if self.keywords:
            terms.append("{" + " ".join(f'"{term}"' for term in self.keywords) + "}")
        return " ".join(terms)


@dataclass(frozen=True)
class MailRow:
    id: str
    sender: str
    subject: str
    received: datetime | None

    def display(self) -> str:
        stamp = self.received.strftime("%Y-%m-%d %H:%M %Z") if self.received else "Time unavailable"
        return f"{stamp} | {self.sender} | {self.subject}"


@dataclass(frozen=True)
class MailResult:
    search: MailSearch
    rows: tuple[MailRow, ...]
    more: bool


def job_keywords(text: str) -> tuple[str, ...]:
    terms = tuple(dict.fromkeys(" ".join(part.split()) for part in re.split(r"[,，]", text) if part.strip()))
    if not 1 <= len(terms) <= 10 or any(not re.fullmatch(r"[\w -]{1,60}", term) for term in terms):
        raise GmailError("Enter 1–10 comma-separated keywords or phrases (letters, digits, spaces, or hyphens).")
    return terms


def chat_mail_kind(message: str) -> str | None:
    text = message.casefold()
    if not re.search(r"郵件|邮件|電郵|电邮|\b(?:gmail|e-?mails?|inbox)\b", text):
        return None
    if not re.search(r"今天|今日|\btoday\b", text):
        return None
    if re.search(r"求職|求职|面試|面试|招聘|\b(?:job|recruiting|interview)\b", text):
        return "jobs"
    if re.search(r"未讀|未读|\bunread\b", text):
        return "today"
    return None


class GmailReader:
    def __init__(self, data_dir: Path) -> None:
        self.client = data_dir / "google_calendar_client.json"
        self.token = data_dir / "gmail_token.json"
        self.thread_cache: dict[str, tuple[tuple, tuple[MailAction, ...]]] = {}

    def credentials(self, connect: bool) -> Credentials:
        if connect:
            if not self.client.is_file():
                raise GmailError(r"Save the Desktop OAuth client as %LOCALAPPDATA%\Chihaya\google_calendar_client.json first.")
            config = json.loads(self.client.read_text(encoding="utf-8-sig"))
            installed = config.get("installed") if isinstance(config, dict) else None
            if (not isinstance(installed, dict) or installed.get("auth_uri") != AUTH_URI
                    or installed.get("token_uri") != TOKEN_URI):
                raise GmailError("Gmail requires a Google OAuth client of type Desktop app.")
            flow = DesktopFlow.from_client_config(config, scopes=[SCOPE], autogenerate_code_verifier=True)
            try:
                creds = flow.run_local_server(
                    host="127.0.0.1", port=0, timeout_seconds=60, prompt="consent",
                    authorization_prompt_message=None, success_message="Return to Chihaya to see the Gmail sign-in result.",
                )
            except Warning as error:
                if not hasattr(error, "old_scope"):
                    raise
                raise ReconnectGmail("Gmail did not grant the requested read-only permission. Reconnect Gmail.") from None
            granted = creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
            if set(granted or ()) != {SCOPE}:
                raise ReconnectGmail("Gmail did not grant exactly the requested read-only permission. Reconnect Gmail.")
            return creds
        try:
            info = json.loads(self.token.read_text(encoding="utf-8"))
            if not isinstance(info, dict) or info.get("scopes") != [SCOPE]:
                raise ValueError
            return Credentials.from_authorized_user_info(info, scopes=[SCOPE])
        except FileNotFoundError:
            raise ReconnectGmail("Gmail is not connected. Choose Connect Gmail in Today.") from None
        except (OSError, ValueError):
            raise ReconnectGmail("Saved Gmail authorization could not be read. Reconnect Gmail.") from None

    def _use(self, operation: Callable[[AuthorizedSession], T], *, connect: bool = False) -> T:
        try:
            creds = self.credentials(connect)
            with AuthorizedSession(creds, refresh_timeout=TIMEOUT, max_refresh_attempts=1) as session:
                result = operation(session)
            # Do not recreate authorization removed during an in-flight read.
            if connect or (creds.refresh_token and self.token.is_file()):
                atomic_json(self.token, creds.to_json(), service="Gmail")
            return result
        except RefreshError as error:
            if error.retryable:
                raise GmailError("Gmail authorization could not refresh. Try again.") from None
            raise ReconnectGmail("Gmail permission expired or was revoked. Reconnect Gmail.") from None
        except OAuth2Error:
            raise ReconnectGmail("Gmail sign-in was denied or incomplete. Reconnect Gmail.") from None
        except WSGITimeoutError:
            raise ReconnectGmail("Gmail sign-in timed out. Reconnect Gmail.") from None
        except (RequestException, GoogleAuthError, TimeoutError):
            raise GmailError("Could not reach Gmail. Check your connection and retry.") from None
        except CalendarError as error:
            raise GmailError(str(error)) from None
        except (OSError, ValueError, KeyError, TypeError, OverflowError, AttributeError):
            raise GmailError("Gmail data or sign-in configuration could not be read. Refresh or reconnect.") from None

    @staticmethod
    def _get(session: AuthorizedSession, suffix: str = "", *, resource: str = "messages", **params: object) -> dict:
        response = session.get(f"{GMAIL}/{resource}{suffix}", params=params, timeout=TIMEOUT, allow_redirects=False)
        if response.status_code == 401:
            raise ReconnectGmail("Gmail permission expired or was revoked. Reconnect Gmail.")
        if response.status_code == 403:
            try:
                reasons = {item.get("reason") for item in response.json().get("error", {}).get("errors", [])}
            except (ValueError, AttributeError, TypeError):
                reasons = set()
            if reasons & {"accessNotConfigured", "serviceDisabled"}:
                raise GmailError("Gmail API is not enabled for the OAuth project.")
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}:
                raise GmailError("Gmail rate or quota limit reached. Try again later.")
            raise ReconnectGmail("Gmail access was denied. Reconnect Gmail or check account restrictions.")
        if response.status_code == 404:
            raise GmailItemMissing("The Gmail item is no longer available.")
        if response.status_code != 200:
            raise GmailError(f"Gmail returned HTTP {response.status_code}. Try refreshing.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise GmailError("Gmail returned an invalid response. Try refreshing.")
        return payload

    def read(self, search: MailSearch, zone: ZoneInfo, *, connect: bool = False) -> MailResult:
        def read_page(session: AuthorizedSession) -> MailResult:
            page = self._get(session, labelIds=["INBOX", "UNREAD"], maxResults=LIMIT,
                             q=search.query(zone), fields="messages/id,nextPageToken")
            entries = page.get("messages", [])
            if not isinstance(entries, list):
                raise ValueError
            rows = []
            for entry in entries[:LIMIT]:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                    raise ValueError
                message_id = entry["id"]
                data = self._get(session, "/" + quote(message_id, safe=""), format="metadata",
                                 metadataHeaders=["From", "Subject"], fields="internalDate,payload/headers")
                headers = message_headers(data.get("payload") or {})
                received = received_time(data, zone)
                if search.day is not None and received is not None and received.date() != search.day:
                    continue
                sender = " ".join(headers.get("from", "").split()) or "(Unknown sender)"
                subject = " ".join(headers.get("subject", "").split()) or "(No subject)"
                rows.append(MailRow(message_id, sender, subject, received))
            rows.sort(key=lambda row: row.received.timestamp() if row.received else float("-inf"), reverse=True)
            return MailResult(search, tuple(rows), bool(page.get("nextPageToken") or len(entries) > LIMIT))

        return self._use(read_page, connect=connect)

    def scan_actions(self, now: datetime, zone: ZoneInfo, *, candidate_limit: int = SCAN_LIMIT,
                     content_limit: int = CONTENT_LIMIT, semantic: bool = True,
                     context: tuple[str, ...] = ()) -> MailScan:
        if not 2 <= candidate_limit <= 200 or not 1 <= content_limit <= candidate_limit:
            raise GmailError("Gmail scan budgets must be 2–200 candidates and 1–candidate limit content checks.")
        classifier = LocalClassifier() if semantic else None
        context = tuple(title[:180] for title in context[:24])

        def scan(session: AuthorizedSession) -> MailScan:
            today = now.astimezone(zone).date()
            start, _ = day_bounds(today - timedelta(days=SCAN_DAYS - 1), zone)
            _, end = day_bounds(today, zone)
            query = f"after:{int(start.timestamp()) - 1} before:{int(end.timestamp())} -in:sent"
            signals = (' {is:important is:starred interview assessment application recruiter availability '
                       '"please confirm" "could you" "action required" rescheduled}')
            failures = 0
            exhausted = [False, False]

            def pages(index: int):
                nonlocal failures
                token = ""
                seen_tokens = set()
                # Overlapping pages cannot make retrieval unbounded.
                for _ in range((candidate_limit + 19) // 20 + 2):
                    try:
                        result = self._get(session, resource="threads", labelIds=["INBOX"], maxResults=20,
                                           q=query + (signals if index else ""), fields="threads/id,nextPageToken",
                                           **({"pageToken": token} if token else {}))
                    except ReconnectGmail:
                        raise
                    except (GmailError, RequestException):
                        failures += 1
                        return
                    for entry in result.get("threads", [])[:20]:
                        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                            failures += 1
                            continue
                        yield entry["id"]
                    token = result.get("nextPageToken")
                    if not token:
                        exhausted[index] = True
                        return
                    if token in seen_tokens:
                        failures += 1
                        return
                    seen_tokens.add(token)

            candidates: dict[str, int] = {}
            sources = [iter(pages(0)), iter(pages(1))]

            def take(index: int) -> bool:
                for ident in sources[index]:
                    if ident not in candidates:
                        candidates[ident] = index
                        return True
                return False

            for index, reserve in ((0, (candidate_limit + 1) // 2), (1, candidate_limit // 2)):
                for _ in range(reserve):
                    if not take(index):
                        break
            for index in (0, 1):
                while len(candidates) < candidate_limit and take(index):
                    pass
            # At the boundary, look ahead for a distinct candidate, without adding it.
            capped = False
            if len(candidates) == candidate_limit:
                capped = any(any(ident not in candidates for ident in source) for source in sources)
            capped = capped or (not all(exhausted) and not failures)
            metadata = [[], []]
            for ident, index in candidates.items():
                try:
                    header = self._get(
                        session, "/" + quote(ident, safe=""), resource="threads", format="metadata",
                        metadataHeaders=["Subject", "From", "Message-ID", "In-Reply-To", "List-Unsubscribe", "Precedence"],
                        fields="historyId,messages(id,internalDate,labelIds,payload/headers)",
                    )
                    messages = sorted((msg for msg in header.get("messages", []) if received_time(msg, zone)),
                                      key=lambda msg: int(msg["internalDate"]), reverse=True)
                    inbound = [msg for msg in messages if "INBOX" in msg.get("labelIds", [])
                               and "SENT" not in msg.get("labelIds", [])
                               and start.timestamp() <= int(msg["internalDate"]) / 1000 < end.timestamp()]
                    if not inbound:
                        continue
                    latest = inbound[0]
                    hdr = message_headers(latest.get("payload") or {})
                    subject = hdr.get("subject", "")
                    labels = latest.get("labelIds", [])
                    bulk = bool(campaign(subject) or hdr.get("list-unsubscribe") or "CATEGORY_PROMOTIONS" in labels)
                    hint = any(topic_match(subject, title) for title in context) or bool(
                        re.search(r"\b(?:interview|assessment|reschedul\w*|document|availability)\b", subject, re.I)
                        or {"STARRED", "IMPORTANT"}.intersection(labels))
                    rank = (bulk and not hint, not hint, -int(latest["internalDate"]))
                    metadata[index].append((rank, ident, header.get("historyId"), messages, inbound))
                except ReconnectGmail:
                    raise
                except (GmailError, RequestException, KeyError, TypeError, ValueError):
                    failures += 1
            for group in metadata:
                group.sort(key=lambda item: item[0])
            reserve = (content_limit + 1) // 2
            chosen = metadata[0][:reserve] + metadata[1][:content_limit - reserve]
            rest = sorted(metadata[0][reserve:] + metadata[1][content_limit - reserve:], key=lambda item: item[0])
            chosen.extend(rest[:content_limit - len(chosen)])
            actions = []
            checked = 0
            for _, ident, revision, messages, inbound in chosen:
                key = (revision, tuple(msg["id"] for msg in messages), inbound[0]["id"], zone.key, semantic, context)
                saved = self.thread_cache.get(ident)
                try:
                    if saved and saved[0] == key:
                        result = saved[1]
                    else:
                        result = self._thread_actions(session, messages, inbound, zone, context, classifier)
                        self.thread_cache[ident] = (key, result)
                    actions.extend(result)
                    checked += 1
                except ReconnectGmail:
                    raise
                except (GmailError, RequestException, KeyError, TypeError, ValueError):
                    failures += 1
            self.thread_cache = {ident: value for ident, value in self.thread_cache.items() if ident in candidates}
            if failures and not checked:
                raise GmailError("Gmail scan failed before any thread could be checked. Refresh or reconnect.")
            return MailScan(today, len(candidates), capped, tuple(sorted(actions, key=action_order)), datetime.now(zone),
                            checked, candidate_limit, content_limit, len(chosen) < sum(map(len, metadata)),
                            failures, semantic)
        return self._use(scan)

    def _thread_actions(self, session: AuthorizedSession, messages: list[dict], inbound: list[dict],
                        zone: ZoneInfo, context: tuple[str, ...],
                        classifier: Adjudicator | None) -> tuple[MailAction, ...]:
        latest = inbound[0]
        ident = latest["id"]
        headers = message_headers(latest.get("payload") or {})
        body = self._body(session, ident, excerpt_only=True)
        actions = adjudicate(ident, headers.get("from", "(Unknown sender)"), headers.get("subject", "(No subject)"),
                             body, received_time(latest, zone), answered=False, direction_known=True,
                             context=context, classifier=classifier)
        # A short acknowledgement is not a new request or evidence of completion.
        if len(inbound) > 1 and re.fullmatch(r"(?:thanks|thank you|got it|noted)[.! ]*", current_text(body), re.I):
            return self._thread_actions(session, messages, inbound[1:2], zone, context, classifier)
        reply_kinds = {"availability", "reply", "interview"}
        if not any(item.kind in reply_kinds and not item.review for item in actions):
            return tuple(actions)
        message_id = headers.get("message-id")
        sent = [msg for msg in messages if "SENT" in msg.get("labelIds", [])
                and int(msg["internalDate"]) > int(latest["internalDate"])][:4]
        for reply in sent:
            target = message_headers(reply.get("payload") or {}).get("in-reply-to")
            if target and (not message_id or message_id not in target.split()):
                continue
            text = current_text(self._body(session, reply["id"], excerpt_only=True))
            if not text or re.fullmatch(r"(?:thanks|thank you|got it|noted)[.! ]*", text, re.I):
                continue
            availability = re.search(r"\b(?:i(?:'m| am) (?:free|available)|i can (?:meet|do)|works for me|"
                                     r"my availability|i(?:'ve| have) booked|i confirm)\b", text, re.I)
            answered = {"availability", "interview"} if availability else set()
            if re.search(r"\b(?:yes|no|i can|i would|i'd|works for me|interested)\b", text, re.I):
                answered.add("reply")
            actions = [item for item in actions if item.kind not in answered]
        return tuple(actions)

    def preview(self, message_id: str) -> str:
        return self._use(lambda session: self._body(session, message_id) or "No displayable text body.")

    def _body(self, session: AuthorizedSession, message_id: str, *, excerpt_only: bool = False) -> str:
        path = "/" + quote(message_id, safe="")

        def read_body(session: AuthorizedSession) -> str:
            data = self._get(session, path, format="full", fields="payload")
            for preference in ("text/plain", "text/html"):
                for part in body_parts(data.get("payload") or {}, preference):
                    body = part.get("body") or {}
                    encoded = body.get("data")
                    if not encoded and body.get("attachmentId") and not excerpt_only:
                        encoded = self._get(session, path + "/attachments/" + quote(body["attachmentId"], safe=""),
                                            fields="data").get("data")
                    if not encoded:
                        continue
                    if excerpt_only:
                        encoded = encoded[:16000]
                    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                    content_type = message_headers(part).get("content-type", "")
                    charset = re.search(r'charset\s*=\s*["\']?([\w.-]+)', content_type, re.I)
                    try:
                        text = raw.decode(charset[1] if charset else "utf-8", errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                    if preference == "text/html":
                        parser = TextPreview(skip_quotes=excerpt_only)
                        parser.feed(text)
                        parser.close()
                        text = "".join(parser.lines)
                    if text.strip():
                        return text.strip()
            return ""

        return read_body(session)


def received_time(data: dict, zone: ZoneInfo) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(data["internalDate"]) / 1000, zone)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def message_headers(part: dict) -> dict[str, str]:
    if not isinstance(part, dict):
        raise ValueError
    return {item["name"].casefold(): item["value"] for item in part.get("headers") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)}


def body_parts(part: dict, wanted: str):
    if part.get("filename") or message_headers(part).get("content-disposition", "").lower().startswith("attachment"):
        return
    if part.get("mimeType") == wanted:
        yield part
    if part.get("mimeType", "").startswith("multipart/"):
        for child in part.get("parts", []):
            yield from body_parts(child, wanted)


class TextPreview(HTMLParser):
    hidden_tags = {"head", "script", "style", "template"}
    line_tags = {"br", "div", "li", "p", "tr", "blockquote"}

    def __init__(self, *, skip_quotes: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        if skip_quotes:
            self.hidden_tags = self.hidden_tags | {"blockquote"}
        self.hidden: list[str] = []
        self.lines: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.line_tags:
            self.lines.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        elif not self.hidden and tag in self.line_tags - {"br"}:
            self.lines.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self.hidden_tags:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.lines.append(data)
