/* Painel da fábrica: conversa com painel.py pelas rotas /api/... */
const REDES = {
  youtube:   { nome: "YouTube",   dica: "Shorts: título até 100 letras" },
  tiktok:    { nome: "TikTok",    dica: "Legenda + hashtags" },
  instagram: { nome: "Instagram", dica: "Reels: legenda + hashtags" },
  facebook:  { nome: "Facebook",  dica: "Reels: legenda + hashtags" },
};
const ESTADOS = { a_postar: "A postar", agendado: "Agendado", postado: "Postado", pular: "Pular" };
const PARTES = [["postado", "Postado", "--s-postado"], ["agendado", "Agendado", "--s-agendado"], ["a_postar", "A postar", "--s-apostar"]];

let est = {}, videos = [], aba = "inicio", filtro = {}, assinatura = "", tarefaAntes = null;
try { aba = localStorage.getItem("aba") || aba; filtro = JSON.parse(localStorage.getItem("filtro") || "{}"); } catch (e) {}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString("pt-BR");
const cor = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const compacto = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(".", ",") + " mi"
  : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(".", ",") + " mil" : String(n);
const hora = (t) => new Date(t * 1000).toLocaleTimeString("pt-BR");

function aviso(msg, erro = false) {
  const a = $("aviso"); a.textContent = msg; a.hidden = false; a.classList.toggle("erro", erro);
  clearTimeout(a._h); a._h = setTimeout(() => (a.hidden = true), erro ? 5000 : 2200);
}
async function api(rota, dados) {
  const r = await fetch(rota, dados === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(dados) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.erro || "Erro");
  return j;
}

/* ---------------- abas ---------------- */
function irPara(nova) {
  if (!document.querySelector(`.aba[data-aba="${nova}"]`)) nova = "inicio";
  aba = nova; try { localStorage.setItem("aba", aba); } catch (e) {}
  history.replaceState(null, "", "#/" + aba);  // "#/" pra o navegador não rolar até a seção
  document.querySelectorAll(".aba").forEach((b) => b.classList.toggle("ativa", b.dataset.aba === aba));
  const secao = REDES[aba] ? "rede" : aba;
  document.querySelectorAll(".painel").forEach((p) => p.classList.toggle("ativo", p.id === secao));
  if (aba === "config") carregarConfig();
  desenharVideos(true);
  if (aba === "numeros") desenharNumeros();
  if (aba === "registro") desenharLog();
}

/* ---------------- estado (topo, início, registro) ---------------- */
async function atualizarEstado() {
  try { est = await api("/api/estado"); } catch (e) { return; }
  $("marca").textContent = est.nome || "Fábrica de shorts";
  $("arroba").textContent = est.arroba || "";
  document.title = (est.nome ? est.nome + " · " : "") + "Painel";

  const pil = (id, classe, txt) => { const p = $(id); p.className = "pilula " + classe; p.textContent = txt; };
  pil("p-twitch", est.twitch ? "on" : "off", est.twitch ? "Twitch: chaves salvas" : "Twitch sem chave");
  pil("p-telegram", est.telegram_ouvindo ? "on" : est.telegram ? "esperando" : "",
      est.telegram_ouvindo ? "Telegram" : est.telegram ? "Telegram: mande /start" : "Telegram (opcional)");
  pil("p-fabrica", est.tarefa ? "esperando" : "on", est.tarefa ? "Trabalhando..." : "Fábrica pronta");

  const ocupado = $("ocupado");
  ocupado.hidden = !est.tarefa;
  if (est.tarefa) $("ocupado-txt").textContent = `${est.tarefa} · ${Math.round(Date.now() / 1000 - est.desde)}s`;
  $("hero").classList.toggle("rodando", !!est.tarefa);
  $("hero-titulo").textContent = est.tarefa ? est.tarefa + "..." : "Vamos colocar a fábrica pra trabalhar";
  $("hero-sub").textContent = est.tarefa ? "Pode acompanhar no quadro \"Acontecendo agora\". Os vídeos prontos vão aparecer em Aprovar."
    : est.ultima ? `Última: ${est.ultima.nome} (${est.ultima.ok ? "ok" : "deu erro, veja o Registro"}) às ${hora(est.ultima.fim)}.`
    : "Ela busca os clipes de GTA mais vistos da Twitch, edita e deixa pra você aprovar.";
  document.querySelectorAll("[data-tarefa]").forEach((b) => (b.disabled = !!est.tarefa));
  $("n-gravacoes").textContent = est.gravacoes ? `(${est.gravacoes} na pasta)` : "(pasta vazia)";

  const feito = (id, ok) => $(id).classList.toggle("feito", !!ok);
  feito("passo-twitch", est.twitch);
  feito("passo-telegram", est.telegram_ouvindo);
  feito("passo-streamers", est.autorizados > 0);
  feito("passo-buscar", videos.length > 0);
  $("s-twitch-id").placeholder = est.twitch ? "ID salvo ✔ (cole outro pra trocar)" : "ID do cliente";
  $("s-twitch-secret").placeholder = est.twitch ? "Segredo salvo ✔" : "Segredo do cliente";
  $("s-telegram").placeholder = est.telegram ? "Token salvo ✔ (cole outro pra trocar)" : "Token do bot (123456:ABC...)";
  $("telegram-info").textContent = est.telegram_ouvindo ? "Conectado: os vídeos novos vão pro seu Telegram."
    : est.telegram ? "Falta mandar /start pro bot e clicar em Salvar e testar." : "";
  $("streamers-info").textContent = est.autorizados
    ? `${est.autorizados} streamer(s) marcado(s) como liberado. Os outros chegam com aviso pra você decidir.`
    : "Marque em Configurações quem te autorizou. Os outros chegam com um aviso pra você decidir.";

  desenharLog();
  // quando uma tarefa termina, puxa os vídeos novos na hora
  if (tarefaAntes && !est.tarefa) carregarVideos();
  tarefaAntes = est.tarefa;
  clearTimeout(timerEstado);
  timerEstado = setTimeout(atualizarEstado, est.tarefa ? 1500 : 5000);
}
let timerEstado;

