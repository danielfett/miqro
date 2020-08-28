import paho.mqtt.client as mqtt
from pathlib import Path
from yaml import safe_load
import logging
from typing import List, Dict, Callable, Optional
import json
import sys
from dataclasses import dataclass
from datetime import timedelta, datetime


@dataclass
class Loop:
    fn: Callable
    interval: timedelta
    next_call: Optional[datetime] = None

    def run_if_needed(self, instance, now):
        if self.next_call is None or now >= self.next_call:
            self.fn(instance)
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
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.enable_logger(self.log)
        self.mqtt_client.connect(**self.config["broker"])

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

    def on_connect(self, client, userdata, flags, rc):
        self.log.info("MQTT connected")
        client.subscribe(f"{self.data_topic_prefix}#")

    def on_message(self, client, userdata, msg):
        stripped_topic = msg.topic[len(self.data_topic_prefix) :]
        payload = str(msg.payload.decode('ascii')).strip()
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
        self.log.debug(f"MQTT publish: {topic}: {message}")
        self.mqtt_client.publish(topic, message)

    def publish_json(self, ext, message_json):
        self.publish(ext, json.dumps(self.round_floats(message_json)))

    def run(self):
        willtopic = self.data_topic_prefix + "online"
        self.mqtt_client.publish(willtopic, "1", retain=True)
        self.mqtt_client.will_set(willtopic, "0", retain=True)

        while not self.stop:
            self.mqtt_client.loop(timeout=self.LOOP_INTERVAL)
            now = datetime.now()
            for loop in self.LOOPS:
                loop.run_if_needed(self, now)

    def round_floats(self, o):
        if isinstance(o, float):
            return round(o, self.JSON_FLOAT_PRECISION)
        if isinstance(o, dict):
            return {k: self.round_floats(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.round_floats(x) for x in o]
        return o
