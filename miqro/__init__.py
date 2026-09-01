from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import warnings
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
            # Give the class its own list unless it already has one.  Testing
            # for emptiness instead would append to the *parent's* list when
            # subclassing a service that already has loops, so the parent would
            # start running its child's loops as well.
            if "PREPARED_LOOPS" not in owner.__dict__:
                owner.PREPARED_LOOPS = list(owner.PREPARED_LOOPS)
            owner.PREPARED_LOOPS.append((self.fn, timedelta(*args, **kwargs)))

    return class_decorator


def handle(topic_ext):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner: "Service", name):
            # See the note in loop(): the child's handlers must not be added to
            # the parent's list, or the parent subscribes to its child's topics.
            if "CLASS_MQTT_HANDLERS" not in owner.__dict__:
                owner.CLASS_MQTT_HANDLERS = list(owner.CLASS_MQTT_HANDLERS)
            owner.CLASS_MQTT_HANDLERS.append((topic_ext, self.fn))

    return class_decorator


def handle_global(topic):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner: "Service", name):
            # See the note in loop().
            if "CLASS_MQTT_GLOBAL_HANDLERS" not in owner.__dict__:
                owner.CLASS_MQTT_GLOBAL_HANDLERS = list(
                    owner.CLASS_MQTT_GLOBAL_HANDLERS
                )
            owner.CLASS_MQTT_GLOBAL_HANDLERS.append((topic, self.fn))

    return class_decorator


def accept_json(fn):
    def actual_fn(self, arg):
        return fn(self, **json.loads(arg))

    return actual_fn


