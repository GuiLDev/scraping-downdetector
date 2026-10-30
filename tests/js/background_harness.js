// Roda extension/coletor/background.js num contexto isolado com um `chrome` falso (sem navegador).
// Uso: node background_harness.js <background.js>  (cenario JSON na stdin) -> imprime o relatorio JSON.
"use strict";
const fs = require("fs");
const vm = require("vm");

const [, , scriptPath] = process.argv;
const sc = JSON.parse(fs.readFileSync(0, "utf8"));
const cfg = { servidor: "http://127.0.0.1:8765", token: "TOKEN-DE-TESTE", alvo: "http://127.0.0.1:8777/dd",
              intervalo_min: 5, ...(sc.config || {}) };
const report = { reloads: [], forbidden: [], posts: [], alarms: [], cleared: [], listeners: 0, estadoGets: 0,
                 estadoUrls: [], reads: 0, scripted: [], reservas: [], order: [] };
let clock = 0;
let listeners = [];

// Sem a permissao "tabs", `tab.url` so aparece para abas cobertas pelos host_permissions do manifest.
const manifest = JSON.parse(fs.readFileSync(require("path").join(require("path").dirname(scriptPath), "manifest.json"), "utf8"));
const hostPermissions = sc.hostPermissions || manifest.host_permissions;
if ((manifest.permissions || []).includes("tabs")) report.forbidden.push("permissao tabs no manifest");
const origin = (u) => { try { return new URL(u).origin; } catch (_) { return null; } };
const covered = (u) => hostPermissions.some((p) => origin(p.replace("/*", "/")) === origin(u));
const allTabs = sc.tabs || [
  { id: 1, url: cfg.alvo + "?x=1", active: false },
  { id: 2, url: "http://127.0.0.1:9999/outra", active: true },
];
const readings = sc.readings || [];
const visibleTabs = () => allTabs.map((t) => (covered(t.url) ? t : { id: t.id, active: t.active }));

const chrome = {
  runtime: { getURL: (p) => "ext://" + p, onInstalled: { addListener() {} }, onStartup: { addListener() {} } },
  alarms: {
    create: (name, info) => report.alarms.push({ name, ...info }),
    clear: async (name) => { report.cleared.push(name); return true; },
    onAlarm: { addListener: (fn) => { report.alarmHandler = fn; } },
  },
  storage: { local: { set: async () => {} } },
  tabs: {
    query: async () => visibleTabs(),
    reload: async (id) => {
      const tab = allTabs.find((t) => t.id === id);
      if (!tab || !covered(tab.url)) throw new Error("sem permissao de host para recarregar");
      report.reloads.push(id);
      report.order.push("reload");
      if (!sc.neverComplete) setImmediate(() => listeners.slice().forEach((fn) => fn(id, { status: "complete" })));
    },
    create: () => report.forbidden.push("tabs.create"),
    update: () => report.forbidden.push("tabs.update"),
    onUpdated: {
      addListener: (fn) => { listeners.push(fn); report.listeners++; },
      removeListener: (fn) => { listeners = listeners.filter((x) => x !== fn); },
    },
  },
  windows: { update: () => report.forbidden.push("windows.update") },
  scripting: {
    executeScript: async (opts) => {
      const tab = allTabs.find((t) => t.id === opts.target.tabId);
      if (!tab || !covered(tab.url)) throw new Error("sem permissao de host para ler a aba");
      report.scripted.push(opts.target.tabId);
      const r = readings[Math.min(report.reads, readings.length - 1)];
      report.reads++;
      return [{ result: r }];
    },
  },
};

async function fakeFetch(url, opts = {}) {
  const json = (obj, status = 200) => ({ ok: status < 300, status, json: async () => obj });
  if (url === "ext://config.json") return json(cfg);
  if (url.startsWith(cfg.servidor + "/api/coleta/estado")) {
    report.estadoGets++;
    report.order.push("estado");
    report.estadoUrls.push(url);
    if (sc.estadoStatus) return json({ erro: "x" }, sc.estadoStatus);
    report.estadoToken = opts.headers && opts.headers["X-BankMonitor-Token"];
    return json({ estado: sc.estado || "ativo", ...(sc.estadoBody || {}) });
  }
  if (url === cfg.servidor + "/api/coleta/reserva") {
    report.order.push("reserva");
    report.reservas.push({ headers: opts.headers, body: JSON.parse(opts.body) });
    if (sc.reservaStatus) return json(sc.reservaBody || { erro: "x" }, sc.reservaStatus);
    return json({ reserva_id: "RES-1", expira_em: "2026-10-28T15:32:00Z" });
  }
  if (url === cfg.servidor + "/api/coleta") {
    report.order.push("post");
    report.posts.push({ headers: opts.headers, body: JSON.parse(opts.body) });
    return json({ estado: "ativo", run_id: 1, page_state: "ok", servicos: 12 }, sc.postStatus || 200);
  }
  throw new Error("fetch inesperado: " + url);
}

const context = {
  chrome, fetch: fakeFetch, console: { log() {}, warn() {}, error() {} }, URL, TextEncoder, JSON, Math, Promise,
  Date: { now: () => clock },
  // Relogio virtual: esperas curtas (<10 s) disparam logo; o limite longo de carregamento dispara so
  // depois de varios ciclos (dando chance ao evento `complete`) e entao avanca o relogio.
  setTimeout: (fn, ms) => {
    const handle = { cancelled: false };
    const step = (n) => setImmediate(() => {
      if (handle.cancelled) return;
      if (n > 1) return step(n - 1);
      clock += ms;
      fn();
    });
    step(ms >= 10000 ? 20 : 1);
    return handle;
  },
  clearTimeout: (h) => { if (h) h.cancelled = true; },
  globalThis: null,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);

(async () => {
  try {
    if (sc.mode === "alarm") {
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));
      report.hasAlarmHandler = typeof report.alarmHandler === "function";
    } else if (sc.mode === "fire_alarm") {
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); // garantirAlarme inicial
      report.alarmHandler({ name: "bankmon-coleta" });
      await new Promise((r) => setTimeout(r, 0));
      for (let i = 0; i < 200; i++) await new Promise((r) => setImmediate(r));
    } else {
      report.result = await context.coletarAgora();
    }
    report.elapsedMs = clock;
  } catch (err) {
    report.error = String(err && err.message || err);
  }
  console.log = process.stdout.write.bind(process.stdout);
  process.stdout.write(JSON.stringify(report));
})();
