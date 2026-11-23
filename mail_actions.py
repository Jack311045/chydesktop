from dataclasses import dataclass
from datetime import date, datetime, time
import re
from typing import Protocol

from tasks import TaskError, local_date

SCAN_LIMIT = 100
CONTENT_LIMIT = 30
SCAN_DAYS = 7
EXCERPT_LIMIT = 360


@dataclass(frozen=True)
class MailAction:
    message_id: str
    kind: str
    sender: str
    subject: str
    received: datetime | None
    excerpt: str
    deadline: str = ""
    deadline_quote: str = ""
    review: bool = False
    status: bool = False
    missing: str = ""


@dataclass(frozen=True)
class MailScan:
    day: date
    scanned: int
    limited: bool
    actions: tuple[MailAction, ...]
    refreshed: datetime
    checked: int = 0
    candidate_limit: int = SCAN_LIMIT
    content_limit: int = CONTENT_LIMIT
    content_capped: bool = False
    failures: int = 0
    semantic: bool = True


RULES = {
    "availability": r"\b(?:confirm|share|send|provide|let (?:me|us) know)\b.{0,48}\b(?:availability|available|time slots?)\b",
    "interview": r"\b(?:schedule|book|pick|select|confirm)\b.{0,32}\b(?:interview|interview slot)\b",
    "resume": r"\b(?:send|submit|upload|provide|attach|share)\b.{0,45}\b(?:r[eé]sum[eé]|cv)\b",
    "assessment": r"\b(?:complete|submit|finish|take)\b.{0,35}\b(?:assessment|coding test|exercise)\b",
    "reply": r"\b(?:please (?:reply|respond|confirm)|(?:could|can|would) you (?:reply|respond)|(?:reply|respond) (?:by|with|to))\b",
}
OPT_OUT = r"\b(?:no (?:reply|response)(?: is)? (?:needed|required|necessary)|(?:do not|don't) reply|you need not reply)\b"
UNCERTAIN = r"\b(?:interview|assessment|availability|follow[ -]?up|next steps|application)\b"
CAMPAIGN = (r"\b(?:newsletter|weekly digest|(?:job|priority) matches|roles you (?:might|may) like|"
            r"recommended (?:jobs|roles)|recommend(?:ed|ing)? (?:these|some) (?:jobs|roles)|"
            r"apply now|based on your profile|explore these (?:jobs|roles))\b")
UPDATE = (r"\b(?:your|the|our)\s+(?:[\w-]+\s+){0,3}(?:interview|assessment|meeting|call)\s+"
          r"(?:(?:has been|is|was|will be)\s+)?(?:confirmed|rescheduled|postponed|cancelled|canceled|moved)\b")


def current_text(text: str) -> str:
    text = re.split(r"(?im)^\s*(?:On .{0,240}wrote:|[- ]*Original Message[- ]*|Begin forwarded message:)\s*",
                    text, maxsplit=1)[0]
    # Gmail snippets flatten quoted lines; still recognize the usual reply separator.
    text = re.split(r"(?i)\bOn .{0,160}wrote:", text, maxsplit=1)[0]
    return " ".join(line.strip() for line in text.splitlines() if not line.lstrip().startswith(">"))


def campaign(text: str) -> bool:
    return bool(re.search(CAMPAIGN, text, re.I))


def topic_match(left: str, right: str) -> bool:
    def tokens(text: str) -> set[str]:
        return set(re.findall(r"\w{3,}", text.casefold())) - {
            "the", "your", "our", "with", "and", "for", "recruiter", "request", "invitation", "scheduled",
        }
    first, second = tokens(left), tokens(right)
    generic = {"interview", "assessment", "meeting", "call", "application", "availability", "schedule"}
    return len(first & second) >= 2 and bool((first & second) - generic) and (
        first <= second or second <= first or len(first & second) / min(len(first), len(second)) >= 0.75)


