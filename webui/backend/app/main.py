import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .canopen_service import service
from .config import settings
from .models import (
    ConnectionDeviceRequest,
    NodeAddRequest,
    NodeDomainRequest,
    NodeHeartbeatWriteRequest,
    NodeNmtWriteRequest,
    NodeSdoWriteRequest,
    NodeSyncWriteRequest,
    NodeTpdoWriteRequest,
    NodeWriteRequest,
)

app = FastAPI(title=settings.app_name)
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup() -> None:
    await service.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await service.stop()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "mock": settings.mock}


@app.get("/api/connection")
async def connection_status() -> dict:
    return service.connection_status().model_dump()


@app.get("/api/connection/devices")
async def list_can_devices() -> list[dict]:
    return [device.model_dump() for device in await service.list_can_devices()]


@app.post("/api/connection/device")
async def update_connection_device(payload: ConnectionDeviceRequest) -> dict:
    try:
        result = await service.set_device(payload.bustype, payload.channel)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/connection/connect")
async def connect_bus() -> dict:
    return (await service.connect()).model_dump()


@app.post("/api/connection/disconnect")
async def disconnect_bus() -> dict:
    return (await service.disconnect()).model_dump()


@app.get("/api/nodes")
async def list_nodes() -> list[dict]:
    return [node.model_dump() for node in await service.list_nodes()]


@app.post("/api/nodes")
async def add_node(payload: NodeAddRequest) -> dict:
    try:
        result = await service.add_node(payload.node_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/remove")
async def remove_node(node_id: int) -> dict:
    try:
        await service.remove_node(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/logs")
async def list_logs() -> list[dict]:
    return [entry.model_dump() for entry in await service.list_logs()]


@app.post("/api/logs/clear")
async def clear_logs() -> dict:
    await service.clear_logs()
    return {"ok": True}


@app.post("/api/nodes/{node_id}/sdo")
async def write_sdo(node_id: int, payload: NodeSdoWriteRequest) -> dict:
    try:
        snapshot = await service.write_sdo(
            node_id=node_id,
            index=payload.index,
            subindex=payload.subindex,
            value=payload.value,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.model_dump()


@app.post("/api/nodes/{node_id}/refresh")
async def refresh_node_values(node_id: int) -> dict:
    try:
        snapshot = await service.read_node_values(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.model_dump()


@app.get("/api/nodes/{node_id}/heartbeat")
async def read_heartbeat_config(node_id: int) -> dict:
    try:
        result = await service.read_heartbeat_config(node_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/heartbeat")
async def write_heartbeat_config(node_id: int, payload: NodeHeartbeatWriteRequest) -> dict:
    try:
        result = await service.write_heartbeat_config(node_id, payload.producer_time_ms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.get("/api/nodes/{node_id}/nmt")
async def read_nmt_config(node_id: int) -> dict:
    try:
        result = await service.read_nmt_config(node_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/nmt")
async def write_nmt_config(node_id: int, payload: NodeNmtWriteRequest) -> dict:
    try:
        result = await service.write_nmt_config(node_id, payload.state)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.get("/api/nodes/{node_id}/sync")
async def read_sync_config(node_id: int) -> dict:
    try:
        result = await service.read_sync_config(node_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/sync")
async def write_sync_config(node_id: int, payload: NodeSyncWriteRequest) -> dict:
    try:
        result = await service.write_sync_config(
            node_id=node_id,
            enabled=payload.enabled,
            cob_id=payload.cob_id,
            period_us=payload.period_us,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.get("/api/nodes/{node_id}/tpdo1")
async def read_tpdo1_config(node_id: int) -> dict:
    try:
        result = await service.read_tpdo1_config(node_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/tpdo1")
async def write_tpdo1_config(node_id: int, payload: NodeTpdoWriteRequest) -> dict:
    try:
        result = await service.write_tpdo1_config(
            node_id=node_id,
            enabled=payload.enabled,
            cob_id=payload.cob_id,
            transmission_type=payload.transmission_type,
            inhibit_time=payload.inhibit_time,
            event_timer=payload.event_timer,
            sync_start_value=payload.sync_start_value,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/testvar2")
async def write_testvar2_compat(node_id: int, payload: NodeWriteRequest) -> dict:
    try:
        snapshot = await service.write_sdo(node_id=node_id, index=0x2001, subindex=0, value=payload.value)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.model_dump()


@app.get("/api/nodes/{node_id}/domain")
async def read_domain(node_id: int, index: int = Query(...), subindex: int = Query(0)) -> dict:
    try:
        result = await service.read_domain(node_id=node_id, index=index, subindex=subindex)
    except KeyError as exc:
        service.append_log("ERROR", "webui", f"Domain read failed node={node_id} index=0x{index:04X}:{subindex} detail={exc}")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        service.append_log("ERROR", "python-canopen", f"Domain read failed node={node_id} index=0x{index:04X}:{subindex} detail={exc}")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        service.append_log("ERROR", "python-canopen", f"Domain read failed node={node_id} index=0x{index:04X}:{subindex} detail={exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/nodes/{node_id}/domain")
async def write_domain(node_id: int, payload: NodeDomainRequest) -> dict:
    try:
        result = await service.write_domain(
            node_id=node_id,
            index=payload.index,
            subindex=payload.subindex,
            payload=bytes.fromhex(payload.hex_data),
        )
    except KeyError as exc:
        service.append_log(
            "ERROR",
            "webui",
            f"Domain write failed node={node_id} index=0x{payload.index:04X}:{payload.subindex} detail={exc}",
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        service.append_log(
            "ERROR",
            "python-canopen",
            f"Domain write failed node={node_id} index=0x{payload.index:04X}:{payload.subindex} detail={exc}",
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        service.append_log(
            "ERROR",
            "python-canopen",
            f"Domain write failed node={node_id} index=0x{payload.index:04X}:{payload.subindex} "
            f"hex_length={len(payload.hex_data)} detail={exc}",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.websocket("/ws/nodes")
async def nodes_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            snapshots = [node.model_dump() for node in await service.list_nodes()]
            logs = [entry.model_dump() for entry in await service.list_logs()]
            await websocket.send_text(json.dumps({"type": "state", "items": snapshots, "logs": logs}))
            await asyncio.sleep(settings.poll_interval)
    except WebSocketDisconnect:
        return


if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/")
    async def serve_index() -> FileResponse:
        return FileResponse(frontend_dist / "index.html")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str) -> FileResponse:
        target = frontend_dist / full_path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(frontend_dist / "index.html")
