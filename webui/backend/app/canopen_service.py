import asyncio
import contextlib
import io
import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import settings
from .models import (
    CanDeviceInfo,
    ConnectionStatus,
    LogEntry,
    NodeDomainResponse,
    NodeHeartbeatConfig,
    NodeNmtConfig,
    NodeSnapshot,
    NodeSyncConfig,
    NodeTpdoConfig,
)

try:
    import canopen
except ImportError:  # pragma: no cover
    canopen = None

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    list_ports = None


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

SDO_ABORT_EXPLANATIONS = {
    "0x05040000": "SDO protocol timed out. The node did not complete the transfer in time.",
    "0x05040001": "Invalid client/server command specifier for this SDO transfer.",
    "0x06010000": "Unsupported access to this object.",
    "0x06010001": "Attempted to read a write-only object.",
    "0x06010002": "Attempted to write a read-only object.",
    "0x06020000": "Object does not exist in the object dictionary.",
    "0x06040041": "Object cannot be mapped to PDO.",
    "0x06040042": "Too many objects are mapped to the PDO.",
    "0x06070010": "Data type or length does not match the object definition.",
    "0x06070013": "Data type does not match, because the provided service parameter is too short.",
    "0x06090011": "Sub-index does not exist.",
    "0x06090030": "Parameter value is outside the valid range.",
    "0x08000000": "General application error.",
    "0x08000022": "Object access failed because of the current device state.",
}

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
        self._tpdo_times: dict[int, float] = {}
        self._sdo_times: dict[int, float] = {}
        self._activity_timeout_seconds = 1.5
        self._offline_logged: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._discovery_pending: set[int] = set()
        self._previous_sys_excepthook = None
        self._previous_threading_excepthook = None
        self._previous_asyncio_exception_handler = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._setup_logging()
        self._install_exception_hooks()
        self.append_log("INFO", "webui", "Starting CANopen WebUI service")
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.disconnect()
        self._restore_exception_hooks()
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

    async def list_can_devices(self) -> list[CanDeviceInfo]:
        serial_devices = await self._list_slcan_devices()
        native_devices = await asyncio.to_thread(self._detect_native_can_devices_sync)
        devices = serial_devices + native_devices
        devices.sort(key=lambda item: (item.category, item.bustype, item.channel))
        return devices

    async def _list_slcan_devices(self) -> list[CanDeviceInfo]:
        if list_ports is None:
            return []
        ports = await asyncio.to_thread(lambda: list(list_ports.comports()))
        return [
            CanDeviceInfo(
                label=f"slcan · {port.device}",
                category="SLCAN Serial",
                supported=True,
                bustype="slcan",
                channel=port.device,
                description=port.description or port.device,
                hwid=getattr(port, "hwid", None),
                manufacturer=getattr(port, "manufacturer", None),
                vid=getattr(port, "vid", None),
                pid=getattr(port, "pid", None),
                serial_number=getattr(port, "serial_number", None),
            )
            for port in ports
        ]

    @staticmethod
    def _detect_native_can_devices_sync() -> list[CanDeviceInfo]:
        if canopen is None:
            return []
        import can

        interfaces = ["pcan"]
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                configs = can.detect_available_configs(interfaces=interfaces, timeout=0.5)
        except Exception:
            return []

        devices: list[CanDeviceInfo] = []
        for config in configs:
            bustype = str(config.get("interface") or config.get("bustype") or "")
            channel = str(config.get("channel") or "")
            if not bustype or not channel:
                continue
            devices.append(
                CanDeviceInfo(
                    label=f"{bustype} · {channel}",
                    category="Native CAN",
                    supported=True,
                    bustype=bustype,
                    channel=channel,
                    description=str(config.get("app_name") or config.get("description") or channel),
                )
            )
        return devices

    @staticmethod
    def _detect_winusb_devices_sync() -> list[CanDeviceInfo]:
        try:
            result = subprocess.run(
                ["pnputil", "/enum-devices", "/connected"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                check=False,
            )
        except Exception:
            return []

        text = result.stdout or ""
        if not text:
            return []

        devices: list[CanDeviceInfo] = []
        current: dict[str, str] = {}

        def flush_current() -> None:
            if not current:
                return
            description = current.get("Device Description", "").strip()
            class_name = current.get("Class Name", "").strip()
            manufacturer = current.get("Manufacturer Name", "").strip()
            instance_id = current.get("Instance ID", "").strip()
            searchable = " ".join([description, class_name, manufacturer, instance_id]).lower()
            if "winusb" not in searchable or not instance_id:
                return
            label = description or manufacturer or instance_id
            devices.append(
                CanDeviceInfo(
                    label=label,
                    category="WinUSB / Unclassified",
                    supported=False,
                    bustype="winusb",
                    channel=instance_id,
                    description=manufacturer or description or None,
                    manufacturer=manufacturer or None,
                    hwid=instance_id,
                )
            )

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_current()
                current = {}
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()

        flush_current()

        deduped: dict[str, CanDeviceInfo] = {}
        for item in devices:
            deduped[item.channel] = item
        return list(deduped.values())

    async def set_device(self, bustype: str, channel: str) -> ConnectionStatus:
        async with self._lock:
            if self._network is not None:
                raise RuntimeError("Disconnect the bus before changing the CAN device")
            if bustype == "winusb":
                raise RuntimeError("This WinUSB device is visible in Windows but not supported by the current CAN backend")
            settings.bustype = bustype
            settings.channel = channel
            self.append_log("INFO", "webui", f"Selected CAN device bustype={bustype} channel={channel}")
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

    def _enrich_log_message(self, source: str, message: str) -> str:
        if "Transfer aborted by client with code" in message:
            match = re.search(r"(0x[0-9a-fA-F]+)", message)
            if match:
                code = match.group(1).lower()
                explanation = SDO_ABORT_EXPLANATIONS.get(code)
                if explanation:
                    return f"{message} Meaning: {explanation}"
        if "SdoAbortedError" in message or "Code 0x" in message:
            match = re.search(r"(0x[0-9a-fA-F]+)", message)
            if match:
                code = match.group(1).lower()
                explanation = SDO_ABORT_EXPLANATIONS.get(code)
                if explanation:
                    return f"{message} Meaning: {explanation}"
        if "No SDO response received" in message:
            return (
                f"{message} Meaning: the node did not answer the SDO request. "
                "Common causes are wrong node ID, node offline, bus issue, or the object being inaccessible in the current state."
            )
        return message

    def append_log(self, level: str, source: str, message: str) -> None:
        message = self._enrich_log_message(source, message)
        with self._state_lock:
            self._logs.append(
                LogEntry(
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    level=level,
                    source=source,
                    message=message,
                )
            )

    @staticmethod
    def _extract_abort_code(exc: BaseException) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return f"0x{code:08x}"
        match = re.search(r"(0x[0-9a-fA-F]+)", str(exc))
        return match.group(1).lower() if match else ""

    def append_exception(self, source: str, header: str, exc: BaseException) -> None:
        summary = f"{header}: {exc.__class__.__name__}: {exc}"
        self.append_log("ERROR", source, summary)
        self.append_log("ERROR", source, f"detail: exception type = {exc.__class__.__name__}")

        abort_code = self._extract_abort_code(exc)
        if abort_code:
            self.append_log("ERROR", source, f"detail: abort code = {abort_code}")
            explanation = SDO_ABORT_EXPLANATIONS.get(abort_code.lower())
            if explanation:
                self.append_log("ERROR", source, f"detail: meaning = {explanation}")

        cause = exc.__cause__ or exc.__context__
        if cause:
            self.append_log("ERROR", source, f"detail: cause = {cause.__class__.__name__}: {cause}")

    def _install_exception_hooks(self) -> None:
        if self._previous_sys_excepthook is None:
            self._previous_sys_excepthook = sys.excepthook
            sys.excepthook = self._sys_excepthook
        if self._previous_threading_excepthook is None and hasattr(threading, "excepthook"):
            self._previous_threading_excepthook = threading.excepthook
            threading.excepthook = self._threading_excepthook
        if self._loop is not None and self._previous_asyncio_exception_handler is None:
            self._previous_asyncio_exception_handler = self._loop.get_exception_handler()
            self._loop.set_exception_handler(self._asyncio_exception_handler)

    def _restore_exception_hooks(self) -> None:
        if self._previous_sys_excepthook is not None:
            sys.excepthook = self._previous_sys_excepthook
            self._previous_sys_excepthook = None
        if self._previous_threading_excepthook is not None and hasattr(threading, "excepthook"):
            threading.excepthook = self._previous_threading_excepthook
            self._previous_threading_excepthook = None
        if self._loop is not None:
            self._loop.set_exception_handler(self._previous_asyncio_exception_handler)
            self._previous_asyncio_exception_handler = None

    def _sys_excepthook(self, exc_type, exc_value, exc_traceback) -> None:
        self.append_exception("python", "Uncaught Python exception", exc_value)
        if self._previous_sys_excepthook is not None:
            self._previous_sys_excepthook(exc_type, exc_value, exc_traceback)

    def _threading_excepthook(self, args) -> None:
        self.append_exception("python", f"Uncaught thread exception in {args.thread.name}", args.exc_value)
        if self._previous_threading_excepthook is not None:
            self._previous_threading_excepthook(args)

    def _asyncio_exception_handler(self, loop, context) -> None:
        exc = context.get("exception")
        if exc is not None:
            self.append_exception("python", "Asyncio exception", exc)
        else:
            self.append_log("ERROR", "python", f"Asyncio exception: {context.get('message', 'unknown')}")
        if self._previous_asyncio_exception_handler is not None:
            self._previous_asyncio_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    def _log_canopen_exception(self, action: str, node_id: int, exc: Exception, extra: str = "") -> None:
        suffix = f" {extra}" if extra else ""
        self.append_exception("python-canopen", f"{action} failed node={node_id}{suffix}", exc)

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
        with self._state_lock:
            self._tpdo_times[node_id] = time.monotonic()
        hb_online, hb_state = self._get_heartbeat_status(node_id)
        self._snapshots[node_id] = snapshot.model_copy(
            update={
                "connected": hb_online,
                "nmt_state": hb_state if hb_online else "OFFLINE",
                "status_note": self._build_status_note(node_id, hb_online),
                "testvar1_uint32": value_u32,
                "testvar2_uint16": value_u16,
                "last_error": None,
            }
        )

    def _mark_sdo_activity(self, node_id: int) -> None:
        with self._state_lock:
            self._sdo_times[node_id] = time.monotonic()

    def _has_recent_activity(self, activity_map: dict[int, float], node_id: int) -> bool:
        with self._state_lock:
            last_time = activity_map.get(node_id)
        return last_time is not None and (time.monotonic() - last_time) <= self._activity_timeout_seconds

    def _build_status_note(self, node_id: int, hb_online: bool) -> str | None:
        if hb_online:
            return None
        has_tpdo = self._has_recent_activity(self._tpdo_times, node_id)
        has_sdo = self._has_recent_activity(self._sdo_times, node_id)
        if has_tpdo and has_sdo:
            return "Heartbeat lost, but TPDO and SDO traffic are still active."
        if has_tpdo:
            return "Heartbeat lost, but TPDO traffic is still active."
        if has_sdo:
            return "Heartbeat lost, but SDO communication is still active."
        return None

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
            self._tpdo_times.clear()
            self._sdo_times.clear()
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
            self._mark_sdo_activity(node_id)
            hb_online, hb_state = self._get_heartbeat_status(node_id)
            return NodeSnapshot(
                node_id=node_id,
                connected=hb_online,
                nmt_state=hb_state if hb_online else "OFFLINE",
                status_note=self._build_status_note(node_id, hb_online),
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
            status_note=None,
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
                    "status_note": self._build_status_note(snapshot.node_id, False),
                    "testvar1_uint32": 0,
                    "testvar2_uint16": 0,
                    "heartbeat_ms": snapshot.heartbeat_ms,
                }
            )
        return snapshot.model_copy(
            update={
                "connected": True,
                "nmt_state": hb_state,
                "status_note": None,
            }
        )

    async def list_nodes(self) -> list[NodeSnapshot]:
        async with self._lock:
            return [
                self._snapshots.get(node_id, self._make_placeholder_snapshot(node_id, "DISCONNECTED"))
                for node_id in self._node_ids
            ]

    def _ensure_active_node_sync(self, node_id: int):
        if node_id not in self._node_ids:
            raise KeyError(f"Unknown node: {node_id}")
        if self._network is None:
            raise RuntimeError("CAN bus is not connected")
        if node_id not in self._nodes:
            snapshot = self._add_connected_node_sync(node_id)
            self._snapshots[node_id] = snapshot
            self.append_log("INFO", "webui", f"Attached node{node_id} for direct access")
        return self._nodes[node_id]

    async def write_sdo(self, node_id: int, index: int, subindex: int, value: int) -> NodeSnapshot:
        async with self._lock:
            if node_id not in self._snapshots:
                raise KeyError(f"Unknown node: {node_id}")

            node = self._ensure_active_node_sync(node_id)
            try:
                await asyncio.to_thread(self._write_sdo_sync, node, index, subindex, value)
                snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, node)
            except Exception as exc:
                self._log_canopen_exception(
                    "SDO write",
                    node_id,
                    exc,
                    f"index=0x{index:04X}:{subindex} value={value}",
                )
                raise

            self._snapshots[node_id] = snapshot
            self.append_log(
                "INFO",
                "python-canopen",
                f"SDO write node={node_id} index=0x{index:04X}:{subindex} value={value}",
            )
            return snapshot

    async def read_heartbeat_config(self, node_id: int) -> NodeHeartbeatConfig:
        async with self._lock:
            node = self._ensure_active_node_sync(node_id)
            try:
                return await asyncio.to_thread(self._read_heartbeat_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("Heartbeat read", node_id, exc, "index=0x1017:0")
                raise

    async def write_heartbeat_config(self, node_id: int, producer_time_ms: int) -> NodeHeartbeatConfig:
        async with self._lock:
            if producer_time_ms < 0 or producer_time_ms >= 1000:
                raise ValueError("Heartbeat producer time must be between 0 and 999 ms")
            node = self._ensure_active_node_sync(node_id)
            try:
                await asyncio.to_thread(self._write_heartbeat_config_sync, node, producer_time_ms)
                config = await asyncio.to_thread(self._read_heartbeat_config_sync, node_id, node)
                snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, node)
            except Exception as exc:
                self._log_canopen_exception(
                    "Heartbeat write",
                    node_id,
                    exc,
                    f"index=0x1017:0 value_ms={producer_time_ms}",
                )
                raise
            self._snapshots[node_id] = snapshot
            self.append_log(
                "INFO",
                "python-canopen",
                f"Heartbeat config node={node_id} producer_time_ms={config.producer_time_ms}",
            )
            return config

    async def read_nmt_config(self, node_id: int) -> NodeNmtConfig:
        async with self._lock:
            node = self._ensure_active_node_sync(node_id)
            try:
                return await asyncio.to_thread(self._read_nmt_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("NMT read", node_id, exc)
                raise

    async def write_nmt_config(self, node_id: int, state: str) -> NodeNmtConfig:
        async with self._lock:
            node = self._ensure_active_node_sync(node_id)
            try:
                await asyncio.to_thread(self._write_nmt_config_sync, node, state)
                config = await asyncio.to_thread(self._read_nmt_config_sync, node_id, node)
                snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("NMT write", node_id, exc, f"state={state}")
                raise
            self._snapshots[node_id] = snapshot
            self.append_log(
                "INFO",
                "python-canopen",
                f"NMT node={node_id} state={config.state}",
            )
            return config

    async def read_node_values(self, node_id: int) -> NodeSnapshot:
        async with self._lock:
            node = self._ensure_active_node_sync(node_id)
            try:
                snapshot = await asyncio.to_thread(self._read_node_snapshot, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("SDO read", node_id, exc)
                raise
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

    @staticmethod
    def _read_nmt_config_sync(node_id: int, node) -> NodeNmtConfig:
        return NodeNmtConfig(node_id=node_id, state=str(node.nmt.state))

    @staticmethod
    def _write_nmt_config_sync(node, state: str) -> None:
        node.nmt.state = str(state).upper()

    async def read_domain(self, node_id: int, index: int, subindex: int) -> NodeDomainResponse:
        async with self._lock:
            if node_id not in self._snapshots:
                raise KeyError(f"Unknown node: {node_id}")

            node = self._ensure_active_node_sync(node_id)
            try:
                payload = await asyncio.to_thread(self._read_domain_sync, node, index, subindex)
            except Exception as exc:
                self._log_canopen_exception(
                    "Domain read",
                    node_id,
                    exc,
                    f"index=0x{index:04X}:{subindex}",
                )
                raise

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
            node = self._ensure_active_node_sync(node_id)
            try:
                return await asyncio.to_thread(self._read_sync_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("SYNC read", node_id, exc)
                raise

    async def write_sync_config(self, node_id: int, enabled: bool, cob_id: int, period_us: int) -> NodeSyncConfig:
        async with self._lock:
            node = self._ensure_active_node_sync(node_id)
            try:
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
                    node,
                    enabled,
                    cob_id,
                    period_us,
                )
                config = await asyncio.to_thread(self._read_sync_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception(
                    "SYNC write",
                    node_id,
                    exc,
                    f"enabled={enabled} cob_id=0x{cob_id:X} period_us={period_us}",
                )
                raise
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
            node = self._ensure_active_node_sync(node_id)
            try:
                return await asyncio.to_thread(self._read_tpdo1_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception("TPDO1 read", node_id, exc)
                raise

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
            node = self._ensure_active_node_sync(node_id)
            try:
                await asyncio.to_thread(
                    self._write_tpdo1_config_sync,
                    node,
                    enabled,
                    cob_id,
                    transmission_type,
                    inhibit_time,
                    event_timer,
                    sync_start_value,
                )
                await asyncio.to_thread(self._refresh_tpdo1_subscription_sync, node, node_id)
                config = await asyncio.to_thread(self._read_tpdo1_config_sync, node_id, node)
            except Exception as exc:
                self._log_canopen_exception(
                    "TPDO1 write",
                    node_id,
                    exc,
                    "enabled="
                    f"{enabled} cob_id=0x{cob_id:X} transmission_type={transmission_type} "
                    f"inhibit_time={inhibit_time} event_timer={event_timer} sync_start_value={sync_start_value}",
                )
                raise
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
            node = self._ensure_active_node_sync(node_id)
            try:
                await asyncio.to_thread(self._write_domain_sync, node, index, subindex, payload)
                rx_payload = await asyncio.to_thread(self._read_domain_sync, node, index, subindex)
            except Exception as exc:
                self._log_canopen_exception(
                    "Domain write",
                    node_id,
                    exc,
                    f"index=0x{index:04X}:{subindex} length={len(payload)}",
                )
                raise

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
