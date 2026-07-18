from concurrent.futures import ThreadPoolExecutor

from negotiator.core import EventBus, Journal
from negotiator.core.contracts import BusEvent


def event(index: int) -> BusEvent:
    return BusEvent(call_id="call-1", module="test", kind="tick", payload={"index": index})


def test_every_published_event_is_journaled(tmp_path):
    bus = EventBus()
    journal = Journal(tmp_path / "call.jsonl")
    detach = journal.attach(bus)
    bus.publish(event(1))
    detach()
    bus.publish(event(2))
    assert [row.payload["index"] for row in journal.replay()] == [1]


def test_sequence_is_monotonic_under_threads_and_restart(tmp_path):
    path = tmp_path / "call.jsonl"
    journal = Journal(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(journal.append, (event(i) for i in range(50))))
    assert [row.seq for row in journal.replay()] == list(range(1, 51))
    assert Journal(path).append(event(50)).seq == 51


def test_bus_kind_and_global_subscriptions():
    bus = EventBus()
    seen = []
    bus.subscribe("tick", lambda value: seen.append(("kind", value.kind)))
    bus.subscribe_all(lambda value: seen.append(("all", value.kind)))
    bus.publish(event(1))
    assert seen == [("all", "tick"), ("kind", "tick")]


def test_failing_subscriber_cannot_prevent_global_journal(tmp_path):
    bus = EventBus()
    bus.subscribe_all(lambda _: (_ for _ in ()).throw(RuntimeError("subscriber failed")))
    journal = Journal(tmp_path / "call.jsonl")
    journal.attach(bus)
    try:
        bus.publish(event(1))
    except RuntimeError:
        pass
    assert len(journal.replay()) == 1
