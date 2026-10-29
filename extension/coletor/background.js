// Coletor Bank Monitor: service worker MV3.
//
// O QUE FAZ: a cada alarme (ou ao chamar `coletarAgora()`), pergunta ao servidor local o estado da
// coleta. Se estiver pausada, para. Se ativa, RESERVA a vaga no servidor (que aplica o intervalo
// minimo) e so entao recarrega a aba que o USUARIO ja abriu (sem traze-la para a frente), espera a
// pagina carregar e estabilizar, le o HTML dela e o envia com o `reserva_id`. Reserva recusada
// (429/409) = nao recarrega. Se o estado for "verificando" (o usuario resolveu o bloqueio e clicou em
// "Retomar verificacao"), le o DOM ATUAL da aba, sem recarregar e sem reservar, e o envia com
// `modo: "verificacao"`.
//
// O QUE NUNCA FAZ (proibicoes deliberadas; nao as remova):
//  - nao le, copia nem envia cookies, storage da pagina, cabecalhos ou credenciais (sem permissao
//    "cookies"; so o HTML da aba e enviado);
//  - nao clica, digita nem interage com a pagina; nao resolve nem automatiza CAPTCHA ou desafio de
//    bloqueio (Cloudflare etc.): se a pagina vier como desafio, ela e enviada como esta e o servidor
//    PAUSA a coleta ate o usuario resolver manualmente na aba e clicar em "Retomar verificacao";
//  - nao abre abas nem janelas, nao ativa a aba, nao muda cabecalhos de requisicao;
//  - so fala com o servidor local e so le a aba cujo endereco comeca com o `alvo` configurado.
//
// A configuracao (servidor, token, alvo, intervalo) vem de config.json, gerado por
// `python -m bankmon extensao` fora do git. Este arquivo nao contem segredos.
"use strict";

const ALARME = "bankmon-coleta";
const ESPERA_MAX_MS = 45000; // limite para carregar e estabilizar
const PASSO_MS = 1000; // intervalo entre leituras de prontidao
const MAX_HTML_BYTES = 5 * 1024 * 1024;
const INTERVALO_MIN_MIN = 0.5; // minimo dos alarmes do navegador

let emExecucao = false;

// 401/403 do servidor: token errado ou origem revogada. A extensao para de vez (alarme cancelado).
class NaoAutorizado extends Error {}

async function carregarConfig() {
  const resp = await fetch(chrome.runtime.getURL("config.json"));
  const cfg = await resp.json();
  if (!cfg.servidor || !cfg.token || !cfg.alvo) throw new Error("config.json incompleto");
  return cfg;
}

function esperar(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function exigirAutorizado(resp) {
  if (resp.status === 401 || resp.status === 403) throw new NaoAutorizado("HTTP " + resp.status);
}

// Informa o alvo ao servidor: se a origem foi revogada, ele responde 403 e nada e recarregado.
// So consulta (o servidor nao muda nada); devolve o JSON (`estado`, `pode_reservar`, ...).
async function consultarEstado(cfg) {
  const resp = await fetch(cfg.servidor + "/api/coleta/estado?alvo=" + encodeURIComponent(cfg.alvo), {
    headers: { "X-BankMonitor-Token": cfg.token },
  });
  exigirAutorizado(resp);
  if (!resp.ok) throw new Error("estado HTTP " + resp.status);
  return resp.json();
}

// Reserva a vaga ANTES de qualquer recarga. 401/403 param tudo; 429/409 (intervalo, reserva ativa,
// pausado) devolvem o motivo e a extensao NAO recarrega.
async function reservar(cfg) {
  const resp = await fetch(cfg.servidor + "/api/coleta/reserva", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-BankMonitor-Token": cfg.token },
    body: JSON.stringify({ alvo: cfg.alvo }),
  });
  exigirAutorizado(resp);
  if (resp.status === 429) return { recusada: "intervalo", retryAfter: resp.headers && resp.headers.get ? resp.headers.get("Retry-After") : null };
  if (resp.status === 409) {
    let motivo = "reserva_ativa";
    try {
      motivo = (await resp.json()).erro || motivo;
    } catch (_) {
      /* corpo opcional */
    }
    return { recusada: motivo };
  }
  if (!resp.ok) throw new Error("reserva HTTP " + resp.status);
  return { reservaId: (await resp.json()).reserva_id };
}

// Mesma origem E o caminho da aba dentro do caminho do alvo (nunca `startsWith` cru: "http://localhost"
// nao pode casar com "http://localhost.evil.example/" nem com "http://localhost:3000/").
function abaDoAlvo(urlDaAba, alvo) {
  let aba;
  let base;
  try {
    aba = new URL(urlDaAba);
    base = new URL(alvo);
  } catch (_) {
    return false;
  }
  if (aba.origin !== base.origin) return false;
  const comBarra = (p) => (p.endsWith("/") ? p : p + "/");
  return comBarra(aba.pathname).startsWith(comBarra(base.pathname));
}

// Sem a permissao "tabs": `tab.url` so e visivel para abas cobertas pelos host_permissions.
async function acharAba(alvo) {
  const abas = await chrome.tabs.query({});
  return abas.find((aba) => typeof aba.url === "string" && abaDoAlvo(aba.url, alvo)) || null;
}

