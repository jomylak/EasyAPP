"""Regression checks for database._local_date -- the UTC-to-Eastern day
bucketing fix. Real bug: a job discovered at 9pm Eastern is already
"tomorrow" in UTC, and the old date(...) expression bucketed by the raw UTC
date, so Browse showed tomorrow's jobs hours before it was tomorrow locally.
"""

from applypilot.database import _local_date


def test_utc_evening_still_reads_as_local_day():
    # 11pm EDT (UTC-4) on the 21st is 3am UTC on the 22nd -- must bucket to
    # the 21st, not the 22nd.
    assert _local_date("2026-06-22T03:00:00+00:00") == "2026-06-21"


def test_utc_morning_is_also_local_morning():
    # Well past the UTC/Eastern midnight gap either direction -- same day.
    assert _local_date("2026-06-22T18:00:00+00:00") == "2026-06-22"


def test_dst_boundary_uses_edt_offset_in_summer():
    # July: EDT is UTC-4. 3:30am UTC on the 5th is 11:30pm EDT on the 4th.
    assert _local_date("2026-07-05T03:30:00+00:00") == "2026-07-04"


def test_dst_boundary_uses_est_offset_in_winter():
    # January: EST is UTC-5. 4:30am UTC on the 5th is 11:30pm EST on the 4th.
    assert _local_date("2026-01-05T04:30:00+00:00") == "2026-01-04"


def test_bare_date_passes_through_unchanged():
    # No time component -- already names a calendar day, never shifted.
    assert _local_date("2026-06-22") == "2026-06-22"


def test_none_passes_through():
    assert _local_date(None) is None
