from typing import Literal

from pydantic import BaseModel


class NodeSnapshot(BaseModel):
    node_id: int
    connected: bool
    nmt_state: str
    status_note: str | None = None
    testvar1_uint32: int
    testvar2_uint16: int
    heartbeat_ms: int | None = None
    last_error: str | None = None
    source: Literal["mock", "canopen"]


class NodeWriteRequest(BaseModel):
    value: int


class NodeAddRequest(BaseModel):
    node_id: int


class NodeSdoWriteRequest(BaseModel):
    index: int
    subindex: int = 0
    value: int


class NodeDomainRequest(BaseModel):
    index: int
    subindex: int = 0
    hex_data: str


class NodeDomainResponse(BaseModel):
    node_id: int
    index: int
    subindex: int
    hex_data: str
    length: int


class LogEntry(BaseModel):
    timestamp: str
    level: str
    source: str
    message: str


class ConnectionStatus(BaseModel):
    connected: bool
    channel: str
    bustype: str
    bitrate: int


class CanDeviceInfo(BaseModel):
    label: str
    category: str = "Native CAN"
    supported: bool = True
    bustype: str
    channel: str
    description: str | None = None
    hwid: str | None = None
    manufacturer: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None


class ConnectionDeviceRequest(BaseModel):
    bustype: str
    channel: str


class NodeSyncConfig(BaseModel):
    node_id: int
    enabled: bool
    cob_id: int
    period_us: int
    producer: bool


class NodeSyncWriteRequest(BaseModel):
    enabled: bool
    cob_id: int = 0x80
    period_us: int = 100000


class NodeTpdoConfig(BaseModel):
    node_id: int
    enabled: bool
    cob_id: int
    transmission_type: int
    inhibit_time: int
    event_timer: int
    sync_start_value: int


class NodeTpdoWriteRequest(BaseModel):
    enabled: bool
    cob_id: int
    transmission_type: int
    inhibit_time: int = 0
    event_timer: int = 0
    sync_start_value: int = 0


class NodeHeartbeatConfig(BaseModel):
    node_id: int
    producer_time_ms: int


class NodeHeartbeatWriteRequest(BaseModel):
    producer_time_ms: int


class NodeNmtConfig(BaseModel):
    node_id: int
    state: str


class NodeNmtWriteRequest(BaseModel):
    state: str
