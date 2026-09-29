from dataclasses import dataclass
from typing import Protocol

from webhook_delivery.models import EventEnvelope


@dataclass(frozen=True)
class SendResult:
    status_code: int


class TransportError(Exception):
    """An expected network failure prevented obtaining an HTTP response."""


class Sender(Protocol):
    def send(self, url: str, event: EventEnvelope) -> SendResult:
        """Return an HTTP result or raise TransportError."""