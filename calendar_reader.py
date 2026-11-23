from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, UTC
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from urllib.parse import quote
from zoneinfo import ZoneInfo

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error
from requests import RequestException

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
)
API = "https://www.googleapis.com/calendar/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TIMEOUT = 10
SNAPSHOT_MAX_AGE = timedelta(minutes=5)


class CalendarError(Exception):
    pass


class ReconnectRequired(CalendarError):
    pass


class DesktopFlow(InstalledAppFlow):
    def fetch_token(self, **kwargs):
        return super().fetch_token(timeout=TIMEOUT, **kwargs)


@dataclass(frozen=True)
class Calendar:
    id: str
    name: str


@dataclass(frozen=True)
class Event:
    calendar: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    busy: bool


@dataclass(frozen=True)
class CalendarDay:
    day: date
    zone: ZoneInfo
    calendars: tuple[Calendar, ...]
    events: tuple[Event, ...]
    failures: tuple[str, ...]
    refreshed: datetime
    selected_ids: tuple[str, ...] = ()


def day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone)


def atomic_json(path: Path, text: str, *, service: str = "Google Calendar") -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=f"{path.stem}_", suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
            file.write(text)
        tmp.replace(path)
    except OSError:
        raise CalendarError(f"Could not save {service} settings in your local app folder.") from None
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)


def pages(session: AuthorizedSession, path: str, **params: object):
    while True:
        response = session.get(API + path, params=params.copy(), timeout=TIMEOUT, allow_redirects=False)
        if response.status_code == 401:
            raise ReconnectRequired("Google authorization expired or was revoked. Reconnect Google Calendar.")
        if response.status_code != 200:
            raise CalendarError(f"Google Calendar returned HTTP {response.status_code}. Retry the refresh.")
        payload = response.json()
        yield from payload.get("items", [])
        token = payload.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def event_time(value: dict[str, str], zone: ZoneInfo) -> datetime:
    if "date" in value:
        return datetime.combine(date.fromisoformat(value["date"]), time.min, zone)
    stamp = datetime.fromisoformat(value["dateTime"])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(value["timeZone"]) if "timeZone" in value else zone)
    return stamp.astimezone(zone)


