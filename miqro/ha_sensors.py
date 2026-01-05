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
        else:
            if isinstance(self.identifiers, str):
                self.identifiers = [self.identifiers]
            if self._unique_id not in self.identifiers:
                self.identifiers.append(self._unique_id)

    @property
    def entities(self):
        return self.__entities

    def add_entity(self, entity):
        self.__entities.append(entity)

    def publish_discovery(self, prefix):
        device_payload = {
            k: v
            for k, v in self.__dict__.items()
            if v is not None and not k.startswith("_")
        }

        del device_payload["service"]

        if self.via_device is not None:
            device_payload["via_device"] = self.via_device._unique_id

        payload = {
            "device": device_payload,
            "origin": {"name": f"MIQRO service {self.service.SERVICE_NAME}"},
            "availability": [
                {
                    "topic": self.service.willtopic,
                    "payload_available": "1",
                    "payload_not_available": "0",
                }
            ],
            "components": {},
        }

        for entity in self.__entities:
            payload["components"][entity.unique_id] = entity.get_discover_payload()

        topic = f"{prefix}/device/{self._unique_id}/config"
        self.service.publish_json(topic, payload, qos=1, retain=True, global_=True)


@dataclass
class EntityWithoutStateTopic:
    device: Device | None
    name: str
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
    service: "miqro.Service | None" = None

    json_attributes_topic_postfix: str | None = None
    json_attributes_template: str | None = None

    def __post_init__(self):
        # device OR service may be used, but not both
        if self.device is None and self.service is None:
            raise ValueError("Either device or service must be set for an entity")
        if self.device is not None and self.service is not None:
            raise ValueError("Only one of device or service may be set for an entity")

        if self.default_entity_id is None:
            self.default_entity_id = f"{self._component}.{clean_string(self.name)}"
        if self.unique_id is None:
            if self.device is not None:
                self.unique_id = f"{self.device._unique_id}__{self.default_entity_id.replace('.', '_')}"
            else:
                self.unique_id = f"{clean_string(self.service.SERVICE_NAME)}__{self.default_entity_id.replace('.', '_')}"

        # iterate through all attributes and find any callback functions,
        # x_topic_prefix uses the corresponding x_callback function
        for attr_name in dir(self):
            if attr_name.endswith("_topic_postfix"):
                callback_attr_name = attr_name[:-14] + "_callback"
                topic_postfix = getattr(self, attr_name)
                callback = getattr(self, callback_attr_name, None)
                if callback is not None:
                    self.device.service.add_handler(topic_postfix, callback)

        if self.device is not None:
            self.device.add_entity(self)
        else:
            self.service.ha_entities.append(self)

    def get_discover_payload(self):
        payload = {
            k: v
            for k, v in self.__dict__.items()
            if v is not None
            and not k.startswith("_")
            and k != "device"
            and k != "service"
        }
        payload["platform"] = self._component

        # delete x_callback entries from payload
        for attr_name in dir(self):
            if attr_name.endswith("_callback"):
                if attr_name in payload:
                    del payload[attr_name]

        service = self.device.service if self.device is not None else self.service
        for key, value in list(payload.items()):
            if key.endswith("_postfix"):
                full_key = key[:-8]
                payload[full_key] = f"{service.data_topic_prefix}{value}"
                del payload[key]

        return payload

    def publish_discovery(self, prefix):
        # only if device is none
        if self.device is not None:
            raise ValueError(
                "publish_discovery can only be called for entities without device"
            )

        payload = self.get_discover_payload()
        topic = f"{prefix}/{self._component}/{self.unique_id}/config"
        self.service.publish_json(topic, payload, qos=1, retain=True, global_=True)


@dataclass
class Entity(EntityWithoutStateTopic):
    state_topic_postfix: str = ""
    value_template: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.state_topic_postfix == "":
            raise ValueError("state_topic_postfix must be set")
        if self.default_entity_id is None:
            self.default_entity_id = (
                f"{self._component}.{self.state_topic_postfix.replace('/', '_')}"
            )

        super().__post_init__()


@dataclass
class Event(Entity):
    _component: str = "event"
    event_types: list | None = None

    def __post_init__(self):
        super().__post_init__()

        if self.event_type is None:
            self.event_type = self.state_topic_postfix.replace("/", "_")


@dataclass
class BinarySensor(Entity):
    _component: str = "binary_sensor"
    off_delay: int | None = None
    payload_off: str | None = None
    payload_on: str | None = None

    def __post_init__(self):
        super().__post_init__()

        service = self.device.service if self.device is not None else self.service

        if self.payload_on is None:
            self.payload_on = str(service.PAYLOAD_ON)
        if self.payload_off is None:
            self.payload_off = str(service.PAYLOAD_OFF)


@dataclass
class Sensor(Entity):
    _component: str = "sensor"
    unit_of_measurement: str | None = None
    state_class: str | None = None
    last_reset_value_template: str | None = None
    suggested_display_precision: int | None = None
    options: list | None = None

    def __post_init__(self):
        super().__post_init__()

        if self.options is not None:
            self.device_class = "enum"


