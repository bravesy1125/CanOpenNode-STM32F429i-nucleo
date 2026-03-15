<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import * as echarts from "echarts";
import {
  addNode,
  clearLogs,
  connectBus,
  connectNodesSocket,
  disconnectBus,
  fetchConnection,
  fetchCanDevices,
  fetchHeartbeatConfig,
  fetchLogs,
  fetchNmtConfig,
  fetchNodes,
  fetchSyncConfig,
  fetchTpdo1Config,
  readDomain,
  refreshNodeValues,
  removeNode,
  updateConnectionDevice,
  writeDomain,
  writeHeartbeatConfig,
  writeNmtConfig,
  writeSdo,
  writeSyncConfig,
  writeTpdo1Config,
} from "./api";

const nodes = ref([]);
const logs = ref([]);
const connection = ref({ connected: false, channel: "COM6", bustype: "slcan", bitrate: 1000000 });
const canDevices = ref([]);
const selectedDeviceKey = ref("slcan|COM6");
const writeState = reactive({});
const domainState = reactive({});
const heartbeatState = reactive({});
const nmtState = reactive({});
const syncState = reactive({});
const tpdoState = reactive({});
const nodeUiState = reactive({});
const chartHistory = reactive({});
const status = ref("Connecting...");
const activeTab = ref("overview");
const tabTransitionName = ref("tab-slide-left");
const addNodeId = ref("");
const chartNodeId = ref(null);
const selectedMonitorNodeIds = ref([]);
const selectedMonitorMetrics = ref(["testvar1", "testvar2"]);
const chartModalOpen = ref(false);
const chartPaused = ref(false);
const chartHostPrimary = ref(null);
const chartHostSecondary = ref(null);
const quickChartHost = ref(null);
const logListHost = ref(null);
const quickChartMetric = ref("testvar1");
const chartZoomState = reactive({
  primary: { yStart: 0, yEnd: 100 },
  secondary: { yStart: 0, yEnd: 100 },
  quick: { yStart: 0, yEnd: 100 },
});
const loadingSyncNodes = new Set();
const loadingTpdoNodes = new Set();
const loadingHeartbeatNodes = new Set();
const loadingNmtNodes = new Set();
const loadedSyncNodes = new Set();
const loadedTpdoNodes = new Set();
const loadedHeartbeatNodes = new Set();
const loadedNmtNodes = new Set();
let socket;
let chartInstancePrimary;
let chartInstanceSecondary;
let quickChartInstance;
let chartRenderToken = 0;
const TAB_ORDER = ["overview", "nodes", "monitor"];
const activeTabIndex = computed(() => Math.max(0, TAB_ORDER.indexOf(activeTab.value)));

function applyInitialViewState() {
  const params = new URLSearchParams(window.location.search);
  const tab = params.get("tab");
  if (tab === "overview" || tab === "nodes" || tab === "monitor") {
    activeTab.value = tab;
  }

  const selectedNodes = params
    .get("nodes")
    ?.split(",")
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isInteger(value) && value >= 1 && value <= 127);
  if (selectedNodes?.length) {
    selectedMonitorNodeIds.value = [...new Set(selectedNodes)].sort((a, b) => a - b);
  }

  const selectedMetrics = params
    .get("metrics")
    ?.split(",")
    .map((value) => value.trim())
    .filter((value) => value === "testvar1" || value === "testvar2");
  if (selectedMetrics?.length) {
    selectedMonitorMetrics.value = [...new Set(selectedMetrics)];
  }
}

function setActiveTab(tab) {
  if (tab === activeTab.value) {
    return;
  }
  const currentIndex = TAB_ORDER.indexOf(activeTab.value);
  const nextIndex = TAB_ORDER.indexOf(tab);
  tabTransitionName.value = nextIndex > currentIndex ? "tab-slide-left" : "tab-slide-right";
  activeTab.value = tab;
}

const pythonLogs = computed(() =>
  logs.value.filter((entry) => entry.source.includes("python") || entry.source === "webui"),
);

async function scrollLogsToBottom() {
  await nextTick();
  const host = logListHost.value;
  if (!host) {
    return;
  }
  host.scrollTop = host.scrollHeight;
}
const activeSyncProducerId = computed(() => {
  for (const node of nodes.value) {
    if (syncState[node.node_id]?.enabled) {
      return node.node_id;
    }
  }
  return null;
});
const activeSyncProducerLabel = computed(() =>
  activeSyncProducerId.value === null ? "-" : `node${activeSyncProducerId.value}`,
);
const groupedCanDevices = computed(() => {
  const groups = new Map();
  for (const device of canDevices.value) {
    const category = device.category || "Other";
    if (!groups.has(category)) {
      groups.set(category, []);
    }
    groups.get(category).push(device);
  }
  return Array.from(groups.entries()).map(([label, items]) => ({ label, items }));
});
const selectedChartNode = computed(() => {
  if (chartNodeId.value !== null) {
    return nodes.value.find((node) => node.node_id === chartNodeId.value) ?? null;
  }
  return nodes.value[0] ?? null;
});
const monitoredNodes = computed(() =>
  nodes.value.filter((node) => node.connected && selectedMonitorNodeIds.value.includes(node.node_id)),
);

function formatValue(value) {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) {
    return String(value);
  }
  return `0x${numeric.toString(16)}`;
}

function reconcileMonitorSelection(items) {
  const validIds = new Set(items.map((node) => node.node_id));
  const selectableIds = new Set(items.filter((node) => node.connected).map((node) => node.node_id));
  selectedMonitorNodeIds.value = selectedMonitorNodeIds.value.filter((nodeId) => selectableIds.has(nodeId));
  if (chartNodeId.value !== null && !validIds.has(chartNodeId.value)) {
    chartNodeId.value = items[0]?.node_id ?? null;
  }
}

function toggleMonitorNode(nodeId) {
  const node = nodes.value.find((item) => item.node_id === nodeId);
  if (!node?.connected) {
    return;
  }
  if (selectedMonitorNodeIds.value.includes(nodeId)) {
    selectedMonitorNodeIds.value = selectedMonitorNodeIds.value.filter((value) => value !== nodeId);
  } else {
    selectedMonitorNodeIds.value = [...selectedMonitorNodeIds.value, nodeId].sort((a, b) => a - b);
  }
  queueChartRender();
}

function selectAllMonitorNodes() {
  selectedMonitorNodeIds.value = nodes.value.filter((node) => node.connected).map((node) => node.node_id);
  queueChartRender();
}

function clearAllMonitorNodes() {
  selectedMonitorNodeIds.value = [];
  queueChartRender();
}

function toggleMonitorMetric(metric) {
  if (selectedMonitorMetrics.value.includes(metric)) {
    selectedMonitorMetrics.value = selectedMonitorMetrics.value.filter((value) => value !== metric);
  } else {
    selectedMonitorMetrics.value = [...selectedMonitorMetrics.value, metric];
  }
  queueChartRender();
}

function parseNumericValue(value) {
  if (typeof value === "string" && value.toLowerCase().startsWith("0x")) {
    return Number.parseInt(value, 16);
  }
  return Number(value);
}

function formatNumericInput(value) {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) {
    return String(value);
  }
  return `0x${numeric.toString(16)}`;
}

function ensureHistory(nodeId) {
  if (!(nodeId in chartHistory)) {
    chartHistory[nodeId] = {
      testvar1: [],
      testvar2: [],
    };
  }
}

function pushHistoryPoint(series, value) {
  series.push({
    timestamp: Date.now(),
    value: Number(value) || 0,
  });
}

