import argparse
import inspect
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Callable, Dict, List, Optional
import random

import paho.mqtt.client as mqtt
from yaml import safe_load


class Loop:
    fn: Callable
    interval: timedelta
    next_call: Optional[datetime] = None

    STARTUP_VARIABILITY = 30 # up to x seconds

    def __init__(self, fn, interval):
        self.fn = fn
        self.interval = interval
        self.next_call = datetime.now() + timedelta(seconds=random.randint(0, self.STARTUP_VARIABILITY))

    def run_if_needed(self, instance, now):
        if self.next_call is None or now >= self.next_call:
            if self.fn(instance) is not False:
                self.next_call = now + self.interval


# args/kwargs can be anything that the constructor of timedelta accepts
def loop(*args, **kwargs):
    # See https://stackoverflow.com/questions/2366713/can-a-decorator-of-an-instance-method-access-the-class
    class class_decorator:
        def __init__(self, fn):
            self.fn = fn

        def __set_name__(self, owner, name):
            ml = Loop(self.fn, timedelta(*args, **kwargs))

            if owner.LOOPS == []:
                owner.LOOPS = []
            owner.LOOPS.append(ml)

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

    log: logging.Logger
    config: Dict
    service_config: Dict
    data_topic_prefix: str
    mqtt_client: mqtt
    ignore_recv_topics: List[str]
    stop = False

    def __init__(self, add_config_file_path=None):
        if add_config_file_path is not None:
            self.CONFIG_FILE_PATHS.insert(0, Path(add_config_file_path))

        self.prepare_logger()
        self.read_config()

        self.mqtt_client = mqtt.Client(self.SERVICE_NAME)
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_disconnect = self.on_disconnect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.enable_logger(self.mqtt_log)
        self.mqtt_client.will_set(self.willtopic, "0", retain=True)
        self.mqtt_client.connect_async(**self.config["broker"])

        self.enabled = True

        self.ignore_recv_topics = []

        self.log.info("started")

    def prepare_logger(self):
        log_handler = logging.StreamHandler(sys.stderr)
        log_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(name)s  %(levelname)s \t%(message)s")
        )
        log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

        self.log = logging.getLogger(self.SERVICE_NAME)
        self.mqtt_log = logging.getLogger(self.SERVICE_NAME)
        self.mqtt_log.setLevel(logging.INFO)

    def read_config(self):
        for path in self.CONFIG_FILE_PATHS:
            if path.exists():
                self.log.debug(f"Using configuration file at {path}")
                with path.open("r") as f:
                    self.config = safe_load(f)
                    break
        else:
            raise Exception(
                "Config file not found; searched paths: "
                + ", ".join(map(str, self.CONFIG_FILE_PATHS))
            )

        if self.SERVICE_NAME not in self.config["services"]:
            self.log.warning(
                "Service configuration not found in 'services' section of configuration file. Using empty configuration."
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
        client.publish(self.willtopic, "1", retain=True)
        self.log.info(f"Subscribing to ...")
        for topic in list(self.MQTT_HANDLERS.keys()) + ["enable"]:
            self.log.info(f"  - {self.data_topic_prefix}{topic}")
            client.subscribe(self.data_topic_prefix + topic)

    def on_disconnect(self, client, userdata, rc):
        self.log.warning(f"MQTT disconnected, rc={rc}")

    def on_message(self, client, userdata, msg):
        stripped_topic = msg.topic[len(self.data_topic_prefix) :]
        payload = str(msg.payload.decode("ascii")).strip()
        self.log.debug(
            f"Received MQTT message on topic {stripped_topic} containing {payload}"
        )

        if stripped_topic == "enable":
            if payload == "1":
                self.enabled = True
            else:
                self.enabled = False
            self.log.info(f"set enabled to {self.enabled!r}")
            return

        for topic_ext, handler in self.MQTT_HANDLERS.items():
            if topic_ext == stripped_topic:
                handler(self, payload)
                return

        if getattr(self, "handle_message", None) is not None and self.handle_message(
            stripped_topic, payload
        ):
            return

        if stripped_topic not in self.ignore_recv_topics:
            self.log.error(f"Unhandled topic! {self.ignore_recv_topics}")

    def publish(self, ext, message):
        topic = self.data_topic_prefix + ext
        if ext not in self.ignore_recv_topics:
            self.ignore_recv_topics.append(ext)
        message = self.round_floats(message)
        self.log.debug(f"MQTT publish: {topic}: {message}")
        try:
            self.mqtt_client.publish(topic, message)
        except Exception as e:
            self.log.exception(e)

    def publish_json(self, ext, message_json):
        self.publish(ext, json.dumps(self.round_floats(message_json)))

    def publish_json_keys(self, message_dict, ext=None):
        for key, value in message_dict.items():
            if ext:
                key = ext + "/" + key
            self.publish(key, value)

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
    parser.add_argument("--config", "-c", help="config file")
    parser.add_argument(
        "--install", action="store_true", help="Setup this service as a systemd unit."
    )

    args = parser.parse_args()

    if not args.install:
        service(getattr(parser, "config", None)).run()
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
