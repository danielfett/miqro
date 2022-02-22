# MIQRO: MQTT Micro-Services for Python

MIQRO is a Python 3 library that aims to simplify development and operation of MQTT-based microservices, for example for 

 * transmitting sensor values,
 * controlling actors, and
 * gathering system information

 in smart home and other applications. 
 
 MIQRO aims at providing simple and easy-to-use APIs for common and generally boring tasks in MQTT-based services, in particular,

  * time-based loops, e.g., to retrieve and publish sensor data,
  * subscribing to topics within a service's base topic,
  * publishing in JSON and plain formats,
  * deduplication of unchanged values,
  * retrieving configuration from a system-wide configuration file,
  * storing service state.

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
    def my_echo_handler(self, payload):
        self.publish("echo_response", payload)


echosvc = EchoSvc(add_config_file_path="examples/miqro.yml")
echosvc.run()
```

## Features

### Service Creation

A MIQRO service can be installed as a linux system service using

```bash
servicefile.py --install
```

This works on Debian-based distributions using systemd.

### Subscribing

The MQTT base topic for each service is `service/SERVICE_NAME/`. Services can specify message handlers for topics below this tree using the `miqro.handle` decorator. In the above example, the service is listening to `service/echo/echo`. The handler function is called with the received payload as argument, unless the topic contains the wildcard `#`, in which case the payload and topic are passed to the handler.

Using `miqro.handle_global`, handler function for all MQTT topics (outside of the service's prefix) can be registered.

### Publishing

Services can publish messages using `publish`, `publish_json`, or `publish_json_keys` methods:

  * `publish(ext, message, ...)`: Publish to `service/SERVICE_NAME/ext`. For example, `self.publish("foo", "bar")` publishes the string `bar` in the MQTT topic `service/echo/foo`.  If `message` is a string, it will be published as-is. If it is `True` or `False`, `1` or `0` will be sent. If it is `None`, an empty string will be sent. For dictionaries or lists, `publish_json` will be called (see below). Float values are rounded to a specific number of decimals, configurable using `JSON_FLOAT_PRECISION` (default 5).
  * `publish_json(ext, message_json)`: Publish a JSON object to `service/SERVICE_NAME/ext`. For example, `self.publish_json("foo", {"answer": 42.01})` publishes the JSON object `{"answer": 42.01}` in the MQTT topic `service/echo/foo`. 
  * `publish_json_keys(message_dict, ext)`: Takes a dictionary and publishes each entry to `service/SERVICE_NAME/key` with the value as the message. In this case, `ext` is optional and added before the key. E.g., `publish_json_keys({"answer": 42.01}, "foo")` publishes the JSON object `{"answer": 42.01}` in the MQTT topic `service/echo/foo/answer`.

All three methods accept a number of arguments:
 * `retain`: if set to `True`, the message will be retained by the MQTT broker
 * `qos`: the quality of service level to use, `QOS_MAX_ONCE` (default), `QOS_AT_LEAST_ONCE` or   `QOS_EXACTLY_ONCE`
 * `only_if_changed`: Ensures, per topic, that the message is only sent if the payload has changed. This is useful for sensors that send the same value every time. If `True`, the message will only be sent if the payload has changed. If set to a `datetime.timedelta` object, the message will be sent if either the payload has changed or the defined time span has elapsed.
 * `global_`: If set, the service's prefix will not be added. In this case `topic` equals the topic to which the message will be sent.

Services always publish `service/SERVICE_NAME/online` as a last-will topic (`1` if the service is online, `0` if offline).

### Loops

Using the decorator `miqro.loop`, functions can be called in regular intervals, for example to read and publish sensor values. In the code above, `do_something` is called roughly every 10 seconds. The main loop of the MIQRO library ensures that the loop functions are called consecutively. Therefore, functions block the main thread (and other functions) and should return quickly.

`miqro.loop` takes the same arguments as Python's `timedelta` (see https://docs.python.org/3/library/datetime.html#timedelta-objects). The granularity in which loops are executed is defined in `LOOP_INTERVAL` (default 0.2 seconds).

### State File

Set `USE_STATE_FILE` to `True` to enable a persistent service state file. This file is intended to store the service's state to restore it after a restart. The file is handled automatically by MIQRO. The contents are available for reading and writing in a dict-like interface under `self.state` within the service. `self.state.save()` must be called to persist the state. The methods `set_path(*keys, value)` and `get_path(*keys, default)` are available to quickly read and update nested dictionary structures. For example, `state.set_path("foo", "bar", 42)` sets `state["foo"]["bar"] = 42` and creates `state["foo"]` and `state["foo"]["bar"]` in case they do not exist already. `state.get_path("foo", "bar", 23)` returns `state["foo"]["bar"]` or `23` if the key does not exist.

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

  some_other_service:
    has_some:
      custom:
        - "configuration values"
```

`broker` takes the same arguments as Paho's `connect()` (see https://www.eclipse.org/paho/index.php?page=clients/python/docs/index.php#connect-reconnect-disconnect).

`services` contains a separate section for each service (using the service's name). The contents of the service configuration can be defined freely for each service. They are available as `self.service_config` within the service.

# Development

Running the tests requires `mosquitto` installed. For testing, a `mosquitto` instance on port 18883 is started.

```bash
$ pytest-3
```