function desenharLog() {
  const linhas = (est.logs || []).map((l) => `<span class="h">${hora(l.t)}</span>  ${esc(l.txt)}`);
  const curto = $("log-curto"), longo = $("log");
  const fimC = curto.scrollTop + curto.clientHeight >= curto.scrollHeight - 20;
  curto.innerHTML = linhas.slice(-14).join("\n") || '<span class="h">Nada por enquanto.</span>';
  if (fimC) curto.scrollTop = curto.scrollHeight;
  if (aba === "registro") {
    const fim = longo.scrollTop + longo.clientHeight >= longo.scrollHeight - 30;
    longo.innerHTML = linhas.join("\n") || '<span class="h">Nada por enquanto.</span>';
    if (fim) longo.scrollTop = longo.scrollHeight;
  }
}

/* ---------------- vídeos ---------------- */
async function carregarVideos() {
  try { videos = (await api("/api/videos")).videos; } catch (e) { return; }
  const aprov = videos.filter((v) => v.status === "aprovado");
  const pend = videos.filter((v) => v.status === "pendente").length;
  const marcar = (id, n) => { $(id).textContent = n; n ? $(id).removeAttribute("data-zero") : $(id).setAttribute("data-zero", ""); };
  marcar("n-aprovar", pend);
  let postagens = 0, fila = 0;
  for (const r in REDES) {
    const n = aprov.filter((v) => v.redes[r] === "a_postar").length;
    marcar("n-" + r, n); fila += n;
    postagens += aprov.filter((v) => v.redes[r] === "postado").length;
  }
  $("k-aprovar").textContent = num(pend); $("k-aprovados").textContent = num(aprov.length);
  $("k-postagens").textContent = num(postagens); $("k-fila").textContent = num(fila);
  desenharVideos();
  if (aba === "numeros") desenharNumeros();
}

function editando() {
  const a = document.activeElement;
  return a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && a.closest(".videos");
}
function tocando() { return [...document.querySelectorAll(".videos video")].some((v) => !v.paused); }