function adjustZoomWindow(start, end, direction) {
  const span = Math.max(1, end - start);
  const center = (start + end) / 2;
  const factor = direction > 0 ? 0.8 : 1.25;
  const nextSpan = Math.min(100, Math.max(6, span * factor));
  let nextStart = center - nextSpan / 2;
  let nextEnd = center + nextSpan / 2;

  if (nextStart < 0) {
    nextEnd -= nextStart;
    nextStart = 0;
  }
  if (nextEnd > 100) {
    nextStart -= nextEnd - 100;
    nextEnd = 100;
  }

  return {
    start: Math.max(0, nextStart),
    end: Math.min(100, nextEnd),
  };
}

function nudgeChartZoom(instance, axis, direction) {
  if (!instance) {
    return;
  }

  const option = instance.getOption();
  const key = axis === "x" ? "xAxisIndex" : "yAxisIndex";
  const zooms = option.dataZoom ?? [];
  const index = zooms.findIndex((item) => item[key] !== undefined);

  if (index < 0) {
    return;
  }

  const current = zooms[index];
  const next = adjustZoomWindow(current.start ?? 0, current.end ?? 100, direction);
  instance.dispatchAction({
    type: "dataZoom",
    dataZoomIndex: index,
    start: next.start,
    end: next.end,
  });
}

function zoomCharts(axis, direction) {
  nudgeChartZoom(chartInstancePrimary, axis, direction);
  nudgeChartZoom(chartInstanceSecondary, axis, direction);
}

function applyChartZoomReset(instance, target) {
  target.yStart = 0;
  target.yEnd = 100;
  if (!instance) {
    return;
  }
  const option = instance.getOption();
  for (let index = 0; index < (option.dataZoom?.length ?? 0); index += 1) {
    instance.dispatchAction({
      type: "dataZoom",
      dataZoomIndex: index,
      start: 0,
      end: 100,
    });
  }
}

function resetChartsZoom(target) {
  if (target === "primary") {
    applyChartZoomReset(chartInstancePrimary, chartZoomState.primary);
    return;
  }
  if (target === "secondary") {
    applyChartZoomReset(chartInstanceSecondary, chartZoomState.secondary);
    return;
  }
  applyChartZoomReset(chartInstancePrimary, chartZoomState.primary);
  applyChartZoomReset(chartInstanceSecondary, chartZoomState.secondary);
}

function resetQuickChartZoom() {
  applyChartZoomReset(quickChartInstance, chartZoomState.quick);
}

function bindChartZoomTracking(instance, target) {
  instance.off("datazoom");
  instance.on("datazoom", () => {
    const option = instance.getOption();
    const zoom = (option.dataZoom ?? []).find((item) => item.yAxisIndex !== undefined);
    if (!zoom) {
      return;
    }
    target.yStart = zoom.start ?? 0;
    target.yEnd = zoom.end ?? 100;
  });
}

function resetChartHistory() {
  if (chartModalOpen.value) {
    if (chartNodeId.value === null) {
      return;
    }
    chartHistory[chartNodeId.value] = {
      testvar1: [],
      testvar2: [],
    };
    queueQuickChartRender();
    return;
  }

  if (selectedMonitorNodeIds.value.length === 0) {
    return;
  }
  for (const nodeId of selectedMonitorNodeIds.value) {
    chartHistory[nodeId] = {
      testvar1: [],
      testvar2: [],
    };
  }
  queueChartRender();
}

function toggleChartPaused() {
  chartPaused.value = !chartPaused.value;
  if (!chartPaused.value) {
    queueChartRender();
    queueQuickChartRender();
  }
}

function formatChartTime(value) {
  const date = new Date(value);
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

function updateNodeHistory(items) {
  for (const node of items) {
    ensureHistory(node.node_id);
    pushHistoryPoint(chartHistory[node.node_id].testvar1, node.testvar1_uint32);
    pushHistoryPoint(chartHistory[node.node_id].testvar2, node.testvar2_uint16);
  }
  if (activeTab.value === "monitor" && !chartPaused.value) {
    queueChartRender();
  }
  if (chartModalOpen.value && !chartPaused.value) {
    queueQuickChartRender();
  }
}

function openMetricChart(nodeId, metric) {
  chartNodeId.value = nodeId;
  quickChartMetric.value = metric;
  chartModalOpen.value = true;
  chartPaused.value = false;
  queueQuickChartRender();
}

function closeChart() {
  chartModalOpen.value = false;
  chartPaused.value = false;
  chartRenderToken += 1;
  if (quickChartInstance) {
    quickChartInstance.dispose();
    quickChartInstance = null;
  }
}

function disposeChart() {
  if (chartInstancePrimary) {
    chartInstancePrimary.dispose();
    chartInstancePrimary = null;
  }
  if (chartInstanceSecondary) {
    chartInstanceSecondary.dispose();
    chartInstanceSecondary = null;
  }
}

async function queueChartRender() {
  const token = ++chartRenderToken;
  await nextTick();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (token !== chartRenderToken) {
        return;
      }
      renderChart();
    });
  });
}

async function queueQuickChartRender() {
  const token = ++chartRenderToken;
  await nextTick();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (token !== chartRenderToken) {
        return;
      }
      renderQuickChart();
    });
  });
}

function renderQuickChart() {
  if (!chartModalOpen.value || !selectedChartNode.value || !quickChartHost.value) {
    return;
  }

  if (quickChartInstance && quickChartInstance.getDom() !== quickChartHost.value) {
    quickChartInstance.dispose();
    quickChartInstance = null;
  }

  if (!quickChartInstance) {
    quickChartInstance = echarts.init(quickChartHost.value);
    bindChartZoomTracking(quickChartInstance, chartZoomState.quick);
  }

  const metric = quickChartMetric.value;
  const label = metric === "testvar1" ? "0x2000 testvar1 uint32" : "0x2001 testvar2 uint16";
  const color = metric === "testvar1" ? "#0e6a73" : "#bf5b2c";
  const series = (chartHistory[selectedChartNode.value.node_id]?.[metric] ?? []).map((point) => [point.timestamp, point.value]);

  quickChartInstance.setOption({
    animation: true,
    backgroundColor: "transparent",
    grid: { left: 56, right: 24, top: 46, bottom: 36 },
    tooltip: { trigger: "axis" },
    dataZoom: [
      {
        type: "inside",
        yAxisIndex: [0],
        filterMode: "none",
        zoomLock: false,
        throttle: 50,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        start: chartZoomState.quick.yStart,
        end: chartZoomState.quick.yEnd,
      },
    ],
    xAxis: {
      type: "time",
      name: "time",
      nameTextStyle: { color: "#74675a" },
      axisLabel: {
        color: "#74675a",
        hideOverlap: true,
        formatter: (value) => formatChartTime(value),
      },
      axisLine: { lineStyle: { color: "rgba(61, 44, 29, 0.18)" } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: "#74675a",
        formatter: (value) => `${Number(value)}`,
      },
      splitLine: { lineStyle: { color: "rgba(61, 44, 29, 0.08)" } },
    },
    title: {
      text: `node${selectedChartNode.value.node_id} · ${label}`,
      left: 10,
      top: 8,
      textStyle: { fontSize: 14, color: "#23170f" },
    },
    graphic:
      series.length === 0
        ? [
            {
              type: "text",
              left: "center",
              top: "middle",
              style: {
                text: "Waiting for samples",
                fill: "#74675a",
                fontSize: 14,
              },
            },
          ]
        : [],
    series: [
      {
        name: label,
        type: "line",
        smooth: true,
        symbol: "none",
        animationDuration: 500,
        animationDurationUpdate: 240,
        endLabel: {
          show: series.length > 0,
          formatter: ({ value }) => `${value[1]}`,
          color,
          fontWeight: 700,
        },
        labelLayout: { moveOverlap: "shiftY" },
        lineStyle: { width: 3, color },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: `${color}33` },
            { offset: 1, color: `${color}05` },
          ]),
        },
        data: series,
      },
    ],
  }, false);

  quickChartInstance.resize();
}

