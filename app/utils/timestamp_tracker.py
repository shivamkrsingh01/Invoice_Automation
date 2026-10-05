# from datetime import datetime, timedelta, timezone
# import os
# import json
# from typing import Optional
# from zoneinfo import ZoneInfo
# from app.utils.logging_config import logger

# IST = ZoneInfo("Asia/Kolkata")

# _LEGACY_NAIVE_IST_OFFSET = timedelta(hours=5, minutes=30)


# class TimestampTracker:
#     """Track the last processed timestamp (Microsoft Graph message time) to
#     avoid re-processing already-seen emails.

#     Storage -> UTC. Timestamps are always kept internally, and persisted to
#     disk, as timezone-aware UTC datetimes - the only unambiguous
#     representation to send to the Microsoft Graph API filter.

#     Display -> IST. IST is only ever produced for human-facing display (see
#     get_last_processed_ist_display), via a proper zoneinfo conversion - never
#     a manual +5:30/-5:30 offset.
#     """

#     def __init__(self, timestamp_file: str = "last_processed.json"):
#         self.timestamp_file = timestamp_file
#         self.last_processed_time: Optional[datetime] = None  # always tz-aware UTC
#         self._load_timestamp()

#     def _load_timestamp(self):
#         """Load the last processed timestamp from file.

#         Backward compatible with a file written by the pre-Phase-1 version
#         of this app, which stored a *naive* datetime that was actually IST.
#         A naive value found here is migrated once - interpreted as that
#         legacy IST value and converted to real UTC - so upgrading doesn't
#         suddenly shift the "since" filter by 5 hours 30 minutes and cause a
#         block of emails to be skipped or reprocessed.
#         """
#         try:
#             if os.path.exists(self.timestamp_file):
#                 with open(self.timestamp_file, 'r') as f:
#                     data = json.load(f)
#                     timestamp_str = data.get('last_processed')
#                     if timestamp_str:
#                         parsed = datetime.fromisoformat(timestamp_str)
#                         if parsed.tzinfo is None:
#                             legacy_naive_ist = parsed
#                             self.last_processed_time = (
#                                 legacy_naive_ist - _LEGACY_NAIVE_IST_OFFSET
#                             ).replace(tzinfo=timezone.utc)
#                             logger.info(
#                                 f"Migrated legacy naive timestamp "
#                                 f"{legacy_naive_ist.isoformat()} (assumed IST) to "
#                                 f"UTC: {self.last_processed_time.isoformat()}"
#                             )
#                         else:
#                             self.last_processed_time = parsed.astimezone(timezone.utc)
#                         logger.info(
#                             f"Loaded last processed timestamp (UTC): "
#                             f"{self.last_processed_time.isoformat()}"
#                         )
#         except Exception as e:
#             logger.warning(f"Could not load timestamp file: {str(e)}")
#             self.last_processed_time = None

#     def update_timestamp(self, timestamp: datetime):
#         """Update the last processed timestamp.

#         Accepts either a timezone-aware datetime (converted to UTC) or a
#         naive datetime (assumed to already be UTC, e.g. from
#         datetime.utcnow()) - always stored internally, and persisted to
#         disk, as timezone-aware UTC. No manual timezone arithmetic here.
#         """
#         if timestamp.tzinfo is None:
#             timestamp = timestamp.replace(tzinfo=timezone.utc)
#         else:
#             timestamp = timestamp.astimezone(timezone.utc)

#         self.last_processed_time = timestamp
#         try:
#             with open(self.timestamp_file, 'w') as f:
#                 json.dump({
#                     'last_processed': timestamp.isoformat(),
#                     'updated_at': datetime.now(timezone.utc).isoformat()
#                 }, f, indent=2)
#             logger.info(f"Updated last processed timestamp to (UTC): {timestamp.isoformat()}")
#         except Exception as e:
#             logger.error(f"Could not save timestamp file: {str(e)}")