function desenharVideos(forcar = false) {
  const sig = aba + JSON.stringify(filtro) + JSON.stringify(videos.map((v) => [v.nome, v.status, v.redes, v.titulo, v.legenda]));
  if (!forcar && (sig === assinatura || editando() || tocando())) return;
  assinatura = sig;
  if (aba === "aprovar") {
    const l = videos.filter((v) => v.status === "pendente");
    $("lista-aprovar").innerHTML = l.map(cardAprovar).join("") ||
      `<div class="vazio">Nada esperando aprovação. Na aba Início, clique em <b>Buscar na Twitch</b> ou <b>Buscar no YouTube</b>.</div>`;
  } else if (aba === "recusados") {
    const l = videos.filter((v) => v.status === "recusado");
    $("lista-recusados").innerHTML = l.map(cardRecusado).join("") || `<div class="vazio">Nenhum vídeo recusado.</div>`;
  } else if (REDES[aba]) {
    const f = filtro[aba] || "a_postar", aprov = videos.filter((v) => v.status === "aprovado");
    $("barra-rede").innerHTML = Object.entries(ESTADOS).map(([k, n]) =>
      `<button class="chip ${f === k ? "ativo" : ""}" data-filtro="${k}">${n} (${aprov.filter((v) => v.redes[aba] === k).length})</button>`).join("") +
      `<span class="espaco"></span><span class="suave">${REDES[aba].dica}</span>`;
    const l = aprov.filter((v) => v.redes[aba] === f);
    $("lista-rede").innerHTML = l.map((v) => cardRede(v, aba)).join("") ||
      `<div class="vazio">Nenhum vídeo em "${ESTADOS[f]}" no ${REDES[aba].nome}.</div>`;
  }
}

function metaHTML(v) {
  const i = v.info || {}, p = [];
  const yt = i.plataforma === "youtube";
  const canal = i.canal_url || (i.login ? `https://twitch.tv/${i.login}` : "");
  if (i.streamer) p.push(`${yt ? "▶️ YouTube" : "🎮 Twitch"} · <a href="${esc(canal)}" target="_blank" rel="noopener">${esc(i.streamer)}</a>`);
  if (i.views) p.push(`👁 ${num(i.views)} views`);
  if (i.url) p.push(`<a href="${esc(i.url)}" target="_blank" rel="noopener">${yt ? "vídeo original" : "clipe original"}</a>`);
  p.push(`${v.mb} MB`);
  return `<div class="meta">${p.join("<span>·</span>")}</div>` +
    (i.streamer && i.autorizado === false ? `<div class="alerta">⚠️ Streamer sem autorização confirmada. Aprove só se tiver certeza.</div>` : "");
}
const player = (v) => `<video src="${v.url}#t=0.5" controls preload="metadata" playsinline></video>`;

function cardAprovar(v) {
  return `<article class="card video" data-nome="${esc(v.nome)}">${player(v)}<div class="corpo">${metaHTML(v)}
    <label>Título<input class="e-titulo" value="${esc(v.titulo)}"></label>
    <label>Legenda (créditos e hashtags)<textarea class="e-legenda" rows="6">${esc(v.legenda)}</textarea></label>
    <div class="acoes"><button class="btn vermelho" data-status="recusado">Recusar</button>
      <button class="btn verde" data-status="aprovado">Aprovar</button></div></div></article>`;
}
function cardRecusado(v) {
  return `<article class="card video" data-nome="${esc(v.nome)}">${player(v)}<div class="corpo">
    <div class="titulo">${esc(v.titulo || v.nome)}</div>${metaHTML(v)}
    <div class="acoes"><button class="btn fantasma" data-status="pendente">Voltar pra fila</button></div></div></article>`;
}
function cardRede(v, rede) {
  const mini = Object.entries(REDES).filter(([k]) => k !== rede).map(([k, r]) => `<span>${r.nome}: ${ESTADOS[v.redes[k]]}</span>`).join("");
  return `<article class="card video" data-nome="${esc(v.nome)}">${player(v)}<div class="corpo">
    <div class="titulo">${esc(v.titulo || v.nome)}</div>${metaHTML(v)}
    <div class="estado">${Object.entries(ESTADOS).map(([k, n]) =>
      `<button class="${v.redes[rede] === k ? "sel" : ""}" data-e="${k}" data-rede="${rede}">${n}</button>`).join("")}</div>
    <div class="acoes"><button class="btn fantasma peq" data-copiar="titulo" data-rede="${rede}">Copiar título</button>
      <button class="btn fantasma peq" data-copiar="legenda">Copiar legenda</button>
      <a class="btn fantasma peq" href="${v.url}" download="${esc(v.nome)}">Baixar</a></div>
    <div class="redes-mini">${mini}</div></div></article>`;
}

