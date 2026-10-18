"""Literal date and URL normalization shared by local bulletin adapters.

These helpers never fetch, call a model, or infer a year from an issue date.
Evidence strings are preserved; normalized_text is for matching, not display.
"""
from datetime import date
import re
import unicodedata

from nannanews import acquisition

SCHEMA_VERSION = 1
FULL_DATE = re.compile(r'(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?:日|号)?(?!\d)')
MONTH_DAY = re.compile(r'(?<![\d:])(?:\d{1,2}\s*月\s*\d{1,2}(?:日|号)?|\d{1,2}\s*[-/.]\s*\d{1,2})(?![\d:])')
PARTIAL_DAY_END = re.compile(r'(?:至|到|[-—–~～])\s*\d{1,2}\s*(?:(?:日|号)(?!\d)|$)')


def normalized_text(text):
    """Whitespace/NFC comparison key; callers keep original text separately."""
    return ' '.join(unicodedata.normalize('NFC', str(text or '')).split())


def source_url(value):
    """Retain exact public URL identity and queries without accessing the URL."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return acquisition.canonical_url(value)
    except acquisition.AcquisitionError:
        return None


def date_field(kind, raw, evidence=None, locator=None):
    """Normalize a single explicit-year claim, retaining unresolved raw claims.

    A range or a list remains ambiguous rather than collapsing to its first date.
    A single date followed by a time is still a single date. Partial endpoints do
    not inherit the year from the beginning of a range or from issue metadata.
    """
    raw = str(raw or '').strip()
    matches = list(FULL_DATE.finditer(raw))
    values = []
    invalid = False
    remaining = raw
    for match in reversed(matches):
        remaining = remaining[:match.start()] + ' ' + remaining[match.end():]
    for match in matches:
        try:
            value = date(*map(int, match.groups()))
        except ValueError:
            invalid = True
        else:
            values.append(value.isoformat())
    normalized = None
    if invalid:
        resolution = 'invalid'
    elif len(set(values)) == 1 and not MONTH_DAY.search(remaining) and not PARTIAL_DAY_END.search(remaining):
        normalized = values[0]
        resolution = 'explicit'
    elif values:
        resolution = 'ambiguous'
    elif MONTH_DAY.search(raw):
        resolution = 'year_unknown'
    else:
        resolution = 'ambiguous'
    return {
        'kind': kind, 'raw_text': raw, 'normalized_date': normalized,
        'resolution': resolution, 'evidence': raw if evidence is None else str(evidence),
        'locator': locator,
    }
