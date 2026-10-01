from copy import deepcopy

import pytest

from webhook_delivery.engine import (
    DeliveryEngine,
    DeliveryNotFoundError,
    EndpointNotFoundError,
)
from webhook_delivery.models import (
    DeliverySnapshot,
    DeliveryStatus,
    EventEnvelope,
)
from webhook_delivery.sender import SendResult, TransportError


class FakeSender:
    """Records calls and returns a configurable result without using HTTP."""

    def __init__(self) -> None:
        self.outcome: SendResult | TransportError = SendResult(200)
        self.calls: list[tuple[str, EventEnvelope]] = []

    def send(self, url: str, event: EventEnvelope) -> SendResult:
        # Capture what was sent, independently of later mutations.
        self.calls.append((url, deepcopy(event)))

        if isinstance(self.outcome, TransportError):
            raise self.outcome

        return self.outcome


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def engine(sender: FakeSender) -> DeliveryEngine:
    return DeliveryEngine(sender)


@pytest.fixture
def endpoint_id(engine: DeliveryEngine) -> str:
    return engine.register_endpoint("https://example.com/webhook")


def test_submission_creates_pending_delivery_without_sending(
    engine, sender, endpoint_id
):

    payload = {"order_id": "order-123"}


    delivery_id = engine.submit_event(
        endpoint_id, "order.created", payload
    )
    delivery = engine.get_delivery(delivery_id)


    assert isinstance(delivery, DeliverySnapshot)
    assert delivery.delivery_id == delivery_id
    assert isinstance(delivery.event_id, str)
    assert delivery.event_id
    assert delivery.endpoint_id == endpoint_id
    assert delivery.event_type == "order.created"
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert delivery.last_status_code is None
    assert delivery.last_error is None
    assert sender.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/webhook",
        "http://localhost:8000/webhook",
        "https://example.com/webhook?source=orders",
    ],
)
def test_registration_accepts_valid_urls(engine, url):
    endpoint_id = engine.register_endpoint(url)

    assert isinstance(endpoint_id, str)
    assert endpoint_id


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/webhook",
        "/webhook",
        "ftp://example.com/webhook",
        "https:///webhook",
        "https://user:password@example.com/webhook",
        "https://example.com/webhook#section",
    ],
)
def test_registration_rejects_invalid_urls(engine, url):
    with pytest.raises(ValueError):
        engine.register_endpoint(url)


def test_duplicate_urls_create_separate_registrations(engine):
    first = engine.register_endpoint("https://example.com/webhook")
    second = engine.register_endpoint("https://example.com/webhook")

    assert first != second


def test_submission_rejects_unknown_endpoint(engine, sender):
    with pytest.raises(EndpointNotFoundError):
        engine.submit_event("missing-endpoint", "order.created", {})

    assert engine.process_next() is None
    assert sender.calls == []


@pytest.mark.parametrize("event_type", ["", "   ", "\t\n"])
def test_submission_rejects_blank_event_type(
    engine, endpoint_id, event_type
):
    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, event_type, {})

    assert engine.process_next() is None


@pytest.mark.parametrize(
    "payload",
    [
        [],                            # Top-level value must be a dict.
        {"tags": {"a", "b"}},           # Sets are not JSON values.
        {"value": object()},           # Arbitrary objects are unsupported.
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
        {123: "non-string key"},
        {"nested": [{123: "non-string key"}]},
    ],
)
def test_submission_rejects_invalid_payload(
    engine, endpoint_id, payload
):
    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, "order.created", payload)

    assert engine.process_next() is None


def test_submission_rejects_circular_payload(engine, endpoint_id):
    payload = {}
    payload["self"] = payload

    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, "order.created", payload)

    assert engine.process_next() is None


def test_lookup_rejects_unknown_delivery(engine):
    with pytest.raises(DeliveryNotFoundError):
        engine.get_delivery("missing-delivery")


def test_identical_submissions_create_distinct_events_and_deliveries(
    engine, endpoint_id
):
    first_id = engine.submit_event(endpoint_id, "order.created", {})
    second_id = engine.submit_event(endpoint_id, "order.created", {})

    first = engine.get_delivery(first_id)
    second = engine.get_delivery(second_id)

    assert first_id != second_id
    assert first.event_id != second.event_id


@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
def test_processing_records_success(
    engine, sender, endpoint_id, status_code
):
    sender.outcome = SendResult(status_code)
    delivery_id = engine.submit_event(
        endpoint_id, "order.created", {"order_id": "order-123"}
    )
    original = engine.get_delivery(delivery_id)

    result = engine.process_next()

    assert result is not None
    assert result.delivery_id == delivery_id
    assert result.event_id == original.event_id
    assert result.status == DeliveryStatus.SUCCEEDED
    assert result.attempt_count == 1
    assert result.last_status_code == status_code
    assert result.last_error is None
    assert engine.get_delivery(delivery_id) == result

    assert len(sender.calls) == 1
    url, event = sender.calls[0]
    assert url == "https://example.com/webhook"
    assert event.id == original.event_id
    assert event.type == "order.created"
    assert event.data == {"order_id": "order-123"}


