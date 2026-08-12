"""Regression tests for runtime failures that used to be invisible.

None of these need a broker: they drive the service through the test doubles in
miqro.test.tools.
"""

import logging
from datetime import datetime, timedelta
from time import monotonic

import pytest
import yaml

import miqro
from miqro.test.tools import DummyMQTTClient, DummyState, expect_next, run, send

log = logging.getLogger("test_reliability")


CONFIG = {"broker": {}, "services": {"probe": {}}}


class ProbeService(miqro.Service):
    SERVICE_NAME = "probe"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "miqro.yml"
    path.write_text(yaml.safe_dump(CONFIG))
    return str(path)


@pytest.fixture
def service(config_path):
    return ProbeService(config_path, mqtt_client_cls=DummyMQTTClient)


# --------------------------------------------------------------------------
# Message dispatch
# --------------------------------------------------------------------------


def test_wildcard_subscriptions_are_dispatched(service):
    """A '+' subscription was established at the broker but never dispatched.

    Messages arrived and were discarded with an "unhandled topic" error, so the
    input simply never fired.
    """
    seen = []
    service.add_global_handler("zigbee/+/contact", lambda svc, payload: seen.append(payload))

    send(service, "zigbee/frontdoor/contact", "open")
    assert seen == ["open"]

    seen.clear()
    send(service, "zigbee/frontdoor/deeper/contact", "open")
    assert seen == [], "'+' must match exactly one level"


def test_hash_subscriptions_pass_the_remainder(service):
    seen = []
    service.add_global_handler(
        "sensors/#", lambda svc, payload, rest: seen.append((rest, payload))
    )

    send(service, "sensors/kitchen/temp", "21")
    assert seen == [("kitchen/temp", "21")]


def test_handler_exception_is_contained(service):
    """An exception here used to escape into paho and kill the network thread."""

    def broken(svc, payload):
        raise RuntimeError("boom")

    service.add_global_handler("some/topic", broken)

    send(service, "some/topic", "x")

    assert service.failure_count == 1
    assert "boom" in service.last_failure
    # Still usable afterwards.
    seen = []
    service.add_global_handler("other/topic", lambda svc, p: seen.append(p))
    send(service, "other/topic", "still here")
    assert seen == ["still here"]


def test_enable_command_does_not_raise(service):
    """The built-in 'enabled' handler had a mismatched signature."""
    send(service, "service/probe/enabled", "0")
    assert service.failure_count == 0
    assert service.enabled is False

    send(service, "service/probe/enabled", "1")
    assert service.enabled is True


def test_messages_are_handled_on_the_loop_thread(service):
    """Dispatch happens in _loop_step, not in the paho callback."""
    seen = []
    service.add_global_handler("deferred/topic", lambda svc, p: seen.append(p))

    client = service.mqtt_client
    client.ensure_connected()
    client.on_message(client, None, type("M", (), {"topic": "deferred/topic", "payload": b"v"})())

    assert seen == [], "handled on the receiving thread"
    service._loop_step()
    assert seen == ["v"]


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------


def test_run_exits_when_the_mqtt_thread_dies(service):
    """A dead network thread must not leave a process that looks alive."""
    service.mqtt_client.simulate_thread_death()

    with pytest.raises(SystemExit):
        service.run()

    assert not service.mqtt_thread_alive()
    assert not service.healthy()


def test_healthy_reflects_the_connection(service):
    service.mqtt_client.ensure_connected()
    service._mqtt_loop_started = True
    assert service.healthy()

    service.mqtt_client.simulate_disconnect()
    assert not service.healthy()
    assert service.failure_count == 1


# --------------------------------------------------------------------------
# Timers
# --------------------------------------------------------------------------


def test_loops_are_scheduled_on_the_monotonic_clock(service, monkeypatch):
    """A backwards wall-clock step must not postpone loops.

    With datetime-based scheduling, a DST fall-back or an NTP correction
    postponed every loop in the process by the size of the jump -- including the
    one that escalates a prealarm into an alarm.
    """
    calls = []
    loop = miqro.Loop(lambda svc: calls.append(1), timedelta(seconds=0.1), start=True)
    service.add_loop(loop)

    service._loop_step()
    assert len(calls) == 1

    # The wall clock jumps an hour backwards.
    real_datetime = miqro.datetime

    class ShiftedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) - timedelta(hours=1)

    monkeypatch.setattr(miqro, "datetime", ShiftedDatetime)

    deadline = monotonic() + 0.5
    while monotonic() < deadline:
        service._loop_step()

    assert len(calls) > 1, "loop stalled after the clock stepped backwards"


