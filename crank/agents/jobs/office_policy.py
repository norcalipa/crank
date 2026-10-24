# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Fail-closed reader for office-policy prose (issue #537).

``office_days`` returns the number of in-office days (0-5) only when the WHOLE
accepted value is one recognisable statement: an exact code or label, plain
remote, optional office, a day count, in-office with a remote exception, a
stated weekday span or list, or "office not optional". Every other value is
unknown (``None``), never a guess.

The statements are anchored templates (``fullmatch``) built from closed
vocabularies. The only open slot is PLACE: capitalised place names after "in"
in an incidental-office tail, "our <PLACE> office" and "hybrid in <PLACE>". No
keyword search, default or fallback returns a number.

This module imports no Django code.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any

from crank.services.hidden_characters import is_hidden_character

MAX_CHARS = 240

_NON_WORD = re.compile(r"[^\w\s-]+", re.UNICODE)
_LONE_HYPHENS = re.compile(r"(?<!\S)-+|-+(?!\S)")
_WS = re.compile(r"\s+")


def _normalize(value: str) -> str:
    """N1-N4: NFKC, hidden characters dropped, punctuation and lone hyphens to
    spaces, whitespace collapsed. Case is kept (only PLACE looks at it)."""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if c in "\t\n\r" or not is_hidden_character(c))
    text = _NON_WORD.sub(" ", value).replace("_", " ")
    return _WS.sub(" ", _LONE_HYPHENS.sub(" ", text)).strip()


# ---------------------------------------------------------------- vocabularies
TEMPLATE_WORDS: set[str] = set()  # every word the vocabularies can consume (property P1)


def _alt(*items: str) -> str:
    return "(?:" + "|".join(sorted(set(items), key=lambda s: (-len(s), s))) + ")"


def _opt(pattern: str) -> str:
    return f"(?:{pattern} )?"


def _phrase(text: str) -> str:
    """A vocabulary phrase: its words match with a space or a hyphen between them."""
    words = text.split()
    TEMPLATE_WORDS.update(words)
    return r"[ -]".join(re.escape(word) for word in words)


def _phrases(*texts: str) -> str:
    return _alt(*(_phrase(t) for t in texts))


NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
NUM_RE = _alt(*NUM)
TEMPLATE_WORDS.update(NUM)
TIMES = {"once": 1, "twice": 2}
TEMPLATE_WORDS.update(TIMES)

# Words a PLACE token may never be (property P1 relies on this list).
STOP_WORDS = frozenset("""
remote remotely office offices onsite on-site site hybrid person in-person rto return home wfh telework
teleworking telecommute telecommuting virtual virtually distributed coworking hub hubs hq headquarters campus
desk space work works working worked based attend attendance presence commute relocate relocation live
located travel visit visits meet meetups offsite offsites
no not non never none nope nothing without isn aren don doesn won can cannot can't may might must should
shall will would could need needs required require requires requirement mandatory mandate mandated expected
expect optional voluntary encouraged preferred prefer recommended welcome allowed permitted available
possible eligible considered approved approval exception exceptions exceptional only except unless until
after before during starting then when while if but however although otherwise ok okay yes tbd tba unknown
varies vary depends flexible flex maybe temporarily formerly previously partially few longer used ended
stopped discontinued ideally generally typically seldom half eventually initially
day days daily week weeks weekly wk month months monthly quarter quarters quarterly year years yearly
annual annually sometimes occasional occasionally often rarely always usually mostly primarily partly full
full-time part part-time time times once twice first last next now currently soon later summer summers
winter holidays holiday weekend weekends monday mondays tuesday tuesdays wednesday wednesdays thursday
thursdays friday fridays mon tue tues wed thu thur thurs fri
engineer engineers engineering sales staff employee employees contractor contractors manager managers
leadership executives hires hire new role roles team teams everyone some all most certain senior junior
a an and or the of for to from at on by with as is are be was were it its we our you your they their this
that these those there here per each every other rest near nearby around within across outside inside into
out up down over under via plus
""".split())
_STOP_RE = _alt(*(re.escape(w) for w in STOP_WORDS))
_PLACE_TOKEN = rf"(?!{_STOP_RE}(?:\s|$))[^\W\d_]{{2,}}(?=\s|$)"
_PLACE_ONE = rf"{_PLACE_TOKEN}(?: {_PLACE_TOKEN}){{0,2}}"
_GROUP_IDS = iter(range(1, 100_000))
TEMPLATE_WORDS.update(["cities", "countries", "locations", "states", "time", "zones", "and", "or", "the"])


