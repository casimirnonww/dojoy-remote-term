// 机器清单由 hosts.json 经 deploy/render_config.py 生成到 hosts.js，这里不再手写。
const HOSTS = Array.isArray(window.REMOTE_TERM_HOSTS) ? window.REMOTE_TERM_HOSTS : [];
const MIN_SESSIONS = 1; // 切机默认 1 路，不自动开 3
const MAX_SESSIONS = 6;

const hostTabsEl = document.getElementById("hostTabs");
const sessionTabsEl = document.getElementById("sessionTabs");
const workspace = document.getElementById("workspace");
const emptyHint = document.getElementById("emptyHint");
const summary = document.getElementById("summary");
const newSessionBtn = document.getElementById("newSessionBtn");
const reconnectBtn = document.getElementById("reconnectBtn");
const overviewEl = document.getElementById("overview");
const overviewBtn = document.getElementById("overviewBtn");
const sessionRow = document.getElementById("sessionRow");
const hostGrid = document.getElementById("hostGrid");
const statusNotice = document.getElementById("statusNotice");
const refreshStatusBtn = document.getElementById("refreshStatusBtn");

const hosts = {};
let activeHostId = null;
let activeSessionKey = null;
let seq = 1;
let tearingDownAll = false;
let overviewVisible = true;
let snapshot = null;
let statusError = "";
let activeRequest = null;
let requestGeneration = 0;
let pollTimer = null;
let freshnessTimer = null;
let resumeRefresh = false;
const STATUS_INTERVAL_MS = 30000;
const FRESHNESS_MS = 90000;
const statusCards = new Map();

// 设备类型：图标和名称（颜色在 app.css 的 .kind-*）。图标是自绘的简单图形；未知类型按服务器显示。
const SVG_NS = "http://www.w3.org/2000/svg";
const KINDS = {
  linux: { label: "服务器", shapes: [
    ["rect", { x: 3, y: 3.5, width: 18, height: 7, rx: 1.8 }],
    ["rect", { x: 3, y: 13.5, width: 18, height: 7, rx: 1.8 }],
    ["path", { d: "M7 7h.01M7 17h.01M11 7h6M11 17h6" }],
  ] },
  mac: { label: "Mac 电脑", shapes: [
    // 苹果：果身右侧咬掉一口，上面一片叶子
    ["path", { class: "solid", d: "M12 7.2C10.6 6.2 8.4 6 6.9 6.9C4.6 8.3 4.2 11.6 5.2 14.6C6 17.2 7.6 20 9.3 20.3C10.4 20.5 10.9 19.8 12 19.8C13.1 19.8 13.6 20.5 14.7 20.3C16.3 20 17.7 17.6 18.6 15.2C16.9 14.5 15.9 13.1 15.9 11.6C15.9 10.3 16.7 9.3 17.6 8.8C16.6 7.4 15 6.4 13.4 6.7C12.8 6.8 12.4 7 12 7.2Z" }],
    ["path", { class: "solid", d: "M12.2 5.9C12.1 4.4 13.3 2.9 15.1 2.6C15.2 4.2 14 5.7 12.2 5.9Z" }],
  ] },
  windows: { label: "Windows 电脑", shapes: [
    ["rect", { x: 3.5, y: 3.5, width: 7.5, height: 7.5, rx: 1, class: "solid" }],
    ["rect", { x: 13, y: 3.5, width: 7.5, height: 7.5, rx: 1, class: "solid" }],
    ["rect", { x: 3.5, y: 13, width: 7.5, height: 7.5, rx: 1, class: "solid" }],
    ["rect", { x: 13, y: 13, width: 7.5, height: 7.5, rx: 1, class: "solid" }],
  ] },
};

function kindOf(host) { return Object.prototype.hasOwnProperty.call(KINDS, host.kind) ? host.kind : "linux"; }