function renderChart() {
  if (activeTab.value !== "monitor") {
    return;
  }

  if (chartInstancePrimary && chartInstancePrimary.getDom() !== chartHostPrimary.value) {
    chartInstancePrimary.dispose();
    chartInstancePrimary = null;
  }
  if (chartInstanceSecondary && chartInstanceSecondary.getDom() !== chartHostSecondary.value) {
    chartInstanceSecondary.dispose();
    chartInstanceSecondary = null;
  }

  if (chartHostPrimary.value && !chartInstancePrimary) {
    chartInstancePrimary = echarts.init(chartHostPrimary.value);
    bindChartZoomTracking(chartInstancePrimary, chartZoomState.primary);
  }
  if (chartHostSecondary.value && !chartInstanceSecondary) {
    chartInstanceSecondary = echarts.init(chartHostSecondary.value);
    bindChartZoomTracking(chartInstanceSecondary, chartZoomState.secondary);
  }

  const chartNodes = monitoredNodes.value;
  const focusedMode = chartNodes.length === 1;
  const chartSeries1 = chartNodes.map((node, index) => ({
    name: `node${node.node_id}`,
    type: "line",
    smooth: true,
    symbol: "none",
    animationDuration: 500,
    animationDurationUpdate: 240,
    endLabel: {
      show: (chartHistory[node.node_id]?.testvar1?.length ?? 0) > 0,
      formatter: ({ value }) => `${value[1]}`,
      color: ["#0e6a73", "#bf5b2c", "#4169e1", "#2e7d32"][index % 4],
      fontWeight: 700,
    },
    labelLayout: { moveOverlap: "shiftY" },
    lineStyle: { width: 3, color: ["#0e6a73", "#bf5b2c", "#4169e1", "#2e7d32"][index % 4] },
    areaStyle: focusedMode
      ? {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(14, 106, 115, 0.24)" },
            { offset: 1, color: "rgba(14, 106, 115, 0.02)" },
          ]),
        }
      : undefined,
    data: (chartHistory[node.node_id]?.testvar1 ?? []).map((point) => [point.timestamp, point.value]),
  }));
  const chartSeries2 = chartNodes.map((node, index) => ({
    name: `node${node.node_id}`,
    type: "line",
    smooth: true,
    symbol: "none",
    animationDuration: 500,
    animationDurationUpdate: 240,
    endLabel: {
      show: (chartHistory[node.node_id]?.testvar2?.length ?? 0) > 0,
      formatter: ({ value }) => `${value[1]}`,
      color: ["#bf5b2c", "#0e6a73", "#4169e1", "#2e7d32"][index % 4],
      fontWeight: 700,
    },
    labelLayout: { moveOverlap: "shiftY" },
    lineStyle: { width: 3, color: ["#bf5b2c", "#0e6a73", "#4169e1", "#2e7d32"][index % 4] },
    areaStyle: focusedMode
      ? {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(191, 91, 44, 0.24)" },
            { offset: 1, color: "rgba(191, 91, 44, 0.02)" },
          ]),
        }
      : undefined,
    data: (chartHistory[node.node_id]?.testvar2 ?? []).map((point) => [point.timestamp, point.value]),
  }));

  const baseOption = {
    animation: true,
    backgroundColor: "transparent",
    grid: { left: 56, right: 40, top: 46, bottom: 36 },
    tooltip: { trigger: "axis" },
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: [0],
        filterMode: "none",
        disabled: true,
      },
      {
        type: "inside",
        yAxisIndex: [0],
        filterMode: "none",
        zoomLock: false,
        throttle: 50,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        start: 0,
        end: 100,
      },
    ],
    xAxis: {
      type: "time",
      name: "time",
      nameTextStyle: { color: "#74675a" },
      axisLabel: {
        color: "#74675a",
        hideOverlap: true,
        formatter: (value) => formatChartTime(value),
      },
      axisLine: { lineStyle: { color: "rgba(61, 44, 29, 0.18)" } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: "#74675a",
        formatter: (value) => `${Number(value)}`,
      },
      splitLine: { lineStyle: { color: "rgba(61, 44, 29, 0.08)" } },
    },
    legend: {
      show: chartNodes.length > 1,
      top: 10,
      right: 16,
      textStyle: { color: "#74675a" },
    },
  };

  if (chartInstancePrimary) {
    chartInstancePrimary.setOption({
      ...baseOption,
      dataZoom: [
        baseOption.dataZoom[0],
        {
          ...baseOption.dataZoom[1],
          start: chartZoomState.primary.yStart,
          end: chartZoomState.primary.yEnd,
        },
      ],
      title: {
        text: focusedMode ? `node${chartNodes[0].node_id} · 0x2000 testvar1 uint32` : `Selected Nodes · 0x2000 testvar1 uint32`,
        left: 10,
        top: 8,
        textStyle: { fontSize: 14, color: "#23170f" },
      },
      graphic:
        chartSeries1.every((series) => series.data.length === 0)
          ? [
              {
                type: "text",
                left: "center",
                top: "middle",
                style: {
                  text: "Waiting for samples",
                  fill: "#74675a",
                  fontSize: 14,
                },
              },
            ]
          : [],
      series: chartSeries1,
    }, false);
    chartInstancePrimary.resize();
  }

  if (chartInstanceSecondary) {
    chartInstanceSecondary.setOption({
      ...baseOption,
      dataZoom: [
        baseOption.dataZoom[0],
        {
          ...baseOption.dataZoom[1],
          start: chartZoomState.secondary.yStart,
          end: chartZoomState.secondary.yEnd,
        },
      ],
      title: {
        text: focusedMode ? `node${chartNodes[0].node_id} · 0x2001 testvar2 uint16` : `Selected Nodes · 0x2001 testvar2 uint16`,
        left: 10,
        top: 8,
        textStyle: { fontSize: 14, color: "#23170f" },
      },
      graphic:
        chartSeries2.every((series) => series.data.length === 0)
          ? [
              {
                type: "text",
                left: "center",
                top: "middle",
                style: {
                  text: "Waiting for samples",
                  fill: "#74675a",
                  fontSize: 14,
                },
              },
            ]
          : [],
      series: chartSeries2,
    }, false);
    chartInstanceSecondary.resize();
  }
}

function handleWindowResize() {
  if (activeTab.value === "monitor") {
    queueChartRender();
  }
  if (chartModalOpen.value) {
    queueQuickChartRender();
  }
}

function syncNodeForms(items) {
  for (const node of items) {
    if (!(node.node_id in writeState)) {
      writeState[node.node_id] = {
        index: "",
        subindex: "",
        value: "",
      };
    }
    if (!(node.node_id in domainState)) {
      domainState[node.node_id] = {
        index: "0x2004",
        subindex: "0",
        hexData: "",
        length: 0,
      };
    }
    if (!(node.node_id in heartbeatState)) {
      heartbeatState[node.node_id] = {
        producerTimeMs: "",
      };
    }
    if (!(node.node_id in nmtState)) {
      nmtState[node.node_id] = {
        currentState: node.nmt_state || "",
        targetState: "OPERATIONAL",
      };
    } else {
      nmtState[node.node_id].currentState = node.nmt_state || nmtState[node.node_id].currentState;
    }
    if (!(node.node_id in syncState)) {
      syncState[node.node_id] = {
        enabled: false,
        cobId: "",
        periodUs: "",
      };
    }
    if (!(node.node_id in tpdoState)) {
      tpdoState[node.node_id] = {
        enabled: false,
        cobId: "",
        transmissionType: "",
        inhibitTime: "",
        eventTimer: "",
        syncStartValue: "",
      };
    }
    if (!(node.node_id in nodeUiState)) {
      nodeUiState[node.node_id] = {
        expanded: true,
        pane: "sync",
        sections: {
          nmt: true,
          sync: true,
          tpdo: true,
          sdo: true,
          domain: true,
        },
      };
    }
  }
}

