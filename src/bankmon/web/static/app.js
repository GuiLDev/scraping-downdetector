/* JS proprio do dashboard. Sem dependencias externas; os dados dos graficos vem de
   <script type="application/json"> (lidos com JSON.parse), nunca de codigo inline. */
(function () {
  "use strict";

  var STATE_COLORS = {
    critical: "#d32f2f",
    attention: "#f28c00",
    no_alert: "#2e9e5b",
    no_data: "#9aa5ad",
    not_monitored: "#7986cb"
  };

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) { return null; }
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  // --- Tema claro/escuro (Pico: data-theme) -----------------------------------
  function initTheme() {
    var root = document.documentElement;
    var saved = null;
    try { saved = window.localStorage.getItem("bankmon-theme"); } catch (e) { /* ignora */ }
    if (saved === "light" || saved === "dark") { root.setAttribute("data-theme", saved); }
    var btn = document.getElementById("theme-toggle");
    if (!btn) { return; }
    btn.addEventListener("click", function () {
      var current = root.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { window.localStorage.setItem("bankmon-theme", next); } catch (e) { /* ignora */ }
    });
  }

  // A busca e um <form method="get"> nativo (funciona sem JS); nada a fazer aqui.

  // --- Graficos ---------------------------------------------------------------
  var dateFmt = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"
  });

  function themeColors() {
    var cs = window.getComputedStyle(document.body);
    var text = cs.color;
    return { text: text, grid: "rgba(128,128,128,0.25)" };
  }

  function baseOptions(data, title) {
    var c = themeColors();
    var plugins = {
      legend: { labels: { color: c.text } },
      title: { display: !!data.demo, text: data.demo ? "DEMO — dados de demonstração" : "", color: c.text }
    };
    if (title) { plugins.title = { display: true, text: title, color: c.text }; }
    return { responsive: true, maintainAspectRatio: false, animation: false, plugins: plugins, colors: c };
  }

  function historyChart(canvas, data) {
    var base = baseOptions(data);
    var c = base.colors;
    var total = Math.max(data.total, 1);
    // Horas sem verificacao valida viram uma faixa cinza de altura total: nunca "0 alertas".
    var noData = data.has_data.map(function (h) { return h ? 0 : total; });
    new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: data.hours,
        datasets: [
          { label: data.labels.critical, data: data.critical, backgroundColor: STATE_COLORS.critical, stack: "s" },
          { label: data.labels.attention, data: data.attention, backgroundColor: STATE_COLORS.attention, stack: "s" },
          { label: data.labels.no_data + " (sem verificação válida)", data: noData,
            backgroundColor: STATE_COLORS.no_data, stack: "s", categoryPercentage: 1, barPercentage: 1 }
        ]
      },
      options: {
        responsive: base.responsive, maintainAspectRatio: base.maintainAspectRatio, animation: false,
        plugins: {
          legend: base.plugins.legend,
          title: base.plugins.title,
          tooltip: {
            filter: function (item) { return item.raw !== 0 || item.datasetIndex !== 2; },
            callbacks: {
              label: function (item) {
                if (item.datasetIndex === 2) { return data.labels.no_data + ": nenhuma verificação válida"; }
                return item.dataset.label + ": " + item.raw;
              }
            }
          }
        },
        scales: {
          x: { stacked: true, ticks: { color: c.text, autoSkip: true, maxTicksLimit: 12, maxRotation: 0 }, grid: { color: c.grid } },
          y: { stacked: true, beginAtZero: true, max: total, ticks: { color: c.text, precision: 0 },
               title: { display: true, text: "Instituições", color: c.text }, grid: { color: c.grid } }
        }
      }
    });
  }

  function timelineChart(canvas, data) {
    var base = baseOptions(data);
    var c = base.colors;
    var byState = {};
    data.segments.forEach(function (s) {
      if (!byState[s.state]) { byState[s.state] = { label: s.label, points: [] }; }
      byState[s.state].points.push({ x: [s.start, s.end], y: "Estado" });
    });
    var datasets = Object.keys(byState).map(function (state) {
      return {
        label: byState[state].label,
        data: byState[state].points,
        backgroundColor: STATE_COLORS[state] || STATE_COLORS.no_data,
        grouped: false, barPercentage: 1, categoryPercentage: 1, borderSkipped: false
      };
    });
    new window.Chart(canvas, {
      type: "bar",
      data: { labels: ["Estado"], datasets: datasets },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          legend: base.plugins.legend,
          title: base.plugins.title,
          tooltip: {
            callbacks: {
              label: function (item) {
                var r = item.raw.x;
                return item.dataset.label + ": " + dateFmt.format(new Date(r[0])) + " – " + dateFmt.format(new Date(r[1]));
              }
            }
          }
        },
        scales: {
          x: { type: "linear", min: data.min, max: data.max,
               ticks: { color: c.text, maxTicksLimit: 8, callback: function (v) { return dateFmt.format(new Date(v)); } },
               grid: { color: c.grid } },
          y: { stacked: true, display: false }
        }
      }
    });
  }

  function initCharts() {
    if (typeof window.Chart === "undefined") { return; }
    var hc = document.getElementById("history-chart");
    var hd = readJson("history-data");
    if (hc && hd) { historyChart(hc, hd); }
    var tc = document.getElementById("timeline-chart");
    var td = readJson("timeline-data");
    if (tc && td) { timelineChart(tc, td); }
  }

  // --- Recarrega quando chega leitura nova (so no painel, que tem data-versao) -----
  function initAutoReload() {
    var el = document.querySelector("[data-versao]");
    if (!el) { return; }
    var inicial = el.getAttribute("data-versao");
    window.setInterval(function () {
      if (document.hidden) { return; }
      fetch("/painel/versao", { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (data && typeof data.versao === "string" && data.versao !== inicial) {
            window.location.reload();
          }
        })
        .catch(function () { /* ignora: tenta de novo no proximo ciclo */ });
    }, 20000);
  }

  initTheme();
  initCharts();
  initAutoReload();
})();
