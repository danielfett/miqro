"""Test doubles and assertion helpers for driving MIQRO services in tests.

A service under test is never actually connected to a broker and never runs its
own thread.  Instead:

  * :class:`DummyMQTTClient` records everything the service publishes and lets
    tests inject incoming messages,
  * :func:`send` delivers a message to the service the way paho would,
  * :func:`run` advances the service's loops for a wall-clock duration,
  * :func:`expect_next` advances the loops until the published messages satisfy
    a set of expectations (or the window expires).

Timing is real wall-clock time, so tests that exercise loop intervals work
against the same scheduler the service uses in production.
"""

from __future__ import annotations

from copy import deepcopy
from time import monotonic
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

__all__ = [
    "DummyMQTTClient",
    "DummyMessage",
    "DummyPublishInfo",
    "DummyState",
    "ReadOnlyDummyState",
    "eventually",
    "expect_next",
    "reset_saved_state",
    "run",
    "send",
    "DEFAULT_TIMEOUT",
]


class eventually(str):
    """Marks an expectation as "skip until this matches".

    :func:`expect_next` is strict by default: the *next* message on a topic must
    match, which is what makes a sequence of expectations meaningful.  Wrap an
    expression in ``eventually(...)`` where the number of messages before the
    interesting one is a function of wall-clock timing rather than of the logic
    under test -- for example a repeating output that keeps firing until a timer
    elsewhere expires.
    """

    __slots__ = ()


# Generous enough to cover the shortest state transitions the alarm tests rely
# on (a 1s reset delay); tests that assert a topic stays *silent* pass their own
# shorter window explicitly.
DEFAULT_TIMEOUT = 1.5

# Stands in for the on-disk state file.  ``DummyState`` loads from it, both
# state doubles commit into it on ``save()``.
SAVED_STATE: Dict[str, Any] = {}


def reset_saved_state() -> None:
    """Forget anything previous tests persisted."""
    SAVED_STATE.clear()


class DummyMessage:
    """Minimal stand-in for ``paho.mqtt.client.MQTTMessage``."""

    def __init__(self, topic: str, payload: Union[str, bytes], retain: bool = False):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload
        self.retain = retain
        self.qos = 0
        self.mid = 0


class DummyPublishInfo:
    """Stand-in for ``MQTTMessageInfo``; always reports success."""

    rc = 0
    mid = 1

    def is_published(self) -> bool:
        return True

    def wait_for_publish(self, timeout: Optional[float] = None) -> bool:
        return True


class _DummyThread:
    """Stands in for paho's network thread so liveness checks see it running."""

    def is_alive(self) -> bool:
        return True


class DummyMQTTClient:
    """Records published messages, replays injected ones.

    The connection is *not* established during ``connect_async``: at that point
    the service is still half-built and has no handler lists yet, exactly as
    with a real broker where the CONNACK arrives later.  Call
    :meth:`ensure_connected` (``run`` and ``expect_next`` do this for you) to
    fire the service's connect callback.
    """

    def __init__(self, client_id: Optional[str] = None, *args: Any, **kwargs: Any):
        self.client_id = client_id
        self.message_queue: List[Tuple[str, str]] = []
        self.subscribed: List[str] = []
        self.connected = False
        self.will: Optional[Tuple[str, Any, bool]] = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self._thread: Optional[_DummyThread] = _DummyThread()

    # -- configuration no-ops -------------------------------------------------

    def username_pw_set(self, *args: Any, **kwargs: Any) -> None:
        pass

    def tls_set(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable_logger(self, *args: Any, **kwargs: Any) -> None:
        pass

    def will_set(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, retain)

    # -- connection -----------------------------------------------------------

    def connect_async(self, **kwargs: Any) -> None:
        pass

    def connect(self, **kwargs: Any) -> None:
        self.ensure_connected()

    def ensure_connected(self) -> None:
        if self.connected:
            return
        self.connected = True
        if self.on_connect is not None:
            self.on_connect(self, None, {}, 0)

    def simulate_disconnect(self, rc: int = 1) -> None:
        """Drop the connection, as a broker restart or network blip would."""
        if not self.connected:
            return
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, rc)

    def simulate_thread_death(self) -> None:
        """Simulate paho's network thread dying, as an escaping exception does."""
        self._thread = None

    def loop_start(self) -> None:
        self.ensure_connected()

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        self.simulate_disconnect(rc=0)

    # -- traffic --------------------------------------------------------------

    def subscribe(self, topic: str, qos: int = 0) -> Tuple[int, int]:
        self.subscribed.append(topic)
        return (0, 1)

    def publish(
        self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False
    ) -> DummyPublishInfo:
        self.message_queue.append((topic, "" if payload is None else str(payload)))
        return DummyPublishInfo()