/* ---------------- números ---------------- */
function mostrarTip(ev, titulo, linhas) {
  const t = $("tip");
  t.innerHTML = `<div class="t">${esc(titulo)}</div>` + linhas.map(([n, c, v]) =>
    `<div class="l">${c ? `<i style="background:${c}"></i>` : ""}${esc(n)}<b>${esc(v)}</b></div>`).join("");
  t.classList.add("on");
  const r = t.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  t.style.left = x + "px"; t.style.top = y + "px";
}
const esconderTip = () => $("tip").classList.remove("on");
const tabela = (cab, linhas) => `<table><thead><tr>${cab.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
  <tbody>${linhas.map((l) => `<tr>${l.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;

function desenharNumeros() {
  const aprov = videos.filter((v) => v.status === "aprovado");
  const pend = videos.filter((v) => v.status === "pendente").length;
  const recus = videos.filter((v) => v.status === "recusado").length;
  let postagens = 0, agendados = 0;
  for (const v of aprov) for (const r in REDES) { if (v.redes[r] === "postado") postagens++; if (v.redes[r] === "agendado") agendados++; }
  const dec = aprov.length + recus;
  const tile = (rot, val, det, extra = "") => `<div class="card tile ${extra}"><div class="rot">${rot}</div><div class="val">${val}</div><div class="det">${det}</div></div>`;
  $("kpis").innerHTML = tile("Postagens feitas", num(postagens), "somando YouTube, TikTok, Instagram e Facebook", "hero-num") +
    tile("Pra aprovar", num(pend), "esperando você") + tile("Aprovados", num(aprov.length), "vídeos liberados") +
    tile("Agendados", num(agendados), "postagens marcadas") +
    tile("Taxa de aprovação", dec ? Math.round(100 * aprov.length / dec) + "%" : "—", `${num(aprov.length)} de ${num(dec)} decididos`);

  // andamento por rede (barra segmentada, ordem e cores fixas por estado)
  $("sub-redes").textContent = `Situação dos ${num(aprov.length)} vídeos aprovados em cada rede`;
  $("leg-redes").innerHTML = PARTES.map(([, n, c]) => `<span><i style="background:var(${c})"></i>${n}</span>`).join("");
  const medir = document.createElement("canvas").getContext("2d");
  medir.font = "650 12.5px system-ui, Segoe UI, sans-serif";
  const el = $("g-redes"), linhas = [];
  el.innerHTML = Object.entries(REDES).map(([k, r]) =>
    `<div class="segs"><span class="nome">${r.nome}</span><div class="barra-seg" data-rede="${k}" role="img"></div><span class="tot"></span></div>`).join("");
  for (const [k, r] of Object.entries(REDES)) {
    const conta = Object.fromEntries(PARTES.map(([e]) => [e, aprov.filter((v) => v.redes[k] === e).length]));
    const total = PARTES.reduce((s, [e]) => s + conta[e], 0);
    linhas.push([r.nome, ...PARTES.map(([e]) => conta[e])]);
    const barra = el.querySelector(`[data-rede="${k}"]`);
    barra.parentElement.querySelector(".tot").textContent = total ? `${num(conta.postado)}/${num(total)}` : "";
    barra.setAttribute("aria-label", `${r.nome}: ` + PARTES.map(([e, n]) => `${n} ${conta[e]}`).join(", "));
    if (!total) { barra.innerHTML = `<div class="vazia"></div>`; continue; }
    barra.innerHTML = PARTES.filter(([e]) => conta[e]).map(([e, , c]) =>
      `<div class="p" style="flex:${conta[e]};background:var(${c})" data-e="${e}"></div>`).join("");
    requestAnimationFrame(() => barra.querySelectorAll(".p").forEach((p) => {
      const v = conta[p.dataset.e];
      if (medir.measureText(String(v)).width + 16 <= p.clientWidth) p.textContent = v;
    }));
    barra.querySelectorAll(".p").forEach((p) => {
      const [, n, c] = PARTES.find(([e]) => e === p.dataset.e), v = conta[p.dataset.e];
      p.onmousemove = (ev) => mostrarTip(ev, r.nome, [[n, cor(c), `${v} (${Math.round(100 * v / total)}%)`]]);
      p.onmouseleave = esconderTip;
    });
  }
  $("t-redes").innerHTML = tabela(["Rede", ...PARTES.map(([, n]) => n)], linhas);
  graficoDias();
  graficoStreamers();
}

function graficoDias() {
  const N = 14, hoje = new Date(); hoje.setHours(0, 0, 0, 0);
  const dias = [...Array(N)].map((_, i) => new Date(hoje.getTime() - (N - 1 - i) * 864e5));
  const idx = (ts) => { const d = new Date(ts * 1000); d.setHours(0, 0, 0, 0); const i = Math.round((d - dias[0]) / 864e5); return i >= 0 && i < N ? i : -1; };
  const gerados = Array(N).fill(0), postagens = Array(N).fill(0);
  for (const v of videos) {
    const i = idx(v.criado); if (i >= 0) gerados[i]++;
    for (const r in REDES) if (v.postado_em?.[r]) { const j = idx(v.postado_em[r]); if (j >= 0) postagens[j]++; }
  }
  const series = [{ nome: "Vídeos gerados", c: "--s-gerados", v: gerados }, { nome: "Postagens", c: "--s-postado", v: postagens }];
  const rot = (d) => d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  $("t-dias").innerHTML = tabela(["Dia", ...series.map((s) => s.nome)], dias.map((d, i) => [rot(d), ...series.map((s) => s.v[i])]));
  const el = $("g-dias");
  const desenhar = () => {
    const W = el.clientWidth, H = 230, m = { l: 30, r: 16, t: 14, b: 26 };
    if (!W) return;
    const maxv = Math.max(1, ...gerados, ...postagens);
    const passo = maxv <= 5 ? 1 : maxv <= 10 ? 2 : maxv <= 25 ? 5 : Math.ceil(maxv / 50) * 10;
    const topo = Math.ceil(maxv / passo) * passo;
    const x = (i) => m.l + i * (W - m.l - m.r) / (N - 1), y = (v) => m.t + (1 - v / topo) * (H - m.t - m.b);
    let s = `<svg height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Vídeos gerados e postagens por dia">`;
    for (let t = 0; t <= topo; t += passo)
      s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="${cor(t ? "--grade" : "--eixo")}"/><text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end">${t}</text>`;
    const cada = W < 480 ? 3 : 2;
    dias.forEach((d, i) => { if ((N - 1 - i) % cada === 0) s += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${rot(d)}</text>`; });
    for (const se of series) {
      s += `<path d="${se.v.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join("")}" fill="none" stroke="${cor(se.c)}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      s += `<circle cx="${x(N - 1)}" cy="${y(se.v[N - 1])}" r="4" fill="${cor(se.c)}" stroke="${cor("--card")}" stroke-width="2"/>`;
    }
    s += `<g id="cruz" style="display:none"><line id="cruz-l" y1="${m.t}" y2="${H - m.b}" stroke="${cor("--eixo")}"/>${series.map((se, k) =>
      `<circle id="cruz-${k}" r="4" fill="${cor(se.c)}" stroke="${cor("--card")}" stroke-width="2"/>`).join("")}</g>
      <rect x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent" id="alvo"/></svg>`;
    el.innerHTML = s;
    const cruz = el.querySelector("#cruz");
    el.querySelector("#alvo").onmousemove = (ev) => {
      const i = Math.max(0, Math.min(N - 1, Math.round((ev.clientX - el.getBoundingClientRect().left - m.l) / ((W - m.l - m.r) / (N - 1)))));
      cruz.style.display = "";
      ["x1", "x2"].forEach((a) => el.querySelector("#cruz-l").setAttribute(a, x(i)));
      series.forEach((se, k) => { const c = el.querySelector("#cruz-" + k); c.setAttribute("cx", x(i)); c.setAttribute("cy", y(se.v[i])); });
      mostrarTip(ev, dias[i].toLocaleDateString("pt-BR", { weekday: "short", day: "2-digit", month: "2-digit" }), series.map((se) => [se.nome, cor(se.c), se.v[i]]));
    };
    el.querySelector("#alvo").onmouseleave = () => { cruz.style.display = "none"; esconderTip(); };
  };
  desenhar();
  if (window._ro) window._ro.disconnect();
  window._ro = new ResizeObserver(desenhar); window._ro.observe(el);
}

function graficoStreamers() {
  const mapa = {};
  for (const v of videos) {
    if (v.status === "recusado" || !v.info?.streamer) continue;
    const s = (mapa[v.info.streamer] ||= { n: 0, views: 0 }); s.n++; s.views += v.info.views || 0;
  }
  const lista = Object.entries(mapa).sort((a, b) => b[1].n - a[1].n || b[1].views - a[1].views).slice(0, 8);
  const el = $("g-streamers");
  if (!lista.length) { el.innerHTML = `<div class="vazio-graf">Aparece aqui quando chegarem os clipes da Twitch.</div>`; $("t-streamers").innerHTML = ""; return; }
  const max = lista[0][1].n;
  el.innerHTML = lista.map(([nome, s]) => `<div class="hbar"><span class="nome" title="${esc(nome)}">${esc(nome)}</span>
    <div class="trilho"><div class="b" style="width:${(85 * s.n / max).toFixed(1)}%" data-nome="${esc(nome)}"></div><span class="v">${s.n}</span></div></div>`).join("");
  el.querySelectorAll(".b").forEach((b) => {
    const s = mapa[b.dataset.nome];
    b.onmousemove = (ev) => mostrarTip(ev, b.dataset.nome, [["Vídeos", cor("--s-agendado"), s.n], ["Views dos clipes", "", compacto(s.views)]]);
    b.onmouseleave = esconderTip;
  });
  $("t-streamers").innerHTML = tabela(["Streamer", "Vídeos", "Views dos clipes"], lista.map(([n, s]) => [n, s.n, num(s.views)]));
}

/* ---------------- configurações ---------------- */
let cfgAtual = {};
async function carregarConfig() {
  cfgAtual = await api("/api/config");
  const f = $("form-config");
  for (const el of f.elements) {
    if (!el.name || !(el.name in cfgAtual)) continue;
    let v = cfgAtual[el.name];
    // sem valor no config.json: essas duas vêm ligadas por padrão (igual à fábrica)
    if (el.type === "checkbox") el.checked = v == null
      ? ["twitch.descobrir.ativar", "twitch.usar_titulo_do_clipe", "twitch.streamers_qualquer_jogo"].includes(el.name) : !!v;
    else if (Array.isArray(v)) el.value = v.join(el.name === "twitch.hashtags" ? " " : ", ");
    else el.value = v ?? "";
  }
  $("streamers").innerHTML = $("canais").innerHTML = "";
  (cfgAtual["twitch.streamers"] || []).forEach((s) => linhaCriador("streamers", "login", s));
  (cfgAtual["youtube.canais"] || []).forEach((s) => linhaCriador("canais", "canal", s));
}
// uma linha da lista de criadores: Twitch guarda em "login", YouTube em "canal"
function linhaCriador(lista, chave, s = {}) {
  const d = document.createElement("div");
  d.className = "streamer";
  const dica = chave === "login" ? "login na Twitch (ex.: gaules)" : "@ do canal (ex.: @paulinholoko)";
  d.innerHTML = `<input class="st-login" placeholder="${dica}" value="${esc(s[chave] || "")}">
    <label class="check"><input type="checkbox" class="st-aut" ${s.autorizado ? "checked" : ""}> liberou</label>
    <button type="button" class="btn fantasma peq st-tirar" title="Tirar da lista">✕</button>`;
  d.querySelector(".st-tirar").onclick = () => d.remove();
  d._extra = s;
  $(lista).appendChild(d);
}
function lerCriadores(lista, chave, limpar) {
  return [...$(lista).querySelectorAll(".streamer")].map((d) => ({
    ...d._extra, [chave]: limpar(d.querySelector(".st-login").value.trim()),
    autorizado: d.querySelector(".st-aut").checked })).filter((s) => s[chave]);
}
$("btn-add-streamer").onclick = () => linhaCriador("streamers", "login");
$("btn-add-canal").onclick = () => linhaCriador("canais", "canal");
$("form-config").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target, dados = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") dados[el.name] = el.checked;
    else if (el.type === "number") dados[el.name] = el.value === "" ? 0 : Number(el.value);
    else if (el.name === "twitch.hashtags") dados[el.name] = el.value.split(/\s+/).filter(Boolean);
    else if (["twitch.nunca_pegar", "youtube.nunca_pegar", "youtube.buscas"].includes(el.name))
      dados[el.name] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
    else dados[el.name] = el.value.trim();
  }
  dados["twitch.streamers"] = lerCriadores("streamers", "login",
    (v) => v.toLowerCase().replace(/^.*twitch\.tv\//, "").replace(/\/.*$/, ""));
  // aceita "@canal", "canal" ou o link youtube.com/@canal
  dados["youtube.canais"] = lerCriadores("canais", "canal", (v) => {
    const m = v.match(/youtube\.com\/(@[\w.-]+)/i);
    v = m ? m[1] : v.replace(/\/.*$/, "");
    return v && !v.startsWith("@") ? "@" + v : v;
  });
  try { await api("/api/config", dados); aviso("Configurações salvas"); atualizarEstado(); }
  catch (e) { aviso(e.message, true); }
};

/* ---------------- cliques ---------------- */
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button, [data-ir]");
  if (!b) return;
  if (b.dataset.aba) return irPara(b.dataset.aba);
  if (b.dataset.ir) return irPara(b.dataset.ir);
  if (b.dataset.tarefa) {
    try { await api("/api/tarefa", { tarefa: b.dataset.tarefa }); aviso("Começou! Acompanhe em \"Acontecendo agora\"."); atualizarEstado(); }
    catch (e) { aviso(e.message, true); }
    return;
  }
  if (b.dataset.filtro) {
    filtro[aba] = b.dataset.filtro; try { localStorage.setItem("filtro", JSON.stringify(filtro)); } catch (e) {}
    return desenharVideos(true);
  }
  const card = b.closest("[data-nome]"), nome = card?.dataset.nome;
  if (b.dataset.status) {
    const dados = { nome, status: b.dataset.status };
    const t = card.querySelector(".e-titulo");
    if (t) { dados.titulo = t.value; dados.legenda = card.querySelector(".e-legenda").value; }
    try {
      const r = await api("/api/status", dados);
      aviso(r.ok ? { aprovado: "Aprovado ✔", recusado: "Recusado", pendente: "Voltou pra fila" }[b.dataset.status] : "Esse já tinha sido resolvido", !r.ok);
    } catch (e) { aviso(e.message, true); }
    document.activeElement?.blur();
    return carregarVideos().then(() => desenharVideos(true));
  }
  if (b.dataset.e) {
    await api("/api/rede", { nome, rede: b.dataset.rede, estado: b.dataset.e });
    aviso(`${REDES[b.dataset.rede].nome}: ${ESTADOS[b.dataset.e]}`);
    return carregarVideos();
  }
  if (b.dataset.copiar) {
    const v = videos.find((x) => x.nome === nome);
    let texto = b.dataset.copiar === "titulo" ? v.titulo : v.legenda;
    if (b.dataset.copiar === "titulo" && b.dataset.rede === "youtube" && texto.length > 100) texto = texto.slice(0, 97) + "...";
    try { await navigator.clipboard.writeText(texto); aviso("Copiado!"); } catch (e) { prompt("Copie o texto:", texto); }
  }
});