def _positional_arity(fn) -> Optional[Tuple[int, float]]:
    """How many positional arguments ``fn`` accepts, as ``(minimum, maximum)``.

    ``None`` if that cannot be determined (a C callable, say), in which case
    the caller falls back to the historical convention.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return None

    minimum = 0
    maximum: float = 0
    for param in params:
        if param.kind is param.VAR_POSITIONAL:
            maximum = float("inf")
        elif param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            maximum += 1
            if param.default is param.empty:
                minimum += 1
        elif param.kind is param.KEYWORD_ONLY and param.default is param.empty:
            return None  # cannot be satisfied positionally at all

    return minimum, maximum


class Handler:
    """A registered message handler, normalised to one calling convention.

    Handlers arrive in two shapes: plain functions -- from the ``@handle``
    decorators, or a lambda passed to :meth:`Service.add_handler` -- which take
    the service as their first argument, and bound methods, which do not.
    Dispatch used to pass the service either way, so a bound method needed a
    dummy first parameter and a handler with the wrong signature failed at the
    first message that arrived for it, possibly weeks after it was registered.

    Resolving the convention once, here, means :meth:`Service._dispatch` has a
    single way to call a handler and a mis-registered one raises where it is
    registered.
    """

    __slots__ = ("fn", "service", "topic", "takes_service", "wants_remainder",
                 "always_run")

    def __init__(self, service: "Service", topic: str, fn: Callable,
                 always_run: bool = False):
        self.fn = fn
        self.service = service
        self.topic = topic
        self.always_run = always_run
        # "a/b/#" is the only subscription form that passes the remainder of
        # the topic as a second argument.
        self.wants_remainder = topic.endswith("#")
        self.takes_service = self._resolve_takes_service()

    def _resolve_takes_service(self) -> bool:
        needed = 2 if self.wants_remainder else 1
        is_bound = (
            inspect.ismethod(self.fn)
            or getattr(self.fn, "__self__", None) is not None
        )
        arity = _positional_arity(self.fn)
        if arity is None:
            return not is_bound

        minimum, maximum = arity
        # Where both readings fit -- a callable with optional arguments -- take
        # the one that matches how it was registered.
        for takes_service in ((False, True) if is_bound else (True, False)):
            if minimum <= needed + (1 if takes_service else 0) <= maximum:
                return takes_service

        expected = "payload" + (", topic_remainder" if self.wants_remainder else "")
        raise TypeError(
            f"Handler {self!r} cannot be registered for '{self.topic}': it takes "
            f"{minimum} to {maximum} positional argument(s), but a handler for "
            f"this topic is called as handler(service, {expected}) -- or "
            f"handler({expected}) if it is a bound method."
        )

    def __call__(self, payload: str, remainder: Optional[str] = None):
        args: List = [self.service] if self.takes_service else []
        args.append(payload)
        if self.wants_remainder:
            args.append(remainder)
        return self.fn(*args)

    def __repr__(self):
        return getattr(self.fn, "__qualname__", None) or repr(self.fn)


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

    # QoS used for subscriptions.  At-least-once stops the broker from
    # dropping a message on its way to a *connected* client; it says nothing
    # about what happens while this service is away, which is decided by
    # CLEAN_SESSION below.
    SUBSCRIBE_QOS: int = 1

    # A clean session keeps nothing between connections: the broker discards
    # this client's subscriptions and anything addressed to it as soon as the
    # connection drops, so commands sent during an outage are lost and
    # _on_connect has to subscribe again on every reconnect.  Set this to False
    # -- together with SUBSCRIBE_QOS >= 1 and the stable client id that
    # SERVICE_NAME provides -- for a service that must not miss an input; the
    # broker then queues matching messages while it is disconnected.
    CLEAN_SESSION: bool = True

    # Incoming messages are handled on the main loop thread rather than paho's
    # network thread; see _on_message.
    MAX_INCOMING_QUEUE: int = 10000

    # How long the oldest queued message may wait for dispatch before the
    # service reports itself unhealthy.  Anything above a loop iteration means
    # nobody is draining the queue.
    INCOMING_STALL_WARN_SECONDS: float = 5.0

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

        # (topic, payload, enqueued_at); the timestamp is what makes an
        # undrained queue visible, see _incoming_stall().
        self._incoming: Deque[Tuple[str, str, float]] = deque()
        self._incoming_oldest_at: Optional[float] = None
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

        self.mqtt_handlers = [
            (topic, Handler(self, topic, fn)) for topic, fn in self.CLASS_MQTT_HANDLERS
        ]
        # Marked always_run so that a disabled service can still be re-enabled.
        self.mqtt_handlers.append(
            ("enabled", Handler(self, "enabled", self._on_enable, always_run=True))
        )
        self.mqtt_global_handlers = [
            (topic, Handler(self, topic, fn))
            for topic, fn in self.CLASS_MQTT_GLOBAL_HANDLERS
        ]

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
        check should look at that themselves.  A queue that is *currently* not
        being dispatched is a different matter: the service is connected and
        publishing, and deaf.
        """
        if not (self.is_connected and self.mqtt_thread_alive()):
            return False
        stalled, _ = self._incoming_stall()
        return stalled is None or stalled <= self.INCOMING_STALL_WARN_SECONDS

    def _incoming_stall(self) -> Tuple[Optional[float], int]:
        """How long the oldest queued message has waited, and how many wait."""
        with self._incoming_lock:
            depth = len(self._incoming)
            if self._incoming_oldest_at is None:
                return None, depth
            return monotonic() - self._incoming_oldest_at, depth

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
        self.mqtt_handlers.append((topic, Handler(self, topic, handler)))

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

        self.mqtt_global_handlers.append((topic, Handler(self, topic, handler)))

        if self.is_connected:
            self.log.info(f"Subscribing to {topic}")
            self.mqtt_client.subscribe(topic, qos=self.SUBSCRIBE_QOS)

    def _on_enable(self, payload):
        """Enable or disable dispatch of everything but this topic.

        Only the two defined payloads are acted on: treating anything else as
        "disable" means an empty retained message -- what is left behind by a
        ``mosquitto_pub -r -n`` cleanup -- silently switches the service off at
        the next start.
        """
        if payload == self.PAYLOAD_ON:
            self.enabled = True
        elif payload == self.PAYLOAD_OFF:
            self.enabled = False
        else:
            self.log.warning(
                f"Ignoring payload {payload!r} on "
                f"'{self.data_topic_prefix}enabled': expected "
                f"{self.PAYLOAD_ON!r} or {self.PAYLOAD_OFF!r}. Service stays "
                f"{'enabled' if self.enabled else 'disabled'}."
            )
            return
        self.log.info(f"set enabled to {self.enabled!r}")

    def _update_online_status(self, _):
        self.publish(
            self.willtopic, "1", retain=True, qos=self.QOS_AT_LEAST_ONCE, global_=True
        )

        # Runs on a timer regardless of what the rest of the service is doing,
        # which makes it the one place that reliably notices a queue nobody is
        # draining -- long before it reaches MAX_INCOMING_QUEUE and starts
        # discarding messages.
        stalled, depth = self._incoming_stall()
        if stalled is not None and stalled > self.INCOMING_STALL_WARN_SECONDS:
            self.log.error(
                f"{depth} incoming message(s) undispatched for {stalled:.0f}s - "
                f"is _loop_step being called?"
            )
            self.note_failure(f"incoming queue stalled for {stalled:.0f}s")

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
            yield topic, self._as_handler(topic, handler)

        for topic, handler in self.mqtt_handlers:
            yield self.data_topic_prefix + topic, self._as_handler(topic, handler)

    def _as_handler(self, topic, handler) -> Handler:
        """Normalise a handler appended to the lists directly rather than
        through :meth:`add_handler`."""
        if isinstance(handler, Handler):
            return handler
        return Handler(self, topic, handler)

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
        enqueued_at = monotonic()
        self.last_message_received_at = enqueued_at

        with self._incoming_lock:
            if len(self._incoming) >= self.MAX_INCOMING_QUEUE:
                dropped_topic, _, _ = self._incoming.popleft()
                self.log.error(
                    f"Incoming queue full ({self.MAX_INCOMING_QUEUE}); "
                    f"dropped oldest message on {dropped_topic}"
                )
                self.note_failure("incoming message queue overflowed")
            self._incoming.append((msg.topic, payload, enqueued_at))
            self._incoming_oldest_at = self._incoming[0][2]

    def _drain_incoming(self) -> int:
        """Dispatch every queued incoming message. Returns how many were handled."""
        count = 0
        while True:
            with self._incoming_lock:
                if not self._incoming:
                    self._incoming_oldest_at = None
                    return count
                topic, payload, _ = self._incoming.popleft()
                # Track the head rather than clearing only when the queue runs
                # empty: under a steady stream it may never be seen empty, and
                # a stale timestamp would look like a stall.
                self._incoming_oldest_at = (
                    self._incoming[0][2] if self._incoming else None
                )
            self._dispatch(topic, payload)
            count += 1

    def _dispatch(self, msg_topic: str, payload: str) -> None:
        handled = False
        for topic, handler in self._all_handlers():
            matched, remainder = self._match(topic, msg_topic)
            if not matched:
                continue
            handled = True
            if not self.enabled and not handler.always_run:
                self.log.debug(f"Service is disabled, ignoring '{msg_topic}'")
                continue
            # One failing handler must not prevent the others from running:
            # several inputs can share a topic, and a bad payload for one of
            # them is not a reason to drop the message for the rest.
            try:
                handler(payload, remainder)
            except Exception as e:
                self.log.exception(
                    f"Handler {handler!r} failed for topic '{msg_topic}': {e!r}"
                )
                self.note_failure(f"handler for '{msg_topic}' raised {e!r}")

        if handled:
            return

        if not self.enabled:
            self.log.debug(f"Service is disabled, ignoring '{msg_topic}'")
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
        """Run one iteration of the framework loop.

        Do not override this; override :meth:`_wait_for_work` instead.  This
        method carries an obligation of the framework -- dispatch what has
        arrived, then run the loops that are due -- and a subclass that
        replaces it drops that obligation silently.
        """
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

        self._wait_for_work(max(0.0, earliest_next_call - monotonic()))

    def _wait_for_work(self, timeout: float) -> None:
        """Block for up to ``timeout`` seconds before the next loop iteration.

        This is the part of the loop a service is meant to replace.  Services
        that are paced by an I/O source of their own -- a blocking serial read,
        a socket select, a hardware poll -- override this to wait on that
        source instead of sleeping, and keep message dispatch and loop
        scheduling intact.
        """
        sleep(timeout)

    def _warn_if_loop_step_overridden(self) -> None:
        if type(self)._loop_step is Service._loop_step:
            return
        message = (
            f"{type(self).__name__} overrides _loop_step(); override "
            f"_wait_for_work() instead. _loop_step() is the framework's own "
            f"step, and replacing it skips loop scheduling."
        )
        warnings.warn(message, DeprecationWarning, stacklevel=3)
        self.log.warning(message)

    def run(self):
        self._warn_if_loop_step_overridden()
        self.mqtt_client.loop_start()
        self._mqtt_loop_started = True
        try:
            while not self.stop:
                # Drained here and not only in _loop_step(): overriding
                # _loop_step() is a natural thing to do for a service paced by
                # its own I/O, and an override that does not chain used to stop
                # dispatch entirely -- leaving a service that stays connected,
                # keeps publishing and reports itself healthy while discarding
                # every command it receives.  _drain_incoming() returns
                # immediately on an empty queue, so doing it twice per
                # iteration costs one lock acquisition.
                self._drain_incoming()
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
