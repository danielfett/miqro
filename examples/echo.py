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
