from dataclasses import dataclass
from enum import Enum
from typing import Any


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class Endpoint:
    id: str
    url: str


@dataclass(frozen=True)
class EventEnvelope:
    id: str
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class DeliverySnapshot:
    delivery_id: str
    event_id: str
    endpoint_id: str
    event_type: str
    status: DeliveryStatus
    attempt_count: int
    last_status_code: int | None
    last_error: str | None