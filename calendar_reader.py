from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, UTC
import hashlib
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
    id: str = ""
    located: bool = False
    # Local-only overlays. A masked event is hidden from planning but never changed in Google Calendar;
    # a local event is a saved task slot drawn next to the real events.
    masked: bool = field(default=False, compare=False)
    local: bool = False


@dataclass(frozen=True)
class CalendarDay:
    day: date
    zone: ZoneInfo
    calendars: tuple[Calendar, ...]
    events: tuple[Event, ...]
    failures: tuple[str, ...]
    refreshed: datetime
    selected_ids: tuple[str, ...] = ()
    masked: tuple[Event, ...] = ()


def event_key(event: Event) -> str:
    """A stable identity for one calendar instance, so a local mask survives every refresh or reconnect.

    It changes when Google moves or re-identifies the event, so a mask never hides a different appointment.
    """
    raw = "\x1f".join((event.calendar, event.id or event.title, event.start.astimezone(UTC).isoformat(),
                       event.end.astimezone(UTC).isoformat(), str(event.all_day)))
    return hashlib.sha256(raw.encode()).hexdigest()


def apply_masks(snapshot: CalendarDay, keys: Collection[str]) -> CalendarDay:
    """Split a snapshot into effective events and locally masked ones; safe to apply repeatedly."""
    everything = (*snapshot.events, *snapshot.masked)
    effective = tuple(replace(event, masked=False) for event in everything if event_key(event) not in keys)
    masked = tuple(replace(event, masked=True) for event in everything if event_key(event) in keys)
    if effective == snapshot.events and masked == snapshot.masked:
        return snapshot
    order = lambda event: (event.start.astimezone(UTC), event.end.astimezone(UTC), event.title)
    return replace(snapshot, events=tuple(sorted(effective, key=order)), masked=tuple(sorted(masked, key=order)))


def day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone)


def atomic_json(path: Path, text: str, *, service: str = "Google 日历") -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=f"{path.stem}_", suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
            file.write(text)
        tmp.replace(path)
    except OSError:
        raise CalendarError(f"无法将 {service} 设置保存到本地应用文件夹。") from None
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)


