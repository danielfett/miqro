from pathlib import Path
from typing import Dict, Optional
from pydantic import BaseModel
from enum import Enum
import yaml


class LogLevel(str, Enum):
    debug = "DEBUG"
    info = "INFO"
    warning = "WARNING"
    error = "ERROR"
    critical = "CRITICAL"


class Broker(BaseModel):
    host: str = "localhost"
    port: Optional[int] = 1883
    keepalive: Optional[int] = 60


class Auth(BaseModel):
    username: str
    password: Optional[str] = None


class VerifyMode(Enum):
    CERT_NONE = "CERT_NONE"
    CERT_OPTIONAL = "CERT_OPTIONAL"
    CERT_REQUIRED = "CERT_REQUIRED"


class ProtocolVersion(Enum):
    PROTOCOL_TLS_CLIENT = "PROTOCOL_TLS_CLIENT"
    PROTOCOL_TLS_SERVER = "PROTOCOL_TLS_SERVER"
    PROTOCOL_TLSv1 = "PROTOCOL_TLSv1"
    PROTOCOL_TLSv1_1 = "PROTOCOL_TLSv1_1"
    PROTOCOL_TLSv1_2 = "PROTOCOL_TLSv1_2"
    
class TLS(BaseModel):
    ca_certs: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    cert_reqs: VerifyMode = VerifyMode.CERT_REQUIRED
    tls_version: ProtocolVersion = ProtocolVersion.PROTOCOL_TLSv1
    ciphers: Optional[str] = None


class Configuration(BaseModel):
    broker: Broker
    services: Dict[str, Dict]
    log_level: Optional[LogLevel] = LogLevel.info
    tls: Optional[TLS] = None
    auth: Optional[Auth] = None


def load_config_from_yaml(yamlfile: Path):
    with yamlfile.open('r') as f:
        return Configuration(**yaml.safe_load(f))