function parseIndex(value) {
  if (typeof value === "string" && value.toLowerCase().startsWith("0x")) {
    return Number.parseInt(value, 16);
  }
  return Number(value);
}

function applySyncConfig(config) {
  syncState[config.node_id] = {
    enabled: config.enabled,
    cobId: `0x${config.cob_id.toString(16)}`,
    periodUs: String(config.period_us),
  };
}

function applyHeartbeatConfig(config) {
  heartbeatState[config.node_id] = {
    producerTimeMs: String(config.producer_time_ms),
  };
}

function applyNmtConfig(config) {
  const existingTarget = nmtState[config.node_id]?.targetState;
  nmtState[config.node_id] = {
    currentState: config.state,
    targetState: existingTarget || config.state || "OPERATIONAL",
  };
}

function applyTpdoConfig(config) {
  tpdoState[config.node_id] = {
    enabled: config.enabled,
    cobId: `0x${config.cob_id.toString(16)}`,
    transmissionType: formatNumericInput(config.transmission_type),
    inhibitTime: formatNumericInput(config.inhibit_time),
    eventTimer: formatNumericInput(config.event_timer),
    syncStartValue: formatNumericInput(config.sync_start_value),
  };
}

function isSyncLocked(nodeId) {
  return activeSyncProducerId.value !== null && activeSyncProducerId.value !== nodeId;
}

function syncBadge(nodeId) {
  if (syncState[nodeId]?.enabled) {
    return "Producer";
  }
  if (isSyncLocked(nodeId)) {
    return `Locked by node${activeSyncProducerId.value}`;
  }
  return "Standby";
}

function toggleNodeExpanded(nodeId) {
  nodeUiState[nodeId].expanded = !nodeUiState[nodeId].expanded;
}

function setNodePane(nodeId, pane) {
  nodeUiState[nodeId].pane = pane;
}

async function ensureSyncConfigs(nodeIds) {
  const pending = nodeIds.filter((nodeId) => !loadingSyncNodes.has(nodeId) && !loadedSyncNodes.has(nodeId));
  if (pending.length === 0 || !connection.value.connected) {
    return;
  }
  await Promise.all(
    pending.map(async (nodeId) => {
      loadingSyncNodes.add(nodeId);
      try {
        applySyncConfig(await fetchSyncConfig(nodeId));
        loadedSyncNodes.add(nodeId);
      } catch (error) {
        status.value = `SYNC read failed: ${error.message}`;
      } finally {
        loadingSyncNodes.delete(nodeId);
      }
    }),
  );
}

async function ensureHeartbeatConfigs(nodeIds) {
  const pending = nodeIds.filter(
    (nodeId) => !loadingHeartbeatNodes.has(nodeId) && !loadedHeartbeatNodes.has(nodeId),
  );
  if (pending.length === 0 || !connection.value.connected) {
    return;
  }
  await Promise.all(
    pending.map(async (nodeId) => {
      loadingHeartbeatNodes.add(nodeId);
      try {
        applyHeartbeatConfig(await fetchHeartbeatConfig(nodeId));
        loadedHeartbeatNodes.add(nodeId);
      } catch (error) {
        status.value = `Heartbeat read failed: ${error.message}`;
      } finally {
        loadingHeartbeatNodes.delete(nodeId);
      }
    }),
  );
}

async function ensureNmtConfigs(nodeIds) {
  const pending = nodeIds.filter((nodeId) => !loadingNmtNodes.has(nodeId) && !loadedNmtNodes.has(nodeId));
  if (pending.length === 0 || !connection.value.connected) {
    return;
  }
  await Promise.all(
    pending.map(async (nodeId) => {
      loadingNmtNodes.add(nodeId);
      try {
        applyNmtConfig(await fetchNmtConfig(nodeId));
        loadedNmtNodes.add(nodeId);
      } catch (error) {
        status.value = `NMT read failed: ${error.message}`;
      } finally {
        loadingNmtNodes.delete(nodeId);
      }
    }),
  );
}

async function ensureTpdoConfigs(nodeIds) {
  const pending = nodeIds.filter((nodeId) => !loadingTpdoNodes.has(nodeId) && !loadedTpdoNodes.has(nodeId));
  if (pending.length === 0 || !connection.value.connected) {
    return;
  }
  await Promise.all(
    pending.map(async (nodeId) => {
      loadingTpdoNodes.add(nodeId);
      try {
        applyTpdoConfig(await fetchTpdo1Config(nodeId));
        loadedTpdoNodes.add(nodeId);
      } catch (error) {
        status.value = `TPDO1 read failed: ${error.message}`;
      } finally {
        loadingTpdoNodes.delete(nodeId);
      }
    }),
  );
}

async function refreshConfigs(items) {
  const nodeIds = items.filter((node) => node.connected).map((node) => node.node_id);
  await Promise.all([
    ensureHeartbeatConfigs(nodeIds),
    ensureNmtConfigs(nodeIds),
    ensureSyncConfigs(nodeIds),
    ensureTpdoConfigs(nodeIds),
  ]);
}

function makeDeviceKey(bustype, channel) {
  return `${bustype}|${channel}`;
}

async function loadCanDeviceOptions() {
  try {
    const devices = await fetchCanDevices();
    canDevices.value = devices;
    const currentKey = makeDeviceKey(connection.value.bustype, connection.value.channel);
    if (!devices.some((device) => makeDeviceKey(device.bustype, device.channel) === selectedDeviceKey.value)) {
      selectedDeviceKey.value = currentKey;
    }
  } catch (error) {
    status.value = `CAN device list failed: ${error.message}`;
  }
}

async function loadInitial() {
  const [items, logItems, connectionInfo, devices] = await Promise.all([
    fetchNodes(),
    fetchLogs(),
    fetchConnection(),
    fetchCanDevices(),
  ]);
  nodes.value = items;
  logs.value = logItems;
  connection.value = connectionInfo;
  canDevices.value = devices;
  selectedDeviceKey.value = makeDeviceKey(connectionInfo.bustype, connectionInfo.channel);
  reconcileMonitorSelection(items);
  syncNodeForms(items);
  updateNodeHistory(items);
  await refreshConfigs(items);
  status.value = "REST connected";
}

async function handleDeviceChange() {
  try {
    const selected = canDevices.value.find((device) => makeDeviceKey(device.bustype, device.channel) === selectedDeviceKey.value);
    if (!selected) {
      throw new Error("Selected CAN device is unavailable");
    }
    if (!selected.supported) {
      throw new Error("Selected device is visible in Windows but not supported by the current CAN backend");
    }
    connection.value = await updateConnectionDevice({
      bustype: selected.bustype,
      channel: selected.channel,
    });
    await loadCanDeviceOptions();
    status.value = `Selected ${selected.bustype} ${selected.channel}`;
  } catch (error) {
    status.value = `CAN device select failed: ${error.message}`;
    selectedDeviceKey.value = makeDeviceKey(connection.value.bustype, connection.value.channel);
  }
}

async function handleConnect() {
  try {
    connection.value = await connectBus();
    loadedHeartbeatNodes.clear();
    loadedNmtNodes.clear();
    loadedSyncNodes.clear();
    loadedTpdoNodes.clear();
    await Promise.all([loadInitial(), refreshConfigs(nodes.value)]);
  } catch (error) {
    status.value = `Connect failed: ${error.message}`;
  }
}

