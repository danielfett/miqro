# MIQRO: MQTT Micro-Services for Python

MIQRO is a Python 3 library that aims to simplify development and operation of MQTT-based microservices, for example for 

 * transmitting sensor values,
 * controlling actors, and
 * gathering system information

 in smart home and other applications.

 ## Example

 This is a simple "echo" microservice:

 ```python
 import miqro


class EchoSvc(miqro.Service):
    SERVICE_NAME = "echo"

    @miqro.loop(seconds=10)
    def do_something(self):
        self.publish("foo", "bar")
        self.publish_json(
            "foofoo",
            {"answer": 42.01, "config_demo": self.service_config["custom_value"]},
        )

    @miqro.handle("echo")
    def echo(self, payload):
        self.publish("echo_response", payload)


echosvc = EchoSvc(add_config_file_path="examples/miqro.yml")
echosvc.run()
```

## Features

### MQTT layout

For now, the MQTT base topic for each service is `service/SERVICE_NAME/`. Services can specify message handlers for topics below this tree using the `miqro.handle` decorator. In the above example, the service is listening to `service/echo/echo`. 

Services can publish messages using the `publish` method or the `publish_json` method. In both cases, only the subtree and the payload need to be specified. For example, `self.publish("foo", "bar")` publishes the string `bar` in the MQTT topic `service/echo/foo`. 

Float values in JSON payloads (when published using `publish_json`) are rounded to a specific number of decimals, configurable using `JSON_FLOAT_PRECISION` (default 5).

Services always publish `service/SERVICE_NAME/online` as a last-will topic (`1` if the service is online, `0` if offline).

### Configuration file

A `miqro.yml` configuration file is used to define broker settings as well as service-specific configuration values. 

By default, the configuration file (with the name `miqro.yml`) is searched in the current working directory and in `/etc`. A different path can be specified using `add_config_file_path=` in the constructor.

Configuration file example: 

```yaml
broker:
  host: localhost
  port: 1883
  keepalive: 60
log_level: DEBUG
services:
  echo:
    custom_value: "Use for service-specific configuration values"
```

`broker` takes the same arguments as Paho's `connect()` (see https://www.eclipse.org/paho/index.php?page=clients/python/docs/index.php#connect-reconnect-disconnect).

`services` contains a separate section for each service (using the service's name). The contents of the service configuration can be defined freely for each service. They are available as `self.service_config` within the service.

### Loops

Using the decorator `miqro.loop`, functions can be called in regular intervals, for example to read and publish sensor values. In the code above, `do_something` is called roughly every 10 seconds. The main loop of the MIQRO library ensures that the loop functions are called consecutively. Therefore, functions block the main thread (and other functions) and should return quickly.

`miqro.loop` takes the same arguments as Python's `timedelta` (see https://docs.python.org/3/library/datetime.html#timedelta-objects). The granularity in which loops are executed is defined in `LOOP_INTERVAL` (default 0.2 seconds).

# Development

Running the tests requires `mosquitto` installed. For testing, a `mosquitto` instance on port 18883 is started.

```bash
$ pytest-3
```