function kindIcon(kind, className) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  KINDS[kind].shapes.forEach(([tag, attrs]) => {
    const shape = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(name => shape.setAttribute(name, attrs[name]));
    svg.append(shape);
  });
  return svg;
}

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function createStatusCard(host) {
  // 紧凑卡片：8 台在一屏内放下。说明文字只在出问题时显示，细节放在悬停提示里。
  const kind = kindOf(host);
  const card = el("article", "host-card kind-" + kind);
  const heading = el("div", "card-heading");
  const titleRow = el("div", "card-title");
  const icon = el("span", "kind-badge");
  icon.append(kindIcon(kind, "kind-icon"));
  titleRow.append(icon, el("h2", "", host.name));
  const badge = el("span", "status-badge", "尚无数据");
  heading.append(titleRow, badge);
  const type = el("p", "card-type");
  const hostname = el("span", "machine-name", "");
  type.append(el("span", "kind-tag", KINDS[kind].label), el("span", "card-meta", host.meta || ""), hostname);
  const spec = el("p", "card-spec", "—");
  const system = el("p", "card-spec card-os", "—");
  const metrics = el("div", "metrics");
  const gauges = {};
  [["cpu", "CPU"], ["memory", "内存"], ["disk", "磁盘"]].forEach(([key, label]) => {
    const row = el("div", "metric");
    const meter = el("div", "meter");
    meter.setAttribute("aria-hidden", "true");
    const fill = el("div", "meter-fill");
    meter.append(fill);
    const value = el("span", "metric-value", "—");
    row.append(el("span", "metric-label", label), meter, value);
    metrics.append(row);
    gauges[key] = { row, value, fill };
  });
  const networkRow = el("div", "metric metric-network");
  const network = el("span", "metric-value", "—");
  networkRow.append(el("span", "metric-label", "网络"), network);
  metrics.append(networkRow);
  // 底部左侧：正常时是运行时长和更新时间；离线、过期或数据异常时换成提示，不另占一行。
  const foot = el("div", "card-foot");
  const times = el("p", "card-times");
  const uptime = el("span", "", "运行 —");
  const successAt = el("time", "", "");
  times.append(uptime, successAt);
  const note = el("p", "card-note", "");
  const open = el("button", "btn primary", "打开终端");
  open.type = "button";
  open.setAttribute("aria-label", "打开" + host.name + "终端");
  open.addEventListener("click", () => activateHost(host.id));
  foot.append(times, note, open);
  card.append(heading, type, spec, system, metrics, foot);
  hostGrid.append(card);
  statusCards.set(host.id, { card, badge, hostname, spec, system, gauges, network, networkRow, times, note, uptime, successAt });
}