async function handleDisconnect() {
  try {
    connection.value = await disconnectBus();
    loadedHeartbeatNodes.clear();
    loadedNmtNodes.clear();
    loadedSyncNodes.clear();
    loadedTpdoNodes.clear();
  } catch (error) {
    status.value = `Disconnect failed: ${error.message}`;
  }
}

async function handleClearLogs() {
  try {
    await clearLogs();
    logs.value = [];
  } catch (error) {
    status.value = `Clear logs failed: ${error.message}`;
  }
}

async function handleAddNode() {
  try {
    const nodeId = Number(addNodeId.value);
    if (!Number.isInteger(nodeId) || nodeId < 1 || nodeId > 127) {
      throw new Error("Node ID must be an integer between 1 and 127");
    }
    await addNode(nodeId);
    addNodeId.value = "";
    await loadInitial();
    status.value = `Added node${nodeId}`;
  } catch (error) {
    status.value = `Add node failed: ${error.message}`;
  }
}

async function handleRemoveNode(nodeId) {
  try {
    await removeNode(nodeId);
    delete writeState[nodeId];
    delete domainState[nodeId];
    delete heartbeatState[nodeId];
    delete nmtState[nodeId];
    delete syncState[nodeId];
    delete tpdoState[nodeId];
    delete nodeUiState[nodeId];
    await loadInitial();
    status.value = `Removed node${nodeId}`;
  } catch (error) {
    status.value = `Remove node failed: ${error.message}`;
  }
}

async function submit(nodeId) {
  try {
    const form = writeState[nodeId];
    await writeSdo(nodeId, {
      index: parseIndex(form.index),
      subindex: Number(form.subindex),
      value: parseNumericValue(form.value),
    });
  } catch (error) {
    status.value = `SDO write failed: ${error.message}`;
  }
}

async function refreshNode(nodeId) {
  try {
    await refreshNodeValues(nodeId);
  } catch (error) {
    status.value = `SDO read failed: ${error.message}`;
  }
}

async function loadDomain(nodeId) {
  try {
    const form = domainState[nodeId];
    const result = await readDomain(nodeId, {
      index: parseIndex(form.index),
      subindex: Number(form.subindex),
    });
    domainState[nodeId].hexData = result.hex_data;
    domainState[nodeId].length = result.length;
  } catch (error) {
    status.value = `DOMAIN read failed: ${error.message}`;
  }
}

async function submitDomain(nodeId) {
  try {
    const form = domainState[nodeId];
    const compactHex = form.hexData.replace(/\s+/g, "");
    const normalizedHex = compactHex.length % 2 === 0 ? compactHex : `0${compactHex}`;
    const result = await writeDomain(nodeId, {
      index: parseIndex(form.index),
      subindex: Number(form.subindex),
      hexData: normalizedHex,
    });
    domainState[nodeId].hexData = result.hex_data;
    domainState[nodeId].length = result.length;
  } catch (error) {
    status.value = `DOMAIN write failed: ${error.message}`;
  }
}

async function loadSync(nodeId) {
  try {
    applySyncConfig(await fetchSyncConfig(nodeId));
    loadedSyncNodes.add(nodeId);
  } catch (error) {
    status.value = `SYNC read failed: ${error.message}`;
  }
}

async function loadHeartbeat(nodeId) {
  try {
    applyHeartbeatConfig(await fetchHeartbeatConfig(nodeId));
    loadedHeartbeatNodes.add(nodeId);
  } catch (error) {
    status.value = `Heartbeat read failed: ${error.message}`;
  }
}

async function loadNmt(nodeId) {
  try {
    applyNmtConfig(await fetchNmtConfig(nodeId));
    loadedNmtNodes.add(nodeId);
  } catch (error) {
    status.value = `NMT read failed: ${error.message}`;
  }
}

async function submitHeartbeat(nodeId) {
  try {
    const form = heartbeatState[nodeId];
    const producerTimeMs = parseNumericValue(form.producerTimeMs);
    if (!Number.isInteger(producerTimeMs) || producerTimeMs < 0 || producerTimeMs >= 1000) {
      throw new Error("Heartbeat producer time must be 0-999 ms");
    }
    applyHeartbeatConfig(
      await writeHeartbeatConfig(nodeId, {
        producer_time_ms: producerTimeMs,
      }),
    );
    loadedHeartbeatNodes.add(nodeId);
  } catch (error) {
    status.value = `Heartbeat apply failed: ${error.message}`;
  }
}

async function submitNmt(nodeId) {
  try {
    const form = nmtState[nodeId];
    applyNmtConfig(
      await writeNmtConfig(nodeId, {
        state: form.targetState,
      }),
    );
    loadedNmtNodes.add(nodeId);
  } catch (error) {
    status.value = `NMT apply failed: ${error.message}`;
  }
}

async function toggleSync(nodeId) {
  await submitSync(nodeId);
}

async function commitSyncDraft(nodeId) {
  if (!syncState[nodeId]?.enabled) {
    return;
  }
  await submitSync(nodeId);
}

async function submitSync(nodeId) {
  try {
    const form = syncState[nodeId];
    await writeSyncConfig(nodeId, {
      enabled: Boolean(form.enabled),
      cob_id: parseIndex(form.cobId),
      period_us: parseNumericValue(form.periodUs),
    });
    loadedSyncNodes.clear();
    for (const node of nodes.value) {
      await loadSync(node.node_id);
    }
  } catch (error) {
    status.value = `SYNC apply failed: ${error.message}`;
  }
}

async function loadTpdo1(nodeId) {
  try {
    applyTpdoConfig(await fetchTpdo1Config(nodeId));
    loadedTpdoNodes.add(nodeId);
  } catch (error) {
    status.value = `TPDO1 read failed: ${error.message}`;
  }
}

async function toggleTpdo1(nodeId) {
  const previousEnabled = !tpdoState[nodeId].enabled;
  try {
    await submitTpdo1(nodeId);
  } catch (error) {
    tpdoState[nodeId].enabled = previousEnabled;
  }
}

async function submitTpdo1(nodeId) {
  const form = tpdoState[nodeId];
  try {
    applyTpdoConfig(
      await writeTpdo1Config(nodeId, {
        enabled: Boolean(form.enabled),
        cob_id: parseIndex(form.cobId),
        transmission_type: parseNumericValue(form.transmissionType),
        inhibit_time: parseNumericValue(form.inhibitTime),
        event_timer: parseNumericValue(form.eventTimer),
        sync_start_value: parseNumericValue(form.syncStartValue),
      }),
    );
    loadedTpdoNodes.add(nodeId);
  } catch (error) {
    status.value = `TPDO1 apply failed: ${error.message}`;
    throw error;
  }
}

onMounted(async () => {
  applyInitialViewState();
  try {
    await loadInitial();
    await scrollLogsToBottom();
  } catch (error) {
    status.value = `REST failed: ${error.message}`;
  }

  window.addEventListener("resize", handleWindowResize);

  socket = connectNodesSocket((items) => {
    nodes.value = items.items;
    logs.value = items.logs;
    reconcileMonitorSelection(items.items);
    updateNodeHistory(items.items);
    fetchConnection().then((result) => {
      connection.value = result;
      if (result.connected) {
        syncNodeForms(items.items);
        refreshConfigs(items.items);
      }
    });
    syncNodeForms(items.items);
    status.value = "WebSocket live";
  });

  socket.addEventListener("close", () => {
    status.value = "WebSocket disconnected";
  });
});

watch([activeTab, chartNodeId], async ([tab]) => {
  if (tab !== "monitor") {
    return;
  }
  await queueChartRender();
});

watch(
  () => pythonLogs.value.length,
  async () => {
    await scrollLogsToBottom();
  },
);

onBeforeUnmount(() => {
  if (socket) {
    socket.close();
  }
  window.removeEventListener("resize", handleWindowResize);
  disposeChart();
});
</script>