class CalendarReader:
    def __init__(self, data_dir: Path) -> None:
        self.client = data_dir / "google_calendar_client.json"
        self.token = data_dir / "google_calendar_token.json"
        self.selection = data_dir / "google_calendar_selection.json"
        self.lock = Lock()
        self.snapshots: dict[tuple[date, str, tuple[str, ...]], CalendarDay] = {}
        for name in ("google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session", "oauthlib",
                     "google.auth.transport.requests", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def selected(self) -> tuple[str, ...]:
        try:
            data = json.loads(self.selection.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ("primary",)
        except (OSError, ValueError):
            raise CalendarError("Could not read the saved calendar selection.") from None
        if not isinstance(data, list) or not data or not all(isinstance(item, str) and item for item in data):
            raise CalendarError("The saved calendar selection is invalid.")
        return tuple(dict.fromkeys(data))

    def save_selection(self, ids: tuple[str, ...]) -> None:
        if not ids:
            raise CalendarError("Select at least one calendar.")
        atomic_json(self.selection, json.dumps(ids))

    def credentials(self, connect: bool) -> Credentials:
        if connect:
            try:
                config = json.loads(self.client.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                raise CalendarError(f"Desktop OAuth client JSON not found: {self.client}") from None
            except (OSError, ValueError):
                raise CalendarError("Could not read the Desktop OAuth client JSON.") from None
            installed = config.get("installed") if isinstance(config, dict) else None
            if (not isinstance(installed, dict) or installed.get("auth_uri") != AUTH_URI
                    or installed.get("token_uri") != TOKEN_URI):
                raise CalendarError("Use a Google OAuth client of type Desktop app.")
            flow = DesktopFlow.from_client_config(config, scopes=SCOPES, autogenerate_code_verifier=True)
            creds = flow.run_local_server(
                host="127.0.0.1", port=0, timeout_seconds=60, prompt="consent",
                authorization_prompt_message=None, success_message="Return to Chihaya to see the sign-in result.",
            )
            atomic_json(self.token, creds.to_json())
            return creds
        try:
            info = json.loads(self.token.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ReconnectRequired("Connect Google Calendar to use calendar availability.") from None
        except (OSError, ValueError):
            raise ReconnectRequired("Could not read saved Google authorization. Reconnect Google Calendar.") from None
        scopes = info.get("scopes") if isinstance(info, dict) else None
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes) or set(scopes) != set(SCOPES):
            raise ReconnectRequired("Saved Google authorization is incompatible. Reconnect Google Calendar.")
        try:
            return Credentials.from_authorized_user_info(info, scopes=SCOPES)
        except ValueError:
            raise ReconnectRequired("Saved Google authorization is invalid. Reconnect Google Calendar.") from None

    def read(self, day: date, zone: ZoneInfo, *, connect: bool = False, reuse: bool = False,
             selected: tuple[str, ...] | None = None) -> CalendarDay:
        with self.lock:
            selected = selected if selected is not None else self.selected()
            key = day, zone.key, selected
            if connect:
                self.snapshots.clear()
            snapshot = self.snapshots.pop(key, None)
            now = datetime.now(UTC)
            if (reuse and not connect and self.token.is_file() and snapshot is not None
                    and timedelta(0) <= now - snapshot.refreshed.astimezone(UTC) < SNAPSHOT_MAX_AGE):
                self.snapshots[key] = snapshot
                return snapshot
            result = self._read(day, zone, connect, selected)
            if not result.failures:
                self.snapshots[key] = result
                if len(self.snapshots) > 8:
                    del self.snapshots[next(iter(self.snapshots))]
            return result

    def _read(self, day: date, zone: ZoneInfo, connect: bool, selected: tuple[str, ...]) -> CalendarDay:
        try:
            creds = self.credentials(connect)
            with AuthorizedSession(creds, refresh_timeout=TIMEOUT) as session:
                result = self.read_day(session, day, zone, selected)
            if creds.refresh_token and not connect:
                atomic_json(self.token, creds.to_json())
            return result
        except RefreshError as error:
            if error.retryable:
                raise CalendarError("Google authorization refresh failed temporarily. Retry the refresh.") from None
            raise ReconnectRequired("Google authorization expired or was revoked. Reconnect Google Calendar.") from None
        except OAuth2Error:
            raise CalendarError("Google sign-in was not completed. Retry when ready.") from None
        except WSGITimeoutError:
            raise CalendarError("Google sign-in timed out or was cancelled. Retry when ready.") from None
        except (RequestException, GoogleAuthError, TimeoutError):
            raise CalendarError("Could not reach Google Calendar. Check your connection and retry.") from None
        except (OSError, ValueError, KeyError):
            raise CalendarError("Google Calendar data or sign-in configuration could not be read. Retry or reconnect.") from None

    @staticmethod
    def read_day(session: AuthorizedSession, day: date, zone: ZoneInfo, selected: tuple[str, ...]) -> CalendarDay:
        calendars = [Calendar("primary" if entry.get("primary") else entry["id"],
                              entry.get("summaryOverride") or entry.get("summary") or "Calendar")
                     for entry in pages(session, "/users/me/calendarList",
                                        fields="nextPageToken,items(id,primary,summary,summaryOverride)")]
        if not any(calendar.id == "primary" for calendar in calendars):
            calendars.insert(0, Calendar("primary", "Primary calendar"))
        names = {calendar.id: calendar.name for calendar in calendars}
        calendars.extend(Calendar(id_, "Unavailable calendar") for id_ in selected if id_ not in names)
        start, end = day_bounds(day, zone)
        events, failures = [], []
        for calendar_id in selected:
            name = names.get(calendar_id, "Unavailable calendar")
            try:
                if calendar_id not in names:
                    raise CalendarError("Calendar is no longer available.")
                for item in pages(session, f"/calendars/{quote(calendar_id, safe='')}/events",
                                  timeMin=start.isoformat(), timeMax=end.isoformat(), singleEvents="true",
                                  showDeleted="false", orderBy="startTime",
                                  fields="nextPageToken,items(summary,status,transparency,start,end)"):
                    if item.get("status") == "cancelled":
                        continue
                    begins, finishes = event_time(item["start"], zone), event_time(item["end"], zone)
                    if max(begins.astimezone(UTC), start.astimezone(UTC)) < min(finishes.astimezone(UTC), end.astimezone(UTC)):
                        events.append(Event(name, item.get("summary") or "(Untitled)", begins, finishes,
                                            "date" in item["start"], item.get("transparency") != "transparent"))
            except ReconnectRequired:
                raise
            except (CalendarError, RequestException, ValueError, KeyError):
                failures.append(name)
        events.sort(key=lambda item: item.start.astimezone(UTC))
        return CalendarDay(day, zone, tuple(calendars), tuple(events), tuple(failures), datetime.now(zone), selected)
