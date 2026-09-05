"""Normalising stated pay to an hourly number.

Pay arrives from discovery as a display string -- "$35-$50/hr",
"$110500-$160000/yr", "$6k-$11k/mon", "Unpaid" -- which is fine for showing to
a person and useless for filtering. Sorting or thresholding on that text
compares "$9" against "$110500" lexically and puts the nine first.

Everything is converted to dollars per hour so a single threshold works across
all four period formats. Both ends of the range are kept: the low end is what
the posting guarantees, the high end is what it could pay, and those answer
different questions.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Hours per period. A year is the standard 2080 (40 x 52); a month is that
# divided by twelve rather than a calendar average, so annual and monthly
# postings from the same employer normalise to the same number.
_HOURS = {
    "hr": 1.0,
    "wk": 40.0,
    "mon": 2080.0 / 12.0,
    "yr": 2080.0,
}

# "$110500-$160000/yr", "$6k-$11k/mon", "$35-$35/hr", "$20.27-$22.30/hr"
_RANGE = re.compile(
    r"\$\s*(?P<lo>[\d,]+(?:\.\d+)?)\s*(?P<lok>k?)"
    r"\s*(?:-|–|—|to)\s*"
    r"\$?\s*(?P<hi>[\d,]+(?:\.\d+)?)\s*(?P<hik>k?)"
    r"\s*/\s*(?P<per>hr|wk|mon|yr)",
    re.I,
)
# Single figure, no range: "$45/hr"
_SINGLE = re.compile(
    r"\$\s*(?P<lo>[\d,]+(?:\.\d+)?)\s*(?P<lok>k?)\s*/\s*(?P<per>hr|wk|mon|yr)",
    re.I,
)

_UNPAID = {"unpaid", "no pay", "$0", "0"}

# Above this, the figure is not a wage. Discovery occasionally scrapes a
# number out of the wrong element -- the database holds
# "$7650000000-$12134000000/mon" and "$0-$10000000/yr" -- and a single row like
# that is enough to wreck a sort or make a pay threshold return nothing. These
# are rejected rather than clamped: a wrong number that looks plausible is
# worse than no number, because nothing downstream can tell it was invented.
_MAX_PLAUSIBLE_HOURLY = 1000.0


def _num(raw: str, thousands: str) -> float:
    value = float(raw.replace(",", ""))
    return value * 1000 if thousands.lower() == "k" else value


def to_hourly(salary: str | None) -> tuple[float | None, float | None]:
    """Convert a stated pay string to (low, high) dollars per hour.

    Returns (None, None) when there is no usable figure -- absent, "N/A", or
    denominated in a currency other than dollars. A non-dollar posting is
    deliberately not guessed at: applying today's exchange rate to a number
    scraped weeks ago would invent precision that isn't there, and these
    postings are rare (one in the whole database).

    "Unpaid" returns (0.0, 0.0), which is a real answer rather than a missing
    one -- an unpaid posting should be excluded by any pay floor above zero,
    not treated as unknown and let through.
    """
    if not salary:
        return None, None

    text = salary.strip()
    if text.lower() in _UNPAID:
        return 0.0, 0.0

    m = _RANGE.search(text)
    if m:
        per = _HOURS[m.group("per").lower()]
        lo = _num(m.group("lo"), m.group("lok")) / per
        hi = _num(m.group("hi"), m.group("hik")) / per
        # Some postings state "$129000-$0/yr" -- a missing upper bound, not a
        # range down to zero. Treat a zero high end as absent rather than
        # letting it invert the range.
        if hi <= 0:
            hi = lo
        if hi < lo:
            lo, hi = hi, lo
        if hi > _MAX_PLAUSIBLE_HOURLY:
            logger.debug("Implausible pay %r -> %.0f/hr; discarding.", text, hi)
            return None, None
        return round(lo, 2), round(hi, 2)

    m = _SINGLE.search(text)
    if m:
        per = _HOURS[m.group("per").lower()]
        value = round(_num(m.group("lo"), m.group("lok")) / per, 2)
        if value > _MAX_PLAUSIBLE_HOURLY:
            return None, None
        return value, value

    return None, None


def backfill(conn) -> int:
    """Fill pay_min_hourly/pay_max_hourly for rows that don't have them yet.

    Idempotent and cheap to repeat: it only reads rows where the columns are
    still NULL but a salary string exists, so the second run does nothing.
    Rows whose salary cannot be parsed are marked with -1 rather than left
    NULL, so they are not re-examined on every startup forever.
    """
    rows = conn.execute("""
        SELECT url, salary FROM jobs
        WHERE salary IS NOT NULL AND salary != '' AND pay_min_hourly IS NULL
    """).fetchall()
    if not rows:
        return 0

    updates = []
    for row in rows:
        lo, hi = to_hourly(row["salary"])
        # -1 is the "looked, could not parse" marker. NULL means "not looked
        # at yet"; conflating the two would re-parse the same unparseable
        # strings on every single startup.
        updates.append((lo if lo is not None else -1.0,
                        hi if hi is not None else -1.0,
                        row["url"]))

    conn.executemany(
        "UPDATE jobs SET pay_min_hourly = ?, pay_max_hourly = ? WHERE url = ?",
        updates,
    )
    conn.commit()
    logger.info("Normalised pay for %d job(s).", len(updates))
    return len(updates)