function safeText(value) { return typeof value === "string" && value.trim() ? value.trim().slice(0, 200) : "—"; }
function finiteNumber(value) { return typeof value === "number" && Number.isFinite(value); }
function validBytes(value) { return finiteNumber(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER; }
function plainObject(value) { return Boolean(value) && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function positiveInteger(value) { return Number.isSafeInteger(value) && value > 0; }
function validPercent(value) { return finiteNumber(value) && value >= 0 && value <= 100; }
function validMetrics(metrics) {
  if (!plainObject(metrics)) return false;
  if (!["hostname", "os", "arch", "cpu_model"].every(key => typeof metrics[key] === "string" && metrics[key].trim())) return false;
  if (!positiveInteger(metrics.cpu_cores) || metrics.cpu_cores > 65536) return false;
  if (!["memory", "disk"].every(key => positiveInteger(metrics[key + "_total_bytes"]) && validBytes(metrics[key + "_used_bytes"]) && metrics[key + "_used_bytes"] <= metrics[key + "_total_bytes"])) return false;
  if (!validBytes(metrics.uptime_seconds)) return false;
  if (metrics.cpu_percent !== null && !validPercent(metrics.cpu_percent)) return false;
  if (metrics.load_1 !== null && !validBytes(metrics.load_1)) return false;
  if (!["network_rx_bytes_per_second", "network_tx_bytes_per_second"].every(key => metrics[key] === undefined || metrics[key] === null || validBytes(metrics[key]))) return false;
  return metrics.network_interface === undefined || metrics.network_interface === null || typeof metrics.network_interface === "string";
}
function timestamp(value) { return typeof value === "string" ? Date.parse(value) : NaN; }
function timeLabel(value) {
  const ms = timestamp(value);
  return Number.isFinite(ms) ? new Date(ms).toLocaleString("zh-CN", { hour12: false }) : "—";
}
function isFresh(ms, now) { return Number.isFinite(ms) && ms <= now + 10000 && now - ms < FRESHNESS_MS; }
function bytesLabel(value) {
  if (!validBytes(value)) return "—";
  if (value < 1024) return Math.round(value) + " B";
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)) - 1);
  return (value / Math.pow(1024, index + 1)).toFixed(1) + " " + units[index];
}
function durationLabel(seconds) {
  if (!finiteNumber(seconds) || seconds < 0 || seconds > Number.MAX_SAFE_INTEGER) return "—";
  const mins = Math.floor(seconds / 60);
  const days = Math.floor(mins / 1440);
  const hours = Math.floor(mins / 60) % 24;
  if (days) return days + "天" + hours + "小时";
  return hours ? hours + "小时" + mins % 60 + "分" : mins + "分钟";
}
// 「已用 / 总量」用总量的单位，例如「14.0 / 15.6 GiB」，比两边各带单位短一半。
function capacity(used, total) {
  if (!positiveInteger(total) || !validBytes(used) || used > total) return { text: "—", percent: null };
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(total) / Math.log(1024)));
  const scale = Math.pow(1024, index);
  const fixed = value => (value / scale).toFixed(total / scale >= 100 ? 0 : 1);
  return { text: fixed(used) + " / " + fixed(total) + " " + units[index], percent: used / total * 100,
           full: bytesLabel(used) + " / " + bytesLabel(total) };
}
// 今天的时间只显示时分秒，其他日子加上月日；完整时间放在悬停提示里。
function shortTime(value) {
  const ms = timestamp(value);
  if (!Number.isFinite(ms)) return "";
  const date = new Date(ms);
  const time = date.toLocaleTimeString("zh-CN", { hour12: false });
  return date.toDateString() === new Date().toDateString() ? time : (date.getMonth() + 1) + "/" + date.getDate() + " " + time;
}
function updateGauge(gauge, text, percent) {
  gauge.value.textContent = text;
  gauge.fill.style.width = (percent === null ? 0 : percent) + "%";
  gauge.fill.classList.toggle("high", percent !== null && percent >= 75 && percent < 90);
  gauge.fill.classList.toggle("critical", percent !== null && percent >= 90);
}
function hostStatus(data, now) {
  if (!data) return { label: "尚无数据", kind: "unknown" };
  if (data.status === "error") return { label: "上报异常", kind: "unreachable" };
  if (data.status === "unreachable") return { label: "不可达", kind: "unreachable" };
  if (data.status !== "online" || !validMetrics(data.metrics) || !Number.isFinite(timestamp(data.last_success_at))) return { label: "数据异常", kind: "invalid" };
  if (data.status === "online" && isFresh(timestamp(data.last_success_at), now) && isFresh(timestamp(snapshot.generated_at), now)) {
    return { label: "在线", kind: "online" };
  }
  return { label: "数据过期", kind: "stale" };
}

