# webhook delivery

a python webhook delivery engine built to practice queueing, delivery tracking, and failure handling.

## implemented

- endpoint registration with basic url validation
- event submission with json payload validation and copying
- synchronous delivery processing in submission order
- delivery status tracking: pending, succeeded, or failed
- an injectable sender for testing without network requests
- tests for delivery behavior, validation, and failure handling

## running tests

python -m pytest

## current limitations

- in-memory only; data is lost when the process exits
- single-threaded, with manually triggered processing
- no real http sender, retries, or background workers
- delivery records remain in memory without a retention limit

## possible next steps

- http delivery with request timeouts
- retries with backoff and failed-delivery inspection
- bounded concurrency and graceful shutdown
- persistence and crash recovery
