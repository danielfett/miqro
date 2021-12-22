import argparse
import inspect
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
from yaml import safe_load


class Loop:
    fn: Callable
    interval: timedelta
    next_call: Optional[datetime] = None

    def __init__(self, fn, interval, start=True):
        self.fn = fn
        self.interval = interval
        if start:
            self.next_call = datetime.now()

    def run_if_needed(self, instance, now):
        if self.next_call and now >= self.next_call:
            if self.fn(instance) is not False:
                self.next_call = now + self.interval

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

    def add_to(self, service):
        if not service.LOOPS:
            service.LOOPS = []
        service.LOOPS.append(self)
        return self


# args/kwargs can be anything that the constructor of timedelta accepts
def loop(*args, **kwargs):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner, name):
            Loop(self.fn, timedelta(*args, **kwargs)).add_to(owner)

    return class_decorator


def handle(topic_ext):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner, name):
            if owner.MQTT_HANDLERS == {}:
                owner.MQTT_HANDLERS = {}
            owner.MQTT_HANDLERS[topic_ext] = self.fn

    return class_decorator


def handle_global(topic):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner, name):
            if owner.MQTT_GLOBAL_HANDLERS == {}:
                owner.MQTT_GLOBAL_HANDLERS = {}
            owner.MQTT_GLOBAL_HANDLERS[topic] = self.fn

    return class_decorator


def accept_json(fn):
    def actual_fn(self, arg):
        return fn(self, **json.loads(arg))

    return actual_fn


