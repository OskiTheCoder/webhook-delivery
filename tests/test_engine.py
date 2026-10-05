from copy import deepcopy

import pytest

from webhook_delivery.engine import (
    DeliveryEngine,
    DeliveryNotFoundError,
    EndpointNotFoundError,
)
from webhook_delivery.models import DeliveryStatus, EventEnvelope
from webhook_delivery.sender import SendResult, TransportError


class FakeSender:
    def __init__(self) -> None:
        self.outcome: SendResult | TransportError = SendResult(200)
        self.calls: list[tuple[str, EventEnvelope]] = []

    def send(self, url: str, event: EventEnvelope) -> SendResult:
        self.calls.append((url, deepcopy(event)))

        if isinstance(self.outcome, TransportError):
            raise self.outcome

        return self.outcome


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def engine(sender):
    return DeliveryEngine(sender)


@pytest.fixture
def endpoint_id(engine):
    return engine.register_endpoint("https://example.com/webhook")


def test_submission_and_success(engine, sender, endpoint_id):
    delivery_id = engine.submit_event(
        endpoint_id,
        "order.created",
        {"order_id": "123"},
    )
    before = engine.get_delivery(delivery_id)

    assert before.status == DeliveryStatus.PENDING
    assert before.attempt_count == 0
    assert before.last_status_code is None
    assert before.last_error is None
    assert sender.calls == []

    after = engine.process_next()

    assert after.delivery_id == delivery_id
    assert after.endpoint_id == endpoint_id
    assert after.event_type == "order.created"
    assert after.status == DeliveryStatus.SUCCEEDED
    assert after.attempt_count == 1
    assert after.last_status_code == 200
    assert after.last_error is None
    assert engine.get_delivery(delivery_id) == after

    # Previously returned snapshots do not change.
    assert before.status == DeliveryStatus.PENDING
    assert before.attempt_count == 0

    url, event = sender.calls[0]
    assert url == "https://example.com/webhook"
    assert event.id == before.event_id == after.event_id
    assert event.type == "order.created"
    assert event.data == {"order_id": "123"}

    # Completed work is not sent again.
    assert engine.process_next() is None
    assert len(sender.calls) == 1


@pytest.mark.parametrize(
    "outcome, expected_code, expected_error",
    [
        (SendResult(302), 302, "HTTP 302"),
        (SendResult(500), 500, "HTTP 500"),
        (TransportError("timeout"), None, "timeout"),
    ],
)
def test_failure_does_not_block_next_delivery(
    engine, sender, endpoint_id, outcome, expected_code, expected_error
):
    first_id = engine.submit_event(endpoint_id, "first", {})
    second_id = engine.submit_event(endpoint_id, "second", {})

    sender.outcome = outcome
    first = engine.process_next()

    assert first.delivery_id == first_id
    assert first.status == DeliveryStatus.FAILED
    assert first.attempt_count == 1
    assert first.last_status_code == expected_code
    assert first.last_error == expected_error
    assert engine.get_delivery(first_id) == first

    sender.outcome = SendResult(204)
    second = engine.process_next()

    assert second.delivery_id == second_id
    assert second.status == DeliveryStatus.SUCCEEDED
    assert second.last_status_code == 204
    assert engine.process_next() is None
    assert len(sender.calls) == 2


def test_fifo_and_endpoint_routing(engine, sender):
    first_endpoint = engine.register_endpoint("https://first.example/hook")
    second_endpoint = engine.register_endpoint("http://localhost:8000/hook")

    first_id = engine.submit_event(second_endpoint, "first", {})
    second_id = engine.submit_event(first_endpoint, "second", {})

    assert engine.process_next().delivery_id == first_id
    assert engine.process_next().delivery_id == second_id
    assert [url for url, _ in sender.calls] == [
        "http://localhost:8000/hook",
        "https://first.example/hook",
    ]


def test_payload_is_captured_at_submission(engine, sender, endpoint_id):
    payload = {"items": [{"name": "book"}]}
    engine.submit_event(endpoint_id, "order.created", payload)

    payload["items"][0]["name"] = "changed"
    payload["items"].append({"name": "laptop"})

    engine.process_next()

    assert sender.calls[0][1].data == {"items": [{"name": "book"}]}


def test_duplicate_inputs_create_distinct_records(engine):
    first_endpoint = engine.register_endpoint("https://example.com/hook")
    second_endpoint = engine.register_endpoint("https://example.com/hook")
    assert first_endpoint != second_endpoint

    first_id = engine.submit_event(first_endpoint, "event", {})
    second_id = engine.submit_event(first_endpoint, "event", {})

    assert first_id != second_id
    assert (
        engine.get_delivery(first_id).event_id
        != engine.get_delivery(second_id).event_id
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/hook",
        "https:///hook",
        "ftp://example.com/hook",
        "https://user:password@example.com/hook",
        "https://example.com/hook#fragment",
        "https://example.com:invalid/hook",
        "https://exa mple.com/hook",
    ],
)
def test_invalid_urls(engine, url):
    with pytest.raises(ValueError):
        engine.register_endpoint(url)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"value": {1, 2}},
        {"value": float("nan")},
        {"value": float("inf")},
        {"nested": {123: "invalid key"}},
        {"value": (1, 2)},
    ],
)
def test_invalid_payloads_do_not_queue_work(engine, endpoint_id, payload):
    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, "event", payload)

    assert engine.process_next() is None


def test_circular_payload_is_rejected(engine, endpoint_id):
    payload = {}
    payload["self"] = payload

    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, "event", payload)

    assert engine.process_next() is None


def test_unknown_ids_and_blank_event_type(engine, endpoint_id):
    with pytest.raises(EndpointNotFoundError):
        engine.submit_event("missing", "event", {})

    with pytest.raises(DeliveryNotFoundError):
        engine.get_delivery("missing")

    with pytest.raises(ValueError):
        engine.submit_event(endpoint_id, "   ", {})

    assert engine.process_next() is None


def test_unexpected_sender_error_propagates_and_leaves_work_pending():
    class BrokenSender:
        def send(self, url, event):
            raise RuntimeError("sender bug")

    engine = DeliveryEngine(BrokenSender())
    endpoint_id = engine.register_endpoint("https://example.com/hook")
    delivery_id = engine.submit_event(endpoint_id, "event", {})

    with pytest.raises(RuntimeError, match="sender bug"):
        engine.process_next()

    assert engine.get_delivery(delivery_id).status == DeliveryStatus.PENDING