def test_a_failing_loop_does_not_stop_the_others(service):
    """One broken timer used to terminate the process."""
    good = []

    def broken(svc):
        raise ValueError("loop is broken")

    service.add_loop(miqro.Loop(broken, timedelta(seconds=0.05), start=True))
    service.add_loop(miqro.Loop(lambda svc: good.append(1), timedelta(seconds=0.05), start=True))

    deadline = monotonic() + 0.4
    while monotonic() < deadline:
        service._loop_step()

    assert good, "the healthy loop stopped running"
    assert service.failure_count > 0
    assert "loop is broken" in service.last_failure


def test_loop_returning_false_stops_it(service):
    """Existing contract: returning False stops the loop."""
    calls = []

    def once(svc):
        calls.append(1)
        return False

    loop = miqro.Loop(once, timedelta(seconds=0.05), start=True)
    service.add_loop(loop)

    deadline = monotonic() + 0.3
    while monotonic() < deadline:
        service._loop_step()

    assert calls == [1]
    assert not loop.is_running()


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


class FakeService:
    SERVICE_NAME = "fake"

    def __init__(self):
        self.log = log
        self.failures = []

    def note_failure(self, description):
        self.failures.append(description)


def make_state_cls(tmp_path):
    class TmpState(miqro.State):
        DATA_ROOT = tmp_path

    return TmpState


def test_corrupt_state_file_does_not_prevent_startup(tmp_path):
    """A truncated state file used to raise out of the constructor forever."""
    state_file = tmp_path / "fake.yaml"
    state_file.write_text("{this is: not: valid: yaml")

    state = make_state_cls(tmp_path)(FakeService())

    assert state.get_path("anything", default="fallback") == "fallback"
    assert (tmp_path / "fake.yaml.broken").exists(), "bad file was not moved aside"


def test_state_file_of_the_wrong_shape_is_rejected(tmp_path):
    (tmp_path / "fake.yaml").write_text("just a string")
    state = make_state_cls(tmp_path)(FakeService())
    assert state.get_path("anything", default="fallback") == "fallback"


def test_state_is_saved_atomically(tmp_path):
    cls = make_state_cls(tmp_path)
    state = cls(FakeService())
    state.set_path("group_state", "ga", value="ALARM")
    state.save()

    assert not list(tmp_path.glob("*.tmp")), "temporary file was left behind"

    reloaded = cls(FakeService())
    assert reloaded.get_path("group_state", "ga", default=None) == "ALARM"


def test_empty_state_file_is_usable(tmp_path):
    (tmp_path / "fake.yaml").write_text("")
    state = make_state_cls(tmp_path)(FakeService())
    state.set_path("a", value=1)
    assert state.get_path("a", default=None) == 1


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


def test_publish_failure_is_recorded(service, monkeypatch):
    """A QoS 0 publish while disconnected is dropped and reported only via rc."""

    class FailingInfo:
        rc = 4  # MQTT_ERR_NO_CONN

    monkeypatch.setattr(
        service.mqtt_client, "publish", lambda *a, **k: FailingInfo()
    )

    service.publish("some/topic", "value")

    assert service.failure_count == 1
    assert "rc=4" in service.last_failure


def test_qos_is_forwarded_through_json_helpers(service):
    published = []
    service.mqtt_client.publish = lambda topic, payload, qos=0, retain=False: (
        published.append((topic, qos)) or type("I", (), {"rc": 0})()
    )

    service.publish_json("a", {"x": 1}, qos=1)
    service.publish_json_keys({"b": 2}, qos=1)

    assert all(qos == 1 for _, qos in published), published


def test_config_paths_are_not_accumulated(config_path):
    """_read_config used to prepend to the shared class-level list."""
    before = list(miqro.Service.CONFIG_FILE_PATHS)
    ProbeService(config_path, mqtt_client_cls=DummyMQTTClient)
    ProbeService(config_path, mqtt_client_cls=DummyMQTTClient)
    assert list(miqro.Service.CONFIG_FILE_PATHS) == before
