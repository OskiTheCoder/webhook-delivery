import json
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from webhook_delivery.models import (
    DeliverySnapshot,
    DeliveryStatus,
    Endpoint,
    EventEnvelope,
)
from webhook_delivery.sender import Sender, TransportError


class EndpointNotFoundError(KeyError):
    """The requested endpoint does not exist."""


class DeliveryNotFoundError(KeyError):
    """The requested delivery does not exist."""


@dataclass
class _Delivery:
    event: EventEnvelope
    snapshot: DeliverySnapshot


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise ValueError("url must be a nonempty string")

    if any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("url must not contain whitespace or control characters")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing port also validates its format and range.
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid url") from exc

    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("url must be absolute HTTP(S) with a hostname")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain credentials")

    if "#" in url:
        raise ValueError("url must not contain a fragment")


def _check_json_types(value: Any) -> None:
    """Require JSON types and string keys, including nested values."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("payload dictionary keys must be strings")
            _check_json_types(item)
    elif isinstance(value, list):
        for item in value:
            _check_json_types(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("payload contains an unsupported JSON type")


def _capture_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")

    try:
        # Reject circular references, unsupported values, and nonfinite floats.
        serialized = json.dumps(payload, allow_nan=False)

        # json.dumps permits non-string keys and tuples; our contract does not.
        _check_json_types(payload)

        # The round trip also creates an independent nested payload.
        return json.loads(serialized)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(
            "payload must contain valid JSON types and string keys"
        ) from exc


class DeliveryEngine:
    """In-memory, single-threaded engine with explicit synchronous processing.

    Each delivery receives one attempt. Expected failures are recorded.
    Completed records remain in memory for the lifetime of the engine.
    """

    def __init__(self, sender: Sender) -> None:
        self._sender = sender
        self._endpoints: dict[str, Endpoint] = {}
        self._deliveries: dict[str, _Delivery] = {}
        self._pending: deque[str] = deque()

    def register_endpoint(self, url: str) -> str:
        _validate_url(url)

        endpoint_id = str(uuid4())
        self._endpoints[endpoint_id] = Endpoint(id=endpoint_id, url=url)
        return endpoint_id

    def submit_event(
        self,
        endpoint_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        if endpoint_id not in self._endpoints:
            raise EndpointNotFoundError(endpoint_id)

        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a nonblank string")

        # Validate and copy before modifying engine state.
        captured_payload = _capture_payload(payload)

        event = EventEnvelope(
            id=str(uuid4()),
            type=event_type,
            data=captured_payload,
        )
        delivery_id = str(uuid4())
        snapshot = DeliverySnapshot(
            delivery_id=delivery_id,
            event_id=event.id,
            endpoint_id=endpoint_id,
            event_type=event_type,
            status=DeliveryStatus.PENDING,
        )

        self._deliveries[delivery_id] = _Delivery(
            event=event,
            snapshot=snapshot,
        )
        self._pending.append(delivery_id)
        return delivery_id

    def get_delivery(self, delivery_id: str) -> DeliverySnapshot:
        try:
            delivery = self._deliveries[delivery_id]
        except KeyError:
            raise DeliveryNotFoundError(delivery_id) from None

        # All snapshot fields are immutable; a shallow dataclass copy suffices.
        return replace(delivery.snapshot)

    def process_next(self) -> DeliverySnapshot | None:
        if not self._pending:
            return None

        delivery_id = self._pending[0]
        delivery = self._deliveries[delivery_id]
        endpoint = self._endpoints[delivery.snapshot.endpoint_id]

        status_code = None
        error = None

        try:
            # frozen=True does not protect nested data, so isolate the payload.
            result = self._sender.send(
                endpoint.url,
                deepcopy(delivery.event),
            )
            status_code = result.status_code
            if 200 <= status_code < 300:
                status = DeliveryStatus.SUCCEEDED
            else:
                status = DeliveryStatus.FAILED
                error = f"HTTP {status_code}"
        except TransportError as exc:
            status = DeliveryStatus.FAILED
            error = str(exc) or "transport failure"

        # Unexpected exceptions propagate before this point, leaving the
        # delivery pending. There is no automatic retry for programming errors.
        delivery.snapshot = replace(
            delivery.snapshot,
            status=status,
            attempt_count=delivery.snapshot.attempt_count + 1,
            last_status_code=status_code,
            last_error=error,
        )
        self._pending.popleft()

        return self.get_delivery(delivery_id)