<template>
  <main class="page">
    <section class="hero">
      <div class="hero-brand">
        <img class="hero-logo" src="/hero-logo.png" alt="STM32F429I CANopenNode logo" />
        <div>
          <p class="eyebrow">STM32F429I CANopenNode</p>
          <h1>Node Control Panel</h1>
        </div>
      </div>
      <p class="lead">
        This page starts with the core test variables from the current object dictionary
        and can grow into a full OD browser later.
      </p>
      <div class="status">{{ status }}</div>
      <div class="connection-bar">
        <div class="connection-info">
          <span>
            {{ connection.connected ? "Connected" : "Disconnected" }}
            {{ connection.bustype }} {{ connection.channel }} @ {{ connection.bitrate }}
          </span>
          <label class="channel-picker">
            <span>CAN Device</span>
            <select v-model="selectedDeviceKey" :disabled="connection.connected" @change="handleDeviceChange">
              <optgroup v-for="group in groupedCanDevices" :key="group.label" :label="group.label">
                <option
                  v-for="port in group.items"
                  :key="`${port.bustype}-${port.channel}`"
                  :value="`${port.bustype}|${port.channel}`"
                  :disabled="!port.supported"
                >
                  {{ port.label }}{{ port.description && port.description !== port.label ? ` - ${port.description}` : "" }}{{ !port.supported ? " (unsupported)" : "" }}
                </option>
              </optgroup>
              <option
                v-if="selectedDeviceKey && !canDevices.some((port) => `${port.bustype}|${port.channel}` === selectedDeviceKey)"
                :value="selectedDeviceKey"
              >
                {{ selectedDeviceKey }}
              </option>
            </select>
          </label>
        </div>
        <div class="connection-actions">
          <button v-if="!connection.connected" type="button" @click="handleConnect">Connect</button>
          <button v-else type="button" class="secondary" @click="handleDisconnect">Disconnect</button>
        </div>
      </div>
    </section>

    <section class="tabs">
      <div class="tabs-indicator" :style="{ transform: `translateX(${activeTabIndex * 100}%)` }"></div>
      <button
        type="button"
        :class="['tab', { active: activeTab === 'overview' }]"
        @click="setActiveTab('overview')"
      >
        Overview
      </button>
      <button
        type="button"
        :class="['tab', { active: activeTab === 'nodes' }]"
        @click="setActiveTab('nodes')"
      >
        Node Config
      </button>
      <button
        type="button"
        :class="['tab', { active: activeTab === 'monitor' }]"
        @click="setActiveTab('monitor')"
      >
        Monitor
      </button>
    </section>

    <Transition :name="tabTransitionName" mode="out-in">
        <section v-if="activeTab === 'overview'" key="overview" class="overview-grid">
          <article class="overview-card">
            <h2>Bus Status</h2>
            <dl class="overview-metrics overview-metrics-carded">
              <div class="overview-metric-card overview-metric-connection">
                <dt>Connection</dt>
                <dd>{{ connection.connected ? "Connected" : "Disconnected" }}</dd>
              </div>
              <div class="overview-metric-card overview-metric-nodes">
                <dt>Nodes</dt>
                <dd>{{ nodes.length }}</dd>
              </div>
              <div class="overview-metric-card overview-metric-sync">
                <dt>Active SYNC Producer</dt>
                <dd>{{ activeSyncProducerLabel }}</dd>
              </div>
            </dl>
          </article>

        <article class="overview-card">
          <div class="overview-head">
            <h2>Node Summary</h2>
            <div class="node-manager">
              <input
                v-model="addNodeId"
                class="node-id-input"
                type="number"
                min="1"
                max="127"
                placeholder="Node ID"
                @keydown.enter.prevent="handleAddNode"
              />
              <button type="button" class="secondary small-button" @click="handleAddNode">Add Node</button>
            </div>
          </div>
            <div class="summary-list">
              <p v-if="nodes.length === 0" class="muted">No live nodes.</p>
              <div v-for="node in nodes" :key="`summary-${node.node_id}`" class="summary-item">
                <strong class="summary-node-label">Node {{ node.node_id }}</strong>
                <span :class="['pill', node.connected ? 'ok' : 'bad']">
                  {{ node.connected ? node.nmt_state : "OFFLINE" }}
                </span>
                <button type="button" class="value-link" @click="openMetricChart(node.node_id, 'testvar1')">
                 <code class="summary-code summary-code-testvar1">0x2000={{ formatValue(node.testvar1_uint32) }}</code>
                </button>
                <button type="button" class="value-link" @click="openMetricChart(node.node_id, 'testvar2')">
                 <code class="summary-code summary-code-testvar2">0x2001={{ formatValue(node.testvar2_uint16) }}</code>
                </button>
              </div>
            </div>
        </article>
      </section>

      <section v-else-if="activeTab === 'nodes'" key="nodes" class="grid">
      <article
        v-for="(node, index) in nodes"
        :key="node.node_id"
        class="card node-detail"
        :style="{ '--stagger': `${index * 60}ms` }"
      >
        <button type="button" class="card-toggle" @click="toggleNodeExpanded(node.node_id)">
          <div class="card-head">
            <div class="card-title-block">
              <h2 class="node-title">
                <span class="node-title-label">Node</span>
                <span :class="['node-title-id', { online: node.connected, offline: !node.connected }]">{{ node.node_id }}</span>
              </h2>
              <p v-if="node.status_note" class="node-status-note">{{ node.status_note }}</p>
            </div>
            <div class="card-toggle-right">
              <button
                type="button"
                class="ghost-button danger-button"
                @click.stop="handleRemoveNode(node.node_id)"
              >
                Delete
              </button>
              <span :class="['pill', node.connected ? 'ok' : 'bad']">
                {{ node.connected ? node.nmt_state : "OFFLINE" }}
              </span>
              <span class="chevron">{{ nodeUiState[node.node_id].expanded ? "▾" : "▸" }}</span>
            </div>
          </div>
        </button>

        <Transition name="section-collapse">
        <div v-if="nodeUiState[node.node_id].expanded">
          <dl class="metrics metrics-carded">
            <div class="metric-row metric-card metric-card-testvar1">
              <button type="button" class="metric-trigger" @click="openMetricChart(node.node_id, 'testvar1')">
              <div class="metric-body">
             <dt>0x2000 testvar1 uint32</dt>
             <dd>{{ formatValue(node.testvar1_uint32) }}</dd>
              </div>
              </button>
            </div>
            <div class="metric-row metric-card metric-card-testvar2">
              <button type="button" class="metric-trigger" @click="openMetricChart(node.node_id, 'testvar2')">
              <div class="metric-body">
             <dt>0x2001 testvar2 uint16</dt>
             <dd>{{ formatValue(node.testvar2_uint16) }}</dd>
              </div>
              </button>
            </div>
            <div class="metric-card metric-card-heartbeat">
             <dt>Heartbeat</dt>
             <dd>{{ node.heartbeat_ms ?? "-" }} ms</dd>
            </div>
          </dl>

        <div class="node-pane-tabs">
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'heartbeat' }]" @click="setNodePane(node.node_id, 'heartbeat')">HB</button>
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'nmt' }]" @click="setNodePane(node.node_id, 'nmt')">NMT</button>
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'sync' }]" @click="setNodePane(node.node_id, 'sync')">SYNC</button>
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'tpdo' }]" @click="setNodePane(node.node_id, 'tpdo')">TPDO1</button>
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'sdo' }]" @click="setNodePane(node.node_id, 'sdo')">SDO</button>
          <button type="button" :class="['node-pane-tab', { active: nodeUiState[node.node_id].pane === 'domain' }]" @click="setNodePane(node.node_id, 'domain')">Domain</button>
        </div>

        <section
          v-if="nodeUiState[node.node_id].pane === 'heartbeat'"
          class="panel-block panel-sync"
        >
          <div class="panel-head">
            <h3>Heartbeat</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">{{ node.heartbeat_ms ?? 0 }} ms</span>
            </div>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-heartbeat-ms`">Producer Time (ms)</label>
              <input
                :id="`node-${node.node_id}-heartbeat-ms`"
                v-model="heartbeatState[node.node_id].producerTimeMs"
                type="text"
                inputmode="text"
              />
            </div>
            <div class="heartbeat-placeholder"></div>
          </div>
          <div class="editor-actions panel-actions">
            <button type="button" class="secondary" @click="loadHeartbeat(node.node_id)">Read HB</button>
            <button type="button" @click="submitHeartbeat(node.node_id)">Apply HB</button>
          </div>
          <p class="panel-hint">Heartbeat producer time must be 0-999 ms and is shown in decimal.</p>
        </section>

        <section
          v-else-if="nodeUiState[node.node_id].pane === 'nmt'"
          class="panel-block panel-nmt"
        >
          <div class="panel-head">
            <h3>NMT</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">{{ nmtState[node.node_id]?.currentState || node.nmt_state }}</span>
            </div>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-nmt-target`">Target State</label>
              <select
                :id="`node-${node.node_id}-nmt-target`"
                v-model="nmtState[node.node_id].targetState"
              >
                <option value="OPERATIONAL">OPERATIONAL</option>
                <option value="PRE-OPERATIONAL">PRE-OP</option>
                <option value="STOPPED">STOPPED</option>
                <option value="RESET">RESET</option>
                <option value="RESET COMMUNICATION">RESET COMM</option>
              </select>
            </div>
            <div class="heartbeat-placeholder"></div>
          </div>
          <p class="panel-hint">Apply sends the selected NMT command to this node.</p>
          <div class="editor-actions panel-actions">
            <button type="button" @click="submitNmt(node.node_id)">Apply NMT</button>
          </div>
        </section>

        <section
          v-else-if="nodeUiState[node.node_id].pane === 'sync'"
          :class="['panel-block', 'panel-sync', { 'is-disabled': isSyncLocked(node.node_id) }]"
        >
          <div class="panel-head">
            <h3>SYNC</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">{{ syncBadge(node.node_id) }}</span>
            </div>
          </div>
          <fieldset class="panel-fields" :disabled="isSyncLocked(node.node_id)">
            <div class="toggle-row">
              <label class="checkbox">
                <input
                  v-model="syncState[node.node_id].enabled"
                  type="checkbox"
                  @change="toggleSync(node.node_id)"
                />
                <span>Enable SYNC Producer</span>
              </label>
            </div>
            <div class="editor-grid">
              <div>
                <label :for="`node-${node.node_id}-sync-cob-id`">SYNC COB-ID</label>
                <input
                  :id="`node-${node.node_id}-sync-cob-id`"
                  v-model="syncState[node.node_id].cobId"
                  type="text"
                  inputmode="text"
                  :disabled="syncState[node.node_id].enabled"
                  @blur="commitSyncDraft(node.node_id)"
                  @keydown.enter.prevent="commitSyncDraft(node.node_id)"
                />
              </div>
              <div>
                <label :for="`node-${node.node_id}-sync-period`">Period (us)</label>
                <input
                  :id="`node-${node.node_id}-sync-period`"
                  v-model="syncState[node.node_id].periodUs"
                  type="text"
                  inputmode="text"
                  :disabled="syncState[node.node_id].enabled"
                  @blur="commitSyncDraft(node.node_id)"
                  @keydown.enter.prevent="commitSyncDraft(node.node_id)"
                />
              </div>
            </div>
          </fieldset>
          <p v-if="isSyncLocked(node.node_id)" class="panel-hint">
            {{ activeSyncProducerLabel }} is the active SYNC producer. Disable it first to switch.
          </p>
        </section>

        <section
          v-else-if="nodeUiState[node.node_id].pane === 'tpdo'"
          class="panel-block panel-tpdo"
        >
          <div class="panel-head">
            <h3>TPDO1</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">{{ tpdoState[node.node_id]?.enabled ? "Enabled" : "Disabled" }}</span>
            </div>
          </div>
          <div class="toggle-row">
            <label class="checkbox">
              <input
                v-model="tpdoState[node.node_id].enabled"
                type="checkbox"
                @change="toggleTpdo1(node.node_id)"
              />
              <span>Enable TPDO1</span>
            </label>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-tpdo-cob-id`">COB-ID</label>
              <input
                :id="`node-${node.node_id}-tpdo-cob-id`"
                v-model="tpdoState[node.node_id].cobId"
                type="text"
                inputmode="text"
                :disabled="tpdoState[node.node_id].enabled"
              />
            </div>
            <div>
              <label :for="`node-${node.node_id}-tpdo-transmission`">Transmission Type</label>
              <input
                :id="`node-${node.node_id}-tpdo-transmission`"
                v-model="tpdoState[node.node_id].transmissionType"
                type="text"
                inputmode="text"
                :disabled="tpdoState[node.node_id].enabled"
              />
            </div>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-tpdo-inhibit`">Inhibit Time</label>
              <input
                :id="`node-${node.node_id}-tpdo-inhibit`"
                v-model="tpdoState[node.node_id].inhibitTime"
                type="text"
                inputmode="text"
                :disabled="tpdoState[node.node_id].enabled"
              />
            </div>
            <div>
              <label :for="`node-${node.node_id}-tpdo-event`">Event Timer</label>
              <input
                :id="`node-${node.node_id}-tpdo-event`"
                v-model="tpdoState[node.node_id].eventTimer"
                type="text"
                inputmode="text"
                :disabled="tpdoState[node.node_id].enabled"
              />
            </div>
            <div>
              <label :for="`node-${node.node_id}-tpdo-sync-start`">SYNC Start Value</label>
              <input
                :id="`node-${node.node_id}-tpdo-sync-start`"
                v-model="tpdoState[node.node_id].syncStartValue"
                type="text"
                inputmode="text"
                :disabled="tpdoState[node.node_id].enabled"
              />
            </div>
          </div>
          <p v-if="tpdoState[node.node_id]?.enabled" class="panel-hint">
            Disable TPDO1 first to edit its communication parameters.
          </p>
        </section>
        <form
          v-else-if="nodeUiState[node.node_id].pane === 'sdo'"
          class="editor panel-block panel-sdo"
          @submit.prevent="submit(node.node_id)"
        >
          <div class="panel-head">
            <h3>SDO</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">Direct Access</span>
            </div>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-index`">Index</label>
              <input
                :id="`node-${node.node_id}-index`"
                v-model="writeState[node.node_id].index"
                type="text"
                inputmode="text"
              />
            </div>
            <div>
              <label :for="`node-${node.node_id}-subindex`">Subindex</label>
              <input
                :id="`node-${node.node_id}-subindex`"
                v-model="writeState[node.node_id].subindex"
                type="number"
                min="0"
              />
            </div>
          </div>
          <div>
            <label :for="`node-${node.node_id}-value`">Value</label>
            <input
              :id="`node-${node.node_id}-value`"
              v-model="writeState[node.node_id].value"
              type="text"
              inputmode="text"
            />
          </div>
          <div class="editor-actions">
            <button type="button" class="secondary" @click="refreshNode(node.node_id)">Read SDO</button>
            <button type="submit">Write SDO</button>
          </div>
        </form>

        <div v-else class="domain panel-block panel-domain">
          <div class="domain-head">
            <h3>Domain</h3>
            <div class="section-toggle-right">
              <span class="panel-badge">{{ domainState[node.node_id].length }} bytes</span>
            </div>
          </div>
          <div class="editor-grid">
            <div>
              <label :for="`node-${node.node_id}-domain-index`">Domain Index</label>
              <input
                :id="`node-${node.node_id}-domain-index`"
                v-model="domainState[node.node_id].index"
                type="text"
                inputmode="text"
              />
            </div>
            <div>
              <label :for="`node-${node.node_id}-domain-subindex`">Domain Subindex</label>
              <input
                :id="`node-${node.node_id}-domain-subindex`"
                v-model="domainState[node.node_id].subindex"
                type="number"
                min="0"
              />
            </div>
          </div>
          <div>
            <label :for="`node-${node.node_id}-domain-value`">Hex Payload</label>
            <textarea
              :id="`node-${node.node_id}-domain-value`"
              v-model="domainState[node.node_id].hexData"
              rows="4"
            />
          </div>
          <div class="domain-actions">
            <button type="button" class="secondary" @click="loadDomain(node.node_id)">Read Domain</button>
            <button type="button" @click="submitDomain(node.node_id)">Write Domain</button>
          </div>
        </div>

        <p v-if="node.last_error" class="error">{{ node.last_error }}</p>
        </div>
        </Transition>
      </article>
      </section>

      <section v-else key="monitor" class="chart-view">
        <article class="overview-card chart-card">
          <div class="chart-header">
            <div>
              <h2>Monitor</h2>
              <p class="muted">
                {{
                  monitoredNodes.length > 0
                    ? `${monitoredNodes.length} selected · ${connection.bustype} ${connection.channel}`
                    : `No nodes selected · ${connection.bustype} ${connection.channel}`
                }}
              </p>
            </div>
              <div class="chart-header-actions">
                <div class="chart-toolbar chart-toolbar-right">
                  <button type="button" class="ghost-button monitor-reset-button" @click="resetChartHistory">Reset</button>
                  <button type="button" class="ghost-button monitor-pause-button" @click="toggleChartPaused">
                    {{ chartPaused ? "Resume" : "Pause" }}
                  </button>
                </div>
              <div class="monitor-section monitor-section-selection">
                <div class="monitor-section-head">
                  <span class="monitor-section-label">Nodes Selection</span>
                </div>
                <div class="monitor-node-actions">
                  <button
                    type="button"
                    class="ghost-button monitor-select-all"
                    @click="selectAllMonitorNodes"
                  >
                    Select All
                  </button>
                  <button
                    type="button"
                    class="ghost-button monitor-clear-all"
                    @click="clearAllMonitorNodes"
                  >
                    Clear All
                  </button>
                </div>
              </div>
              <div class="monitor-divider"></div>
              <div class="monitor-section monitor-section-nodes">
                <div class="monitor-section-head">
                  <span class="monitor-section-label">Nodes</span>
                </div>
                <div class="monitor-node-strip">
                  <button
                    v-for="node in nodes"
                    :key="`monitor-chip-${node.node_id}`"
                    type="button"
                    :disabled="!node.connected"
                    :class="[
                      'ghost-button',
                      'monitor-node-chip',
                      {
                        active: selectedMonitorNodeIds.includes(node.node_id),
                        offline: !node.connected,
                      },
                    ]"
                    @click="toggleMonitorNode(node.node_id)"
                  >
                    node{{ node.node_id }}
                  </button>
                </div>
              </div>
              <div class="monitor-divider"></div>
              <div class="monitor-section monitor-section-variables">
                <div class="monitor-section-head">
                  <span class="monitor-section-label">Variables</span>
                </div>
                <div class="monitor-metric-strip">
                  <button
                    type="button"
                    :class="['ghost-button', 'monitor-metric-chip', { active: selectedMonitorMetrics.includes('testvar1') }]"
                    @click="toggleMonitorMetric('testvar1')"
                  >
                    0x2000 testvar1
                  </button>
                  <button
                    type="button"
                    :class="['ghost-button', 'monitor-metric-chip', { active: selectedMonitorMetrics.includes('testvar2') }]"
                    @click="toggleMonitorMetric('testvar2')"
                  >
                    0x2001 testvar2
                  </button>
                </div>
              </div>
            </div>
          </div>
          <div v-if="monitoredNodes.length > 0" class="chart-stack">
            <div v-if="selectedMonitorMetrics.includes('testvar1')" class="chart-panel">
              <div class="chart-panel-head">
                <span class="chart-panel-label">0x2000 testvar1 uint32</span>
                <button type="button" class="ghost-button monitor-zoom-reset-button" @click="resetChartsZoom('primary')">
                  Zoom Reset
                </button>
              </div>
              <div ref="chartHostPrimary" class="echart-host"></div>
            </div>
            <div v-if="selectedMonitorMetrics.includes('testvar2')" class="chart-panel">
              <div class="chart-panel-head">
                <span class="chart-panel-label">0x2001 testvar2 uint16</span>
                <button type="button" class="ghost-button monitor-zoom-reset-button" @click="resetChartsZoom('secondary')">
                  Zoom Reset
                </button>
              </div>
              <div ref="chartHostSecondary" class="echart-host"></div>
            </div>
          </div>
          <div v-else-if="selectedMonitorMetrics.length === 0" class="chart-empty">No metric selected.</div>
          <div v-else class="chart-empty">No node selected.</div>
        </article>
      </section>
    </Transition>

    <Transition name="chart-modal" @after-enter="queueQuickChartRender">
      <div v-if="chartModalOpen" class="chart-modal" @click="closeChart">
        <div class="chart-modal-shell" @click.stop>
          <article class="overview-card chart-card">
            <div class="chart-header">
              <div>
                <h2>{{ selectedChartNode ? `node${selectedChartNode.node_id} Live Chart` : "Charts" }}</h2>
                <p class="muted">
                  {{
                    selectedChartNode
                      ? `${selectedChartNode.connected ? selectedChartNode.nmt_state : "OFFLINE"} · ${connection.bustype} ${connection.channel}`
                      : "Select a node value to inspect real-time samples."
                  }}
                </p>
              </div>
              <div class="chart-header-actions">
                <button type="button" class="ghost-button monitor-zoom-reset-button" @click="resetQuickChartZoom">Zoom Reset</button>
                <button type="button" class="ghost-button" @click="resetChartHistory">Reset</button>
                <button type="button" class="ghost-button" @click="toggleChartPaused">
                  {{ chartPaused ? "Resume" : "Pause" }}
                </button>
              </div>
            </div>
            <div v-if="selectedChartNode" class="chart-stack">
              <div ref="quickChartHost" class="echart-host"></div>
            </div>
            <div v-else class="chart-empty">No node selected.</div>
          </article>
        </div>
      </div>
    </Transition>

    <section class="log-panel log-panel-wide">
      <div class="log-head">
        <h2>Python Output</h2>
        <div class="log-toolbar">
          <span>{{ pythonLogs.length }} entries</span>
          <button type="button" class="secondary small-button" @click="handleClearLogs">Clear</button>
        </div>
      </div>
      <div ref="logListHost" class="log-list">
        <p v-if="pythonLogs.length === 0" class="muted">No Python output yet.</p>
          <div
            v-for="entry in pythonLogs"
            :key="`${entry.timestamp}-${entry.source}-${entry.message}`"
            :class="[
              'log-item',
              `log-item-source-${entry.source.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`,
              `log-item-level-${entry.level.toLowerCase()}`,
            ]"
          >
            <span class="log-time">{{ entry.timestamp }}</span>
            <span class="log-source">{{ entry.source }}</span>
            <span :class="['log-level', entry.level.toLowerCase()]">{{ entry.level }}</span>
          <span class="log-message">{{ entry.message }}</span>
        </div>
      </div>
    </section>
  </main>
</template>
