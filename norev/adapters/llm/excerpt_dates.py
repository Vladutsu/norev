"""The day a Transcript excerpt names, to check the due date the model computed (#41).

The model gets a calendar in the prompt and still picks the wrong row ("by Thursday" -> Tuesday).
When the excerpt it quoted names exactly one day ("by Thursday", "tomorrow", "November 13th"),
that day is computed here from the Session date. Weekdays mean the next one after the Session
("until Friday" said on a Friday is the Friday after). "Next week" or "the end of the month" name no
single day, so they are left to the model.
"""
import re
from datetime import date, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")
_MONTH = "|".join(MONTHS)
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_DAY_MONTH = re.compile(rf"\b{_DAY}\s+(?:of\s+)?({_MONTH})\b")
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+(?:the\s+)?{_DAY}\b")


def _explicit(day: int, month_name: str, session_date: date) -> date | None:
    try:
        d = date(session_date.year, MONTHS.index(month_name) + 1, day)
    except ValueError:
        return None
    if (session_date - d).days > 31:  # "January 5th" said in November is next year
        d = d.replace(year=d.year + 1)
    return d


def day_said(excerpt: str, session_date: date) -> date | None:
    """The single day the excerpt names, or None when it names none or more than one."""
    text = excerpt.lower()
    days: set[date] = set()
    for m in _DAY_MONTH.finditer(text):
        d = _explicit(int(m.group(1)), m.group(2), session_date)
        if d:
            days.add(d)
    for m in _MONTH_DAY.finditer(text):
        d = _explicit(int(m.group(2)), m.group(1), session_date)
        if d:
            days.add(d)
    if re.search(r"\bnext week\b|\bend of the (?:week|month)\b|\bevery\b|\beach\b", text):
        return None  # no single day, or a repeating one ("every Tuesday")
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}s?\b", text):
            ahead = (i - session_date.weekday()) % 7 or 7
            days.add(session_date + timedelta(days=ahead))
    if re.search(r"\btomorrow\b", text):
        days.add(session_date + timedelta(days=1))
    if re.search(r"\b(?:today|tonight|this evening|this afternoon)\b", text):
        days.add(session_date)
    return days.pop() if len(days) == 1 else None


def corrected_due_date(due_date: str | None, excerpt: str | None, session_date: date) -> str | None:
    """The model's due date, or the day the excerpt names when the two disagree.

    Only a date the model gave is corrected: an excerpt can name a day that is not a deadline
    ("we meet on Thursday"), and the model already judged whether there is one.
    """
    if not due_date or not excerpt:
        return due_date
    said = day_said(excerpt, session_date)
    return said.isoformat() if said and said.isoformat() != due_date else due_date
