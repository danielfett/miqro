import pytest
import subprocess
import miqro

from threading import Event, Thread

MOSQUITTO_BIN = "mosquitto"
MOSQUITTO_PORT = "18883"  # avoid collisions with other instance running
CONFIG_FILE_PATH = "test/miqro.yml"
TIMEOUT = 2


@pytest.fixture(scope="module")
def mqtt_broker():
    broker = subprocess.Popen([MOSQUITTO_BIN, "-p", MOSQUITTO_PORT, "-v"])
    yield broker
    broker.kill()
    broker.communicate(2)


def test_connect(mqtt_broker):
    done = Event()

    class TestSvc(miqro.Service, Thread):
        def __init__(self):
            Thread.__init__(self)
            miqro.Service.__init__(self, CONFIG_FILE_PATH)

        def on_connect(self, *args, **kwargs):
            miqro.Service.on_connect(self, *args, **kwargs)
            done.set()

    testsvc = TestSvc()
    testsvc.start()

    try:
        if not done.wait(TIMEOUT):
            raise Exception("Test timed out")
    finally:
        testsvc.stop = True
        testsvc.join()


def test_simple_loop(mqtt_broker):
    done = Event()

    class TestSvc(miqro.Service, Thread):
        def __init__(self):
            Thread.__init__(self)
            miqro.Service.__init__(self, CONFIG_FILE_PATH)

        @miqro.loop(seconds=0.1)
        def loop_exec(self):
            done.set()

    testsvc = TestSvc()
    testsvc.start()

    try:
        if not done.wait(TIMEOUT):
            raise Exception("Test timed out")
    finally:
        testsvc.stop = True
        testsvc.join()


def test_two_clients_two_loops(mqtt_broker):
    done1 = Event()
    done2 = Event()
    fail = Event()

    class TestSvc1(miqro.Service, Thread):
        SERVICE_NAME = "test1"

        def __init__(self):
            Thread.__init__(self)
            miqro.Service.__init__(self, CONFIG_FILE_PATH)

        @miqro.loop(seconds=0.1)
        def loop_exec(self):
            if done1.is_set():
                fail.set()
            done1.set()
            self.stop = True

    class TestSvc2(miqro.Service, Thread):
        SERVICE_NAME = "test2"

        def __init__(self):
            Thread.__init__(self)
            miqro.Service.__init__(self, CONFIG_FILE_PATH)

        @miqro.loop(seconds=0.1)
        def loop_exec(self):
            if done2.is_set():
                fail.set()
            done2.set()
            self.stop = True

    testsvc1 = TestSvc1()
    testsvc1.start()
    testsvc2 = TestSvc2()
    testsvc2.start()

    try:
        if not done1.wait(TIMEOUT):
            raise Exception("Test timed out")
        if not done2.wait(TIMEOUT):
            raise Exception("Test timed out")
        assert not fail.is_set()
    finally:
        testsvc1.stop = True
        testsvc1.join()
        testsvc2.stop = True
        testsvc2.join()


def test_publish_and_receive(mqtt_broker):
    done1 = Event()
    done2 = Event()

    class TestSvcSender(miqro.Service, Thread):
        SERVICE_NAME = "testsender"
        JSON_FLOAT_PRECISION = 1

        def __init__(self):
            Thread.__init__(self)
            miqro.Service.__init__(self, CONFIG_FILE_PATH)

        @miqro.loop(seconds=0.1)
        def loop_exec_string(self):
            self.publish("string", "normal")

        @miqro.loop(seconds=0.2)
        def loop_exec_json(self):
            self.publish_json("json", {"some_float": 1.2333336})

        @miqro.handle("string")
        def recv_string(self, payload):
            if payload == "normal":
                done1.set()
            else:
                self.stop = True

        @miqro.handle("json")
        def recv_json(self, payload):
            if payload == '{"some_float": 1.2}':
                done2.set()
            else:
                self.stop = True

    testsvcsender = TestSvcSender()
    testsvcsender.start()

    try:
        if not done1.wait(TIMEOUT):
            raise Exception("Test timed out")
        if not done2.wait(TIMEOUT):
            raise Exception("Test timed out")

    finally:
        testsvcsender.stop = True
        testsvcsender.join()