// Recarrega SEM ativar a aba (so `tabs.reload`; nunca `tabs.update`/`windows.update`).
async function recarregarEAguardar(tabId) {
  let listener;
  let limite;
  const completa = new Promise((resolve) => {
    listener = (id, info) => {
      if (id === tabId && info.status === "complete") resolve(true);
    };
    chrome.tabs.onUpdated.addListener(listener);
    limite = setTimeout(() => resolve(false), ESPERA_MAX_MS);
  });
  try {
    await chrome.tabs.reload(tabId);
    return await completa;
  } finally {
    chrome.tabs.onUpdated.removeListener(listener);
    clearTimeout(limite);
  }
}

// Executa na pagina (somente leitura do DOM): HTML, quantidade de graficos e sinal de desafio.
function lerDom() {
  const html = document.documentElement.outerHTML;
  const graficos = document.querySelectorAll('[role="img"][aria-label^="Relatos de"]').length;
  const desafio = /Just a moment|cf_chl_opt/.test(document.title + " " + html);
  return { html, graficos, desafio };
}

async function lerPagina(tabId) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: lerDom });
  return res.result;
}

// Pronta = desafio detectado, ou quantidade de graficos (> 0) igual em duas leituras seguidas.
// Passado o limite, devolve a ultima leitura (o servidor classifica como incompleta).
async function aguardarProntidao(tabId) {
  const inicio = Date.now();
  let anterior = null;
  let leitura = await lerPagina(tabId);
  while (Date.now() - inicio < ESPERA_MAX_MS) {
    if (leitura.desafio) return leitura;
    if (leitura.graficos > 0 && leitura.graficos === anterior) return leitura;
    anterior = leitura.graficos;
    await esperar(PASSO_MS);
    leitura = await lerPagina(tabId);
  }
  return leitura;
}

async function enviar(cfg, url, html, extra) {
  const resp = await fetch(cfg.servidor + "/api/coleta", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-BankMonitor-Token": cfg.token },
    body: JSON.stringify({ url, html, ...extra }),
  });
  exigirAutorizado(resp);
  return resp.status;
}

function grandeDemais(html) {
  return new TextEncoder().encode(html).length > MAX_HTML_BYTES;
}

async function coletar() {
  const cfg = await carregarConfig();
  const estado = (await consultarEstado(cfg)).estado;
  if (estado === "pausado") return { resultado: "pausado" };
  const aba = await acharAba(cfg.alvo);
  if (!aba) return { resultado: "sem_aba" };
  if (estado === "verificando") {
    // Sem recarga e sem reserva: so le o que a pagina ja mostra (o usuario a resolveu na aba).
    const atual = await aguardarProntidao(aba.id);
    if (grandeDemais(atual.html)) return { resultado: "html_grande_demais" };
    return { resultado: "enviado", http: await enviar(cfg, aba.url, atual.html, { modo: "verificacao" }) };
  }
  const reserva = await reservar(cfg);
  if (reserva.recusada) return { resultado: reserva.recusada, retryAfter: reserva.retryAfter };
  if (!(await recarregarEAguardar(aba.id))) return { resultado: "carregamento_incompleto" };
  const leitura = await aguardarProntidao(aba.id);
  if (grandeDemais(leitura.html)) return { resultado: "html_grande_demais" };
  const http = await enviar(cfg, aba.url, leitura.html, { reserva_id: reserva.reservaId });
  return { resultado: "enviado", http };
}

// Uma coleta por vez; devolve o resultado (tambem guardado em storage para diagnostico, sem token).
async function coletarAgora() {
  if (emExecucao) return { resultado: "ocupado" };
  emExecucao = true;
  let resultado;
  try {
    resultado = await coletar();
  } catch (err) {
    if (err instanceof NaoAutorizado) {
      try {
        await chrome.alarms.clear(ALARME);
      } catch (_) {
        /* sem alarme a limpar */
      }
      resultado = { resultado: "nao_autorizado", detalhe: err.message };
    } else {
      resultado = { resultado: "erro", detalhe: String((err && err.message) || err) };
    }
  } finally {
    emExecucao = false;
  }
  try {
    await chrome.storage.local.set({ ultimo: { ...resultado, quando: new Date().toISOString() } });
  } catch (_) {
    /* diagnostico e opcional */
  }
  return resultado;
}

async function garantirAlarme() {
  const cfg = await carregarConfig();
  const periodInMinutes = Math.max(INTERVALO_MIN_MIN, Number(cfg.intervalo_min) || INTERVALO_MIN_MIN);
  chrome.alarms.create(ALARME, { periodInMinutes });
}

chrome.alarms.onAlarm.addListener((alarme) => {
  if (alarme.name === ALARME) coletarAgora();
});
chrome.runtime.onInstalled.addListener(() => garantirAlarme());
chrome.runtime.onStartup.addListener(() => garantirAlarme());
garantirAlarme().catch(() => {}); // o service worker pode acordar sem onInstalled/onStartup

globalThis.coletarAgora = coletarAgora; // para disparo manual no console do service worker
