import uuid
import math
import validators
from collections import deque
from typing import Any, List

from webhook_delivery.models import DeliverySnapshot, DeliveryStatus
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
        self._endpoints_mapping: dict[str, List[str]] = {}
        self._delivery_mapping: dict[str, DeliverySnapshot] = {}
        self._endpoints: set[str] = set()
        self._events: deque[DeliverySnapshot] = deque()

    def register_endpoint(self, url: str) -> str:
        """Register an endpoint and return its unique ID.

        Require an absolute HTTP(S) URL with a hostname.
        Reject credentials and fragments.
        Duplicate URLs create separate registrations.

        Raises:
            ValueError: If the URL is invalid.
        """
        if not self._is_valid_webhook_url(url):
            raise ValueError("invalid url")
        if url not in self._endpoints_mapping:
            self._endpoints[url] = []
        
        endpoint_id = str(uuid.uuid4())
        self._endpoints_mapping[url].append(endpoint_id)
        self._endpoints.add(endpoint_id)
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
        if endpoint_id not in self._endpoints:
            raise EndpointNotFoundError()
        if not event_type:
            raise ValueError(f"event type {event_type} is invalid")
        if not self._check_strict_json(payload):
            raise ValueError("invalid payload. payload should be a JSON-compatible dictionary")
        
        event_id = str(uuid.uuid4())
        delivery_id = str(uuid.uuid4())
        delivery = DeliverySnapshot(
            delivery_id=delivery_id,
            event_id=event_id,
            endpoint_id=endpoint_id,
            event_type=event_type,
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            last_status_code=None,
            last_error=None
        )
        self._events.append(delivery)
        self._delivery_mapping[delivery_id] = delivery
        return delivery_id

    def get_delivery(self, delivery_id: str) -> DeliverySnapshot:
        """Return a detached snapshot of the delivery.

        Raises:
            DeliveryNotFoundError: If the delivery is unknown.
        """
        if not delivery_id or delivery_id not in self._delivery_mapping:
            raise DeliveryNotFoundError()
        return self._delivery_mapping[delivery_id]

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

    def _is_valid_webhook_url(self, value: str) -> bool:
        return validators.url(
            value,
            validate_scheme=lambda scheme: scheme.lower() in {"http", "https"},
            simple_host=True,
        ) is True

    
    def _check_strict_json(self, obj) -> bool:
        # 1. Handle Dictionaries
        if isinstance(obj, dict):
            for key, value in obj.items():
                if not isinstance(key, str):  # Reject non-string keys
                    return False
                if not self._check_strict_json(value):  # Recursively check values
                    return False
            return True

        # 2. Handle Lists/Arrays
        if isinstance(obj, list):
            return all(self._check_strict_json(item) for item in obj)

        # 3. Handle Floats (Reject NaN and Inf)
        if isinstance(obj, float):
            return math.isfinite(obj)

        # 4. Handle other valid JSON primitives
        if obj is None or isinstance(obj, (int, bool, str)):
            return True

        # Reject custom objects, sets, tuples, etc.
        return False