from datetime import datetime, timedelta, timezone
import os
import json
from typing import Optional
from zoneinfo import ZoneInfo
from app.utils.logging_config import logger

IST = ZoneInfo("Asia/Kolkata")

_LEGACY_NAIVE_IST_OFFSET = timedelta(hours=5, minutes=30)


class TimestampTracker:
    """Track the last processed timestamp (Microsoft Graph message time) to
    avoid re-processing already-seen emails.

    Storage -> UTC. Timestamps are always kept internally, and persisted to
    disk, as timezone-aware UTC datetimes - the only unambiguous
    representation to send to the Microsoft Graph API filter.

    Display -> IST. IST is only ever produced for human-facing display (see
    get_last_processed_ist_display), via a proper zoneinfo conversion - never
    a manual +5:30/-5:30 offset.
    """

    def __init__(self, timestamp_file: str = "last_processed.json"):
        self.timestamp_file = timestamp_file
        self.last_processed_time: Optional[datetime] = None  # always tz-aware UTC
        self._load_timestamp()

    def _load_timestamp(self):
        """Load the last processed timestamp from file.

        Backward compatible with a file written by the pre-Phase-1 version
        of this app, which stored a *naive* datetime that was actually IST.
        A naive value found here is migrated once - interpreted as that
        legacy IST value and converted to real UTC - so upgrading doesn't
        suddenly shift the "since" filter by 5 hours 30 minutes and cause a
        block of emails to be skipped or reprocessed.
        """
        try:
            if os.path.exists(self.timestamp_file):
                with open(self.timestamp_file, 'r') as f:
                    data = json.load(f)
                    timestamp_str = data.get('last_processed')
                    if timestamp_str:
                        parsed = datetime.fromisoformat(timestamp_str)
                        if parsed.tzinfo is None:
                            legacy_naive_ist = parsed
                            self.last_processed_time = (
                                legacy_naive_ist - _LEGACY_NAIVE_IST_OFFSET
                            ).replace(tzinfo=timezone.utc)
                            logger.info(
                                f"Migrated legacy naive timestamp "
                                f"{legacy_naive_ist.isoformat()} (assumed IST) to "
                                f"UTC: {self.last_processed_time.isoformat()}"
                            )
                        else:
                            self.last_processed_time = parsed.astimezone(timezone.utc)
                        logger.info(
                            f"Loaded last processed timestamp (UTC): "
                            f"{self.last_processed_time.isoformat()}"
                        )
        except Exception as e:
            logger.warning(f"Could not load timestamp file: {str(e)}")
            self.last_processed_time = None

    def update_timestamp(self, timestamp: datetime):
        """Update the last processed timestamp.

        Accepts either a timezone-aware datetime (converted to UTC) or a
        naive datetime (assumed to already be UTC, e.g. from
        datetime.utcnow()) - always stored internally, and persisted to
        disk, as timezone-aware UTC. No manual timezone arithmetic here.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)

        self.last_processed_time = timestamp
        try:
            with open(self.timestamp_file, 'w') as f:
                json.dump({
                    'last_processed': timestamp.isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }, f, indent=2)
            logger.info(f"Updated last processed timestamp to (UTC): {timestamp.isoformat()}")
        except Exception as e:
            logger.error(f"Could not save timestamp file: {str(e)}")

    def get_last_processed_time(self) -> Optional[datetime]:
        """Get the last processed timestamp, timezone-aware UTC."""
        return self.last_processed_time

    def get_last_processed_ist_display(self) -> Optional[str]:
        """Get the last processed timestamp formatted for display in IST,
        e.g. '2026-09-15 12:00:00 IST'. Display-only - this value is never
        used for storage or for the Graph API filter (see get_filter_string)."""
        if not self.last_processed_time:
            return None
        ist_time = self.last_processed_time.astimezone(IST)
        return ist_time.strftime('%Y-%m-%d %H:%M:%S IST')

    def get_filter_string(self) -> Optional[str]:
        """Get the filter string for Microsoft Graph API. The tracker
        already stores UTC internally, so this is a direct format - no
        timezone conversion needed here."""
        if self.last_processed_time:
            # Format for Microsoft Graph API: 2024-01-15T10:30:00Z
            return self.last_processed_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        return None
