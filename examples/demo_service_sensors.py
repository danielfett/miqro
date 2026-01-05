import miqro
from time import sleep


class SensorDemoService(miqro.Service):
    SERVICE_NAME = "echo"

    demo_counter = 42

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        ha_device_one = miqro.ha_sensors.Device(
            self, "Demo Device Miqro One", sw_version="demo-0.1"
        )
        ha_device_two = miqro.ha_sensors.Device(
            self,
            "Demo Device Miqro Two",
            sw_version="demo-0.1",
            via_device=ha_device_one,
        )

        self.binary_sensor_demo = miqro.ha_sensors.BinarySensor(
            ha_device_one,
            "Beispiel für Binärsensor",
            state_topic_postfix="hello/binary",
        )

        self.general_sensor_demo = miqro.ha_sensors.Sensor(
            ha_device_two,
            "Hello Sensor",
            state_topic_postfix="hello/sensor",
            options=["one", "two", "three"],
        )

        self.switch_demo = miqro.ha_sensors.Switch(
            ha_device_one,
            "Hello Switch",
            state_topic_postfix="hello/switch/state",
            command_topic_postfix="hello/switch/command",
            optimistic=True,
        )

        self.text_demo = miqro.ha_sensors.Text(
            ha_device_two,
            "Hello Text",
            state_topic_postfix="hello/text/state",
            command_topic_postfix="hello/text/command",
            pattern="^[0-9][a-c]$",
        )

        self.number_demo = miqro.ha_sensors.Number(
            ha_device_one,
            "Hello Number",
            state_topic_postfix="hello/number/state",
            command_topic_postfix="hello/number/command",
            step=3,
            command_callback=self.handle_number_command,
        )

        # Demo for entity without device
        self.sensor_no_device = miqro.ha_sensors.Sensor(
            None,
            "No Device Sensor",
            state_topic_postfix="hello/no_device/sensor",
            service=self,
        )
        # publish needs to be called manually for entities without device
        self.sensor_no_device.publish_discovery("homeassistant")

    @miqro.loop(seconds=10)
    def do_something(self):

        # Sensor topics can be used as normal topics in the publish call,
        # or the sensor itself can be used instead of the topic.
        # The result is the same.
        self.publish("hello/binary", True)
        self.publish("hello/sensor", "one")
        # self.publish("hello/switch/state", True) this doesn't work; remember to use the proper payload when using this format
        self.publish("hello/switch/state", "on")
        sleep(2)
        self.publish(self.binary_sensor_demo, False)
        self.publish(self.general_sensor_demo, "two")
        self.publish(self.switch_demo, False)

        self.demo_counter += 1
        self.publish(self.sensor_no_device, self.demo_counter)

    # Register a callback for a topic that is a sensor's command_topic
    # This is the "old" way of doing things, the newer alternative
    # is to use a callback function.
    @miqro.handle("hello/text/command")
    def handle_text_command(self, payload):
        self.log.info("Received text update: " + payload)

    @miqro.handle("hello/switch/command")
    def handle_switch_command(self, payload):
        self.log.info("Received switch update: " + payload)

    def handle_number_command(self, _, payload):
        self.log.info("Received number command: " + payload)


demosvc = SensorDemoService(add_config_file_path="examples/miqro.yml")
demosvc.run()
