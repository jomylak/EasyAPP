"""Match a worker's browser timezone/locale to its proxy's real exit geo.

A static residential proxy's exit IP doesn't move, so its geo is looked up
once (through the proxy itself, via its local forwarder) and cached forever
under that proxy's own identity -- not the worker_id, so swapping a burned
proxy for a different upstream naturally gets a fresh lookup with no manual
cache invalidation.

Timezone is applied via the Chrome subprocess's TZ env var rather than a CDP
Emulation.setTimezoneOverride call: Chromium's ICU layer reads TZ once at
process startup and it then applies to every render process, tab, and popup
(including SSO popups), where a CDP override would need reapplying per-tab.
Locale and Accept-Language both come from Chrome's native --lang flag, so no
CDP connection is needed here at all.
"""

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

_GEO_API_URL = "http://ip-api.com/json/?fields=timezone,countryCode"

_LOCALE_BY_COUNTRY = {
    "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU",
    "IE": "en-IE", "NZ": "en-NZ", "DE": "de-DE", "FR": "fr-FR",
}
_DEFAULT_LOCALE = "en-US"

_cache: dict[str, dict] = {}


def lookup_geo(cache_key: str, local_proxy_port: int, timeout: float = 5.0) -> dict | None:
    """Resolve {"timezone", "locale"} for whatever proxy sits behind
    127.0.0.1:local_proxy_port, caching by cache_key (the proxy's own
    "host:port" identity). Returns None on any failure -- callers must fall
    back to Chrome's own defaults rather than block launch on this.
    """
    if cache_key in _cache:
        return _cache[cache_key]

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{local_proxy_port}"})
    )
    try:
        with opener.open(_GEO_API_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:
        logger.warning("geo lookup failed for proxy %s", cache_key, exc_info=True)
        return None

    timezone = data.get("timezone")
    if not timezone:
        return None
    geo = {
        "timezone": timezone,
        "locale": _LOCALE_BY_COUNTRY.get(data.get("countryCode"), _DEFAULT_LOCALE),
    }
    _cache[cache_key] = geo
    return geo
