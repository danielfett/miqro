import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Callable, Dict, List, Optional, Tuple, Union

import paho.mqtt.client as mqtt
from yaml import FullLoader, dump, load


class Loop:
    fn: Callable
    interval: timedelta
    next_call: Optional[datetime] = None
    stat_call_count: int = 0
    stat_cumulative_duration: float = 0.0

    def __init__(self, fn: Callable, interval: timedelta, start: bool = True):
        self.fn = fn

        if not isinstance(interval, timedelta):
            raise Exception("interval must be provided as timedelta!")

        self.interval = interval
        if start:
            self.next_call = datetime.now()

    def run_if_needed(self, instance: "Service") -> Optional[datetime]:
        now = datetime.now()
        if self.next_call and now >= self.next_call:
            if self.fn(instance) is not False:
                self.next_call = now + self.interval
                self.stat_call_count += 1
                self.stat_cumulative_duration += (datetime.now() - now).total_seconds()
                return self.next_call
            else:
                self.stop()
        return None

    def start(self, delayed=False):
        if delayed:
            self.next_call = datetime.now() + self.interval
        else:
            self.next_call = datetime.now()

    def stop(self):
        self.next_call = None

    def restart(self, delayed=False):
        self.start(delayed=delayed)

    def get_remaining(self) -> Optional[timedelta]:
        if self.next_call is None:
            return None
        return self.next_call - datetime.now()

    def stat_reset(self):
        self.stat_call_count = 0
        self.stat_cumulative_duration = 0.0

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

        try:
            if not self._file.exists():
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._data = {}
            else:
                with self._file.open() as f:
                    self._data = load(f, Loader=FullLoader)
        except PermissionError as e:
            service.log.error(e)
            self._data = {}

        self.service.log.debug(f"State: Loaded {self._data}")

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
        self.service.log.debug(f"State: Saving {self._data}")
        with self._file.open("w") as f:
            dump(self._data, f)


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

        self.mqtt_client = mqtt_client_cls(self.SERVICE_NAME)
        if "auth" in self.config:
            self.mqtt_client.username_pw_set(
                **self.config["auth"]
            )
        if "tls" in self.config:
            self.mqtt_client.tls_set(
                **self._make_tls_config(self.config['tls'])
            )
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
        paths = self.CONFIG_FILE_PATHS
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
            client.subscribe(topic)

        self.mqtt_client.publish(self.willtopic, "1", retain=True)

    def _on_disconnect(self, client, userdata, rc):
        self.log.warning(f"MQTT disconnected, rc={rc}")
        self.is_connected = False

    def add_handler(self, topic, handler):
        self.mqtt_handlers.append((topic, handler))

        if self.is_connected:
            self.log.info(f"Subscribing to {self.data_topic_prefix + topic}")
            self.mqtt_client.subscribe(self.data_topic_prefix + topic)

    def add_global_handler(self, topic, handler):
        """
        Add a global handler for a topic.
        The handler will be called with the topic and payload as arguments.
        """

        self.mqtt_global_handlers.append((topic, handler))

        if self.is_connected:
            self.log.info(f"Subscribing to {topic}")
            self.mqtt_client.subscribe(topic)

    def _on_enable(self, payload):
        if payload == "1":
            self.enabled = True
        else:
            self.enabled = False
        self.log.info(f"set enabled to {self.enabled!r}")
        return

    def _update_online_status(self, _):
        self.publish(self.willtopic, "1", retain=True, global_=True)

        assert self.LOOPS

        self.log.info("Loop stats:")
        for l in self.LOOPS:
            call_count, average_call_duration, load, is_critical = l.stat_get()
            l.stat_reset()
            self.log.info(
                f" - {l} called {call_count} times, average duration {average_call_duration}s, load={int(load*100)}%"
            )
            if is_critical:
                self.log.warn(
                    f"   {l} takes, on average, longer to execute ({average_call_duration}s) than defined interval ({l.interval.total_seconds()})"
                )

    def _all_handlers(self):
        for topic, handler in self.mqtt_global_handlers:
            yield topic, handler

        for topic, handler in self.mqtt_handlers:
            yield self.data_topic_prefix + topic, handler

    def _on_message(self, client, userdata, msg):
        payload = str(msg.payload.decode("utf-8", errors="replace")).strip()
        self.log.debug(
            f"Received MQTT message on topic {msg.topic} containing {payload}"
        )

        handled = False
        for topic, handler in self._all_handlers():
            if "#" in topic:
                prefix = topic[:-1]
                self.log.debug(f"matching {prefix} against {msg.topic}")
                if msg.topic.startswith(prefix):
                    handler(self, payload, msg.topic[len(prefix) :])
                    handled = True
            else:
                if topic == msg.topic:
                    handler(self, payload)
                    handled = True

        if handled:
            return

        if self.handle_message(msg.topic, payload):
            return

        self.log.error(
            f"Unhandled topic '{msg.topic}', registered handlers for: {', '.join(k for (k, v) in self._all_handlers())}"
        )

    def handle_message(self, topic, payload):
        return False

    def publish(
        self,
        ext,
        message,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed: Union[bool, timedelta] = False,
        global_=False,
    ):
        topic = (self.data_topic_prefix + ext) if not global_ else ext
        # if ext not in self.ignore_recv_topics:
        #    self.ignore_recv_topics.append(ext)
        if type(message) == type(True):  # type is boolean
            message = 1 if message else 0
        elif message is None:
            message = ""
        elif type(message) in [dict, list]:
            self.publish_json(ext, message, retain, qos, only_if_changed)
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
            if last_message == message and last_time and last_time + only_if_changed > now:
                self.log.debug(
                    f"{topic} not changed since {only_if_changed.total_seconds()}s, not publishing."
                )
                return
            else:
                self.last_key_values[topic] = (message, now)

        self.log.debug(f"MQTT publish: {topic}: {message}")
        try:
            self.mqtt_client.publish(topic, message, retain=retain, qos=qos)
        except Exception as e:
            self.log.exception(e)

    def publish_json(
        self,
        ext,
        message_json,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed: Union[bool, timedelta] = False,
        global_=False,
    ):
        self.publish(
            ext,
            json.dumps(self._round_floats(message_json)),
            retain=retain,
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
                    only_if_changed=only_if_changed,
                    global_=global_,
                )

    def _loop_step(self):
        assert self.LOOPS is not None

        earliest_next_call = datetime.now() + timedelta(seconds=self.MAX_LOOP_INTERVAL)
        for loop in self.LOOPS:
            next_call = loop.run_if_needed(self)
            if next_call:
                earliest_next_call = min(earliest_next_call, next_call)

        sleep(max(0, (earliest_next_call - datetime.now()).total_seconds()))

    def run(self):
        self.mqtt_client.loop_start()
        while not self.stop:
            self._loop_step()
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
        "--install-as-user", "-u", help="Install service for specified user (instead of root)", default="root"
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

    filename = Path(sys.argv[0])

    systemd_service_name = f"miqro_{service.SERVICE_NAME}"

    executable = sys.executable
    if not executable:
        executable = "/usr/bin/env python3"

    systemd_unit_file = f"""
[Unit]
Description={service.SERVICE_NAME} MIQRO microservice
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=20
User={cli_args.install_as_user}
ExecStart={executable} {filename.resolve()}

[Install]
WantedBy=multi-user.target
"""

    systemd_path = Path("/etc/systemd/system/", systemd_service_name + ".service")
    systemd_path.write_text(systemd_unit_file)
    systemd_path.chmod(0o644)
    print(
        f"Service successfully installed as {systemd_service_name}.\nYou can now enable the service to start on boot by running:\n sudo systemctl enable {systemd_service_name}\n... and run the service:\n sudo systemctl start {systemd_service_name}"
    )