function renderStatus() {
  const now = Date.now();
  const byId = new Map((snapshot ? snapshot.hosts : []).filter(h => h && typeof h === "object").map(h => [h.id, h]));
  let nextExpiry = Infinity;
  let onlineCount = 0;
  HOSTS.forEach(host => {
    const data = byId.get(host.id);
    const card = statusCards.get(host.id);
    const metrics = data && plainObject(data.metrics) ? data.metrics : {};
    const state = hostStatus(data, now);
    if (state.kind === "online") {
      onlineCount++;
      nextExpiry = Math.min(nextExpiry, timestamp(data.last_success_at) + FRESHNESS_MS, timestamp(snapshot.generated_at) + FRESHNESS_MS);
    }
    card.badge.textContent = state.label;
    card.badge.className = "status-badge " + state.kind;
    card.hostname.textContent = metrics.hostname ? safeText(metrics.hostname) : "";
    card.hostname.title = card.hostname.textContent;
    const cores = Number.isInteger(metrics.cpu_cores) && metrics.cpu_cores > 0 && metrics.cpu_cores <= 65536 ? metrics.cpu_cores + " 核" : "";
    const spec = [metrics.cpu_model ? safeText(metrics.cpu_model) : "", cores, metrics.arch ? safeText(metrics.arch) : ""].filter(Boolean);
    card.spec.textContent = spec.join(" · ") || "—";
    card.spec.title = card.spec.textContent;
    card.system.textContent = safeText(metrics.os);
    card.system.title = card.system.textContent;
    const cpu = validPercent(metrics.cpu_percent) ? metrics.cpu_percent : null;
    updateGauge(card.gauges.cpu, cpu === null ? "—" : cpu.toFixed(1) + "%", cpu);
    const memory = capacity(metrics.memory_used_bytes, metrics.memory_total_bytes);
    const disk = capacity(metrics.disk_used_bytes, metrics.disk_total_bytes);
    updateGauge(card.gauges.memory, memory.text, memory.percent);
    updateGauge(card.gauges.disk, disk.text, disk.percent);
    card.gauges.memory.row.title = [memory.full, metrics.memory_note ? safeText(metrics.memory_note) : ""].filter(Boolean).join("\n");
    card.gauges.disk.row.title = disk.full || "";
    const rate = value => validBytes(value) ? bytesLabel(value) + "/s" : "—";
    card.network.textContent = "↓ " + rate(metrics.network_rx_bytes_per_second) + "  ↑ " + rate(metrics.network_tx_bytes_per_second);
    card.networkRow.title = (metrics.network_interface ? "网卡：" + safeText(metrics.network_interface) + "\n" : "") + "下载 / 上传，采样期间平均速率";
    card.uptime.textContent = "运行 " + durationLabel(metrics.uptime_seconds);
    const success = data && data.last_success_at;
    card.successAt.textContent = Number.isFinite(timestamp(success)) ? "更新 " + shortTime(success) : "";
    card.successAt.title = Number.isFinite(timestamp(success)) ? "最近成功：" + timeLabel(success) : "";
    if (Number.isFinite(timestamp(success))) card.successAt.dateTime = success;
    else card.successAt.removeAttribute("datetime");
    const notes = [];
    if (state.kind === "unreachable") notes.push(data.error ? safeText(data.error) : "最近一次上报未成功");
    if (state.kind === "stale") notes.push("超过 90 秒没有新数据");
    if (state.kind === "unknown") notes.push("尚未收到上报");
    if (state.kind === "invalid") notes.push("数据不完整，等待下一次上报");
    if (notes.length && Number.isFinite(timestamp(success))) notes.push("上次 " + shortTime(success));
    card.note.textContent = notes.join(" · ");
    card.note.title = card.note.textContent + ((state.kind === "unreachable" || state.kind === "stale") && data && data.metrics
      ? "\n卡片上显示的是上次收到的数据" : "") + (state.kind === "unknown" ? "\n终端仍可打开" : "");
    card.times.hidden = notes.length > 0;
  });
  statusNotice.classList.toggle("warning", Boolean(statusError));
  statusNotice.textContent = statusError || (snapshot
    ? "在线 " + onlineCount + " / " + HOSTS.length + " · 更新于 " + shortTime(snapshot.generated_at)
    : "正在读取服务器运行情况…");
  clearTimeout(freshnessTimer);
  if (!document.hidden && Number.isFinite(nextExpiry)) freshnessTimer = setTimeout(renderStatus, Math.max(1, nextExpiry - now + 1));
}

async function refreshStatus() {
  if (document.hidden || activeRequest) return;
  clearTimeout(pollTimer);
  resumeRefresh = false;
  const generation = ++requestGeneration;
  const controller = new AbortController();
  activeRequest = controller;
  refreshStatusBtn.disabled = true;
  refreshStatusBtn.textContent = "读取中…";
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 8000);
  try {
    const response = await fetch("/status.json", { cache: "no-store", signal: controller.signal });
    if (!response.ok) {
      if (response.status === 401) throw new Error("登录已过期，请刷新页面重新登录。");
      if (response.status === 404) throw new Error("运行状态服务尚未启用（404）。终端入口仍可使用。");
      throw new Error("读取运行情况失败（HTTP " + response.status + "）。稍后自动重试，已显示的数据暂时保留。");
    }
    const result = await response.json();
    if (!result || result.schema_version !== 1 || !Array.isArray(result.hosts) || !Number.isFinite(timestamp(result.generated_at))) {
      throw new Error("运行情况数据格式不正确，已显示的数据暂时保留。");
    }
    if (generation !== requestGeneration || document.hidden) return;
    if (snapshot && timestamp(result.generated_at) < timestamp(snapshot.generated_at)) {
      throw new Error("服务器返回了较早的缓存，继续显示已读取的数据，稍后自动重试。");
    }
    snapshot = result;
    statusError = "";
    renderStatus();
  } catch (error) {
    if (generation !== requestGeneration || document.hidden) return;
    statusError = timedOut ? "读取运行情况超时，稍后自动重试。已显示的数据暂时保留。"
      : error instanceof SyntaxError ? "运行情况数据无法解析，已显示的数据暂时保留。"
      : error instanceof TypeError ? "暂时无法读取运行情况，请检查网络。已显示的数据暂时保留。"
      : error.message || "暂时无法读取运行情况，稍后自动重试。";
    renderStatus();
  } finally {
    clearTimeout(timeout);
    if (activeRequest === controller) activeRequest = null;
    refreshStatusBtn.disabled = false;
    refreshStatusBtn.textContent = "读取最新";
    if (!document.hidden) {
      if (resumeRefresh) refreshStatus();
      else pollTimer = setTimeout(refreshStatus, STATUS_INTERVAL_MS);
    }
  }
}

