const API_BASE = import.meta.env.VITE_API_BASE || window.location.origin;

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;

  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload ? payload.detail : payload || `HTTP ${response.status}`;
    throw new Error(String(detail));
  }

  return payload;
}

export async function fetchNodes() {
  return requestJson(`${API_BASE}/api/nodes`);
}

export async function addNode(nodeId) {
  return requestJson(`${API_BASE}/api/nodes`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ node_id: nodeId }),
  });
}

export async function removeNode(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/remove`, {
    method: "POST",
  });
}

export async function fetchLogs() {
  return requestJson(`${API_BASE}/api/logs`);
}

export async function clearLogs() {
  return requestJson(`${API_BASE}/api/logs/clear`, { method: "POST" });
}

export async function fetchConnection() {
  return requestJson(`${API_BASE}/api/connection`);
}

export async function fetchCanDevices() {
  return requestJson(`${API_BASE}/api/connection/devices`);
}

export async function updateConnectionDevice({ bustype, channel }) {
  return requestJson(`${API_BASE}/api/connection/device`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ bustype, channel }),
  });
}

export async function connectBus() {
  return requestJson(`${API_BASE}/api/connection/connect`, { method: "POST" });
}

export async function disconnectBus() {
  return requestJson(`${API_BASE}/api/connection/disconnect`, { method: "POST" });
}

export async function writeSdo(nodeId, { index, subindex, value }) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/sdo`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ index, subindex, value }),
  });
}

export async function refreshNodeValues(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/refresh`, {
    method: "POST",
  });
}

export async function fetchHeartbeatConfig(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/heartbeat`);
}

export async function writeHeartbeatConfig(nodeId, payload) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/heartbeat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

export async function fetchNmtConfig(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/nmt`);
}

export async function writeNmtConfig(nodeId, payload) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/nmt`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

export async function fetchSyncConfig(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/sync`);
}

export async function writeSyncConfig(nodeId, payload) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/sync`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

export async function fetchTpdo1Config(nodeId) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/tpdo1`);
}

export async function writeTpdo1Config(nodeId, payload) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/tpdo1`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

export async function readDomain(nodeId, { index, subindex }) {
  const url = new URL(`${API_BASE}/api/nodes/${nodeId}/domain`);
  url.searchParams.set("index", index);
  url.searchParams.set("subindex", subindex);
  return requestJson(url);
}

export async function writeDomain(nodeId, { index, subindex, hexData }) {
  return requestJson(`${API_BASE}/api/nodes/${nodeId}/domain`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ index, subindex, hex_data: hexData }),
  });
}

export function connectNodesSocket(onMessage) {
  const wsBase = API_BASE.replace("http://", "ws://").replace("https://", "wss://");
  const socket = new WebSocket(`${wsBase}/ws/nodes`);
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "state") {
      onMessage(payload);
    }
  });
  return socket;
}