$("btn-twitch").onclick = async () => {
  const id = $("s-twitch-id").value.trim(), secret = $("s-twitch-secret").value.trim();
  try {
    if (id || secret) await api("/api/segredos", { twitch_client_id: id, twitch_client_secret: secret });
    $("s-twitch-id").value = $("s-twitch-secret").value = "";
    $("twitch-info").textContent = "Testando...";
    const r = await api("/api/testar/twitch", {});
    $("twitch-info").textContent = r.ok ? "Conectou na Twitch ✔" : "Não conectou. O motivo está no Registro.";
    aviso(r.ok ? "Twitch conectada!" : "Não conectou na Twitch (veja o Registro)", !r.ok);
  } catch (e) { aviso(e.message, true); }
  atualizarEstado();
};
$("btn-telegram").onclick = async () => {
  const tok = $("s-telegram").value.trim();
  try {
    if (tok) await api("/api/segredos", { telegram_token: tok });
    $("s-telegram").value = "";
    const r = await api("/api/testar/telegram", {});
    aviso(r.ok ? "Telegram conectado! Olha a mensagem no seu celular." : "Ainda não: mande /start pro bot e tente de novo.", !r.ok);
  } catch (e) { aviso(e.message, true); }
  atualizarEstado();
};
$("btn-pasta").onclick = () => api("/api/abrir-pasta", {}).catch((e) => aviso(e.message, true));

/* ---------------- começo ---------------- */
irPara(location.hash.replace(/^#\/?/, "") || aba);
atualizarEstado();
carregarVideos();
setInterval(carregarVideos, 10000);