function showOverview() {
  overviewVisible = true;
  workspace.classList.remove("terminal-mode");
  overviewEl.hidden = false;
  overviewEl.scrollTop = 0;
  overviewBtn.classList.add("active");
  overviewBtn.setAttribute("aria-pressed", "true");
  sessionRow.hidden = true;
  reconnectBtn.hidden = true;
  emptyHint.classList.remove("show");
  HOSTS.forEach(h => {
    hosts[h.id].tab.classList.remove("active");
    hosts[h.id].tab.setAttribute("aria-selected", "false");
    hosts[h.id].sessions.forEach(s => s.pane.classList.remove("active"));
    statusCards.get(h.id).card.classList.remove("current");
    statusCards.get(h.id).card.removeAttribute("aria-current");
  });
  summary.textContent = "已打开会话：" + HOSTS.reduce((total, h) => total + countSessions(h.id), 0);
  renderStatus();
}

function showTerminal() {
  overviewVisible = false;
  workspace.classList.add("terminal-mode");
  overviewEl.hidden = false;
  overviewBtn.classList.remove("active");
  overviewBtn.setAttribute("aria-pressed", "false");
  sessionRow.hidden = false;
  reconnectBtn.hidden = false;
}

function highlightStatusHost(hostId) {
  statusCards.forEach((state, id) => {
    state.card.classList.toggle("current", id === hostId);
    if (id === hostId) state.card.setAttribute("aria-current", "true");
    else state.card.removeAttribute("aria-current");
  });
  // 只滚动概览容器，保持终端和整页位置不变；不触碰已有 iframe。
  requestAnimationFrame(() => {
    if (overviewVisible || activeHostId !== hostId) return;
    const card = statusCards.get(hostId).card;
    const bounds = overviewEl.getBoundingClientRect();
    const cardBounds = card.getBoundingClientRect();
    const headingHeight = overviewEl.querySelector(".overview-heading").offsetHeight;
    if (cardBounds.top < bounds.top + headingHeight || cardBounds.bottom > bounds.bottom) {
      overviewEl.scrollTop += cardBounds.top - bounds.top - headingHeight - 14;
    }
  });
}

function hostById(id) { return HOSTS.find(h => h.id === id); }
function sessionKey(hostId, sid) { return hostId + "::" + sid; }
function countSessions(hostId) { return hosts[hostId].sessions.length; }

function updateHostCount(hostId) {
  const h = hosts[hostId];
  const n = countSessions(hostId);
  const count = h.tab.querySelector(".count");
  count.textContent = n ? String(n) : "";
  count.title = n + " 个会话";
  reconnectBtn.disabled = !activeSessionKey;
  if (overviewVisible) summary.textContent = "已打开会话：" + HOSTS.reduce((total, host) => total + countSessions(host.id), 0);
  if (activeHostId === hostId) {
    newSessionBtn.disabled = n >= MAX_SESSIONS;
    newSessionBtn.textContent = n >= MAX_SESSIONS ? "会话已满" : "+ 新开终端";
    if (!overviewVisible) summary.textContent = "已打开会话：" + n;
  }
}