class Service:
    SERVICE_NAME: str = "none"
    CONFIG_FILE_PATHS: List[Path] = [Path("miqro.yml"), Path("/etc/miqro.yml")]
    JSON_FLOAT_PRECISION: int = 4
    LOOP_INTERVAL: float = 0.2
    LOOPS: List[Loop] = []
    MQTT_HANDLERS: Dict = {}
    MQTT_GLOBAL_HANDLERS: Dict = {}
    MQTT_ONLINE_UPDATE_INTERVAL: int = 180

    QOS_MAX_ONCE = 0
    QOS_AT_LEAST_ONCE = 1
    QOS_EXACTLY_ONCE = 2

    log: logging.Logger
    config: Dict
    service_config: Dict
    data_topic_prefix: str
    mqtt_client: mqtt
    stop = False

    def __init__(
        self, add_config_file_path=None, log_level=logging.DEBUG, mqtt_client_cls=mqtt.Client
    ):
        if add_config_file_path is not None:
            self.CONFIG_FILE_PATHS.insert(0, Path(add_config_file_path))

        self.prepare_logger(log_level)
        self.read_config()

        self.last_key_values = {}

        self.mqtt_client = mqtt_client_cls(self.SERVICE_NAME)
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_disconnect = self.on_disconnect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.enable_logger(self.mqtt_log)
        self.mqtt_client.will_set(self.willtopic, "0", retain=True)
        self.mqtt_client.connect_async(**self.config["broker"])

        self.enabled = True

        # self.ignore_recv_topics = []

        self.log.info("started")

    def __str__(self):
        return self.SERVICE_NAME

    def prepare_logger(self, log_level):
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

    def read_config(self):
        for path in self.CONFIG_FILE_PATHS:
            if path.exists():
                self.log.debug(f"Using configuration file at {path}")
                with path.open("r") as f:
                    self.config = safe_load(f)
                    break
            else:
                self.log.debug(f"NOT using configuration file at {path}")
        else:
            raise Exception(
                "Config file not found; searched paths: "
                + ", ".join(map(str, self.CONFIG_FILE_PATHS))
            )

        if self.SERVICE_NAME not in self.config["services"]:
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

    def on_connect(self, client, userdata, flags, rc):
        self.log.info(f"MQTT connected, client={client}, userdata={userdata}, rc={rc}")
        self.log.info(f"Subscribing to ...")

        for topic, _ in self._all_handlers():
            self.log.info(f"  - {topic}")
            client.subscribe(topic)

        self.mqtt_client.publish(self.willtopic, "1", retain=True)

    def on_disconnect(self, client, userdata, rc):
        self.log.warning(f"MQTT disconnected, rc={rc}")

    @handle("enable")
    def on_enable(self, payload):
        if payload == "1":
            self.enabled = True
        else:
            self.enabled = False
        self.log.info(f"set enabled to {self.enabled!r}")
        return

    def add_handler(self, topic, handler):
        self.MQTT_HANDLERS[topic] = handler

        if self.mqtt_client and self.mqtt_client.is_connected():
            self.log.info(f"Subscribing to {self.data_topic_prefix + topic}")
            self.mqtt_client.subscribe(self.data_topic_prefix + topic)

    def add_global_handler(self, topic, handler):
        """
        Add a global handler for a topic.
        The handler will be called with the topic and payload as arguments.
        """
        self.MQTT_GLOBAL_HANDLERS[topic] = handler

        if self.mqtt_client and self.mqtt_client.is_connected():
            self.log.info(f"Subscribing to {topic}")
            self.mqtt_client.subscribe(topic)

    @loop(seconds=MQTT_ONLINE_UPDATE_INTERVAL)
    def update_online_status(self):
        try:
            self.mqtt_client.publish(self.willtopic, "1", retain=True)
        except Exception as e:
            self.log.exception(e)

    def _all_handlers(self):
        for topic, handler in self.MQTT_GLOBAL_HANDLERS.items():
            yield topic, handler

        for topic, handler in self.MQTT_HANDLERS.items():
            yield self.data_topic_prefix + topic, handler

    def on_message(self, client, userdata, msg):
        payload = str(msg.payload.decode("ascii")).strip()
        self.log.debug(
            f"Received MQTT message on topic {msg.topic} containing {payload}"
        )

        for topic, handler in self._all_handlers():
            if "#" in topic:
                prefix = topic[:-1]
                self.log.debug(f"matching {prefix} against {msg.topic}")
                if msg.topic.startswith(prefix):
                    handler(self, payload, msg.topic[len(prefix) :])
                    return
            else:
                if topic == msg.topic:
                    handler(self, payload)
                    return

        if getattr(self, "handle_message", None) is not None and self.handle_message(
            msg.topic, payload
        ):
            return

        self.log.error(f"Unhandled topic! {msg.topic}")

    def publish(
        self, ext, message, retain=False, qos=QOS_MAX_ONCE, only_if_changed=False
    ):
        topic = self.data_topic_prefix + ext
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
            message = self.round_floats(message)

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
            if last_message == message and last_time + only_if_changed > now:
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
        self, ext, message_json, retain=False, qos=QOS_MAX_ONCE, only_if_changed=False
    ):
        self.publish(
            ext,
            json.dumps(self.round_floats(message_json)),
            retain=retain,
            only_if_changed=only_if_changed,
        )

    def publish_json_keys(
        self,
        message_dict,
        ext=None,
        retain=False,
        qos=QOS_MAX_ONCE,
        only_if_changed=False,
    ):
        for key, value in message_dict.items():
            if ext:
                key = ext + "/" + key
            self.publish(key, value, retain=retain, only_if_changed=only_if_changed)

    def run(self):
        self.mqtt_client.loop_start()
        while not self.stop:
            now = datetime.now()
            for loop in self.LOOPS:
                loop.run_if_needed(self, now)
            sleep(self.LOOP_INTERVAL)
        self.mqtt_client.loop_stop()

    def round_floats(self, o):
        if isinstance(o, float):
            return round(o, self.JSON_FLOAT_PRECISION)
        if isinstance(o, dict):
            return {k: self.round_floats(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.round_floats(x) for x in o]
        return o


def run(service):
    parser = argparse.ArgumentParser(
        description=f"{service.SERVICE_NAME} MIQRO service"
    )
    parser.add_argument("--config", "-c", help="config file", default=None)
    parser.add_argument(
        "--install", action="store_true", help="Setup this service as a systemd unit."
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
            DebugMQTTClient if cli_args.mqtt_debug_prefix else None,
        ).run()
        return

    filename = Path(inspect.getfile(service))

    systemd_service_name = f"miqro_{service.SERVICE_NAME}"

    systemd_unit_file = f"""
[Unit]
Description={service.SERVICE_NAME} MIQRO microservice
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=20
User=root
ExecStart=/usr/bin/env python3 {filename.resolve()}

[Install]
WantedBy=multi-user.target
"""

    systemd_path = Path("/etc/systemd/system/", systemd_service_name + ".service")
    systemd_path.write_text(systemd_unit_file)
    systemd_path.chmod(0o644)
    print(
        f"Service successfully installed as {systemd_service_name}.\nYou can now enable the service to start on boot by running:\n sudo systemctl enable {systemd_service_name}\n... and run the service:\n sudo systemctl start {systemd_service_name}"
    )
