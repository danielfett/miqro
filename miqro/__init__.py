from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Callable, Deque, Dict, List, Optional, Tuple, Union

import paho.mqtt.client as mqtt
from yaml import FullLoader, dump, load

from miqro import ha_sensors


class Loop:
    """A periodically executed callback.

    Scheduling uses :func:`time.monotonic`, never the wall clock: a DST
    transition or an NTP correction that steps the system clock backwards would
    otherwise postpone every loop in the process by the size of the jump, which
    for a loop that escalates an alarm is a silent, open-ended stall.
    """

    fn: Callable
    interval: timedelta
    stat_call_count: int = 0
    stat_cumulative_duration: float = 0.0
    stat_error_count: int = 0
    consecutive_errors: int = 0

    def __init__(self, fn: Callable, interval: timedelta, start: bool = True):
        self.fn = fn

        if not isinstance(interval, timedelta):
            raise Exception("interval must be provided as timedelta!")

        self.interval = interval
        self._next_call: Optional[float] = monotonic() if start else None

    @property
    def interval_seconds(self) -> float:
        return self.interval.total_seconds()

    @property
    def next_call(self) -> Optional[datetime]:
        """Wall-clock estimate of the next execution, for display only."""
        if self._next_call is None:
            return None
        return datetime.now() + timedelta(seconds=self._next_call - monotonic())

    def is_running(self) -> bool:
        return self._next_call is not None

    def run_if_needed(self, instance: "Service") -> Optional[float]:
        """Execute the callback if it is due.

        Returns the monotonic timestamp of the next execution, or ``None`` if
        the loop is not running.  Exceptions raised by the callback are logged
        and counted but never propagated: one broken loop must not stop the
        others, and must not take the process down silently.
        """
        if self._next_call is None:
            return None

        now = monotonic()
        if now < self._next_call:
            return self._next_call

        try:
            keep_running = self.fn(instance) is not False
        except Exception as e:
            self.stat_error_count += 1
            self.consecutive_errors += 1
            instance.log.exception(f"{self} raised {e!r}; loop continues")
            instance.note_failure(f"{self}: {e!r}")
            # Back off a little so a persistently failing loop cannot spin.
            self._next_call = now + max(self.interval_seconds, 1.0)
            return self._next_call

        self.consecutive_errors = 0
        self.stat_call_count += 1
        self.stat_cumulative_duration += monotonic() - now

        if not keep_running:
            self.stop()
            return None

        self._next_call = now + self.interval_seconds
        return self._next_call

    def start(self, delayed=False):
        self._next_call = monotonic() + (self.interval_seconds if delayed else 0.0)

    def stop(self):
        self._next_call = None

    def restart(self, delayed=False):
        self.start(delayed=delayed)

    def get_remaining(self) -> Optional[timedelta]:
        if self._next_call is None:
            return None
        return timedelta(seconds=self._next_call - monotonic())

    def stat_reset(self):
        self.stat_call_count = 0
        self.stat_cumulative_duration = 0.0
        self.stat_error_count = 0

    def stat_get(self) -> Tuple[int, float, float, bool]:
        average_call_duration = (
            0.0
            if not self.stat_call_count
            else (self.stat_cumulative_duration / self.stat_call_count)
        )
        try:
            load = average_call_duration / self.interval.total_seconds()
        except ZeroDivisionError:
            load = 0.0
        is_critical = load > 1.0
        return self.stat_call_count, average_call_duration, load, is_critical

    def __str__(self):
        return f"Loop({self.fn.__name__})"


# args/kwargs can be anything that the constructor of timedelta accepts
def loop(*args, **kwargs):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner: "Service", name):
            if not owner.PREPARED_LOOPS:
                owner.PREPARED_LOOPS = []
            owner.PREPARED_LOOPS.append((self.fn, timedelta(*args, **kwargs)))

    return class_decorator


def handle(topic_ext):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner: "Service", name):
            if not owner.CLASS_MQTT_HANDLERS:
                owner.CLASS_MQTT_HANDLERS = []
            owner.CLASS_MQTT_HANDLERS.append((topic_ext, self.fn))

    return class_decorator