function renderSessionTabs() {
  sessionTabsEl.replaceChildren();
  if (!activeHostId) return;
  hosts[activeHostId].sessions.forEach(s => {
    const active = s.key === activeSessionKey;
    const tab = el("div", "session-tab" + (active ? " active" : ""));
    const select = el("button", "session-select", s.title);
    select.type = "button";
    select.setAttribute("role", "tab");
    select.setAttribute("aria-selected", String(active));
    select.addEventListener("click", () => activateSession(s.key));
    const close = el("button", "close", "×");
    close.type = "button";
    close.title = "关闭会话";
    close.setAttribute("aria-label", "关闭会话 " + s.title);
    close.addEventListener("click", () => closeSession(s.key));
    tab.append(select, close);
    sessionTabsEl.appendChild(tab);
  });
}

function createHostTab(host) {
  const tab = document.createElement("button");
  tab.type = "button";
  const kind = kindOf(host);
  tab.className = "host-tab kind-" + kind;
  tab.setAttribute("role", "tab");
  tab.setAttribute("aria-selected", "false");
  tab.title = KINDS[kind].label + (host.meta ? " · " + host.meta : "");
  tab.append(kindIcon(kind, "kind-icon tab-icon"), el("span", "name", host.name), el("span", "count", ""));
  tab.addEventListener("click", () => activateHost(host.id));
  hostTabsEl.appendChild(tab);
  hosts[host.id] = { tab: tab, sessions: [], nextNo: 1 };
}

// 卸载终端页面；远端进程是否结束由 ttyd、SSH 和进程自身行为决定。
function killIframe(session) {
  if (!session) return;
  const iframe = session.iframe;
  session.iframe = null;
  session.closed = true;
  if (!iframe) return;
  try { iframe.onload = null; } catch (e) {}
  try { iframe.onerror = null; } catch (e) {}
  // 尝试停止并导航离开旧终端页面。
  try {
    if (iframe.contentWindow) {
      try { iframe.contentWindow.stop(); } catch (e2) {}
      try { iframe.contentWindow.location.replace("about:blank"); } catch (e2) {}
    }
  } catch (e) {}
  try { iframe.src = "about:blank"; } catch (e) {}
  // 从页面移除旧 iframe，释放浏览器端会话。
  try { iframe.remove(); } catch (e) {}
  try { if (session.pane) session.pane.replaceChildren(); } catch (e) {}
}

function ensureIframe(session) {
  if (session.closed) return;
  if (session.iframe) return;
  const host = hostById(session.hostId);
  session.pane.replaceChildren();
  const iframe = document.createElement("iframe");
  iframe.title = host.name + " " + session.title;
  iframe.allow = "clipboard-read; clipboard-write";
  // 每个 iframe 都会建立自己的 WebSocket 连接；ttyd 不读取网址参数，这里也不传。
  iframe.src = host.path;
  session.pane.appendChild(iframe);
  session.iframe = iframe;
}

function createSession(hostId, autoActivate) {
  const host = hostById(hostId);
  const state = hosts[hostId];
  if (!host || !state) return null;
  if (countSessions(hostId) >= MAX_SESSIONS) return null;

  const sid = String(seq++);
  const no = state.nextNo++;
  const key = sessionKey(hostId, sid);
  const pane = document.createElement("div");
  pane.className = "pane";
  pane.dataset.key = key;
  workspace.appendChild(pane);

  const session = {
    key: key, hostId: hostId, sid: sid, no: no, title: "#" + no,
    pane: pane, iframe: null, closed: false
  };
  state.sessions.push(session);
  ensureIframe(session);
  updateHostCount(hostId);
  renderSessionTabs();
  if (autoActivate) activateSession(key);
  return session;
}

function activateHost(hostId) {
  if (!hosts[hostId]) return;
  showTerminal();
  activeHostId = hostId;
  HOSTS.forEach(h => hosts[h.id].tab.classList.toggle("active", h.id === hostId));
  while (countSessions(hostId) < MIN_SESSIONS) createSession(hostId, false);
  const list = hosts[hostId].sessions;
  let target = list.find(s => s.key === activeSessionKey && s.hostId === hostId);
  if (!target) target = list[list.length - 1] || list[0];
  renderSessionTabs();
  updateHostCount(hostId);
  if (target) activateSession(target.key);
}

