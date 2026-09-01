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

## Installing

Via PIP:

```bash
$ pip3 install miqro
```

Via this repository:

```bash
$ git clone https://github.com/danielfett/miqro.git
$ cd miqro
$ python3 setup.py install
```


## Example

This is a simple "echo" microservice:

```python
import miqro


class EchoSvc(miqro.Service):
    SERVICE_NAME = "echo"

    @miqro.handle("echo")
    def my_echo_handler(self, payload):
        self.publish("echo_response", payload)

    @miqro.loop(seconds=10)
    def do_something(self):
        self.publish("foo", "bar")
        self.publish_json(
            "foofoo",
            {"answer": 42.01, "config_demo": self.service_config["custom_value"]},
        )


echosvc = EchoSvc()
echosvc.run()
```

## Usage

MIQRO services are created by subclassing `miqro.Service`. The class property `SERVICE_NAME` must be set to a unique name for the service. The MQTT base topic and the system service name will be derived from this name - as described below in more detail.

A minimal MIQRO service looks as follows:

```python
import miqro


class Minimal(miqro.Service):
    SERVICE_NAME = "minimal"

echosvc = Minimal()
echosvc.run()
```

This service will publish the value `1` to the topic `service/minimal/online` and set a "last will message" to let other services know when the service is down.

Create a custom `__init__` method to initialize the service if needed:

```python
import miqro


class MinimalWithInit(miqro.Service):
    SERVICE_NAME = "minimal"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        print ("Minimal service now initializeing...")
        ...

minimalsvc = Minimal()
minimalsvc.run()
```


### Subscribing

The MQTT base topic for each service is `service/SERVICE_NAME/`. Services can specify message handlers for topics below this tree using the `miqro.handle` decorator. In the `EchoSvc` example above, the service is listening to `service/echo/echo`. The handler function is called with the received payload as argument, unless the topic contains the wildcard `#`, in which case the payload and topic postfix are passed to the handler.

