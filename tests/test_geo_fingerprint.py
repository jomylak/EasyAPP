import json
from unittest.mock import patch, MagicMock

from applypilot.apply import geo_fingerprint


def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_lookup_geo_caches_by_key():
    geo_fingerprint._cache.clear()
    with patch("urllib.request.OpenerDirector.open",
               return_value=_fake_response({"timezone": "America/New_York", "countryCode": "US"})) as m:
        first = geo_fingerprint.lookup_geo("1.2.3.4:8080", 9999)
        second = geo_fingerprint.lookup_geo("1.2.3.4:8080", 9999)
    assert first == {"timezone": "America/New_York", "locale": "en-US"}
    assert second == first
    assert m.call_count == 1  # second call was a cache hit, no new request


def test_lookup_geo_different_key_not_cached():
    geo_fingerprint._cache.clear()
    with patch("urllib.request.OpenerDirector.open",
               return_value=_fake_response({"timezone": "Europe/London", "countryCode": "GB"})):
        geo_fingerprint.lookup_geo("1.1.1.1:80", 1111)
    with patch("urllib.request.OpenerDirector.open",
               return_value=_fake_response({"timezone": "Asia/Tokyo", "countryCode": "JP"})) as m:
        result = geo_fingerprint.lookup_geo("2.2.2.2:80", 2222)
    assert result == {"timezone": "Asia/Tokyo", "locale": "en-US"}
    assert m.call_count == 1


def test_lookup_geo_returns_none_on_failure():
    geo_fingerprint._cache.clear()
    with patch("urllib.request.OpenerDirector.open", side_effect=OSError("boom")):
        assert geo_fingerprint.lookup_geo("3.3.3.3:80", 3333) is None