def pages(session: AuthorizedSession, path: str, **params: object):
    while True:
        response = session.get(API + path, params=params.copy(), timeout=TIMEOUT, allow_redirects=False)
        if response.status_code == 401:
            raise ReconnectRequired("Google 授权已过期或已被撤销。请重新连接 Google 日历。")
        if response.status_code != 200:
            raise CalendarError(f"Google 日历返回了 HTTP {response.status_code}。请重试刷新。")
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
        # Supplied by the app: keys of events the producer cleared locally. Applied on every read, cached or not.
        self.mask_keys: Callable[[], Collection[str]] = lambda: ()
        for name in ("google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session", "oauthlib",
                     "google.auth.transport.requests", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def selected(self) -> tuple[str, ...]:
        try:
            data = json.loads(self.selection.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ("primary",)
        except (OSError, ValueError):
            raise CalendarError("无法读取已保存的日历选择。") from None
        if not isinstance(data, list) or not data or not all(isinstance(item, str) and item for item in data):
            raise CalendarError("已保存的日历选择无效。")
        return tuple(dict.fromkeys(data))

    def save_selection(self, ids: tuple[str, ...]) -> None:
        if not ids:
            raise CalendarError("请至少选择一个日历。")
        atomic_json(self.selection, json.dumps(ids))

    def credentials(self, connect: bool) -> Credentials:
        if connect:
            try:
                config = json.loads(self.client.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                raise CalendarError(f"未找到 Desktop OAuth 客户端 JSON：{self.client}") from None
            except (OSError, ValueError):
                raise CalendarError("无法读取 Desktop OAuth 客户端 JSON。") from None
            installed = config.get("installed") if isinstance(config, dict) else None
            if (not isinstance(installed, dict) or installed.get("auth_uri") != AUTH_URI
                    or installed.get("token_uri") != TOKEN_URI):
                raise CalendarError("请使用类型为 Desktop app 的 Google OAuth 客户端。")
            flow = DesktopFlow.from_client_config(config, scopes=SCOPES, autogenerate_code_verifier=True)
            creds = flow.run_local_server(
                host="127.0.0.1", port=0, timeout_seconds=60, prompt="consent",
                authorization_prompt_message=None, success_message="请返回 Chihaya 查看登录结果。",
            )
            atomic_json(self.token, creds.to_json())
            return creds
        try:
            info = json.loads(self.token.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ReconnectRequired("尚未连接 Google 日历。请到“设置”页连接 Google 日历。") from None
        except (OSError, ValueError):
            raise ReconnectRequired("无法读取已保存的 Google 授权。请重新连接 Google 日历。") from None
        scopes = info.get("scopes") if isinstance(info, dict) else None
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes) or set(scopes) != set(SCOPES):
            raise ReconnectRequired("已保存的 Google 授权不兼容。请重新连接 Google 日历。")
        try:
            return Credentials.from_authorized_user_info(info, scopes=SCOPES)
        except ValueError:
            raise ReconnectRequired("已保存的 Google 授权无效。请重新连接 Google 日历。") from None

    def read(self, day: date, zone: ZoneInfo, *, connect: bool = False, reuse: bool = False,
             selected: tuple[str, ...] | None = None) -> CalendarDay:
        return self.read_days([day], zone, connect=connect, reuse=reuse, selected=selected)[0]

    def read_days(self, days: Sequence[date], zone: ZoneInfo, *, connect: bool = False, reuse: bool = False,
                  selected: tuple[str, ...] | None = None) -> list[CalendarDay]:
        """Read consecutive local dates with one authorization and one events query per calendar.

        Google data is cached raw; local masks are applied to whatever is returned, so a refresh or a new
        login can never bring back an event the producer cleared on this computer.
        """
        if not days or any(later != earlier + timedelta(days=1) for earlier, later in zip(days, days[1:])):
            raise CalendarError("日历日期范围无效。")
        results = self._read_raw(days, zone, connect=connect, reuse=reuse, selected=selected)
        try:
            keys = frozenset(self.mask_keys())
        except Exception:  # an unreadable mask store must not stop the calendar from loading
            keys = frozenset()
        return [apply_masks(item, keys) for item in results]

    def _read_raw(self, days: Sequence[date], zone: ZoneInfo, *, connect: bool, reuse: bool,
                  selected: tuple[str, ...] | None) -> list[CalendarDay]:
        with self.lock:
            selected = selected if selected is not None else self.selected()
            keys = [(day, zone.key, selected) for day in days]
            if connect:
                self.snapshots.clear()
            now = datetime.now(UTC)
            cached = [self.snapshots.get(key) for key in keys]
            if (reuse and not connect and self.token.is_file() and all(
                    item is not None and timedelta(0) <= now - item.refreshed.astimezone(UTC) < SNAPSHOT_MAX_AGE
                    for item in cached)):
                return [item for item in cached if item is not None]
            results = self._read(days, zone, connect, selected)
            for key, result in zip(keys, results):
                self.snapshots.pop(key, None)
                if not result.failures:
                    self.snapshots[key] = result
            while len(self.snapshots) > 16:
                del self.snapshots[next(iter(self.snapshots))]
            return results

    def _read(self, days: Sequence[date], zone: ZoneInfo, connect: bool,
              selected: tuple[str, ...]) -> list[CalendarDay]:
        try:
            creds = self.credentials(connect)
            with AuthorizedSession(creds, refresh_timeout=TIMEOUT) as session:
                result = self.read_span(session, days, zone, selected)
            if creds.refresh_token and not connect:
                atomic_json(self.token, creds.to_json())
            return result
        except RefreshError as error:
            if error.retryable:
                raise CalendarError("Google 授权刷新暂时失败。请重试刷新。") from None
            raise ReconnectRequired("Google 授权已过期或已被撤销。请重新连接 Google 日历。") from None
        except OAuth2Error:
            raise CalendarError("Google 登录未完成。准备好后请重试。") from None
        except WSGITimeoutError:
            raise CalendarError("Google 登录已超时或被取消。准备好后请重试。") from None
        except (RequestException, GoogleAuthError, TimeoutError):
            raise CalendarError("无法连接到 Google 日历。请检查网络连接后重试。") from None
        except (OSError, ValueError, KeyError):
            raise CalendarError("无法读取 Google 日历数据或登录配置。请重试或重新连接。") from None

    @staticmethod
    def read_day(session: AuthorizedSession, day: date, zone: ZoneInfo, selected: tuple[str, ...]) -> CalendarDay:
        return CalendarReader.read_span(session, [day], zone, selected)[0]

    @staticmethod
    def read_span(session: AuthorizedSession, days: Sequence[date], zone: ZoneInfo,
                  selected: tuple[str, ...]) -> list[CalendarDay]:
        calendars = [Calendar("primary" if entry.get("primary") else entry["id"],
                              entry.get("summaryOverride") or entry.get("summary") or "日历")
                     for entry in pages(session, "/users/me/calendarList",
                                        fields="nextPageToken,items(id,primary,summary,summaryOverride)")]
        if not any(calendar.id == "primary" for calendar in calendars):
            calendars.insert(0, Calendar("primary", "主日历"))
        names = {calendar.id: calendar.name for calendar in calendars}
        calendars.extend(Calendar(id_, "不可用的日历") for id_ in selected if id_ not in names)
        start, end = day_bounds(days[0], zone)[0], day_bounds(days[-1], zone)[1]
        events, failures = [], []
        for calendar_id in selected:
            name = names.get(calendar_id, "不可用的日历")
            try:
                if calendar_id not in names:
                    raise CalendarError("该日历已不可用。")
                for item in pages(session, f"/calendars/{quote(calendar_id, safe='')}/events",
                                  timeMin=start.isoformat(), timeMax=end.isoformat(), singleEvents="true",
                                  showDeleted="false", orderBy="startTime",
                                  fields="nextPageToken,items(id,summary,status,transparency,start,end,location)"):
                    if item.get("status") == "cancelled":
                        continue
                    begins, finishes = event_time(item["start"], zone), event_time(item["end"], zone)
                    if max(begins.astimezone(UTC), start.astimezone(UTC)) < min(finishes.astimezone(UTC), end.astimezone(UTC)):
                        # Only whether a place is set is kept; the address itself is never stored.
                        events.append(Event(name, item.get("summary") or "（无标题）", begins, finishes,
                                            "date" in item["start"], item.get("transparency") != "transparent",
                                            str(item.get("id") or ""), bool(item.get("location"))))
            except ReconnectRequired:
                raise
            except (CalendarError, RequestException, ValueError, KeyError):
                failures.append(name)
        events.sort(key=lambda item: item.start.astimezone(UTC))
        refreshed = datetime.now(zone)
        results = []
        for day in days:
            day_start, day_end = day_bounds(day, zone)
            window = day_start.astimezone(UTC), day_end.astimezone(UTC)
            in_day = tuple(event for event in events
                           if max(event.start.astimezone(UTC), window[0]) < min(event.end.astimezone(UTC), window[1]))
            results.append(CalendarDay(day, zone, tuple(calendars), in_day, tuple(failures), refreshed, selected))
        return results