def _place() -> str:
    """M4: up to 3 groups of 1-3 name tokens joined by and/or, or "<1-5> cities"."""
    return (
        rf"(?P<place{next(_GROUP_IDS)}>(?:the )?{_PLACE_ONE}(?: (?:and|or) (?:the )?{_PLACE_ONE}){{0,2}}"
        rf"|{NUM_RE} (?:cities|countries|locations|states|time zones))"
    )


def _place_one() -> str:
    return rf"(?P<place{next(_GROUP_IDS)}>{_PLACE_ONE})"


def _count_n() -> str:
    """'3 days', '3 day', '3d', '3 d': the period may be left unstated."""
    TEMPLATE_WORDS.update(["days", "day", "d"])
    return rf"(?P<n{next(_GROUP_IDS)}>{NUM_RE}) ?(?:days|day|d)"


def _count_per() -> str:
    """'3x', '3 x', '3 times', '3-day': only with a stated week."""
    TEMPLATE_WORDS.update(["x", "times", "day"])
    return rf"(?P<n{next(_GROUP_IDS)}>{NUM_RE})(?:-day| ?x| times)"


def _count_times() -> str:
    return rf"(?P<t{next(_GROUP_IDS)}>{_alt(*TIMES)})"


# --- T1 remote (0)
GEO = _phrases(
    "worldwide", "anywhere", "global", "globally", "international", "us", "usa", "u s", "united states",
    "uk", "united kingdom", "canada", "europe", "eu", "emea", "apac", "latam", "americas",
    "north america", "work from anywhere", "from anywhere",
)
GEO_TAIL = (
    f"{_opt(_phrases('in', 'within', 'across', 'from', 'in the', 'within the', 'across the', 'from the'))}"
    f"{GEO}(?: {_phrase('only')})?"
)
REMOTE_CORE = _phrases("remote", "remote first", "remote only", "remote friendly", "remote native", "remote based")
REMOTE_PRE = _phrases("fully", "100", "100 percent", "entirely", "completely", "all")
REMOTE_NOUN = _phrases(
    "company", "team", "organization", "org", "position", "positions", "role", "roles", "job", "jobs",
    "work", "culture", "workplace", "workforce", "employer", "environment",
)
REMOTE_SUBJECT = _phrases(
    "all employees work remotely", "everyone works remotely", "we work remotely", "our team works remotely",
    "we are fully remote", "we are remote", "we are a remote company", "we are a fully remote company",
    "we are remote first", "we are a remote first company",
)
NOT_HYBRID = _phrases("not hybrid", "no hybrid", "non hybrid", "no hybrid option")
REMOTE_HEAD = _alt(
    f"{_opt(NOT_HYBRID)}{_opt(REMOTE_PRE)}{REMOTE_CORE}(?: {REMOTE_NOUN})?(?: {GEO_TAIL})?",
    REMOTE_SUBJECT,
)

