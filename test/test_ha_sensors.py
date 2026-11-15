import pytest

from miqro import ha_sensors


class FakeService:
    SERVICE_NAME = "demo"
    PAYLOAD_ON = 1
    PAYLOAD_OFF = 0
    willtopic = "service/demo/online"
    data_topic_prefix = "service/demo/"

    def __init__(self):
        self.ha_devices = []
        self.ha_entities = []
        self.handlers = []

    def add_handler(self, topic, callback):
        self.handlers.append(topic)


def sensor(device, name, topic):
    return ha_sensors.Sensor(device=device, name=name, state_topic_postfix=topic)


def test_device_without_children():
    service = FakeService()
    device = ha_sensors.Device(service, name="Box")
    sensor(device, "Temperatur", "temp")

    payload = device.build_discovery_payload()
    assert "child_devices" not in payload
    assert payload["device"] == {"name": "Box", "identifiers": ["demo_Box"]}
    assert list(payload["components"]) == ["demo_Box__sensor_Temperatur"]
    assert service.ha_devices == [device]


def test_child_device_payload():
    service = FakeService()
    device = ha_sensors.Device(service, name="Leiste", manufacturer="ACME")
    sensor(device, "Leistung", "power")
    outlet = ha_sensors.ChildDevice(device, name="Steckdose 1")
    sensor(outlet, "Strom", "outlet1/current")

    payload = device.build_discovery_payload()
    # only the parent is published, the child is part of its message
    assert service.ha_devices == [device]
    assert list(payload["components"]) == ["demo_Leiste__sensor_Leistung"]
    assert payload["child_devices"] == [
        {
            "identifiers": ["demo_Steckdose-1"],
            "name": "Steckdose 1",
            "components": {
                "demo_Steckdose-1__sensor_Strom": {
                    "name": "Strom",
                    "default_entity_id": "sensor.Strom",
                    "unique_id": "demo_Steckdose-1__sensor_Strom",
                    "state_topic": "service/demo/outlet1/current",
                    "platform": "sensor",
                }
            },
        }
    ]
    # device information other than name and identifiers stays on the parent
    assert payload["device"]["manufacturer"] == "ACME"
    assert "child_devices" not in payload["device"]


def test_child_device_keeps_unique_ids_of_moved_entities():
    service = FakeService()
    before = ha_sensors.Device(service, name="Sensors Service")
    old = sensor(before, "Nickwinkel", "level/pitch")

    service = FakeService()
    after = ha_sensors.Device(service, name="Sensors Service")
    child = ha_sensors.ChildDevice(after, name="Nivellierung", keep_parent_unique_ids=True)
    moved = sensor(child, "Nickwinkel", "level/pitch")

    assert moved.unique_id == old.unique_id
    payload = after.build_discovery_payload()
    assert payload["child_devices"][0]["identifiers"] == ["demo_Nivellierung"]
    assert list(payload["child_devices"][0]["components"]) == [old.unique_id]


def test_existing_device_becomes_child_without_changing_entities():
    service = FakeService()
    standalone = ha_sensors.Device(service, name="Alarm Frost")
    old = ha_sensors.Switch(
        device=standalone, name="frost enabled", state_topic_postfix="frost/enabled/state",
        command_topic_postfix="frost/enabled/command",
    )

    service = FakeService()
    parent = ha_sensors.Device(service, name="Alarmanlage")
    child = ha_sensors.ChildDevice(parent, name="Alarm Frost")
    new = ha_sensors.Switch(
        device=child, name="frost enabled", state_topic_postfix="frost/enabled/state",
        command_topic_postfix="frost/enabled/command",
    )

    assert child.identifiers == standalone.identifiers
    assert new.unique_id == old.unique_id


def test_child_device_identifiers_and_validation():
    service = FakeService()
    device = ha_sensors.Device(service, name="Box")
    child = ha_sensors.ChildDevice(device, name="Teil", identifiers="extern-1")
    assert child.identifiers == ["extern-1", "demo_Teil"]
    assert child.service is service

    with pytest.raises(ValueError):
        ha_sensors.ChildDevice(child, name="Enkel")


def test_entities_are_listed_once():
    service = FakeService()
    device = ha_sensors.Device(service, name="Box")
    entity = sensor(device, "Temperatur", "temp")
    assert device.entities == [entity]