Using `miqro.handle_global`, handler function for all MQTT topics (outside of the service's prefix) can be registered.

Handlers can also be registered at runtime with `add_handler(topic_ext, handler)` and `add_global_handler(topic, handler)`. A plain function or lambda is called with the service as its first argument (`handler(service, payload)`), a bound method without it (`handler(payload)`); handlers for a topic ending in `#` take the topic remainder as a further argument. The convention is resolved when the handler is registered, so a handler with a signature that cannot be called raises `TypeError` there rather than at the first message that arrives for it.

#### Message delivery and reconnects

Incoming messages are queued by the MQTT network thread and dispatched on the service's own loop thread, so a handler that raises cannot kill the connection and handlers never run concurrently with loops. A queue that is not being dispatched -- typically because a subclass replaced `_loop_step` instead of `_wait_for_work`, see [Loops](#loops) -- is logged, counted as a failure and makes `healthy()` return `False` after `INCOMING_STALL_WARN_SECONDS`.

`SUBSCRIBE_QOS` (default `1`, at-least-once) governs delivery to a *connected* service only. What happens while a service is disconnected is decided by `CLEAN_SESSION`, which defaults to `True`: the broker keeps nothing for the service between connections, so commands published during a restart or a network outage are lost, and subscriptions are re-established on every reconnect. A service that must not miss an input should set `CLEAN_SESSION = False`; together with `SUBSCRIBE_QOS >= 1` and the stable client id taken from `SERVICE_NAME`, the broker then queues matching messages while the service is away and delivers them on reconnect.

#### Enabling and disabling a service

Every service subscribes to `service/SERVICE_NAME/enabled`. Publishing `0` there stops the service from handling incoming messages (its loops keep running and it keeps publishing); `1` switches handling back on. Any other payload is ignored with a warning, so that an empty retained message cannot silently disable a service.

#### Examples

```python
    @miqro.handle("powersave")
    def handle_powersave(self, payload):
        print(f"Set powersave mode to {payload}")

    @miqro.handle("sms/send/#")
    def send_sms(self, payload, topic_postfix):
        print(f"Sending SMS message to {topic_postfix}") 
        print(f"Text: {payload}")

    @miqro.handle_global("radio_receiver/devices/Oregon-v1/temperature_C")
    def handle_temperature(self, payload):
        print(f"Received temperature value: {payload}")

    @miqro.handle_global("radio_receiver/devices/TPMS/#")
    def handle_tpms(self, payload, topic_postfix):
        print(f"Received radio message from TPMS sensor {topic_postfix}: {payload}")
```

### Publishing

#### Functions

Services can publish messages using `publish`, `publish_json`, or `publish_json_keys` methods:

##### `publish(ext, message, ...)`

Publish to `service/SERVICE_NAME/ext`. 

For example, `self.publish("foo", "bar")` publishes the string `bar` in the MQTT topic `service/echo/foo`.  

Works with various message types:

 * If `message` is a string, it will be published as-is. 
 * If it is `True` or `False`, `1` or `0` will be sent. 
 * If it is `None`, an empty string will be sent. 
 * Float values are rounded to a specific number of decimals, configurable using `JSON_FLOAT_PRECISION` (default 5).
 * For dictionaries or lists, `publish_json` will be called (see below). 

##### `publish_json(ext, message_json)`

Publish a JSON object to `service/SERVICE_NAME/ext`.

For example, `self.publish_json("foo", {"answer": 42.01})` publishes the JSON object `{"answer": 42.01}` in the MQTT topic `service/echo/foo`. 

##### `publish_json_keys(message_dict, ext)`

Takes a dictionary and publishes each entry to `service/SERVICE_NAME/key` with the value as the message. 

In this case, `ext` is optional and added before the key. E.g., `publish_json_keys({"answer": 42.01}, "foo")` publishes the JSON object `{"answer": 42.01}` in the MQTT topic `service/echo/foo/answer`.

#### Arguments

All three methods accept a number of arguments:
 * `retain`: if set to `True`, the message will be retained by the MQTT broker
 * `qos`: the quality of service level to use, `QOS_MAX_ONCE` (default), `QOS_AT_LEAST_ONCE` or   `QOS_EXACTLY_ONCE`
 * `only_if_changed`: Ensures, per topic, that the message is only sent if the payload has changed. This is useful for sensors that send the same value every time. If `True`, the message will only be sent if the payload has changed. If set to a `datetime.timedelta` object, the message will be sent if either the payload has changed or the defined time span has elapsed.
 * `global_`: If set, the service's prefix will not be added. In this case `ext` equals the topic to which the message will be sent.

Services always publish `service/SERVICE_NAME/online` as a last-will topic (`1` if the service is online, `0` if offline).

### Loops

Using the decorator `miqro.loop`, functions can be called in regular intervals, for example to read and publish sensor values. 

In the `EchoSvc` above, `do_something` is called every 10 seconds. The main loop of the MIQRO library ensures that the loop functions are called consecutively. Therefore, functions block the main thread (and other functions) and should return quickly.

Looped functions that return `False` will not be called again.

`miqro.loop` takes the same arguments as [Python's `timedelta`](https://docs.python.org/3/library/datetime.html#timedelta-objects). 

MIQRO outputs information about the execution times of loops to the log file in regular intervals.

#### Services with their own I/O source

Between loop runs, the main loop waits in `_wait_for_work(timeout)`, which by default sleeps. A service that is paced by an I/O source of its own -- a blocking serial read, a socket select, a hardware poll -- should override `_wait_for_work` to wait on that source instead:

```python
    def _wait_for_work(self, timeout):
        self.serial.timeout = timeout
        self.read_one_frame()
```

Do not override `_loop_step`: it is the framework's own step (dispatch incoming messages, run the loops that are due), and replacing it skips loop scheduling. Overriding it raises a `DeprecationWarning` when the service starts.

### State File

Set `USE_STATE_FILE` to `True` to enable a persistent service state file. This file is intended to store the service's state to restore it after a restart. The file is handled automatically by MIQRO. The contents are available for reading and writing in a dict-like interface under `self.state` within the service. `self.state.save()` must be called to persist the state. The methods `set_path(*keys, value)` and `get_path(*keys, default)` are available to quickly read and update nested dictionary structures. 

For example, `state.set_path("foo", "bar", 42)` sets `state["foo"]["bar"] = 42` and creates `state["foo"]` and `state["foo"]["bar"]` in case they do not exist already. `state.get_path("foo", "bar", 23)` returns `state["foo"]["bar"]` or `23` if the key does not exist.

The state file is not optimized for performance and therefore should only be updated infrequently (e.g., when a setting changes).

### Configuration file

A `miqro.yml` configuration file is used to define broker settings as well as any service-specific configuration values.

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

`services` contains a separate section for each service (using the service's name). The contents of the service configuration can be defined freely for each service. They are available as `self.service_config` within the service:

```python
class SomeOtherService(miqro.Service):
    SERVICE_NAME = "some_other_service"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.some_custom_connection = SomeCustomConnector(
            self.service_config["has_some"]["custom"][0],
        )
```

#### Authentication and TLS

If a username or a username and a password are required to connect to the broker, set them using the `username` and `password` keys in the `auth` section of the configuration file, e.g.:


```yaml
broker:
  host: remote-broker.example.com
  port: 1883
  keepalive: 60

auth: 
  username: "foo"
  password: "bar"

```
(Note: Quotation marks around configuration values in the YAML file are optional as long as the value does not start with a number.)

TLS settings can be defined using the section `tls`:

```yaml
broker:
    host: remote-broker.example.com
    port: 8883
    keepalive: 60

tls:
    ca_certs: /path/to/ca.pem
    certfile: /path/to/client.pem
    keyfile: /path/to/key.pem
    cert_reqs: CERT_REQUIRED
    tls_version: PROTOCOL_TLS
    ciphers: None
```

The elements in `tls` are passed to Paho's `tls_set()` (see https://www.eclipse.org/paho/index.php?page=clients/python/docs/index.php#tls_set). For `cert_reqs` and `tls_version`, use the constants defined in the `ssl` module (e.g., `CERT_REQUIRED`).

### System Service Creation

A MIQRO service can be installed as a linux system service using

```bash
servicefile.py --install
```

This works on Debian-based distributions using systemd.

# Development

Running the tests requires `mosquitto` installed. For testing, a `mosquitto` instance on port 18883 is started.

```bash
$ pytest-3
```