# --- T2 optional office (0), also the remote tails (a) and (d)
OFF_W = _phrases(
    "office", "offices", "in office", "in the office", "onsite", "on site", "in person", "rto",
    "return to office", "return to the office", "office attendance", "office presence", "in office attendance",
    "in person attendance", "onsite attendance", "on site attendance",
)
OFF_NOUN = _phrases("attendance", "presence", "days", "day", "time", "work", "space", "visits")
NOT_REQ = _phrases(
    "optional", "voluntary", "not required", "not mandatory", "not needed", "not expected", "not necessary",
    "never required", "never mandatory", "isn t required", "aren t required", "isn t mandatory",
    "not a requirement",
)
NO_REQ = _phrases(
    "required", "requirement", "requirements", "mandate", "mandated", "mandatory", "needed", "expected",
    "obligation", "plans",
)
OPTIONAL_OFFICE = _alt(
    f"{_phrase('optional')} {OFF_W}(?: {OFF_NOUN})?",
    f"{_phrases('no', 'zero')} {OFF_W}(?: {OFF_NOUN})? {NO_REQ}",
    f"{_phrase('no')} {_phrases('mandatory', 'required')} {OFF_W}(?: {OFF_NOUN})?",
    f"{OFF_W}(?: {OFF_NOUN})?(?: {_phrases('is', 'are')})? {NOT_REQ}",
)
# T1 tail (b): an incidental office mention after a remote head
INCIDENTAL = _alt(
    f"{_opt(_phrases('with', 'we have', 'and'))}"
    f"{_opt(_phrases('an', 'a', 'our', 'a small', 'small', 'optional'))}"
    f"{_phrases('office', 'offices', 'office hub', 'office hubs', 'hub', 'hubs', 'office space', 'office spaces', 'coworking space', 'coworking spaces', 'headquarters', 'hq')}"
    f" {_phrase('in')} {_place()}",
    f"{_opt(_phrase('with'))}{_opt(_phrases('optional', 'an', 'our'))}"
    f"{_phrases('office hubs', 'office hub', 'office space', 'coworking space', 'coworking spaces', 'coworking stipend', 'coworking')}"
    f"(?: {_phrases('available', 'stipend')})?",
    f"{_phrase('headquartered')} {_phrase('in')} {_place()}",
    f"{_phrases('employees', 'you', 'staff', 'team members')} {_phrases('may', 'can')} {_phrase('use')} "
    f"{_phrases('our', 'an', 'the')} {_phrases('office', 'offices', 'office space', 'coworking space')}"
    f"(?: {_phrase('in')} {_place()})?",
)
REMOTE_STYLE = _phrases("async by default", "async first", "async")
NO_OFFICE = _phrases(
    "no office", "no offices", "no physical office", "no physical offices", "no headquarters", "no hq",
    "no rto", "no return to office", "no return to the office", "no in person", "no onsite", "no on site",
    "no in office",
)
REMOTE_TAIL = _alt(f"(?:{_phrase('with')} )?{OPTIONAL_OFFICE}", INCIDENTAL, REMOTE_STYLE, NO_OFFICE)
REMOTE_T = f"{REMOTE_HEAD}(?: {REMOTE_TAIL})?"

# --- T6 in-office (5)
O_CORE = _phrases(
    "office", "the office", "in office", "in the office", "at the office", "onsite", "on site", "in person",
    "office first", "office based", "office centric", "return to office", "return to the office", "rto",
    "back in the office", "back to the office", "onsite at hq", "on site at hq", "in office at hq",
    "at hq", "in person company",
)
O_INT = _phrases("fully", "full time", "100", "always", "entirely")
O_SUF = _phrases(
    "only", "required", "mandatory", "daily", "every day", "all week", "full time", "required daily",
    "mandate", "attendance required", "presence required", "collaboration required", "is required",
    "is mandatory",
)
FULL_WEEK = _phrases(
    "monday friday", "monday to friday", "monday through friday", "monday thru friday", "mon fri",
    "mon to fri", "every day", "daily", "all week", "full time", "five days a week", "5 days a week",
    "five days", "5 days",
)
SUBJECT = _phrases(
    "everyone", "employees", "all employees", "our team", "our teams", "staff", "the team", "we", "you",
    "our employees",
)
ATTEND_VERB = _phrases(
    "work", "works", "working", "are", "is", "be", "must be", "must work", "are required to be",
    "are required to work", "is required to be", "required to be", "required to work", "are expected to be",
    "are expected to work", "expected to be", "expected to work", "expected", "based", "are based",
    "is based", "works together", "work together", "come into", "comes into", "report to", "reports to",
)
ATTEND_WHERE = _alt(
    f"{_phrases('in', 'from', 'at', 'to', 'into')} {_phrases('the', 'our')} "
    f"(?:{_place_one()} )?{_phrases('office', 'offices', 'hq', 'headquarters')}"
    f"(?: {_phrase('in')} {_place()})?",
    f"{_phrases('in', 'from', 'at')} {_phrases('office', 'hq', 'headquarters')}",
)
ATTEND_ADV = _phrases(
    "full time", "daily", "every day", "together", "five days a week", "5 days a week", "monday to friday",
    "monday through friday", "monday friday",
)
ATTEND = f"{_opt(SUBJECT)}{ATTEND_VERB} {ATTEND_WHERE}(?: {ATTEND_ADV})?"
REQUIREMENT = _alt(
    f"{_phrases('office', 'in office', 'onsite', 'on site', 'in person', 'office attendance', 'office presence', 'in office attendance', 'onsite attendance', 'in person attendance', 'in person collaboration')}"
    f" {_phrases('required', 'is required', 'mandatory', 'is mandatory', 'expected', 'is expected', 'required daily')}",
    f"{_phrases('mandatory', 'required')} "
    f"{_phrases('office presence', 'office attendance', 'in office attendance', 'onsite attendance', 'in person attendance')}",
    f"{_phrase('must be')} {_phrases('in office', 'in the office', 'onsite', 'on site', 'in person', 'at the office')}",
    f"{_phrases('return to office', 'rto', 'return to the office')} {_phrases('mandate', 'mandatory', 'required')}",
    f"{_phrases('rto policy', 'office policy', 'work policy')} "
    f"{_phrases('in office', 'in the office', 'onsite', 'on site', 'office')}",
)
O_HEAD = _alt(f"{_opt(O_INT)}{O_CORE}(?: {O_SUF})?", f"{O_CORE} {FULL_WEEK}", ATTEND, REQUIREMENT)

WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "mondays": 0, "tue": 1, "tues": 1, "tuesday": 1, "tuesdays": 1,
    "wed": 2, "wednesday": 2, "wednesdays": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "thursdays": 3,
    "fri": 4, "friday": 4, "fridays": 4,
}
WEEKDAY = _phrases(*WEEKDAY_NAMES)
WEEKDAYS = f"(?:{_phrase('on')} )?{WEEKDAY}(?: (?:and )?{WEEKDAY})?"
_SPAN_WORDS = ("to", "through", "thru")
TEMPLATE_WORDS.update(_SPAN_WORDS)
# A stated weekday span or list, up to five weekdays: "Monday-Thursday", "Mon to Thu",
# "Tues/Wed/Thurs", "Tuesdays and Thursdays". The count is computed by _weekday_days.


def _weekday_set() -> str:
    return (
        rf"(?P<wd{next(_GROUP_IDS)}>(?:{_phrase('on')} )?{WEEKDAY}"
        rf"(?:(?:-| (?:{'|'.join(_SPAN_WORDS)}|{_phrases('and', 'or')}) | ){WEEKDAY}){{0,4}})"
    )


REMOTE_EXCEPTION = _alt(
    f"{_opt(_phrase('with'))}{_phrases('remote', 'remote work', 'remotely')} "
    f"{_opt(_phrases('is', 'only', 'is only', 'considered', 'allowed', 'permitted', 'available'))}"
    f"{_phrases('by exception', 'by exception only', 'as an exception', 'as exceptions', 'on request', 'upon request', 'by request', 'by approval', 'by approval only', 'with approval', 'with vp approval', 'with manager approval', 'for exceptions', 'for exceptions only', 'for emergencies', 'for approved exceptions', 'in exceptional circumstances')}",
    f"{_phrase('remote')} "
    f"{_phrases('only with vp approval', 'only with approval', 'only with manager approval', 'only by exception', 'only for approved exceptions', 'only by approval')}",
    _phrases(
        "remote exceptions", "remote exceptions are rare", "remote is rare", "remote rare", "limited remote",
        "remote rarely approved", "remote work rarely approved", "remote not standard", "remote is not standard",
    ),
    _phrases("no remote", "no remote work", "no remote option", "not remote"),
    f"{_phrases('remote', 'remote work', 'remote positions', 'remote roles')} {_opt(_phrases('is', 'are'))}"
    f"{_phrases('not permitted', 'not allowed', 'not offered', 'not available', 'not an option', 'not possible', 'not supported')}",
    f"{_phrases('we don t offer', 'we do not offer', 'we don t hire', 'we do not hire', 'we don t support', 'we do not support')}"
    f" {_phrases('remote', 'remote work', 'remote positions', 'remote roles', 'fully remote', 'fully remote roles')}",
    _phrases("no exceptions", "no in office exceptions", "no remote exceptions"),
)
OPTIONAL_REMOTE_WEEKDAYS = f"{_phrases('optional remote', 'remote optional', 'optional wfh')} {WEEKDAYS}"
NEG_REMOTE = _phrases(
    "no remote", "no remote work", "not remote", "non remote", "remote not permitted",
    "remote not allowed", "remote not an option", "remote is not an option", "remote work not permitted",
)
NEG_REMOTE_PREFIX = _alt(NEG_REMOTE, NOT_HYBRID)
OFFICE_T = f"{_opt(NEG_REMOTE_PREFIX)}{O_HEAD}(?: (?:{REMOTE_EXCEPTION}|{OPTIONAL_REMOTE_WEEKDAYS}))?"

# --- T5 double negative (5)
DOUBLE_NEGATIVE = (
    f"{_opt(NEG_REMOTE_PREFIX)}"
    f"{_phrases('office', 'in office', 'onsite', 'on site', 'in person', 'office attendance', 'office presence', 'office days')}"
    f"(?: {_phrases('is', 'are')})? {_phrases('not', 'never', 'isn t', 'is never', 'aren t')} "
    f"{_phrases('optional', 'voluntary')}"
)

