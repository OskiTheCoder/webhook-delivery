import uuid
import validators
from typing import Any, List

from webhook_delivery.models import DeliverySnapshot
from webhook_delivery.sender import Sender


class EndpointNotFoundError(KeyError):
    """The requested endpoint does not exist."""


class DeliveryNotFoundError(KeyError):
    """The requested delivery does not exist."""


class DeliveryEngine:
    """In-memory, single-threaded delivery engine.

    Processing is synchronous and explicitly initiated by the caller.
    Expected transport failures are recorded; unexpected exceptions propagate.
    """

    def __init__(self, sender: Sender) -> None:
        self._sender = sender
        self._endpoints: dict[str, List[str]] = {}

    def register_endpoint(self, url: str) -> str:
        """Register an endpoint and return its unique ID.

        Require an absolute HTTP(S) URL with a hostname.
        Reject credentials and fragments.
        Duplicate URLs create separate registrations.

        Raises:
            ValueError: If the URL is invalid.
        """
        if not self.is_valid_webhook_url(url):
            raise ValueError("invalid url")
        if url not in self._endpoints:
            self._endpoints[url] = []
        
        endpoint_id = str(uuid.uuid4())
        self._endpoints[url].append(endpoint_id)
        return endpoint_id
        

    def submit_event(
        self,
        endpoint_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Capture an event and queue one delivery; return its delivery ID.

        Require a nonblank event type and a JSON-compatible dictionary.
        Reject nonfinite floats and non-string dictionary keys.
        Isolate the captured payload from subsequent caller mutations.
        Each submission creates a new event ID and delivery ID.

        Raises:
            EndpointNotFoundError: If the endpoint is unknown.
            ValueError: If the event type or payload is invalid.
        """
        raise NotImplementedError

    def get_delivery(self, delivery_id: str) -> DeliverySnapshot:
        """Return a detached snapshot of the delivery.

        Raises:
            DeliveryNotFoundError: If the delivery is unknown.
        """
        raise NotImplementedError

    def process_next(self) -> DeliverySnapshot | None:
        """Attempt the oldest pending delivery, or return None if empty.

        Call the sender with an isolated event envelope.
        Count one attempt.
        Mark HTTP 2xx as succeeded.
        Mark other HTTP responses or TransportError as failed.
        Record the status code or error as appropriate.
        Return the updated delivery snapshot.

        No retries in this milestone. Unexpected exceptions propagate.
        """
        raise NotImplementedError

    def is_valid_webhook_url(value: str) -> bool:
        return validators.url(
            value,
            validate_scheme=lambda scheme: scheme.lower() in {"http", "https"},
            simple_host=True,
        ) is True