#     def get_last_processed_time(self) -> Optional[datetime]:
#         """Get the last processed timestamp, timezone-aware UTC."""
#         return self.last_processed_time

#     def get_last_processed_ist_display(self) -> Optional[str]:
#         """Get the last processed timestamp formatted for display in IST,
#         e.g. '2026-09-15 12:00:00 IST'. Display-only - this value is never
#         used for storage or for the Graph API filter (see get_filter_string)."""
#         if not self.last_processed_time:
#             return None
#         ist_time = self.last_processed_time.astimezone(IST)
#         return ist_time.strftime('%Y-%m-%d %H:%M:%S IST')

#     def get_filter_string(self) -> Optional[str]:
#         """Get the filter string for Microsoft Graph API. The tracker
#         already stores UTC internally, so this is a direct format - no
#         timezone conversion needed here."""
#         if self.last_processed_time:
#             # Format for Microsoft Graph API: 2024-01-15T10:30:00Z
#             return self.last_processed_time.strftime('%Y-%m-%dT%H:%M:%SZ')
#         return None







from datetime import datetime, timedelta, timezone
import os
import json
# from typing import Optional
from typing import List, Optional, Set
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

    Note: The stored timestamp is incremented by 1 second when saved,
    to ensure the Graph 'gt' filter will always be strictly greater than
    any message we've seen (works around Graph's behavior where equal
    timestamps can still match a 'gt' filter).

    Display -> IST. IST is only ever produced for human-facing display (see
    get_last_processed_ist_display), via a proper zoneinfo conversion - never
    a manual +5:30/-5:30 offset.
    """

    # def __init__(self, timestamp_file: str = "last_processed.json"):
    #     self.timestamp_file = timestamp_file
    #     self.last_processed_time: Optional[datetime] = None  # always tz-aware UTC
    #     self._load_timestamp()

    def __init__(self, timestamp_file: str = "last_processed.json"):
        self.timestamp_file = timestamp_file
        self.last_processed_time: Optional[datetime] = None  # always tz-aware UTC (with 1 second increment)
        self.seen_ids_at_watermark: Set[str] = set()
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
        #                 else:
        #                     self.last_processed_time = parsed.astimezone(timezone.utc)
        #                 logger.info(
        #                     f"Loaded last processed timestamp (UTC): "
        #                     f"{self.last_processed_time.isoformat()}"
        #                 )
        # except Exception as e:
        #     logger.warning(f"Could not load timestamp file: {str(e)}")
        #     self.last_processed_time = None

                        else:
                            self.last_processed_time = parsed.astimezone(timezone.utc)
                        logger.info(
                            f"Loaded last processed timestamp (UTC): "
                            f"{self.last_processed_time.isoformat()}"
                        )
                        self.seen_ids_at_watermark = set(data.get('seen_ids_at_watermark', []))
        except Exception as e:
            logger.warning(f"Could not load timestamp file: {str(e)}")
            self.last_processed_time = None
            self.seen_ids_at_watermark = set()

    # def update_timestamp(self, timestamp: datetime):
    #     """Update the last processed timestamp.

    #     Accepts either a timezone-aware datetime (converted to UTC) or a
    #     naive datetime (assumed to already be UTC, e.g. from
    #     datetime.utcnow()) - always stored internally, and persisted to
    #     disk, as timezone-aware UTC. No manual timezone arithmetic here.
    #     """
    #     if timestamp.tzinfo is None:
    #         timestamp = timestamp.replace(tzinfo=timezone.utc)
    #     else:
    #         timestamp = timestamp.astimezone(timezone.utc)

    #     self.last_processed_time = timestamp
    #     try:
    #         with open(self.timestamp_file, 'w') as f:
    #             json.dump({
    #                 'last_processed': timestamp.isoformat(),
    #                 'updated_at': datetime.now(timezone.utc).isoformat()
    #             }, f, indent=2)
    #         logger.info(f"Updated last processed timestamp to (UTC): {timestamp.isoformat()}")
    #     except Exception as e:
    #         logger.error(f"Could not save timestamp file: {str(e)}")

    def update_timestamp(self, timestamp: datetime, message_ids: Optional[List[str]] = None):
        """Update the last processed timestamp, and the set of message IDs
        already handled at that exact timestamp.

        `message_ids`: the IDs of every message whose receivedDateTime
        equals exactly `timestamp` (almost always just one message - more
        than one only if two genuinely arrived at the identical instant).
        - If `timestamp` is a NEW point in time (later than what was
          already stored), the set is RESET to just `message_ids`: nothing
          earlier needs tracking by ID, since the Graph filter does
          reliably exclude anything clearly in the past - the imprecision
          this guards against only shows up exactly at the boundary.
        - If `timestamp` equals what was already stored (this run found no
          genuinely new moment in time - e.g. the same already-seen
          message matched the filter again), `message_ids` are MERGED into
          the existing set instead of replacing it, so nothing already
          tracked is forgotten.

        The stored timestamp is incremented by 1 second before saving.
        This ensures the Graph 'gt' filter will always be strictly greater
        than any message we've seen, working around Graph's behavior where
        equal timestamps can still match a 'gt' filter.

        Accepts either a timezone-aware datetime (converted to UTC) or a
        naive datetime (assumed to already be UTC, e.g. from
        datetime.utcnow()) - always stored internally, and persisted to
        disk, as timezone-aware UTC. No manual timezone arithmetic here.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)

        # Compare against the previous watermark minus 1 second (the actual message timestamp)
        previous_message_timestamp = None
        if self.last_processed_time is not None:
            previous_message_timestamp = self.last_processed_time - timedelta(seconds=1)

        if previous_message_timestamp is not None and timestamp == previous_message_timestamp:
            if message_ids:
                self.seen_ids_at_watermark.update(message_ids)
        else:
            self.seen_ids_at_watermark = set(message_ids or [])

        # Add 1 second to ensure the next filter is strictly greater than
        # this message's timestamp. This works around Microsoft Graph's behavior
        # where messages with equal timestamps can still match a 'gt' filter.
        self.last_processed_time = timestamp + timedelta(seconds=1)
        try:
            with open(self.timestamp_file, 'w') as f:
                json.dump({
                    'last_processed': self.last_processed_time.isoformat(),
                    'seen_ids_at_watermark': sorted(self.seen_ids_at_watermark),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }, f, indent=2)
            logger.info(
                f"Updated last processed timestamp to (UTC): {self.last_processed_time.isoformat()} "
                f"({len(self.seen_ids_at_watermark)} message ID(s) tracked at this timestamp)"
            )
        except Exception as e:
            logger.error(f"Could not save timestamp file: {str(e)}")

    def is_already_seen(self, message_id: Optional[str], received_dt: datetime) -> bool:
        """The authoritative "have I truly already handled this exact
        message" check - used IN ADDITION to the Graph `since` filter,
        never instead of it. Only ever True for a message whose timestamp
        exactly equals the current watermark (minus the 1 second increment)
        AND whose ID was recorded there; a message from any other point in time
        is never affected by this check at all.
        """
        if not message_id or self.last_processed_time is None:
            return False
        if received_dt.tzinfo is None:
            received_dt = received_dt.replace(tzinfo=timezone.utc)
        else:
            received_dt = received_dt.astimezone(timezone.utc)
        # Compare against watermark minus 1 second (the actual message timestamp)
        watermark_without_increment = self.last_processed_time - timedelta(seconds=1)
        return received_dt == watermark_without_increment and message_id in self.seen_ids_at_watermark



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
            # The stored timestamp is already incremented by 1 second to ensure
            # strict greater-than behavior with Graph's filter.
            iso = self.last_processed_time.isoformat()
            return iso[:-6] + "Z" if iso.endswith("+00:00") else iso
        return None