class DummyState:
    """In-memory replacement for :class:`miqro.State`.

    Loads whatever a previous service persisted via :meth:`save`, so tests can
    check that state survives a "restart" by building a second service.
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self._data: Dict[str, Any] = deepcopy(SAVED_STATE)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def set_path(self, *keys: str, value: Any) -> None:
        d = self._data
        for key in keys[:-1]:
            if key not in d:
                d[key] = {}
            d = d[key]
        d[keys[-1]] = value

    def get_path(self, *keys: str, default: Any) -> Any:
        d = self._data
        for key in keys:
            if not isinstance(d, dict) or key not in d:
                return default
            d = d[key]
        return d

    def save(self) -> None:
        SAVED_STATE.clear()
        SAVED_STATE.update(deepcopy(self._data))


class ReadOnlyDummyState(DummyState):
    """Starts empty regardless of what was persisted before.

    This keeps tests isolated from each other while still letting a test show
    that state written here is visible to a later :class:`DummyState`.
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self._data = {}


def send(service: Any, topic: str, payload: Union[str, bytes]) -> None:
    """Deliver an incoming MQTT message to the service.

    Anything still queued from before is discarded first, so a following
    :func:`expect_next` describes what this message caused rather than what a
    repeating output happened to emit beforehand.
    """
    client = service.mqtt_client
    client.ensure_connected()
    client.message_queue.clear()
    client.on_message(client, None, DummyMessage(topic, payload))
    # The service queues incoming messages and handles them on its loop thread;
    # dispatch now so that the message is handled by the time send() returns.
    service._drain_incoming()


def run(service: Any, seconds: float) -> None:
    """Advance the service's loops for ``seconds`` of wall-clock time.

    Messages published during the window are discarded: ``run`` is for letting
    the service settle, and what happened while it settled is not what the
    following :func:`expect_next` is asserting about.
    """
    service.mqtt_client.ensure_connected()
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        service._loop_step()
    service.mqtt_client.message_queue.clear()


def _describe(log: Sequence[Tuple[str, str]]) -> str:
    if not log:
        return "no messages were published"
    return "published:\n" + "\n".join(f"    {t} -> {p!r}" for t, p in log)


def expect_next(
    service: Any,
    expectations: Dict[str, Union[None, str, Sequence[Any]]],
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Tuple[str, str]]:
    """Advance the service until published messages satisfy ``expectations``.

    Each key is a topic; the value is one of:

    ``None``
        No message may be published on this topic during the whole window.
    ``"<expression>"``
        The next message on this topic must satisfy the Python expression,
        which is evaluated with ``m`` bound to the payload string.
    ``["<expression>", n]``
        The next ``n`` messages on this topic must each satisfy it.

    Messages on topics that are not mentioned are ignored.  Messages already
    queued when the call starts count.
    """
    required: Dict[str, Tuple[str, int]] = {}
    forbidden: Set[str] = set()

    for topic, expectation in expectations.items():
        if expectation is None:
            forbidden.add(topic)
        elif isinstance(expectation, (list, tuple)):
            required[topic] = (expectation[0], int(expectation[1]))
        else:
            required[topic] = (expectation, 1)

    outstanding = {topic: count for topic, (_, count) in required.items()}
    seen: List[Tuple[str, str]] = []
    deadline = monotonic() + timeout

    service.mqtt_client.ensure_connected()

    while True:
        queue = service.mqtt_client.message_queue
        while queue:
            topic, payload = queue.pop(0)
            seen.append((topic, payload))

            if topic in forbidden:
                raise AssertionError(
                    f"expected no message on {topic!r}, but got {payload!r}\n"
                    f"  {_describe(seen)}"
                )

            if topic in required and outstanding[topic] > 0:
                expression = required[topic][0]
                matched = eval(expression, {"m": payload})  # noqa: S307 - test helper
                if not matched:
                    if isinstance(expression, eventually):
                        continue  # keep looking on this topic
                    raise AssertionError(
                        f"message on {topic!r} did not satisfy {expression!r}\n"
                        f"  got: {payload!r}\n  {_describe(seen)}"
                    )
                outstanding[topic] -= 1

                # Stop mid-queue once satisfied so that messages published in
                # the same breath stay queued for the next expectation.
                if not forbidden and all(c == 0 for c in outstanding.values()):
                    return seen

        # A call that only asserts silence has nothing to wait for, so it has to
        # run its whole window.  Otherwise, stop as soon as the required
        # messages have arrived -- forbidden topics are still policed for every
        # message seen up to that point.
        if required and all(count == 0 for count in outstanding.values()):
            return seen

        if monotonic() >= deadline:
            break

        service._loop_step()

    unmet = {topic: count for topic, count in outstanding.items() if count > 0}
    if unmet:
        raise AssertionError(
            f"timed out after {timeout}s still waiting for {unmet}\n  {_describe(seen)}"
        )
    return seen