function activateSession(key) {
  let found = null;
  HOSTS.forEach(h => {
    hosts[h.id].sessions.forEach(s => { if (s.key === key) found = s; });
  });
  if (!found) return;
  showTerminal();
  activeHostId = found.hostId;
  activeSessionKey = key;
  emptyHint.classList.remove("show");
  HOSTS.forEach(h => {
    hosts[h.id].tab.classList.toggle("active", h.id === found.hostId);
    hosts[h.id].tab.setAttribute("aria-selected", String(h.id === found.hostId));
    hosts[h.id].sessions.forEach(s => s.pane.classList.toggle("active", s.key === key));
  });
  ensureIframe(found);
  renderSessionTabs();
  updateHostCount(found.hostId);
  highlightStatusHost(found.hostId);
}

function closeSession(key) {
  let hostId = null;
  let idx = -1;
  HOSTS.forEach(h => {
    const i = hosts[h.id].sessions.findIndex(s => s.key === key);
    if (i >= 0) { hostId = h.id; idx = i; }
  });
  if (hostId == null || idx < 0) return;
  const state = hosts[hostId];
  const session = state.sessions[idx];

  // 先卸载终端页面，再移除本地会话记录。
  killIframe(session);
  if (session.pane) session.pane.remove();
  state.sessions.splice(idx, 1);

  if (activeSessionKey === key) {
    activeSessionKey = null;
    const next = state.sessions[state.sessions.length - 1];
    if (next) activateSession(next.key);
    else {
      emptyHint.classList.add("show");
      renderSessionTabs();
      updateHostCount(hostId);
    }
  } else {
    renderSessionTabs();
    updateHostCount(hostId);
  }
}

function reconnectCurrent() {
  if (!activeSessionKey) return;
  let session = null;
  HOSTS.forEach(h => {
    hosts[h.id].sessions.forEach(s => { if (s.key === activeSessionKey) session = s; });
  });
  if (!session) return;
  // 重连保留当前会话标签，卸载旧页面后允许创建新 iframe。
  killIframe(session);
  session.closed = false;
  ensureIframe(session);
  updateHostCount(session.hostId);
  renderSessionTabs();
}

function destroyAllSessions() {
  if (tearingDownAll) return;
  tearingDownAll = true;
  HOSTS.forEach(h => {
    const list = hosts[h.id].sessions.slice();
    list.forEach(s => {
      killIframe(s);
      if (s.pane) s.pane.remove();
    });
    hosts[h.id].sessions = [];
    updateHostCount(h.id);
  });
  activeSessionKey = null;
}

HOSTS.forEach(createHostTab);
HOSTS.forEach(createStatusCard);
overviewBtn.addEventListener("click", showOverview);
refreshStatusBtn.addEventListener("click", refreshStatus);
newSessionBtn.addEventListener("click", () => {
  if (!activeHostId) return;
  createSession(activeHostId, true);
});
reconnectBtn.addEventListener("click", reconnectCurrent);

// 有终端开着时，刷新或关闭页面先让浏览器确认；ttyd 断开时会挂断远端前台程序。
window.addEventListener("beforeunload", event => {
  if (HOSTS.some(h => countSessions(h.id) > 0)) {
    event.preventDefault();
    event.returnValue = "";
  }
});
// 真正离开页面时释放浏览器端会话。
window.addEventListener("pagehide", destroyAllSessions);

document.addEventListener("visibilitychange", () => {
  clearTimeout(pollTimer);
  clearTimeout(freshnessTimer);
  if (document.hidden) {
    requestGeneration++;
    if (activeRequest) activeRequest.abort();
  } else {
    resumeRefresh = true;
    renderStatus();
    refreshStatus();
  }
});
window.addEventListener("pagehide", () => {
  clearTimeout(pollTimer);
  clearTimeout(freshnessTimer);
  requestGeneration++;
  if (activeRequest) activeRequest.abort();
});
window.addEventListener("pageshow", event => {
  if (event.persisted) {
    tearingDownAll = false;
    showOverview();
    refreshStatus();
  }
});
// 首页只读取轻量状态数据；用户选择机器后才建立终端连接。
// 还有机器没接入时，入口机会生成 join.txt（要登录才能看），这里只负责显示链接。
fetch("join.txt", { method: "HEAD", cache: "no-store" })
  .then(response => { document.getElementById("joinLink").hidden = !response.ok; })
  .catch(() => {});
showOverview();
if (HOSTS.length) refreshStatus();
else statusNotice.textContent = "未读取到机器清单（hosts.js），请检查部署。";
