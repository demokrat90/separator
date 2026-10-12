"""The lead's local time, from the country code of the number."""

import logging
from zoneinfo import ZoneInfo

from django.utils import timezone

logger = logging.getLogger("django")

DEFAULT_TZ = "Asia/Dubai"


def lead_timezone(phone):
    """First time zone of the number's country/region; Asia/Dubai if unknown."""
    try:
        import phonenumbers
        from phonenumbers import timezone as pn_timezone

        zones = pn_timezone.time_zones_for_number(phonenumbers.parse(phone, None))
        for zone in zones or ():
            if zone and zone != "Etc/Unknown":
                return ZoneInfo(zone)
    except Exception as exc:  # unparseable number, or phonenumbers without tz data
        logger.info("waq: no time zone for a lead number (%s), using %s", type(exc).__name__, DEFAULT_TZ)
    return ZoneInfo(DEFAULT_TZ)


def local_hour(phone, now=None):
    now = now or timezone.now()
    return now.astimezone(lead_timezone(phone)).hour


def in_window(phone, start, end, now=None):
    return start <= local_hour(phone, now) < end