@pytest.mark.parametrize("status_code", [199, 300, 400, 429, 500])
def test_processing_records_unsuccessful_http_response(
    engine, sender, endpoint_id, status_code
):
    sender.outcome = SendResult(status_code)
    delivery_id = engine.submit_event(endpoint_id, "order.created", {})

    result = engine.process_next()

    assert result is not None
    assert result.delivery_id == delivery_id
    assert result.status == DeliveryStatus.FAILED
    assert result.attempt_count == 1
    assert result.last_status_code == status_code
    assert isinstance(result.last_error, str)
    assert result.last_error
    assert engine.get_delivery(delivery_id) == result


def test_processing_records_transport_failure(engine, sender, endpoint_id):
    sender.outcome = TransportError("connection timed out")
    delivery_id = engine.submit_event(endpoint_id, "order.created", {})

    result = engine.process_next()

    assert result is not None
    assert result.delivery_id == delivery_id
    assert result.status == DeliveryStatus.FAILED
    assert result.attempt_count == 1
    assert result.last_status_code is None
    assert "connection timed out" in result.last_error
    assert engine.get_delivery(delivery_id) == result


def test_processing_empty_queue_returns_none(engine, sender):
    assert engine.process_next() is None
    assert sender.calls == []


def test_processing_uses_submission_order_and_correct_endpoints(
    engine, sender
):
    first_endpoint = engine.register_endpoint("https://first.example/hook")
    second_endpoint = engine.register_endpoint("https://second.example/hook")

    submitted_ids = [
        engine.submit_event(second_endpoint, "event.one", {}),
        engine.submit_event(first_endpoint, "event.two", {}),
        engine.submit_event(second_endpoint, "event.three", {}),
    ]

    results = [engine.process_next() for _ in submitted_ids]

    assert [result.delivery_id for result in results] == submitted_ids
    assert [url for url, _ in sender.calls] == [
        "https://second.example/hook",
        "https://first.example/hook",
        "https://second.example/hook",
    ]
    assert engine.process_next() is None


@pytest.mark.parametrize(
    "failure",
    [SendResult(503), TransportError("connection refused")],
)
def test_expected_failure_does_not_block_next_delivery(
    engine, sender, endpoint_id, failure
):
    first_id = engine.submit_event(endpoint_id, "event.one", {})
    second_id = engine.submit_event(endpoint_id, "event.two", {})

    sender.outcome = failure
    first = engine.process_next()

    sender.outcome = SendResult(200)
    second = engine.process_next()

    assert first.delivery_id == first_id
    assert first.status == DeliveryStatus.FAILED
    assert second.delivery_id == second_id
    assert second.status == DeliveryStatus.SUCCEEDED


@pytest.mark.parametrize(
    "outcome",
    [SendResult(200), SendResult(500), TransportError("timeout")],
)
def test_processed_delivery_is_not_sent_again(
    engine, sender, endpoint_id, outcome
):
    sender.outcome = outcome
    engine.submit_event(endpoint_id, "order.created", {})

    engine.process_next()

    assert engine.process_next() is None
    assert engine.process_next() is None
    assert len(sender.calls) == 1


def test_submission_captures_nested_payload(engine, sender, endpoint_id):
    payload = {"order": {"items": ["book"]}}
    engine.submit_event(endpoint_id, "order.created", payload)

    # Changing the caller's object must not change the queued event.
    payload["order"]["items"].append("laptop")

    engine.process_next()

    _, event = sender.calls[0]
    assert event.data == {"order": {"items": ["book"]}}


def test_snapshot_does_not_change_when_delivery_is_processed(
    engine, endpoint_id
):
    delivery_id = engine.submit_event(endpoint_id, "order.created", {})
    before = engine.get_delivery(delivery_id)

    engine.process_next()
    after = engine.get_delivery(delivery_id)

    assert before.status == DeliveryStatus.PENDING
    assert before.attempt_count == 0
    assert after.status == DeliveryStatus.SUCCEEDED
    assert after.attempt_count == 1


def test_unexpected_sender_exception_propagates():
    class BrokenSender:
        def send(self, url, event):
            raise RuntimeError("unexpected sender bug")

    engine = DeliveryEngine(BrokenSender())
    endpoint_id = engine.register_endpoint("https://example.com/webhook")
    engine.submit_event(endpoint_id, "order.created", {})

    with pytest.raises(RuntimeError, match="unexpected sender bug"):
        engine.process_next()