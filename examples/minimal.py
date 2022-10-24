import miqro


class Minimal(miqro.Service):
    SERVICE_NAME = "minimal"

echosvc = Minimal(add_config_file_path="examples/miqro.yml")
echosvc.run()