def deadline_in(text: str, received: datetime | None) -> tuple[str, str]:
    match = re.search(r"\b(?:by|before|due(?: on)?|no later than)\s+"
                      r"(\d{4}-\d{2}-\d{2}|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
                      r"(?:\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{2}:\d{2}))?"
                      r"(?:\s+((?-i:[A-Z]{2,5})(?![A-Za-z])|[+-]\d{2}:?\d{2}))?", text, re.I)
    if not match:
        unsure = re.search(r"\b(?:by|before|due)\s+([^.!?;\n]{1,65})", text, re.I)
        return "", unsure[0].strip() if unsure else ""
    evidence = match[0].strip()
    try:
        # Unknown or foreign time zones are kept verbatim rather than guessed.
        zone_word = match[3]
        if ((zone_word and (received is None or zone_word != received.tzname()))
                or (received is None and not match[1][0].isdigit())):
            return "", evidence
        day = (date.fromisoformat(match[1]) if match[1][0].isdigit()
               else local_date(match[1], received.date()))
        if match[2]:
            clock = match[2].lower().replace(".", "").replace(" ", "")
            if clock.endswith(("am", "pm")):
                clock = datetime.strptime(clock, "%I:%M%p" if ":" in clock else "%I%p").strftime("%H:%M")
            return f"{day} {time.fromisoformat(clock):%H:%M}", evidence
        return str(day), evidence
    except (TaskError, ValueError):
        return "", evidence


def classify(message_id: str, sender: str, subject: str, body: str, received: datetime | None,
             *, answered: bool, direction_known: bool, context: tuple[str, ...] = (),
             conservative: bool = True) -> list[MailAction]:
    text = current_text(body)
    no_reply = bool(re.search(OPT_OUT, text, re.I))
    informational = bool(re.search(r"\b(?:interview (?:is )?(?:confirmed|scheduled)|"
                                   r"thank you for applying|application received)\b", text, re.I))
    found = []
    for kind, pattern in RULES.items():
        match = next((match for match in re.finditer(pattern, text, re.I)
                      if not re.search(r"\b(?:do not|don't|no need to|not required to|already)\s*$",
                                       text[max(0, match.start() - 32):match.start()], re.I)), None)
        if not match:
            continue
        expects_reply = kind in {"availability", "reply"} or (
            kind == "interview" and match[0].lower().startswith("confirm"))
        if expects_reply and (answered or no_reply):
            continue
        if kind == "reply" and (found or re.search(RULES["resume"], text, re.I)
                                or re.search(RULES["assessment"], text, re.I)):
            continue
        # Associate a deadline with the matched request, not a different sentence.
        sentence_end = re.search(r"[.!?](?=\s+[A-Z]|\s*$)", text[match.end():])
        end = match.end() + sentence_end.start() if sentence_end else len(text)
        fragment = text[match.start():end][:EXCERPT_LIMIT]
        deadline, quote = deadline_in(fragment, received)
        found.append(MailAction(message_id, kind, sender, subject, received, fragment, deadline, quote,
                                review=expects_reply and not direction_known))
    if found or answered or no_reply or informational:
        return found
    obligation = (any(topic_match(subject, title) for title in context) or
                  re.search(r"\b(?:your|our|the scheduled)\s+(?:[\w-]+\s+){0,3}"
                            r"(?:interview|assessment|appointment|application)\b", text, re.I))
    if obligation or (not conservative and re.search(UNCERTAIN, text, re.I)):
        return [MailAction(message_id, "review", sender, subject, received, text[:EXCERPT_LIMIT],
                           review=True, missing="next_action")]
    return []


@dataclass(frozen=True)
class Decision:
    disposition: str
    evidence_id: str
    excerpt: str
    kind: str = ""
    deadline: str = ""
    missing: str = ""


class Adjudicator(Protocol):
    def decide(self, ident: str, subject: str, text: str, context: tuple[str, ...]) -> tuple[Decision, ...]: ...