# --- T4 day count (n)
WEEK = _phrases("a week", "per week", "each week", "every week", "a wk", "per wk", "wk", "week", "weekly")
C_LOC = _phrases(
    "in office", "in the office", "at the office", "in our office", "onsite", "on site", "in person",
    "at hq", "at headquarters", "from the office", "in office days", "in office day",
)
HYBRID_LEAD = f"{_phrases('hybrid', 'hybrid schedule', 'hybrid work', 'hybrid model')}(?: {_phrase('with')})?"
OFFICE_LEAD = (
    f"{_phrases('in office', 'in the office', 'office', 'onsite', 'on site', 'in person', 'rto', 'return to office', 'return to the office', 'office based', 'at the office', 'back in the office', 'office attendance')}"
    f"(?: {_phrases('required', 'mandatory')})?"
)
C_LEAD = _alt(HYBRID_LEAD, OFFICE_LEAD)
REQUIRED_SUFFIX = _phrases("required", "mandatory")
OPTIONAL_DAYS_CLAUSE = (
    f"{_phrases('office', 'in office', 'onsite', 'on site', 'in person', 'office attendance')}"
    f"(?: {_phrases('is', 'are')})? {_phrases('optional', 'not required', 'voluntary', 'not mandatory')}"
    f" {_alt(WEEKDAYS, _phrases('the other days', 'the other two days', 'the other day', 'other days', 'on other days', 'during the holidays', 'during holidays', 'the rest of the week'))}"
)
REMOTE_WEEKDAYS = f"{_phrases('remote', 'wfh', 'work from home')} {WEEKDAYS}"
COUNT_FORM = _alt(
    f"{C_LEAD} {_count_n()}(?: {WEEK})?(?: {C_LOC})?",
    f"{_opt(C_LEAD)}{_count_n()}(?: {WEEK})? {C_LOC}",
    f"{C_LEAD} {_count_per()} {WEEK}(?: {C_LOC})?",
    f"{_opt(C_LEAD)}{_count_per()} {WEEK} {C_LOC}",
    f"{C_LEAD} {_count_times()} {WEEK}(?: {C_LOC})?",
    f"{_opt(C_LEAD)}{_count_times()} {WEEK} {C_LOC}",
    f"(?P<n{next(_GROUP_IDS)}>{NUM_RE}) "
    f"{_phrases('in office days', 'in office day', 'office days', 'office day', 'onsite days', 'onsite day', 'on site days', 'on site day', 'in person days', 'in person day')}(?: {WEEK})?",
    f"{_opt(_phrase('weekly'))}"
    f"{_phrases('days in office', 'days in the office', 'in office days', 'office days', 'onsite days', 'days onsite')}"
    f"(?: {WEEK})? (?P<n{next(_GROUP_IDS)}>{NUM_RE})",
    # a stated weekday span or list under an office or in-person lead (owner rule, Q3)
    f"{_opt(HYBRID_LEAD)}{OFFICE_LEAD} {_weekday_set()}",
    f"{_opt(HYBRID_LEAD)}{_weekday_set()} {C_LOC}",
)
COUNT_T = (
    f"{_opt(NEG_REMOTE)}{COUNT_FORM}(?: {REQUIRED_SUFFIX})?"
    f"(?: (?:{OPTIONAL_DAYS_CLAUSE}|{REMOTE_EXCEPTION}|{REMOTE_WEEKDAYS}))?"
)

# --- T3 zero days (0)
ZERO_DAYS = f"(?:zero|0|no)[ -]?{_phrases('days', 'day')} {C_LOC}"

# --- T7 hybrid, no count (3)
HYBRID_T = (
    f"{_phrase('hybrid')}"
    f"(?: {_phrases('schedule', 'work model', 'model', 'role', 'position', 'work', 'policy', 'arrangement', 'setup', 'company', 'team', 'workplace', 'details shared in interview', 'details shared at interview', 'schedule shared in interview')})?"
    f"(?: {_phrase('in')} {_place()})?"
)

LABEL = _phrases(
    "rto policy", "remote policy", "office policy", "work policy", "workplace policy", "work model",
    "work arrangement", "work location", "working model", "policy",
)

# M1: exact codes and labels, compared case-folded.
EXACT = {
    "r": 0, "remote": 0, "h": 3, "hybrid": 3,
    "o": 5, "in-office": 5, "in office": 5, "onsite": 5, "on-site": 5, "on site": 5,
}

