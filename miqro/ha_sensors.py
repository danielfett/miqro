from dataclasses import dataclass, field
import re


def clean_string(raw: str) -> str:
    """
    MQTT Discovery protocol only allows [a-zA-Z0-9_-]
    """
    result = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    return result.strip("-")

@dataclass
class Device:
    service: "miqro.Service"
    name: str
    model: str | None = None
    manufacturer: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None
    identifiers: list[str] | str | None = None
    connections: list[tuple] | None = None
    suggested_area: str | None = None
    via_device: "Device | None" = None

    _unique_id: str | None = None

    __entities: list["Entity"] = field(default_factory=list)

    def __post_init__(self):
        self.service.ha_devices.append(self)
        self._unique_id = f"{self.service.SERVICE_NAME}_{clean_string(self.name)}"
        if self.identifiers is None:
            self.identifiers = [self._unique_id]


    @property
    def entities(self):
        return self.__entities

    def add_entity(self, entity):
        self.__entities.append(entity)  

    def publish_discovery(self, prefix):
        device_payload = {k:v for k,v in self.__dict__.items() if v is not None and not k.startswith('_')}

        del device_payload["service"]

        if self.via_device is not None:
            device_payload["via_device"] = self.via_device._unique_id

        payload = {
            "device": device_payload,
            "origin": {"name": f"MIQRO service {self.service.SERVICE_NAME}"},
            "availability": [{
                "topic": self.service.willtopic,
                "payload_available": "1",
                "payload_not_available": "0"
            }],
            "components": {}
        }

        for entity in self.__entities:
            payload["components"][entity.unique_id] = entity.get_discover_payload()

        print (payload)
            
        topic = f"{prefix}/device/{self._unique_id}/config"
        self.service.publish_json(topic, payload, qos=1, retain=True, global_=True)


@dataclass
class Entity:
    device: Device
    name: str
    state_topic_postfix: str
    display_name: str | None = None
    device_class: str | None = None
    enabled_by_default: bool | None = None
    entity_category: str | None = None
    expire_after: int | None = None
    force_update: bool | None = None
    icon: str | None = None
    default_entity_id: str | None = None
    qos: int | None = None
    unique_id: str | None = None


    def __post_init__(self):
        if self.default_entity_id is None:
            self.default_entity_id = f"{self._component}.{self.state_topic_postfix.replace('/', '_')}"
        if self.unique_id is None:
            self.unique_id = f"{self.device._unique_id}__{self.default_entity_id.replace('.', '_')}"
        self.device.add_entity(self)

    def get_discover_payload(self):
        payload = {k:v for k,v in self.__dict__.items() if v is not None and not k.startswith('_') and k != 'device'}
        payload["platform"] = self._component
        
        payload["state_topic"] = f"{self.device.service.data_topic_prefix}{self.state_topic_postfix}"
        del payload["state_topic_postfix"]
        return payload

@dataclass
class EntityWithCommand(Entity):
    command_topic_postfix: str = ""
    callback: "callable | None" = None

    def __post_init__(self):
        super().__post_init__()
        if self.command_topic_postfix == "":
            raise ValueError("command_topic_postfix must be set")
        if self.callback is not None:
            self.device.service.add_handler(self.command_topic_postfix, self.callback)


    def get_discover_payload(self):
        payload = super().get_discover_payload()
        payload["command_topic"] = f"{self.device.service.data_topic_prefix}{self.command_topic_postfix}"
        del payload["command_topic_postfix"]
        if "callback" in payload:
            del payload["callback"]
        return payload


@dataclass
class BinarySensor(Entity):
    _component: str = "binary_sensor"
    off_delay: int | None = None
    payload_off: str | None = None
    payload_on: str | None = None

    def __post_init__(self):
        super().__post_init__()

        if self.payload_on is None:
            self.payload_on = str(self.device.service.PAYLOAD_ON)
        if self.payload_off is None:
            self.payload_off = str(self.device.service.PAYLOAD_OFF)

@dataclass
class Sensor(Entity):
    _component: str = "sensor"
    unit_of_measurement: str | None = None
    state_class: str | None = None
    value_template: str | None = None
    last_reset_value_template: str | None = None
    suggested_display_precision: int | None = None
    options: list | None = None

    def __post_init__(self):
        super().__post_init__()

        if self.options is not None:
            self.device_class = "enum"

@dataclass
class Switch(EntityWithCommand):
    _component: str = "switch"
    optimistic: bool | None = None
    payload_off: str = "off"
    payload_on: str = "on"

    def __post_init__(self):
        super().__post_init__()

        if self.payload_on is None:
            self.payload_on = self.device.service.PAYLOAD_ON
        if self.payload_off is None:
            self.payload_off = self.device.service.PAYLOAD_OFF


@dataclass
class Text(EntityWithCommand):
    _component: str = "text"
    max: int = 255
    min: int = 0
    mode: str | None = "text"
    pattern: str | None = None
    retain: bool | None = None


@dataclass
class Number(EntityWithCommand):
    _component: str = "number"
    max: float | int = 100
    min: float | int = 1
    mode: str | None = None
    optimistic: bool | None = None
    payload_reset: str | None = None
    retain: bool | None = None
    step: float | None = None
    unit_of_measurement: str | None = None

