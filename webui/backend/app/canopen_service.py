import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import settings
from .models import (
    ConnectionStatus,
    LogEntry,
    NodeDomainResponse,
    NodeHeartbeatConfig,
    NodeSnapshot,
    NodeSyncConfig,
    NodeTpdoConfig,
)

try:
    import canopen
except ImportError:  # pragma: no cover
    canopen = None


class LogBufferHandler(logging.Handler):
    def __init__(self, service, source: str) -> None:
        super().__init__()
        self._service = service
        self._source = source

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._service.append_log(
            level=record.levelname,
            source=self._source,
            message=message,
        )


HEARTBEAT_STATES = {
    0x00: "BOOTUP",
    0x04: "STOPPED",
    0x05: "OPERATIONAL",
    0x7F: "PRE-OPERATIONAL",
}

SYNC_PRODUCER_BIT = 0x40000000
COB_ID_VALID_BIT = 0x80000000
COB_ID_MASK = 0x1FFFFFFF

class CANopenService:
    def __init__(self) -> None:
        self._network = None
        self._nodes = {}
        self._node_ids = list(settings.node_ids)
        self._snapshots: dict[int, NodeSnapshot] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._started = False
        self._logs: deque[LogEntry] = deque(maxlen=200)
        self._state_lock = threading.Lock()
        self._logging_ready = False
        self._logger = logging.getLogger("webui.backend")
        self._heartbeat_times: dict[int, float] = {}
        self._heartbeat_states: dict[int, int] = {}
        self._heartbeat_timeout_seconds = 1.0
        self._offline_logged: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._discovery_pending: set[int] = set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._setup_logging()
        self.append_log("INFO", "webui", "Starting CANopen WebUI service")
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.disconnect()
        self.append_log("INFO", "webui", "Stopped CANopen WebUI service")

    async def connect(self) -> ConnectionStatus:
        async with self._lock:
            if self._network is not None:
                return self.connection_status()
            await asyncio.to_thread(self._connect_sync)
            return self.connection_status()

    async def disconnect(self) -> ConnectionStatus:
        async with self._lock:
            await asyncio.to_thread(self._disconnect_sync)
            return self.connection_status()

    def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            connected=self._network is not None,
            channel=settings.channel,
            bustype=settings.bustype,
            bitrate=settings.bitrate,
        )

    def _setup_logging(self) -> None:
        if self._logging_ready:
            return
        self._logging_ready = True

        formatter = logging.Formatter("%(name)s: %(message)s")

        canopen_logger = logging.getLogger("canopen")
        canopen_logger.setLevel(logging.INFO)
        canopen_handler = LogBufferHandler(self, "python-canopen")
        canopen_handler.setFormatter(formatter)
        canopen_logger.addHandler(canopen_handler)

        can_logger = logging.getLogger("can")
        can_logger.setLevel(logging.INFO)
        can_handler = LogBufferHandler(self, "python-can")
        can_handler.setFormatter(formatter)
        can_logger.addHandler(can_handler)

        webui_handler = LogBufferHandler(self, "webui")
        webui_handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(webui_handler)
        self._logger.propagate = False

    def append_log(self, level: str, source: str, message: str) -> None:
        with self._state_lock:
            self._logs.append(
                LogEntry(
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    level=level,
                    source=source,
                    message=message,
                )
            )

    async def list_logs(self) -> list[LogEntry]:
        with self._state_lock:
            return list(self._logs)

    async def clear_logs(self) -> None:
        with self._state_lock:
            self._logs.clear()

    async def add_node(self, node_id: int) -> NodeSnapshot:
        async with self._lock:
            if node_id < 1 or node_id > 127:
                raise ValueError("Node ID must be between 1 and 127")
            if node_id in self._node_ids:
                raise ValueError(f"Node {node_id} already exists")
            if len(self._node_ids) >= 127:
                raise ValueError("CANopen node list is full (max 127 nodes)")

            self._node_ids.append(node_id)
            self._node_ids.sort()

            if self._network is not None:
                snapshot = self._make_placeholder_snapshot(
                    node_id,
                    "OFFLINE",
                    "Node added, waiting for heartbeat/bootup",
                )
                self._snapshots[node_id] = snapshot
                self.append_log("INFO", "webui", f"Added node{node_id} to configuration, waiting for heartbeat")
                return snapshot

            snapshot = self._make_placeholder_snapshot(node_id, "DISCONNECTED", "Node added, connect bus to query data")
            self._snapshots[node_id] = snapshot
            self.append_log("INFO", "webui", f"Added node{node_id} to configuration")
            return snapshot

    async def remove_node(self, node_id: int) -> None:
        async with self._lock:
            if node_id not in self._node_ids:
                raise KeyError(f"Unknown node: {node_id}")

            if self._network is not None and node_id in self._nodes:
                await asyncio.to_thread(self._remove_connected_node_sync, node_id)

            self._node_ids = [existing for existing in self._node_ids if existing != node_id]
            self._snapshots.pop(node_id, None)
            with self._state_lock:
                self._heartbeat_times.pop(node_id, None)
                self._heartbeat_states.pop(node_id, None)
                self._offline_logged.discard(node_id)
            self.append_log("INFO", "webui", f"Removed node{node_id}")

    def record_heartbeat(self, node_id: int, state_code: int) -> None:
        state_name = HEARTBEAT_STATES.get(state_code, f"UNKNOWN({hex(state_code)})")
        now = time.monotonic()
        should_log = False
        should_discover = False
        with self._state_lock:
            previous_state = self._heartbeat_states.get(node_id)
            self._heartbeat_times[node_id] = now
            self._heartbeat_states[node_id] = state_code
            if node_id in self._offline_logged:
                self._offline_logged.remove(node_id)
                should_log = True
            elif previous_state != state_code:
                should_log = True
            if (
                self._network is not None
                and 1 <= node_id <= 127
                and node_id not in self._nodes
                and node_id not in self._discovery_pending
            ):
                self._discovery_pending.add(node_id)
                should_discover = True
        if should_log:
            self.append_log("INFO", "python-can", f"Heartbeat node={node_id} state={state_name}")
        if should_discover:
            self.append_log("INFO", "webui", f"Discovered node{node_id} from {state_name}, adding to configuration")
            self._schedule_discovery(node_id)

    def _schedule_discovery(self, node_id: int) -> None:
        if self._loop is None or self._network is None:
            with self._state_lock:
                self._discovery_pending.discard(node_id)
            return
        self._loop.call_soon_threadsafe(asyncio.create_task, self._discover_node(node_id))

    async def _discover_node(self, node_id: int) -> None:
        try:
            async with self._lock:
                if self._network is None or node_id in self._nodes:
                    return
                preconfigured = node_id in self._node_ids
                if not preconfigured and len(self._node_ids) >= 127:
                    self.append_log("ERROR", "webui", f"Auto-discovery skipped for node{node_id}: node list is full")
                    return

                if not preconfigured:
                    self._node_ids.append(node_id)
                    self._node_ids.sort()
                try:
                    snapshot = await asyncio.to_thread(self._add_connected_node_sync, node_id)
                except Exception as exc:
                    if not preconfigured:
                        self._node_ids = [existing for existing in self._node_ids if existing != node_id]
                    self.append_log(
                        "ERROR",
                        "webui",
                        f"Node activation failed for node{node_id}: {exc.__class__.__name__}: {exc}",
                    )
                    return

                self._snapshots[node_id] = snapshot
                if preconfigured:
                    self.append_log("INFO", "webui", f"Activated configured node{node_id} from heartbeat")
        finally:
            with self._state_lock:
                self._discovery_pending.discard(node_id)

    def _make_heartbeat_callback(self):
        def callback(can_id: int, data: bytes, timestamp: float) -> None:
            del timestamp
            if not data:
                return
            node_id = can_id - 0x700
            if node_id < 1 or node_id > 127:
                return
            self.record_heartbeat(node_id=node_id, state_code=data[0])

        return callback

    def _make_tpdo1_callback(self, node_id: int):
        def callback(message) -> None:
            try:
                value_u32 = int(message["testvar1_uint32"].phys)
                value_u16 = int(message["testvar2_uint16"].phys)
            except Exception as exc:
                self.append_log("ERROR", "python-canopen", f"TPDO1 decode failed node={node_id}: {exc}")
                return

            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._apply_tpdo1_update, node_id, value_u32, value_u16)

        return callback

    def _apply_tpdo1_update(self, node_id: int, value_u32: int, value_u16: int) -> None:
        snapshot = self._snapshots.get(node_id)
        if snapshot is None:
            return
        hb_online, hb_state = self._get_heartbeat_status(node_id)
        self._snapshots[node_id] = snapshot.model_copy(
            update={
                "connected": hb_online,
                "nmt_state": hb_state if hb_online else "OFFLINE",
                "testvar1_uint32": value_u32,
                "testvar2_uint16": value_u16,
                "last_error": None,
            }
        )

    def _get_heartbeat_status(self, node_id: int) -> tuple[bool, str]:
        with self._state_lock:
            last_time = self._heartbeat_times.get(node_id)
            state_code = self._heartbeat_states.get(node_id)
            already_logged = node_id in self._offline_logged

        if last_time is None or (time.monotonic() - last_time) > self._heartbeat_timeout_seconds:
            if not already_logged:
                with self._state_lock:
                    self._offline_logged.add(node_id)
                self.append_log(
                    "ERROR",
                    "python-can",
                    f"Heartbeat timeout node={node_id} timeout={self._heartbeat_timeout_seconds:.1f}s",
                )
            return False, "OFFLINE"

        state_name = HEARTBEAT_STATES.get(state_code, "UNKNOWN")
        return True, state_name

    def _connect_sync(self) -> None:
        if settings.mock:
            self.append_log("WARNING", "python-canopen", "Mock mode is disabled in strict real-data mode")
            return

        if canopen is None:
            self.append_log("ERROR", "python-canopen", "python-canopen is not installed")
            return

        eds_path = self._resolve_eds_path(settings.eds_path)
        self.append_log("INFO", "python-canopen", f"Using EDS path: {eds_path}")
        network = canopen.Network()
        nodes = {}

        try:
            heartbeat_callback = self._make_heartbeat_callback()
            for node_id in self._node_ids:
                node = canopen.RemoteNode(node_id, str(eds_path))
                network.add_node(node)
                nodes[node_id] = node

            network.connect(
                bustype=settings.bustype,
                channel=settings.channel,
                bitrate=settings.bitrate,
            )
            for node_id in range(1, 128):
                network.subscribe(0x700 + node_id, heartbeat_callback)
            self._heartbeat_callback = heartbeat_callback
            for node_id in self._node_ids:
                try:
                    nodes[node_id].tpdo.read()
                    tpdo1_callback = self._make_tpdo1_callback(node_id)
                    nodes[node_id]._webui_tpdo1_callback = tpdo1_callback
                    nodes[node_id].tpdo[1].add_callback(tpdo1_callback)
                except Exception as exc:
                    self.append_log(
                        "ERROR",
                        "python-canopen",
                        f"TPDO1 subscribe failed node={node_id}: {exc}",
                    )
            self._network = network
            self._nodes = nodes
            self._snapshots = {
                node_id: self._read_node_snapshot(node_id, node) for node_id, node in nodes.items()
            }
            self.append_log(
                "INFO",
                "python-can",
                f"Connected CAN bus bustype={settings.bustype} channel={settings.channel} bitrate={settings.bitrate}",
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                network.disconnect()
            self._network = None
            self._nodes = {}
            self._snapshots = {
                node_id: self._make_placeholder_snapshot(
                    node_id,
                    "DISCONNECTED",
                    f"CANopen connect failed: {exc.__class__.__name__}: {exc}",
                )
                for node_id in self._node_ids
            }
            self.append_log(
                "ERROR",
                "python-canopen",
                f"CANopen connect failed: {exc.__class__.__name__}: {exc}",
            )

    def _disconnect_sync(self) -> None:
        if self._network is not None:
            sync = getattr(self._network, "sync", None)
            if sync is not None:
                with contextlib.suppress(Exception):
                    sync.stop()
            with contextlib.suppress(Exception):
                self._network.disconnect()
            self.append_log("INFO", "python-can", f"Disconnected CAN bus channel={settings.channel}")
        self._network = None
        self._nodes = {}
        self._snapshots = {
            node_id: self._refresh_snapshot_status(
                self._make_placeholder_snapshot(node_id, "DISCONNECTED", "Bus disconnected")
            )
            for node_id in self._node_ids
        }
        with self._state_lock:
            self._heartbeat_times.clear()
            self._heartbeat_states.clear()
            self._offline_logged.clear()

    def _resolve_eds_path(self, configured_path: Path) -> Path:
        if configured_path.is_absolute():
            return configured_path
        base_dir = Path(__file__).resolve().parent
        return (base_dir / configured_path).resolve()

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                self.append_log("ERROR", "webui", f"Refresh loop failed: {exc.__class__.__name__}: {exc}")
            await asyncio.sleep(settings.poll_interval)

    async def refresh(self) -> None:
        async with self._lock:
            if not self._nodes:
                return

            self._snapshots = {
                node_id: self._refresh_snapshot_status(snapshot)
                for node_id, snapshot in self._snapshots.items()
            }

    def _read_node_snapshot(self, node_id: int, node) -> NodeSnapshot:
        try:
            value_u32 = int(node.sdo["testvar1_uint32"].phys)
            value_u16 = int(node.sdo["testvar2_uint16"].phys)
            heartbeat_ms = int(node.sdo[0x1017].raw)
            hb_online, hb_state = self._get_heartbeat_status(node_id)
            nmt_state = hb_state if hb_online else "OFFLINE"
            return NodeSnapshot(
                node_id=node_id,
                connected=hb_online,
                nmt_state=nmt_state,
                testvar1_uint32=value_u32,
                testvar2_uint16=value_u16,
                heartbeat_ms=heartbeat_ms,
                source="canopen",
            )
        except Exception as exc:
            self.append_log("ERROR", "python-canopen", f"Node {node_id} snapshot read failed: {exc}")
            return self._make_placeholder_snapshot(node_id, "UNKNOWN", str(exc))

    def _make_placeholder_snapshot(self, node_id: int, state: str, error: str | None = None) -> NodeSnapshot:
        return NodeSnapshot(
            node_id=node_id,
            connected=False,
            nmt_state=state,
            testvar1_uint32=0,
            testvar2_uint16=0,
            heartbeat_ms=None,
            last_error=error,
            source="canopen",
        )

    def _refresh_snapshot_status(self, snapshot: NodeSnapshot) -> NodeSnapshot:
        hb_online, hb_state = self._get_heartbeat_status(snapshot.node_id)
        if not hb_online:
            return snapshot.model_copy(
                update={
                    "connected": False,
                    "nmt_state": "OFFLINE",
                    "testvar1_uint32": 0,
                    "testvar2_uint16": 0,
                    "heartbeat_ms": None,
                }
            )
        return snapshot.model_copy(
            update={
                "connected": True,
                "nmt_state": hb_state,
            }
        )

    async def list_nodes(self) -> list[NodeSnapshot]:
        async with self._lock:
            return [
                self._snapshots.get(node_id, self._make_placeholder_snapshot(node_id, "DISCONNECTED"))
                for node_id in self._node_ids
            ]

    async def write_sdo(self, node_id: int, index: int, subindex: int, value: int) -> NodeSnapshot:
        async with self._lock:
            if node_id not in self._snapshots:
                raise KeyError(f"Unknown node: {node_id}")

            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")

            await asyncio.to_thread(self._write_sdo_sync, self._nodes[node_id], index, subindex, value)
            snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, self._nodes[node_id])

            self._snapshots[node_id] = snapshot
            self.append_log(
                "INFO",
                "python-canopen",
                f"SDO write node={node_id} index=0x{index:04X}:{subindex} value={value}",
            )
            return snapshot

    async def read_heartbeat_config(self, node_id: int) -> NodeHeartbeatConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            return await asyncio.to_thread(self._read_heartbeat_config_sync, node_id, self._nodes[node_id])

    async def write_heartbeat_config(self, node_id: int, producer_time_ms: int) -> NodeHeartbeatConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            await asyncio.to_thread(self._write_heartbeat_config_sync, self._nodes[node_id], producer_time_ms)
            config = await asyncio.to_thread(self._read_heartbeat_config_sync, node_id, self._nodes[node_id])
            snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, self._nodes[node_id])
            self._snapshots[node_id] = snapshot
            self.append_log(
                "INFO",
                "python-canopen",
                f"Heartbeat config node={node_id} producer_time_ms={config.producer_time_ms}",
            )
            return config

    async def read_node_values(self, node_id: int) -> NodeSnapshot:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, self._nodes[node_id])
            self._snapshots[node_id] = snapshot
            self.append_log("INFO", "python-canopen", f"SDO read node={node_id} values refreshed")
            return snapshot

    @staticmethod
    def _write_sdo_sync(node, index: int, subindex: int, value: int) -> None:
        if subindex == 0:
            node.sdo[index].raw = value
            return
        node.sdo[index][subindex].raw = value

    @staticmethod
    def _read_heartbeat_config_sync(node_id: int, node) -> NodeHeartbeatConfig:
        return NodeHeartbeatConfig(node_id=node_id, producer_time_ms=int(node.sdo[0x1017].raw))

    @staticmethod
    def _write_heartbeat_config_sync(node, producer_time_ms: int) -> None:
        node.sdo[0x1017].raw = int(producer_time_ms)

    async def read_domain(self, node_id: int, index: int, subindex: int) -> NodeDomainResponse:
        async with self._lock:
            if node_id not in self._snapshots:
                raise KeyError(f"Unknown node: {node_id}")

            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")

            payload = await asyncio.to_thread(self._read_domain_sync, self._nodes[node_id], index, subindex)

            self.append_log(
                "INFO",
                "python-canopen",
                f"Domain read node={node_id} index=0x{index:04X}:{subindex} length={len(payload)}",
            )
            return NodeDomainResponse(
                node_id=node_id,
                index=index,
                subindex=subindex,
                hex_data=payload.hex(),
                length=len(payload),
            )

    async def read_sync_config(self, node_id: int) -> NodeSyncConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            return await asyncio.to_thread(self._read_sync_config_sync, node_id, self._nodes[node_id])

    async def write_sync_config(self, node_id: int, enabled: bool, cob_id: int, period_us: int) -> NodeSyncConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")

            if enabled:
                for other_node_id, other_node in self._nodes.items():
                    if other_node_id == node_id:
                        continue
                    other_config = await asyncio.to_thread(self._read_sync_config_sync, other_node_id, other_node)
                    await asyncio.to_thread(
                        self._write_sync_config_sync,
                        other_node,
                        False,
                        other_config.cob_id,
                        0,
                    )

            await asyncio.to_thread(
                self._write_sync_config_sync,
                self._nodes[node_id],
                enabled,
                cob_id,
                period_us,
            )
            config = await asyncio.to_thread(self._read_sync_config_sync, node_id, self._nodes[node_id])
            self.append_log(
                "INFO",
                "python-canopen",
                f"SYNC config node={node_id} producer={config.producer} cob_id=0x{config.cob_id:X} period_us={config.period_us}",
            )
            return config

    def _read_sync_config_sync(self, node_id: int, node) -> NodeSyncConfig:
        raw_cob_id = int(node.sdo[0x1005].raw)
        period_us = int(node.sdo[0x1006].raw)
        producer = bool(raw_cob_id & SYNC_PRODUCER_BIT) and period_us > 0
        return NodeSyncConfig(
            node_id=node_id,
            enabled=producer,
            cob_id=raw_cob_id & COB_ID_MASK,
            period_us=period_us,
            producer=producer,
        )

    @staticmethod
    def _write_sync_config_sync(node, enabled: bool, cob_id: int, period_us: int) -> None:
        raw_cob_id = cob_id & COB_ID_MASK
        if enabled:
            raw_cob_id |= SYNC_PRODUCER_BIT
        node.sdo[0x1005].raw = raw_cob_id
        node.sdo[0x1006].raw = int(period_us if enabled else 0)

    async def read_tpdo1_config(self, node_id: int) -> NodeTpdoConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            return await asyncio.to_thread(self._read_tpdo1_config_sync, node_id, self._nodes[node_id])

    async def write_tpdo1_config(
        self,
        node_id: int,
        enabled: bool,
        cob_id: int,
        transmission_type: int,
        inhibit_time: int,
        event_timer: int,
        sync_start_value: int,
    ) -> NodeTpdoConfig:
        async with self._lock:
            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")
            await asyncio.to_thread(
                self._write_tpdo1_config_sync,
                self._nodes[node_id],
                enabled,
                cob_id,
                transmission_type,
                inhibit_time,
                event_timer,
                sync_start_value,
            )
            await asyncio.to_thread(self._refresh_tpdo1_subscription_sync, self._nodes[node_id], node_id)
            config = await asyncio.to_thread(self._read_tpdo1_config_sync, node_id, self._nodes[node_id])
            self.append_log(
                "INFO",
                "python-canopen",
                "TPDO1 config "
                f"node={node_id} enabled={config.enabled} cob_id=0x{config.cob_id:X} "
                f"transmission_type={config.transmission_type} inhibit_time={config.inhibit_time} "
                f"event_timer={config.event_timer} sync_start_value={config.sync_start_value}",
            )
            return config

    def _read_tpdo1_config_sync(self, node_id: int, node) -> NodeTpdoConfig:
        raw_cob_id = int(node.sdo[0x1800][1].raw)
        return NodeTpdoConfig(
            node_id=node_id,
            enabled=(raw_cob_id & COB_ID_VALID_BIT) == 0,
            cob_id=raw_cob_id & COB_ID_MASK,
            transmission_type=int(node.sdo[0x1800][2].raw),
            inhibit_time=int(node.sdo[0x1800][3].raw),
            event_timer=int(node.sdo[0x1800][5].raw),
            sync_start_value=int(node.sdo[0x1800][6].raw),
        )

    @staticmethod
    def _write_tpdo1_config_sync(
        node,
        enabled: bool,
        cob_id: int,
        transmission_type: int,
        inhibit_time: int,
        event_timer: int,
        sync_start_value: int,
    ) -> None:
        disabled_cob_id = (cob_id & COB_ID_MASK) | COB_ID_VALID_BIT
        final_cob_id = cob_id & COB_ID_MASK
        if not enabled:
            final_cob_id |= COB_ID_VALID_BIT

        node.sdo[0x1800][1].raw = disabled_cob_id
        node.sdo[0x1800][2].raw = int(transmission_type)
        node.sdo[0x1800][3].raw = int(inhibit_time)
        node.sdo[0x1800][5].raw = int(event_timer)
        node.sdo[0x1800][6].raw = int(sync_start_value)
        node.sdo[0x1800][1].raw = final_cob_id

    def _refresh_tpdo1_subscription_sync(self, node, node_id: int) -> None:
        node.tpdo.read()
        callbacks = list(node.tpdo[1].callbacks)
        expected = getattr(node, "_webui_tpdo1_callback", None)
        if expected is not None and expected not in callbacks:
            node.tpdo[1].add_callback(expected)
        self.append_log(
            "INFO",
            "python-canopen",
            f"TPDO1 subscription refreshed node={node_id} enabled={node.tpdo[1].enabled}",
        )

    def _add_connected_node_sync(self, node_id: int) -> NodeSnapshot:
        eds_path = self._resolve_eds_path(settings.eds_path)
        node = canopen.RemoteNode(node_id, str(eds_path))
        self._network.add_node(node)
        try:
            node.tpdo.read()
            tpdo1_callback = self._make_tpdo1_callback(node_id)
            node._webui_tpdo1_callback = tpdo1_callback
            node.tpdo[1].add_callback(tpdo1_callback)
        except Exception as exc:
            self.append_log("ERROR", "python-canopen", f"TPDO1 subscribe failed node={node_id}: {exc}")
        self._nodes[node_id] = node
        snapshot = self._read_node_snapshot(node_id, node)
        self.append_log("INFO", "webui", f"Added node{node_id} to active bus")
        return snapshot

    def _remove_connected_node_sync(self, node_id: int) -> None:
        if self._network is None:
            return
        if node_id in self._network:
            del self._network[node_id]
        self._nodes.pop(node_id, None)

    async def write_domain(
        self,
        node_id: int,
        index: int,
        subindex: int,
        payload: bytes,
    ) -> NodeDomainResponse:
        async with self._lock:
            if node_id not in self._snapshots:
                raise KeyError(f"Unknown node: {node_id}")

            if node_id not in self._nodes:
                raise RuntimeError(f"Node {node_id} is not connected")

            await asyncio.to_thread(self._write_domain_sync, self._nodes[node_id], index, subindex, payload)
            rx_payload = await asyncio.to_thread(self._read_domain_sync, self._nodes[node_id], index, subindex)

            self.append_log(
                "INFO",
                "python-canopen",
                f"Domain write node={node_id} index=0x{index:04X}:{subindex} length={len(payload)}",
            )
            return NodeDomainResponse(
                node_id=node_id,
                index=index,
                subindex=subindex,
                hex_data=rx_payload.hex(),
                length=len(rx_payload),
            )

    @staticmethod
    def _read_domain_sync(node, index: int, subindex: int) -> bytes:
        if subindex == 0:
            return bytes(node.sdo[index].raw)
        return bytes(node.sdo[index][subindex].raw)

    @staticmethod
    def _write_domain_sync(node, index: int, subindex: int, payload: bytes) -> None:
        if subindex == 0:
            node.sdo[index].raw = payload
            return
        node.sdo[index][subindex].raw = payload

service = CANopenService()