@dataclass
class Switch(Entity):
    _component: str = "switch"
    optimistic: bool | None = None
    command_topic_postfix: str = ""
    command_callback: "callable | None" = None
    payload_off: str = "off"
    payload_on: str = "on"

    def __post_init__(self):
        super().__post_init__()

        if self.payload_on is None:
            self.payload_on = self.device.service.PAYLOAD_ON
        if self.payload_off is None:
            self.payload_off = self.device.service.PAYLOAD_OFF


@dataclass
class Text(Entity):
    _component: str = "text"
    optimistic: bool | None = None
    command_topic_postfix: str = ""
    command_callback: "callable | None" = None
    max: int = 255
    min: int = 0
    mode: str | None = "text"
    pattern: str | None = None
    retain: bool | None = None


@dataclass
class Number(Entity):
    _component: str = "number"
    optimistic: bool | None = None
    command_topic_postfix: str = ""
    command_callback: "callable | None" = None
    max: float | int = 100
    min: float | int = 1
    mode: str | None = None
    payload_reset: str | None = None
    retain: bool | None = None
    step: float | None = None
    unit_of_measurement: str | None = None


@dataclass
class Select(Entity):
    _component: str = "select"
    optimistic: bool | None = None
    command_topic_postfix: str = ""
    command_callback: "callable | None" = None
    retain: bool | None = None
    options: list = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()

        if not self.options:
            raise ValueError("options must be set for select entity")


@dataclass
class Button(EntityWithoutStateTopic):
    _component: str = "button"
    optimistic: bool | None = None
    command_topic_postfix: str = ""
    command_callback: "callable | None" = None
    payload_press: str = "PRESS"
    retain: bool | None = None


@dataclass
class ClimateController(EntityWithoutStateTopic):
    _component = "climate"
    optimistic: bool | None = None
    current_humidity_template: str | None = None
    current_humidity_topic_postfix: str | None = None

    current_temperature_template: str | None = None
    current_temperature_topic_postfix: str | None = None

    fan_mode_command_template: str | None = None
    fan_mode_command_topic_postfix: str | None = None
    fan_mode_command_callback: "callable | None" = None
    fan_mode_state_topic_postfix: str | None = None
    fan_mode_state_template: str | None = None
    fan_modes: list | None = None

    initial: float | None = None  # initial target temperature

    max_humidity: float | None = None
    max_temp: float | None = None
    min_humidity: float | None = None
    min_temp: float | None = None

    mode_command_template: str | None = None
    mode_command_topic_postfix: str | None = None
    mode_state_template: str | None = None
    mode_state_topic_postfix: str | None = None
    modes: list | None = None

    payload_on: str | None = None
    payload_off: str | None = None
    power_command_callback: "callable | None" = None
    power_command_topic_postfix: str | None = None
    power_command_template: str | None = None

    precision: str | None = None

    preset_mode_command_template: str | None = None
    preset_mode_command_topic_postfix: str | None = None
    preset_mode_command_callback: "callable | None" = None
    preset_mode_state_template: str | None = None
    preset_mode_state_topic_postfix: str | None = None
    preset_modes: list | None = None

    swing_horizontal_mode_command_template: str | None = None
    swing_horizontal_mode_command_topic_postfix: str | None = None
    swing_horizontal_mode_command_callback: "callable | None" = None
    swing_horizontal_mode_state_template: str | None = None
    swing_horizontal_mode_state_topic_postfix: str | None = None
    swing_horizontal_modes: list | None = None

    swing_mode_command_template: str | None = None
    swing_mode_command_topic_postfix: str | None = None
    swing_mode_command_callback: "callable | None" = None
    swing_mode_state_template: str | None = None
    swing_mode_state_topic_postfix: str | None = None
    swing_modes: list | None = None

    target_humidity_command_template: str | None = None
    target_humidity_command_topic_postfix: str | None = None
    target_humidity_command_callback: "callable | None" = None
    target_humidity_state_template: str | None = None
    target_humidity_state_topic_postfix: str | None = None

    temperature_command_topic_postfix: str | None = None
    temperature_command_template: str | None = None
    temperature_command_callback: "callable | None" = None
    temperature_state_template: str | None = None
    temperature_state_topic_postfix: str | None = None

    temperature_high_command_topic_postfix: str | None = None
    temperature_high_command_template: str | None = None
    temperature_high_command_callback: "callable | None" = None
    temperature_high_state_template: str | None = None
    temperature_high_state_topic_postfix: str | None = None

    temperature_low_command_topic_postfix: str | None = None
    temperature_low_command_template: str | None = None
    temperature_low_command_callback: "callable | None" = None
    temperature_low_state_template: str | None = None
    temperature_low_state_topic_postfix: str | None = None

    temperature_unit: str | None = None
    temp_step: float | None = None

    def __post_init__(self):
        super().__post_init__()

        # precision must be one of '0.1', '0.5', '1.0'
        if self.precision is not None:
            if self.precision not in ["0.1", "0.5", "1.0"]:
                raise ValueError("precision must be one of '0.1', '0.5', '1.0'")

        # temperature unit must be C or F
        if self.temperature_unit is not None:
            if self.temperature_unit not in ["C", "F"]:
                raise ValueError("temperature_unit must be 'C' or 'F'")


@dataclass
class DeviceTracker(EntityWithoutStateTopic):
    _component = "device_tracker"
    json_attributes_topic_postfix: str | None = None
    json_attributes_template: str | None = None
