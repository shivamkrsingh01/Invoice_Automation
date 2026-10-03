import time
import msal
import requests
from typing import List, Dict, Optional, Any
from app.config.settings import settings
from app.utils.logging_config import logger


class GraphService:
    """Microsoft Graph API service for email and attachment handling."""

    # Refresh the token this many seconds *before* it actually expires,
    # so we never fire off a request with a token that dies mid-flight.
    TOKEN_REFRESH_BUFFER_SECONDS = 300

    def __init__(self):
        self.client_id = settings.microsoft_client_id
        self.client_secret = settings.microsoft_client_secret
        self.tenant_id = settings.microsoft_tenant_id
        self.mailbox = settings.outlook_mailbox
        self.scopes = ["https://graph.microsoft.com/.default"]
        self.access_token: Optional[str] = None
        # Unix timestamp (seconds) after which the current access_token
        # should be treated as expired and refreshed before use.
        self.token_expires_at: float = 0

    def _get_access_token(self) -> str:
        """Get a fresh Microsoft Graph access token using client credentials flow,
        and record when it expires so callers know when to refresh it."""
        if not all([self.client_id, self.client_secret, self.tenant_id]):
            raise ValueError("Microsoft Graph credentials not configured")

        config = {
            "authority": f"https://login.microsoftonline.com/{self.tenant_id}",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scopes
        }

        app = msal.ConfidentialClientApplication(
            config["client_id"],
            authority=config["authority"],
            client_credential=config["client_secret"]
        )

        result = app.acquire_token_for_client(scopes=config["scope"])

        if "access_token" in result:
            self.access_token = result["access_token"]
            # Microsoft tokens typically last ~3600s; fall back to a
            # conservative 3600 if expires_in is ever missing.
            expires_in = result.get("expires_in", 3600)
            self.token_expires_at = time.time() + expires_in - self.TOKEN_REFRESH_BUFFER_SECONDS
            logger.info(f"Successfully obtained Microsoft Graph access token (expires in {expires_in}s)")
            return self.access_token
        else:
            error = result.get("error", "Unknown error")
            error_description = result.get("error_description", "No description")
            logger.error(f"Failed to get access token: {error} - {error_description}")
            raise Exception(f"Authentication failed: {error} - {error_description}")

    def _ensure_valid_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, transparently refreshing it if we
        don't have one yet or it's about to expire (or force_refresh is set,
        e.g. after a 401 tells us the token was rejected)."""
        if force_refresh or not self.access_token or time.time() >= self.token_expires_at:
            self._get_access_token()
        return self.access_token

    def _make_graph_request(self, endpoint: str, method: str = "GET", data: Optional[Dict] = None, params: Optional[Dict] = None) -> Any:
        """Make a request to Microsoft Graph API. Proactively refreshes the
        token when it's near expiry, and if Graph still rejects it with a
        401 (e.g. token revoked early, clock drift), fetches a brand new
        token and retries the request exactly once before giving up."""
        self._ensure_valid_token()

        url = f"https://graph.microsoft.com/v1.0{endpoint}"

        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }

            try:
                if method == "GET":
                    response = requests.get(url, headers=headers, params=params, timeout=30)
                elif method == "POST":
                    response = requests.post(url, headers=headers, json=data, params=params, timeout=30)
                elif method == "PATCH":
                    response = requests.patch(url, headers=headers, json=data, params=params, timeout=30)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                if response.status_code == 401 and attempt == 1:
                    logger.warning("Graph API returned 401 (expired/invalid token) - refreshing token and retrying once")
                    self._ensure_valid_token(force_refresh=True)
                    continue

                response.raise_for_status()

                if not response.content:
                    return {}
                return response.json()

            except requests.exceptions.RequestException as e:
                logger.error(f"Graph API request failed: {str(e)}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                raise

    def get_messages(self, limit: int = 10, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve messages from the mailbox, optionally filtering by date.

        Ordered OLDEST first (not newest first). This matters when there are
        more matching messages than `limit`: with an ascending order, a
        capped batch is guaranteed to be the oldest still-unprocessed
        messages, so InvoiceProcessor can safely advance its "last
        processed" watermark to just past this batch without ever skipping
        an older message that didn't fit in this page - see
        InvoiceProcessor.process_inbox for the watermark logic this
        ordering is required by.
        """
        if not self.mailbox:
            raise ValueError("Outlook mailbox not configured")

        logger.info(f"Fetching messages from mailbox: {self.mailbox}")

        endpoint = f"/users/{self.mailbox}/mailFolders/Inbox/messages"
        params = {
            "$top": limit,
            "$orderby": "receivedDateTime asc",
            "$select": "id,subject,from,receivedDateTime,hasAttachments,body"
        }

        # Add date filter if provided
        if since:
            params["$filter"] = f"receivedDateTime ge {since}"
            logger.info(f"Filtering messages received since: {since}")

        try:
            response = self._make_graph_request(endpoint, params=params)
            messages = response.get("value", [])
            logger.info(f"Retrieved {len(messages)} messages")
            return messages
        except Exception as e:
            logger.error(f"Failed to retrieve messages: {str(e)}")
            raise

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """Get list of attachments for a specific message."""
        logger.info(f"Fetching attachments for message: {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}/attachments"
        params = {
            "$select": "id,name,contentType,size,isInline"
        }

        try:
            response = self._make_graph_request(endpoint, params=params)
            attachments = response.get("value", [])

            # Filter out inline attachments (like embedded images in email body)
            attachments = [att for att in attachments if not att.get("isInline", False)]

            logger.info(f"Found {len(attachments)} attachments (excluding inline)")
            return attachments
        except Exception as e:
            logger.error(f"Failed to retrieve attachments: {str(e)}")
            raise

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download attachment content."""
        logger.info(f"Downloading attachment {attachment_id} from message {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"
        url = f"https://graph.microsoft.com/v1.0{endpoint}"

        self._ensure_valid_token()

        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self.access_token}"
            }

            try:
                response = requests.get(url, headers=headers, timeout=60)

                if response.status_code == 401 and attempt == 1:
                    logger.warning("Graph API returned 401 while downloading attachment - refreshing token and retrying once")
                    self._ensure_valid_token(force_refresh=True)
                    continue

                response.raise_for_status()

                logger.info(f"Successfully downloaded attachment (size: {len(response.content)} bytes)")
                return response.content

            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to download attachment: {str(e)}")
                raise

    def get_message_details(self, message_id: str) -> Dict[str, Any]:
        """Get detailed information about a specific message."""
        logger.info(f"Fetching details for message: {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}"
        params = {
            "$select": "id,subject,from,toRecipients,receivedDateTime,hasAttachments,body"
        }

        try:
            response = self._make_graph_request(endpoint, params=params)
            return response
        except Exception as e:
            logger.error(f"Failed to retrieve message details: {str(e)}")
            raise

    def get_message_body(self, message_id: str) -> Optional[str]:
        """Get the text body content of a message."""
        logger.info(f"Fetching body content for message: {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}"
        params = {
            "$select": "body"
        }

        try:
            response = self._make_graph_request(endpoint, params=params)
            body_content = response.get("body", {})
            # Get HTML content and convert to text
            html_content = body_content.get("content", "")

            if html_content:
                # Simple HTML to text conversion
                from html import unescape
                import re

                # Remove HTML tags
                text_content = re.sub(r'<[^>]+>', ' ', html_content)
                # Decode HTML entities
                text_content = unescape(text_content)
                # Clean up whitespace
                text_content = ' '.join(text_content.split())

                logger.info(f"Extracted body content (length: {len(text_content)} chars)")
                return text_content

            return None
        except Exception as e:
            logger.error(f"Failed to retrieve message body: {str(e)}")
            return None

    def get_message_html(self, message_id: str) -> Optional[str]:
        """Get the RAW HTML body content of a message (tags intact).
        Unlike get_message_body(), this preserves <table> structure so a
        table listing multiple invoices in the email body can be parsed row
        by row instead of being flattened into one blob of text."""
        logger.info(f"Fetching raw HTML body for message: {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}"
        params = {
            "$select": "body"
        }

        try:
            response = self._make_graph_request(endpoint, params=params)
            body_content = response.get("body", {})
            content_type = body_content.get("contentType", "")
            html_content = body_content.get("content", "")

            if not html_content:
                return None

            # If the body is plain text (not HTML), wrap it so downstream
            # HTML parsing (BeautifulSoup) still works and just finds no tables.
            if content_type.lower() != "html":
                from html import escape
                html_content = f"<div>{escape(html_content)}</div>"

            return html_content
        except Exception as e:
            logger.error(f"Failed to retrieve message HTML body: {str(e)}")
            return None

    def mark_as_read(self, message_id: str) -> bool:
        """Mark a message as read (isRead = true) once it's been processed,
        so a processed email doesn't keep showing as unread in the mailbox.
        Returns True on success, False on failure - failure here shouldn't
        be treated as a processing failure by the caller (the invoice data
        has already been extracted/stored either way), so this deliberately
        doesn't raise."""
        logger.info(f"Marking message as read: {message_id}")

        endpoint = f"/users/{self.mailbox}/messages/{message_id}"

        try:
            self._make_graph_request(endpoint, method="PATCH", data={"isRead": True})
            logger.info(f"Marked message as read: {message_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to mark message as read ({message_id}): {str(e)}")
            return False