def handle_global(topic):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner: "Service", name):
            if not owner.CLASS_MQTT_GLOBAL_HANDLERS:
                owner.CLASS_MQTT_GLOBAL_HANDLERS = []
            owner.CLASS_MQTT_GLOBAL_HANDLERS.append((topic, self.fn))

    return class_decorator


def accept_json(fn):
    def actual_fn(self, arg):
        return fn(self, **json.loads(arg))

    return actual_fn


class State:
    """Store data in a YAML file."""

    service: "Service"

    DATA_ROOT = Path("/var/lib/miqro/data")

    def __init__(self, service) -> None:
        self.service = service
        self._file = self.DATA_ROOT / (service.SERVICE_NAME + ".yaml")

        self._data = self._load()
        self.service.log.debug(f"State: Loaded {self._data}")

    def _load(self) -> Dict:
        """Load the state file, tolerating anything that is wrong with it.

        A truncated or corrupt file (a power loss during a previous save, a
        full disk) must not stop the service from starting: starting with
        default state is recoverable, refusing to start is not.
        """
        try:
            if not self._file.exists():
                self._file.parent.mkdir(parents=True, exist_ok=True)
                return {}

            with self._file.open() as f:
                data = load(f, Loader=FullLoader)

            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ValueError(f"expected a mapping, found {type(data).__name__}")
            return data
        except PermissionError as e:
            self.service.log.error(e)
            return {}
        except Exception as e:
            self.service.log.error(
                f"State: {self._file} is unusable ({e!r}); starting with empty state."
            )
            self._quarantine()
            return {}

    def _quarantine(self) -> None:
        """Move a broken state file aside so the next save starts clean."""
        broken = self._file.with_suffix(self._file.suffix + ".broken")
        try:
            self._file.replace(broken)
            self.service.log.error(f"State: moved unusable state file to {broken}")
        except Exception as e:
            self.service.log.error(f"State: could not move {self._file} aside: {e!r}")

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def set_path(self, *keys, value):
        self.service.log.debug(f"State: Setting {keys} to {value}")
        d = self._data
        for key in keys[:-1]:  # -1 because we don't want to set the last key
            if not key in d:
                d[key] = {}
            d = d[key]

        d[keys[-1]] = value

    def get_path(self, *keys, default):
        d = self._data
        for key in keys:
            if not key in d:
                self.service.log.debug(f"State: {key} not found, returning '{default}'")
                return default
            d = d[key]

        self.service.log.debug(f"State: {keys} found, returning {d}")
        return d

    def save(self):
        """Write the state file atomically.

        Writing in place risks leaving a truncated file behind if the machine
        loses power mid-write, which on the next start would take the service
        down.  Write a temporary file, fsync it, then rename over the target:
        readers see either the old file or the new one.
        """
        self.service.log.debug(f"State: Saving {self._data}")
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        try:
            with tmp.open("w") as f:
                dump(self._data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._file)
        except Exception as e:
            self.service.log.error(f"State: could not save {self._file}: {e!r}")
            self.service.note_failure(f"state save failed: {e!r}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


class Service:
    SERVICE_NAME: str = "none"
    CONFIG_FILE_PATHS: List[Path] = [Path("miqro.yml"), Path("/etc/miqro.yml")]
    JSON_FLOAT_PRECISION: int = 4
    MAX_LOOP_INTERVAL: float = 0.2
    PREPARED_LOOPS: List[Tuple[Callable, timedelta]] = []
    LOOPS: Optional[List[Loop]] = None
    CLASS_MQTT_HANDLERS: List[Tuple[str, Callable]] = []
    CLASS_MQTT_GLOBAL_HANDLERS: List[Tuple[str, Callable]] = []
    MQTT_ONLINE_UPDATE_INTERVAL: int = 180

    PAYLOAD_ON = "1"
    PAYLOAD_OFF = "0"

    # QoS used for subscriptions.  At-least-once means the broker redelivers
    # after a dropped connection instead of silently discarding.
    SUBSCRIBE_QOS: int = 1

    # With a persistent session the broker queues matching messages while the
    # service is disconnected, instead of dropping them.  Services that must
    # not miss an input should set this to False.
    CLEAN_SESSION: bool = True

    # Incoming messages are handled on the main loop thread rather than paho's
    # network thread; see _on_message.
    MAX_INCOMING_QUEUE: int = 10000

    ha_devices: List[ha_sensors.Device] = []
    ha_entities: List[ha_sensors.Entity] = []  # only entities without device

    USE_STATE_FILE = False

    QOS_MAX_ONCE = 0
    QOS_AT_LEAST_ONCE = 1
    QOS_EXACTLY_ONCE = 2

    log: logging.Logger
    config: Dict
    service_config: Dict
    data_topic_prefix: str
    mqtt_client: mqtt.Client
    stop = False
    is_connected = False
    mqtt_handlers: List[Tuple[str, Callable]]
    mqtt_global_handlers: List[Tuple[str, Callable]]
    state: Optional[State] = None

    # Health bookkeeping.  Anything that swallows an exception in order to keep
    # running records it here so that watchdogs can tell "running" from
    # "running correctly".
    last_failure: Optional[str] = None
    last_failure_at: Optional[datetime] = None
    failure_count: int = 0
    last_message_received_at: Optional[float] = None
    disconnect_count: int = 0

    def __init__(
        self,
        add_config_file_path=None,
        log_level=logging.DEBUG,
        mqtt_client_cls=mqtt.Client,
        state_cls=State,
    ):
        self._prepare_logger(log_level)
        self._read_config(add_config_file_path)

        self.last_key_values = {}

        self._incoming: Deque[Tuple[str, str]] = deque()
        self._incoming_lock = Lock()
        self._mqtt_loop_started = False

        self.mqtt_client = self._make_mqtt_client(mqtt_client_cls)
        if "auth" in self.config:
            self.mqtt_client.username_pw_set(**self.config["auth"])
        if "tls" in self.config:
            self.mqtt_client.tls_set(**self._make_tls_config(self.config["tls"]))
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message = self._on_message
        self.mqtt_client.enable_logger(self.mqtt_log)
        self.mqtt_client.will_set(self.willtopic, "0", retain=True)
        self.mqtt_client.connect_async(**self.config["broker"])

        self.enabled = True

        self.mqtt_handlers = [h for h in self.CLASS_MQTT_HANDLERS]
        self.mqtt_handlers.append(("enabled", self._on_enable))
        self.mqtt_global_handlers = [h for h in self.CLASS_MQTT_GLOBAL_HANDLERS]

        if self.USE_STATE_FILE:
            self.state = state_cls(self)

        self._create_loops()

        self.log.info("started")

    def __str__(self):
        return self.SERVICE_NAME

    def _make_mqtt_client(self, mqtt_client_cls):
        # Only pass clean_session when the service opts out of it, so that
        # client doubles and older signatures keep working unchanged.
        if self.CLEAN_SESSION:
            return mqtt_client_cls(self.SERVICE_NAME)
        try:
            return mqtt_client_cls(self.SERVICE_NAME, clean_session=False)
        except TypeError:
            self.log.warning(
                "MQTT client does not accept clean_session; "
                "messages may be lost while disconnected."
            )
            return mqtt_client_cls(self.SERVICE_NAME)

    # -- health ---------------------------------------------------------------

    def note_failure(self, description: str) -> None:
        """Record that something was swallowed in order to keep running.

        Callers that catch an exception to protect the process should report it
        here so that :meth:`healthy` and any external watchdog can see that the
        service is degraded even though it is still up.
        """
        self.failure_count += 1
        self.last_failure = description
        self.last_failure_at = datetime.now()

    def mqtt_thread_alive(self) -> bool:
        """Whether paho's network thread is still running.

        An exception escaping a callback kills that thread while leaving the
        process running, which would otherwise be invisible: the main loop keeps
        spinning and publishes keep being queued but never sent.
        """
        if not self._mqtt_loop_started:
            return True  # not started yet; nothing to be wrong with
        thread = getattr(self.mqtt_client, "_thread", None)
        if thread is None:
            return False
        is_alive = getattr(thread, "is_alive", None)
        return is_alive() if callable(is_alive) else True

    def healthy(self) -> bool:
        """Whether the service is not just running but working.

        Deliberately does *not* consider :attr:`failure_count`, which counts
        historical, already-recovered problems; callers wanting a stricter
        check should look at that themselves.
        """
        return self.is_connected and self.mqtt_thread_alive()

    def _make_tls_config(self, config):
        # cert_reqs and tls_version are strings pointing to properties in
        # the ssl module - parse from string to property!
        import ssl

        return {
            "ca_certs": config.get("ca_certs", None),
            "certfile": config.get("certfile", None),
            "keyfile": config.get("keyfile", None),
            "cert_reqs": getattr(ssl, config.get("cert_reqs", "CERT_REQUIRED")),
            "tls_version": getattr(ssl, config.get("tls_version", "PROTOCOL_TLS")),
            "ciphers": config.get("ciphers", None),
        }

    def add_loop(self, loop):
        if not self.LOOPS:
            self.LOOPS = []

        self.LOOPS.append(loop)
        return loop

    def _create_loops(self):
        self.add_loop(
            Loop(
                self._update_online_status,
                timedelta(seconds=self.MQTT_ONLINE_UPDATE_INTERVAL),
            )
        )
        for fn, interval in self.PREPARED_LOOPS:
            self.add_loop(Loop(fn, interval))

    def _prepare_logger(self, log_level):
        if not logging.getLogger().hasHandlers():
            log_handler = logging.StreamHandler(sys.stderr)
            log_handler.setFormatter(
                logging.Formatter("%(asctime)s  %(name)s  %(levelname)s \t%(message)s")
            )
            log_handler.setLevel(logging.DEBUG)
            logging.getLogger().addHandler(log_handler)
        logging.getLogger().setLevel(log_level)

        self.log = logging.getLogger(self.SERVICE_NAME + ".main")
        self.mqtt_log = logging.getLogger(self.SERVICE_NAME + ".mqtt")
        self.mqtt_log.setLevel(logging.INFO)

    def _read_config(self, add_config_file_path=None):
        # Copy: CONFIG_FILE_PATHS is a class attribute, and prepending to it
        # directly would accumulate across every service constructed in the
        # process.
        paths = list(self.CONFIG_FILE_PATHS)
        if add_config_file_path:
            paths.insert(0, Path(add_config_file_path))

        for path in paths:
            if path.exists():
                self.log.debug(f"Using configuration file at {path}")
                with path.open("r") as f:
                    self.config = load(f, Loader=FullLoader)
                    break
            else:
                self.log.debug(f"NOT using configuration file at {path}")
        else:
            raise Exception(
                "No MIQRO config file found; searched paths: "
                + ", ".join(map(str, self.CONFIG_FILE_PATHS))
            )

        if self.SERVICE_NAME not in self.config.get("services", {}):
            self.log.warning(
                f"Service configuration for {self.SERVICE_NAME} not found in 'services' section of configuration file {path}. Using empty configuration."
            )
            self.service_config = {}
        else:
            self.service_config = self.config["services"][self.SERVICE_NAME]

        self.data_topic_prefix = self.service_config.get(
            "data_topic", f"service/{self.SERVICE_NAME}/"
        )
        self.willtopic = self.data_topic_prefix + "online"

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.log.error(f"MQTT connection failed with code {rc}")
            return
        self.log.info(f"MQTT connected, client={client}, userdata={userdata}, rc={rc}")
        self.is_connected = True
        self.log.info(f"Subscribing to ...")

        for topic, _ in self._all_handlers():
            self.log.info(f"  - {topic}")
            client.subscribe(topic, qos=self.SUBSCRIBE_QOS)

        self.mqtt_client.publish(
            self.willtopic, "1", retain=True, qos=self.QOS_AT_LEAST_ONCE
        )

        self._publish_ha_discovery()

    def _publish_ha_discovery(self):
        if "ha_discovery_prefix" in self.config:
            prefix = self.config["ha_discovery_prefix"]
        else:
            prefix = "homeassistant"

        for device in self.ha_devices:
            device.publish_discovery(prefix)
        for entity in self.ha_entities:
            entity.publish_discovery(prefix)

    def _on_disconnect(self, client, userdata, rc):
        self.disconnect_count += 1
        self.is_connected = False
        if rc == 0:
            self.log.info("MQTT disconnected cleanly")
        else:
            self.log.warning(
                f"MQTT disconnected unexpectedly, rc={rc} "
                f"(disconnect #{self.disconnect_count})"
            )
            self.note_failure(f"MQTT disconnected, rc={rc}")

    def add_handler(self, topic, handler):
        self.mqtt_handlers.append((topic, handler))

        if self.is_connected:
            self.log.info(f"Subscribing to {self.data_topic_prefix + topic}")
            self.mqtt_client.subscribe(
                self.data_topic_prefix + topic, qos=self.SUBSCRIBE_QOS
            )

    def add_global_handler(self, topic, handler):
        """
        Add a global handler for a topic.
        The handler will be called with the topic and payload as arguments.
        """

        self.mqtt_global_handlers.append((topic, handler))

        if self.is_connected:
            self.log.info(f"Subscribing to {topic}")
            self.mqtt_client.subscribe(topic, qos=self.SUBSCRIBE_QOS)

    def _on_enable(self, _, payload):
        # Registered as a bound method, so dispatch passes (service, payload)
        # on top of the bound self -- hence the ignored first argument.
        if payload == "1":
            self.enabled = True
        else:
            self.enabled = False
        self.log.info(f"set enabled to {self.enabled!r}")
        return

    def _update_online_status(self, _):
        self.publish(
            self.willtopic, "1", retain=True, qos=self.QOS_AT_LEAST_ONCE, global_=True
        )

        assert self.LOOPS

        self.log.debug("Loop stats:")
        for l in self.LOOPS:
            call_count, average_call_duration, load, is_critical = l.stat_get()
            error_count = l.stat_error_count
            l.stat_reset()
            self.log.debug(
                f" - {l} called {call_count} times, average duration {average_call_duration}s, load={int(load*100)}%"
            )
            if is_critical:
                self.log.warning(
                    f"   {l} takes, on average, longer to execute ({average_call_duration}s) than defined interval ({l.interval.total_seconds()})"
                )
            if error_count:
                self.log.error(
                    f"   {l} raised {error_count} time(s) in the last reporting period"
                )

    def _all_handlers(self):
        for topic, handler in self.mqtt_global_handlers:
            yield topic, handler

        for topic, handler in self.mqtt_handlers:
            yield self.data_topic_prefix + topic, handler

    def _on_message(self, client, userdata, msg):
        """Queue an incoming message; it is handled on the main loop thread.

        Handling messages here, on paho's network thread, has two problems: an
        exception escaping this callback kills that thread (leaving the process
        running but deaf), and handlers race with the loops that share their
        state.  Queueing sidesteps both.
        """
        payload = str(msg.payload.decode("utf-8", errors="replace")).strip()
        self.log.debug(
            f"Received MQTT message on topic {msg.topic} containing {payload}"
        )
        self.last_message_received_at = monotonic()

        with self._incoming_lock:
            if len(self._incoming) >= self.MAX_INCOMING_QUEUE:
                dropped_topic, _ = self._incoming.popleft()
                self.log.error(
                    f"Incoming queue full ({self.MAX_INCOMING_QUEUE}); "
                    f"dropped oldest message on {dropped_topic}"
                )
                self.note_failure("incoming message queue overflowed")
            self._incoming.append((msg.topic, payload))

    def _drain_incoming(self) -> int:
        """Dispatch every queued incoming message. Returns how many were handled."""
        count = 0
        while True:
            with self._incoming_lock:
                if not self._incoming:
                    return count
                topic, payload = self._incoming.popleft()
            self._dispatch(topic, payload)
            count += 1

    def _dispatch(self, msg_topic: str, payload: str) -> None:
        handled = False
        for topic, handler in self._all_handlers():
            matched, remainder = self._match(topic, msg_topic)
            if not matched:
                continue
            handled = True
            # One failing handler must not prevent the others from running:
            # several inputs can share a topic, and a bad payload for one of
            # them is not a reason to drop the message for the rest.
            try:
                if remainder is None:
                    handler(self, payload)
                else:
                    handler(self, payload, remainder)
            except Exception as e:
                self.log.exception(
                    f"Handler {getattr(handler, '__qualname__', handler)} failed for "
                    f"topic '{msg_topic}': {e!r}"
                )
                self.note_failure(f"handler for '{msg_topic}' raised {e!r}")

        if handled:
            return

        try:
            if self.handle_message(msg_topic, payload):
                return
        except Exception as e:
            self.log.exception(f"handle_message failed for '{msg_topic}': {e!r}")
            self.note_failure(f"handle_message for '{msg_topic}' raised {e!r}")
            return

        self.log.error(
            f"Unhandled topic '{msg_topic}', registered handlers for: {', '.join(k for (k, v) in self._all_handlers())}"
        )

    @staticmethod
    def _match(subscription: str, msg_topic: str) -> Tuple[bool, Optional[str]]:
        """Match a topic against a subscription pattern.

        Returns ``(matched, remainder)``, where ``remainder`` is the part of the
        topic covered by a trailing ``#`` (passed to the handler as a second
        argument) or ``None`` for subscriptions without one.

        Uses paho's matcher so that ``+`` and mid-topic wildcards behave the way
        the broker does -- comparing topics by equality means a subscription
        containing ``+`` is established at the broker but never dispatched here,
        so those messages arrive and are silently discarded.
        """
        if not mqtt.topic_matches_sub(subscription, msg_topic):
            return False, None
        if subscription.endswith("#"):
            # "a/b/#" also matches "a/b" itself, which has no remainder.
            return True, msg_topic[len(subscription) - 1 :]
        return True, None

    def handle_message(self, topic, payload):
        return False

    def publish(
        self,
        ext: str | ha_sensors.Entity,
        message,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed: Union[bool, timedelta] = False,
        global_=False,
    ):
        ha_sensor = None
        if isinstance(ext, ha_sensors.Entity):
            ha_sensor = ext
            ext = ext.state_topic_postfix

        topic = (self.data_topic_prefix + ext) if not global_ else ext
        # if ext not in self.ignore_recv_topics:
        #    self.ignore_recv_topics.append(ext)
        if type(message) == type(True):  # type is boolean
            if ha_sensor is None or not isinstance(ha_sensor, ha_sensors.Switch):
                message = self.PAYLOAD_ON if message else self.PAYLOAD_OFF
            else:
                message = ha_sensor.payload_on if message else ha_sensor.payload_off
        elif message is None:
            message = ""
        elif type(message) in [dict, list]:
            self.publish_json(
                ext, message, retain, qos, only_if_changed, global_=global_
            )
            return
        else:
            message = self._round_floats(message)

        if only_if_changed is True:
            last_message = self.last_key_values.get(topic, None)
            if last_message == message:
                self.log.debug(f"{topic} not changed, not publishing.")
                return
            else:
                self.last_key_values[topic] = message
        elif isinstance(only_if_changed, timedelta):
            now = datetime.now()
            last_message, last_time = self.last_key_values.get(topic, (None, None))
            if (
                last_message == message
                and last_time
                and last_time + only_if_changed > now
            ):
                self.log.debug(
                    f"{topic} not changed since {only_if_changed.total_seconds()}s, not publishing."
                )
                return
            else:
                self.last_key_values[topic] = (message, now)

        self.log.debug(f"MQTT publish: {topic}: {message}")
        try:
            info = self.mqtt_client.publish(topic, message, retain=retain, qos=qos)
        except Exception as e:
            self.log.exception(e)
            self.note_failure(f"publish to '{topic}' raised {e!r}")
            return None

        # A QoS 0 publish while disconnected is discarded by paho, which reports
        # it only through this return code.  Silence here means a message the
        # caller believes was delivered never left the process.
        rc = getattr(info, "rc", 0)
        if rc != 0:
            if self.is_connected:
                self.log.error(f"MQTT publish to '{topic}' failed with rc={rc}")
                self.note_failure(f"publish to '{topic}' failed with rc={rc}")
            else:
                # Expected before the first connection and during an outage;
                # already visible through the online topic and healthy().
                self.log.warning(
                    f"MQTT publish to '{topic}' dropped: not connected (rc={rc})"
                )

        return info

    def publish_json(
        self,
        ext,
        message_json,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed: Union[bool, timedelta] = False,
        global_=False,
    ):
        return self.publish(
            ext,
            json.dumps(self._round_floats(message_json)),
            retain=retain,
            qos=qos,
            only_if_changed=only_if_changed,
            global_=global_,
        )

    def publish_json_keys(
        self,
        message_dict: Dict,
        ext=None,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed: Union[bool, timedelta] = False,
        global_=False,
    ):
        for key, value in message_dict.items():
            if ext:
                key = ext + "/" + key
            # print(key, type(value))
            if type(value) is dict:
                self.publish_json_keys(
                    value, key, retain, qos, only_if_changed, global_
                )
            else:
                self.publish(
                    key,
                    value,
                    retain=retain,
                    qos=qos,
                    only_if_changed=only_if_changed,
                    global_=global_,
                )

    def _loop_step(self):
        assert self.LOOPS is not None

        self._drain_incoming()

        earliest_next_call = monotonic() + self.MAX_LOOP_INTERVAL
        for loop in self.LOOPS:
            next_call = loop.run_if_needed(self)
            if next_call is not None:
                earliest_next_call = min(earliest_next_call, next_call)

        # Do not sleep past a message that is already waiting.
        with self._incoming_lock:
            if self._incoming:
                return

        sleep(max(0.0, earliest_next_call - monotonic()))

    def run(self):
        self.mqtt_client.loop_start()
        self._mqtt_loop_started = True
        try:
            while not self.stop:
                self._loop_step()

                if not self.mqtt_thread_alive():
                    # Nothing can be received or sent any more.  Exiting lets
                    # the service manager restart us; carrying on would leave a
                    # process that looks alive but handles nothing.
                    self.log.critical(
                        "MQTT network thread has died; shutting down so that "
                        "the service manager can restart this service."
                    )
                    self.note_failure("MQTT network thread died")
                    raise SystemExit(1)
        finally:
            self.mqtt_client.loop_stop()

    def _round_floats(self, o):
        if isinstance(o, float):
            return round(o, self.JSON_FLOAT_PRECISION)
        if isinstance(o, dict):
            return {k: self._round_floats(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self._round_floats(x) for x in o]
        return o


def run(service):
    parser = argparse.ArgumentParser(
        description=f"{service.SERVICE_NAME} MIQRO service"
    )
    parser.add_argument("--config", "-c", help="config file", default=None)
    parser.add_argument(
        "--install", action="store_true", help="Setup this service as a systemd unit."
    )
    parser.add_argument(
        "--install-as-user",
        "-u",
        help="Install service for specified user (instead of root)",
        default="root",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--mqtt-debug-prefix",
        "-d",
        help="Prefix for all outgoing MQTT messages for debugging purposes",
        default=None,
    )
    cli_args = parser.parse_args()

    class DebugMQTTClient(mqtt.Client):
        def publish(self, topic, *args, **kwargs):
            return super().publish(cli_args.mqtt_debug_prefix + topic, *args, **kwargs)

        def will_set(self, topic, *args, **kwargs):
            return super().will_set(cli_args.mqtt_debug_prefix + topic, *args, **kwargs)

    if not cli_args.install:
        service(
            cli_args.config,
            logging.DEBUG if cli_args.verbose else logging.INFO,
            DebugMQTTClient if cli_args.mqtt_debug_prefix else mqtt.Client,
        ).run()
        return

    systemd_service_name = f"miqro_{service.SERVICE_NAME}"

    executable = sys.executable
    if not executable:
        executable = "/usr/bin/env python3"

    # Check if the service was started with 'python -m module'
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if main_spec and main_spec.name and main_spec.name != "__main__":
        exec_start = f"{executable} -m {main_spec.name}"
    else:
        exec_start = f"{executable} {Path(sys.argv[0]).resolve()}"

    systemd_unit_file = f"""
[Unit]
Description={service.SERVICE_NAME} MIQRO microservice
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=20
User={cli_args.install_as_user}
ExecStart={exec_start}

[Install]
WantedBy=multi-user.target
"""

    systemd_path = Path("/etc/systemd/system/", systemd_service_name + ".service")
    systemd_path.write_text(systemd_unit_file)
    systemd_path.chmod(0o644)
    print(
        f"Service successfully installed as {systemd_service_name}.\nYou can now enable the service to start on boot by running:\n sudo systemctl enable {systemd_service_name}\n... and run the service:\n sudo systemctl start {systemd_service_name}"
    )