class LocalClassifier:
    """Small on-device intent classifier with no tools or network client."""

    def decide(self, ident: str, subject: str, text: str, context: tuple[str, ...]) -> tuple[Decision, ...]:
        decisions = []
        related = any(topic_match(subject, title) for title in context)
        for sentence in re.split(r"(?<=[!?])\s+|(?<!\b[AP]M)(?<=\.)\s+(?=[A-Z])", text):
            if update := re.search(UPDATE, sentence, re.I):
                if re.search(r"\b(?:rescheduled|postponed|cancelled|canceled|moved)\b", update[0], re.I):
                    decisions.append(Decision("update", ident, sentence[:EXCERPT_LIMIT]))
            if personal_request(sentence, text, related) and not re.search(OPT_OUT, text, re.I):
                decisions.append(Decision("action", ident, sentence[:EXCERPT_LIMIT], "reply"))
        return tuple(decisions)


def personal_request(sentence: str, text: str, related: bool) -> bool:
    direct = re.search(r"\b(?:could|would|can|will) you\b|\b(?:please|let me know|tell me)\b", sentence, re.I)
    personal = re.search(r"\b(?:your (?:background|experience|work)|i (?:saw|read|noticed|came across)|"
                         r"our (?:team|conversation)|we(?:'d| would) like to (?:speak|meet))\b", text, re.I)
    conversation = re.search(r"\b(?:chat|speak|meet|talk|connect|work for you|spare .{0,15}minutes|"
                             r"have .{0,18}minutes|a few minutes|what works for you)\b", sentence, re.I)
    return bool(direct and conversation and (personal or related))


def adjudicate(ident: str, sender: str, subject: str, body: str, received: datetime | None,
               *, answered: bool, direction_known: bool, context: tuple[str, ...],
               classifier: Adjudicator | None) -> list[MailAction]:
    text = current_text(body)[:8000]
    if not text:
        return []
    if re.search(r"\b(?:newsletter|weekly digest)\b", subject, re.I):
        return []
    marketing = campaign(subject + " " + text)
    supported = bool(re.search(r"\b(?:your|our|the)\s+(?:[\w-]+\s+){0,3}(?:interview|assessment)\b",
                               text, re.I))
    if marketing and not supported:
        return []
    found = classify(ident, sender, subject, text, received, answered=answered,
                     direction_known=direction_known, context=context)
    if marketing:
        found = [item for item in found if not item.review and item.kind in {"assessment", "interview", "availability"}]
    decisions = classifier.decide(ident, subject, text, context) if classifier else ()
    if classifier is None and (update := re.search(UPDATE, text, re.I)):
        if re.search(r"\b(?:rescheduled|postponed|cancelled|canceled|moved)\b", update[0], re.I):
            decisions = (Decision("update", ident, text[update.start():update.start() + EXCERPT_LIMIT]),)
    for decision in decisions:
        if (decision.evidence_id != ident or not decision.excerpt.strip()
                or len(decision.excerpt) > EXCERPT_LIMIT or decision.excerpt not in text
                or (decision.deadline and decision.deadline not in decision.excerpt)):
            continue
        if decision.disposition == "update" and re.search(UPDATE, decision.excerpt, re.I):
            found.append(MailAction(ident, "update", sender, subject, received, decision.excerpt, status=True))
        elif (decision.disposition == "action" and decision.kind == "reply" and not marketing
              and personal_request(decision.excerpt, text, any(topic_match(subject, title) for title in context))
              and not answered and not re.search(OPT_OUT, text, re.I) and not any(not item.review for item in found)):
            due, quoted = deadline_in(decision.excerpt, received)
            found = [MailAction(ident, "reply", sender, subject, received, decision.excerpt, due, quoted,
                                review=not direction_known, missing="reply_state" if not direction_known else "")]
    if any(item.status for item in found):
        found = [item for item in found if not item.review]
    return found


def action_order(action: MailAction) -> tuple:
    return (action.review, action.status, not action.deadline, action.deadline,
            -(action.received.timestamp() if action.received else 0))