COUNT = "count"  # reading marker: the number is in the match's n / t / wd groups
_TEMPLATE_SOURCES = [
    ("remote", 0, REMOTE_T),
    ("optional-office", 0, OPTIONAL_OFFICE),
    ("zero-days", 0, ZERO_DAYS),
    ("count", COUNT, COUNT_T),
    ("double-negative", 5, DOUBLE_NEGATIVE),
    ("in-office", 5, OFFICE_T),
    ("hybrid", 3, HYBRID_T),
]
TEMPLATES = [
    (name, reading, re.compile(rf"(?:{LABEL} )?(?:{pattern})", re.IGNORECASE))
    for name, reading, pattern in _TEMPLATE_SOURCES
]


# ---------------------------------------------------------------------- reading
def _weekday_days(text: str) -> int | None:
    """Days named by a weekday span or list: 1-3 as stated, four or more is 5.

    "Mon-Thu" and "Monday through Thursday" are spans; "Tues/Wed/Thurs" and
    "Tuesdays and Thursdays" are lists. Weekdays with nothing but a space
    between them are a list of three or more, or two adjacent days; two apart
    ("Tuesday - Thursday" and "Tuesday, Thursday" normalize alike) and a bare gap
    beside a span are unknown.
    """
    words = re.findall(r"[a-z]+|-", text.lower())
    if words[0] == "on":
        words = words[1:]
    weekdays = [WEEKDAY_NAMES[w] for w in words if w in WEEKDAY_NAMES]
    gaps: list[str] = []  # the connector before each weekday after the first
    gap: list[str] = []
    for word in words:
        if word in WEEKDAY_NAMES:
            if len(gap) > 1:
                return None
            if not gap:
                gaps.append("bare")
            elif gap[0] in ("-", *_SPAN_WORDS):
                gaps.append("span")
            elif gap[0] in ("and", "or"):
                gaps.append("list")
            else:
                return None
            gap = []
        else:
            gap.append(word)
    gaps = gaps[1:]
    days = {weekdays[0]}
    for index, (kind, day) in enumerate(zip(gaps, weekdays[1:])):
        if kind == "span":
            if day < weekdays[index] or (index and gaps[index - 1] == "span"):
                return None
            days.update(range(weekdays[index], day + 1))
        else:
            days.add(day)
    if "bare" in gaps and ("span" in gaps or (gaps == ["bare"] and abs(weekdays[1] - weekdays[0]) > 1)):
        return None
    return 5 if len(days) >= 4 else len(days)


def _places_ok(match: re.Match) -> bool:
    """M4: every PLACE token starts with an upper-case letter in the original text."""
    for key, value in match.groupdict().items():
        if key.startswith("place") and value:
            if re.fullmatch(rf"{NUM_RE} (?:cities|countries|locations|states|time zones)", value, re.IGNORECASE):
                continue
            for token in value.split():
                if token.lower() not in {"and", "or", "the"} and not token[0].isupper():
                    return False
    return True


def _match_days(match: re.Match, reading: Any) -> int | None:
    if reading != COUNT:
        return reading
    for key, value in match.groupdict().items():
        if not value:
            continue
        if key.startswith("n"):
            return NUM[value.lower()]
        if key.startswith("t"):
            return TIMES[value.lower()]
        if key.startswith("wd"):
            return _weekday_days(value)
    return None  # pragma: no cover - every COUNT template captures one of the groups


@lru_cache(maxsize=8192)
def _read(value: str) -> tuple[int | None, str | None]:
    text = _normalize(value)
    if len(text) > MAX_CHARS:
        return None, None
    exact = EXACT.get(text.casefold())
    if exact is not None:
        return exact, "exact"
    hits = []
    for name, reading, pattern in TEMPLATES:
        match = pattern.fullmatch(text)
        if match is None or not _places_ok(match):
            continue
        days = _match_days(match, reading)
        if days is not None:
            hits.append((days, name))
    readings = {days for days, _ in hits}
    if len(readings) == 1:  # M3: families that disagree read unknown
        return hits[0]
    return None, None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def office_days(value: Any) -> int | None:
    """In-office days (0-5) for an office-policy value, or None when unknown."""
    return _read(_text(value))[0]


def office_template(value: Any) -> str | None:
    """The template family that read ``value``, or None when it reads unknown."""
    return _read(_text(value))[1]
