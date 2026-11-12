/* Kanban da Área de Dev — núcleo.
 *
 * Monta toda a aba "Planejamento" dentro de #dvk-root. Só perfil 'dev' chega
 * aqui (o backend também barra: /api/dev-tarefas devolve 403 pra outros).
 *
 * Extensões (IA, conexões) se penduram por DVK.registrar*() — ver o fim do
 * arquivo — sem mexer neste núcleo.
 */
(function () {
  'use strict';

  const STATUS = [
    { k: 'backlog', nome: 'Backlog', cor: '#94A3B8' },
    { k: 'todo', nome: 'A fazer', cor: '#64748B' },
    { k: 'doing', nome: 'Em progresso', cor: '#06B6D4' },
    { k: 'done', nome: 'Concluído', cor: '#5B2EE0' },
  ];
  const PRIO = {
    urgente: 'Urgente', alta: 'Alta', media: 'Média', baixa: 'Baixa',
  };
  const CORES = ['#7C3AED', '#0EA5E9', '#10B981', '#F43F5E', '#F59E0B', '#14B8A6', '#D946EF', '#6366F1'];
  const CONCLUIDAS_DIAS = 14;
  const K_FILTROS = 'dvk_filtros_v1';
  const K_VISAO = 'dvk_visao_v1';

  // ── estado ────────────────────────────────────────────────────
  const S = {
    tarefas: [], usuarios: [], montado: false,
    filtros: lerLS(K_FILTROS, { busca: '', tag: '', atrib: '', prio: '', atrasadas: false }),
    visao: lerLS(K_VISAO, 'quadro'),
    todasConcluidas: false, dragId: null,
    drawer: null,           // { id|null, form, dirty, checklist, comentarios }
    lanesFechadas: new Set(),
  };
  const ext = { botoes: [], acoesPaleta: [], fallbacksPaleta: [], secoesDrawer: [], focoExtra: [], aposCarregar: [] };

  function lerLS(k, padrao) {
    try {
      const v = JSON.parse(localStorage.getItem(k));
      if (v == null) return padrao;
      return (typeof padrao === 'object' && !Array.isArray(padrao)) ? Object.assign({}, padrao, v) : v;
    } catch (e) { return padrao; }
  }
  function salvarLS(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* modo privado */ } }

  // ── utilitários ───────────────────────────────────────────────
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const $ = (sel, raiz) => (raiz || document).querySelector(sel);
  const $$ = (sel, raiz) => Array.from((raiz || document).querySelectorAll(sel));

  function hoje() {
    // Data LOCAL (toISOString é UTC e vira "amanhã" depois das 21h em Brasília).
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  const diasAte = (iso, h) => Math.round((Date.parse(iso) - Date.parse(h)) / 86400000);
  function fmtData(iso, h) {
    const [a, m, d] = iso.slice(0, 10).split('-');
    return a === h.slice(0, 4) ? `${d}/${m}` : `${d}/${m}/${a}`;
  }
  function corTag(tag) {
    let h = 0; const s = String(tag).toLowerCase();
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return CORES[h % CORES.length];
  }
  const projetoDe = t => (t.tags && t.tags[0]) || null;
  const iniciais = n => (n || '?').trim().charAt(0).toUpperCase();

  function tagsDistintas() {
    const mapa = new Map();
    S.tarefas.forEach(t => (t.tags || []).forEach(tg => {
      const k = tg.toLowerCase(); const a = mapa.get(k);
      if (a) a.n++; else mapa.set(k, { tag: tg, n: 1 });
    }));
    return [...mapa.values()].sort((a, b) => b.n - a.n || a.tag.localeCompare(b.tag)).map(x => x.tag);
  }

  let toastTimer = null;
  function toast(msg, tipo) {
    let el = $('#dvk-toast');
    if (!el) { el = document.createElement('div'); el.id = 'dvk-toast'; document.body.appendChild(el); }
    el.className = 'dvk-toast on' + (tipo === 'erro' ? ' erro' : '');
    el.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), tipo === 'erro' ? 4200 : 2400);
  }

  async function api(caminho, opts) {
    const o = Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {});
    if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
    const res = await fetch(caminho, o);
    let dados = null;
    try { dados = await res.json(); } catch (e) { /* sem corpo */ }
    if (!res.ok) {
      let msg = dados && dados.detail;
      if (Array.isArray(msg)) msg = msg.map(m => m.msg).join('; ');
      const err = new Error(msg || ('Erro ' + res.status));
      err.status = res.status;
      throw err;
    }
    return dados;
  }

  // ── dados ─────────────────────────────────────────────────────
  async function carregar() {
    try {
      const [tarefas, usuarios] = await Promise.all([
        api('/api/dev-tarefas'),
        S.usuarios.length ? Promise.resolve(S.usuarios) : api('/api/dev-tarefas/usuarios'),
      ]);
      S.tarefas = tarefas; S.usuarios = usuarios;
      validarFiltros();
      renderTudo();
      ext.aposCarregar.forEach(fn => { try { fn(S.tarefas); } catch (err) { console.error(err); } });
    } catch (e) { toast('Não consegui carregar as tarefas: ' + e.message, 'erro'); }
  }

  function validarFiltros() {
    const f = S.filtros;
    const tags = tagsDistintas();
    if (f.tag) {
      const exata = tags.find(t => t.toLowerCase() === f.tag.toLowerCase());
      f.tag = exata || '';
    }
    if (f.atrib && !S.usuarios.some(u => String(u.id) === String(f.atrib))) f.atrib = '';
  }

  function colunaOrdenada(status) {
    return S.tarefas.filter(t => t.status === status).sort((a, b) =>
      status === 'done'
        ? (b.concluido_em || '').localeCompare(a.concluido_em || '') || b.id - a.id
        : (a.ordem - b.ordem) || (a.id - b.id));
  }

  function filtrosAtivos() { const f = S.filtros; return !!(f.busca || f.tag || f.atrib || f.prio || f.atrasadas); }

  function passa(t, h) {
    const f = S.filtros;
    if (f.atrib && String(t.atribuido_a) !== String(f.atrib)) return false;
    if (f.prio && t.prioridade !== f.prio) return false;
    if (f.tag && !(t.tags || []).some(x => x.toLowerCase() === f.tag.toLowerCase())) return false;
    if (f.atrasadas && !(t.prazo && t.prazo < h && t.status !== 'done')) return false;
    if (f.busca) {
      const q = f.busca.toLowerCase();
      if (![t.titulo, t.descricao, (t.tags || []).join(' '), t.atribuido_a_nome].join(' ').toLowerCase().includes(q)) return false;
    }
    return true;
  }

  function corpoDe(t, sobre) {
    return Object.assign({
      titulo: t.titulo, descricao: t.descricao || '', status: t.status, prioridade: t.prioridade,
      prazo: t.prazo, tags: t.tags || [], link: t.link || '', atribuido_a: t.atribuido_a || null, pausado: !!t.pausado,
    }, sobre || {});
  }

  // ── render: esqueleto ─────────────────────────────────────────
  function montar() {
    const raiz = $('#dvk-root');
    if (!raiz || S.montado) return;
    S.montado = true;
    raiz.innerHTML = `
      <div class="dvk">
        <div class="dvk-top">
          <button class="dvk-cmd" id="dvk-cmd" type="button"><i class="ph ph-magnifying-glass"></i><span>Buscar, criar ou perguntar…</span><kbd>Ctrl K</kbd></button>
          <div class="dvk-seg" id="dvk-visao"><button data-v="quadro">Quadro</button><button data-v="projetos">Projetos</button></div>
          <div class="dvk-top-acoes" id="dvk-top-acoes"></div>
        </div>
        <section class="dvk-foco" id="dvk-foco"></section>
        <div class="dvk-filtros">
          <nav class="dvk-projetos" id="dvk-projetos"></nav>
          <select class="dvk-sel" id="dvk-f-atrib"></select>
          <select class="dvk-sel" id="dvk-f-prio"><option value="">Qualquer prioridade</option>${Object.keys(PRIO).map(k => `<option value="${k}">${PRIO[k]}</option>`).join('')}</select>
          <label class="dvk-toggle"><input type="checkbox" id="dvk-f-atras"> Atrasadas</label>
          <button class="dvk-limpar" id="dvk-limpar" type="button" hidden>Limpar</button>
        </div>
        <div id="dvk-area"></div>
      </div>
      <div class="dvk-scrim" id="dvk-scrim"></div>
      <aside class="dvk-drawer" id="dvk-drawer" aria-hidden="true"></aside>
      <div class="dvk-palette" id="dvk-palette"><div class="dvk-pal-box">
        <input id="dvk-pal-in" placeholder="Digite uma tarefa, comando ou pergunta…" autocomplete="off">
        <div class="dvk-pal-lista" id="dvk-pal-lista"></div>
        <div class="dvk-pal-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> navegar</span><span><kbd>Enter</kbd> escolher</span><span><kbd>Esc</kbd> fechar</span></div>
      </div></div>`;

    $('#dvk-cmd').onclick = abrirPaleta;
    $$('#dvk-visao button').forEach(b => b.onclick = () => { S.visao = b.dataset.v; salvarLS(K_VISAO, S.visao); renderTudo(); });
    $('#dvk-f-atrib').onchange = lerFiltrosDom;
    $('#dvk-f-prio').onchange = lerFiltrosDom;
    $('#dvk-f-atras').onchange = lerFiltrosDom;
    $('#dvk-limpar').onclick = limparFiltros;
    $('#dvk-scrim').onclick = tentarFecharDrawer;
    ligarDragDrop($('#dvk-area'));
    ligarPaleta();
    document.addEventListener('keydown', teclasGlobais);
    renderBotoesTopo();
  }

  function lerFiltrosDom() {
    S.filtros.atrib = $('#dvk-f-atrib').value;
    S.filtros.prio = $('#dvk-f-prio').value;
    S.filtros.atrasadas = $('#dvk-f-atras').checked;
    salvarLS(K_FILTROS, S.filtros);
    renderTudo();
  }
  function setFiltro(parcial) {
    Object.assign(S.filtros, parcial);
    salvarLS(K_FILTROS, S.filtros);
    renderTudo();
  }
  function limparFiltros() { setFiltro({ busca: '', tag: '', atrib: '', prio: '', atrasadas: false }); }

  function renderBotoesTopo() {
    const el = $('#dvk-top-acoes');
    if (!el) return;
    el.innerHTML = ext.botoes.map(b => `<button class="dvk-btn ${b.classe || ''}" data-id="${esc(b.id)}" type="button" title="${esc(b.dica || '')}"><i class="ph ${esc(b.icone || 'ph-sparkle')}"></i>${esc(b.titulo)}</button>`).join('') +
      `<button class="dvk-btn primario" id="dvk-nova" type="button"><i class="ph ph-plus"></i>Nova tarefa</button>`;
    ext.botoes.forEach(b => { const n = $(`[data-id="${CSS.escape(b.id)}"]`, el); if (n) n.onclick = () => b.onclick(); });
    $('#dvk-nova').onclick = () => abrirDrawer(null);
  }

  function renderTudo() {
    if (!S.montado) return;
    renderFiltros();
    renderFoco();
    renderArea();
  }

  // ── render: filtros ───────────────────────────────────────────
  function renderFiltros() {
    const f = S.filtros, tags = tagsDistintas();
    const abertas = t => t.status !== 'done';
    const contagem = tg => S.tarefas.filter(t => abertas(t) && (t.tags || []).some(x => x.toLowerCase() === tg.toLowerCase())).length;
    $('#dvk-projetos').innerHTML =
      `<button class="dvk-pill ${f.tag ? '' : 'on'}" data-tag="">Tudo <em>${S.tarefas.filter(abertas).length}</em></button>` +
      tags.map(tg => `<button class="dvk-pill ${f.tag.toLowerCase() === tg.toLowerCase() ? 'on' : ''}" data-tag="${esc(tg)}"><i style="background:${corTag(tg)}"></i>${esc(tg)} <em>${contagem(tg)}</em></button>`).join('');
    if (f.busca) $('#dvk-projetos').insertAdjacentHTML('afterbegin', `<button class="dvk-pill on" data-busca="1" title="Remover busca"><i class="ph ph-magnifying-glass"></i>“${esc(f.busca)}” ×</button>`);
    $$('#dvk-projetos .dvk-pill[data-tag]').forEach(b => b.onclick = () => setFiltro({ tag: b.dataset.tag }));
    const bb = $('#dvk-projetos [data-busca]'); if (bb) bb.onclick = () => setFiltro({ busca: '' });
    $('#dvk-f-atrib').innerHTML = '<option value="">Todo mundo</option>' + S.usuarios.map(u => `<option value="${u.id}">${esc(u.nome)}</option>`).join('');
    $('#dvk-f-atrib').value = f.atrib; $('#dvk-f-prio').value = f.prio; $('#dvk-f-atras').checked = !!f.atrasadas;
    $('#dvk-limpar').hidden = !filtrosAtivos();
    $$('#dvk-visao button').forEach(b => b.classList.toggle('on', b.dataset.v === S.visao));
  }

  // ── render: foco do dia ───────────────────────────────────────
  function chipMotivo(m) {
    const cls = /atrasada|parada|urgente/.test(m) ? 'late' : /hoje|amanhã|vence/.test(m) ? 'warn' : '';
    return `<span class="dvk-chip ${cls}">${esc(m)}</span>`;
  }

  function renderFoco() {
    const el = $('#dvk-foco');
    const top = S.tarefas.filter(t => (t.score || 0) > 0)
      .sort((a, b) => b.score - a.score || (a.prazo || '9999').localeCompare(b.prazo || '9999') || a.id - b.id).slice(0, 5);
    const data0 = new Date().toLocaleDateString('pt-BR', { weekday: 'long', day: 'numeric', month: 'long' });
    const data = data0.charAt(0).toUpperCase() + data0.slice(1);
    const abertas = S.tarefas.filter(t => t.status !== 'done');
    const atrasadas = abertas.filter(t => t.prazo && t.prazo < hoje()).length;
    const semana = S.tarefas.filter(t => t.status === 'done' && t.concluido_em && (Date.now() - new Date(t.concluido_em).getTime()) <= 7 * 86400000).length;
    el.innerHTML = `
      <div class="dvk-foco-head">
        <h3>Foco de hoje</h3><small>${esc(data)}</small>
        <span class="dvk-spacer"></span>
        <span class="dvk-chip">${abertas.length} em aberto</span>
        ${atrasadas ? `<span class="dvk-chip late">${atrasadas} atrasada${atrasadas > 1 ? 's' : ''}</span>` : ''}
        <span class="dvk-chip ok">${semana} concluída${semana === 1 ? '' : 's'} na semana</span>
      </div>
      ${top.length ? `<div class="dvk-foco-grid">${top.map((t, i) => `
        <div class="dvk-foco-item" data-id="${t.id}">
          <span class="dvk-foco-n">${i + 1}</span>
          <div class="dvk-foco-corpo">
            <h4>${esc(t.titulo)}</h4>
            <div style="display:flex;gap:4px;flex-wrap:wrap">${(t.motivos || []).slice(0, 3).map(chipMotivo).join('')}</div>
          </div>
          <div class="dvk-foco-act">${botoesAcao(t)}</div>
        </div>`).join('')}</div>`
        : '<div class="dvk-vazio-foco">Nada pressionando hoje — bom momento pra puxar algo do backlog.</div>'}
      <div id="dvk-foco-extra"></div>`;
    $$('.dvk-foco-item', el).forEach(n => n.onclick = e => { if (!e.target.closest('button')) abrirDrawer(+n.dataset.id); });
    ligarAcoes(el);
    ext.focoExtra.forEach(fn => { try { fn($('#dvk-foco-extra', el)); } catch (e) { console.error(e); } });
  }

  // ── render: quadro / raias ────────────────────────────────────
  function botoesAcao(t) {
    if (t.status === 'done') return `<button class="dvk-ico warn" data-acao="reabrir" data-id="${t.id}" title="Reabrir"><i class="ph ph-arrow-counter-clockwise"></i></button>`;
    if (t.status === 'doing') return `<button class="dvk-ico warn" data-acao="pausar" data-id="${t.id}" title="Pausar"><i class="ph ph-pause"></i></button><button class="dvk-ico ok" data-acao="concluir" data-id="${t.id}" title="Concluir"><i class="ph ph-check"></i></button>`;
    return `<button class="dvk-ico" data-acao="iniciar" data-id="${t.id}" title="Iniciar"><i class="ph ph-play"></i></button><button class="dvk-ico ok" data-acao="concluir" data-id="${t.id}" title="Concluir"><i class="ph ph-check"></i></button>`;
  }

  function ligarAcoes(raiz) {
    $$('[data-acao]', raiz).forEach(b => b.onclick = e => { e.stopPropagation(); acaoRapida(+b.dataset.id, b.dataset.acao); });
  }

  function infoQuando(t, h) {
    if (t.status === 'done') return t.concluido_em ? { txt: 'Concluída ' + fmtData(t.concluido_em, h), cls: 'ok' } : null;
    if (!t.prazo) return null;
    const d = diasAte(t.prazo, h);
    if (d < 0) return { txt: `Atrasada há ${-d}d`, cls: 'late', atrasada: true };
    if (d === 0) return { txt: 'Vence hoje', cls: 'warn' };
    if (d === 1) return { txt: 'Vence amanhã', cls: 'warn' };
    return { txt: fmtData(t.prazo, h), cls: '' };
  }

  function cardHtml(t, h) {
    const q = infoQuando(t, h);
    const proj = projetoDe(t), resto = (t.tags || []).slice(1, 3);
    const pausada = t.status === 'todo' && t.pausado;
    const ghEstado = ext.badgeCard ? ext.badgeCard(t) : '';
    const pct = t.checklist_total ? Math.round(100 * t.checklist_concluidos / t.checklist_total) : 0;
    const origem = t.origem && t.origem !== 'manual' ? `<span class="dvk-chip" title="Origem: ${esc(t.origem)}">${esc(t.origem)}</span>` : '';
    return `
    <article class="dvk-card p-${esc(t.prioridade)} ${q && q.atrasada ? 'atrasada' : ''} ${pausada ? 'pausada' : ''} ${t.status === 'done' ? 'feita' : ''}" draggable="true" data-id="${t.id}">
      <div class="dvk-card-top">
        ${proj ? `<span class="dvk-proj"><i style="background:${corTag(proj)}"></i><span>${esc(proj)}</span></span>` : '<span class="dvk-proj"><span>Sem projeto</span></span>'}
        ${resto.map(r => `<span class="dvk-sub">#${esc(r)}</span>`).join('')}
        ${pausada ? '<span class="dvk-chip warn"><i class="ph ph-pause"></i>pausada</span>' : ''}
        <div class="dvk-acoes">${botoesAcao(t)}</div>
      </div>
      <h4>${esc(t.titulo)}</h4>
      <div class="dvk-meta">
        ${q ? `<span class="when ${q.cls}">${esc(q.txt)}</span>` : ''}
        ${t.status === 'doing' && t.iniciado_em && -diasAte(t.iniciado_em.slice(0, 10), h) >= 1 ? `<span class="it" title="Em progresso desde ${esc(fmtData(t.iniciado_em, h))}"><i class="ph ph-timer"></i>${-diasAte(t.iniciado_em.slice(0, 10), h)}d</span>` : ''}
        ${t.checklist_total ? `<span class="it ${t.checklist_concluidos === t.checklist_total ? 'ok' : ''}"><i class="ph ph-check-square"></i>${t.checklist_concluidos}/${t.checklist_total}</span>` : ''}
        ${t.comentarios_total ? `<span class="it"><i class="ph ph-chat-circle"></i>${t.comentarios_total}</span>` : ''}
        ${/^https?:\/\//i.test(t.link || '') ? `<a class="it" href="${esc(t.link)}" target="_blank" rel="noopener noreferrer" data-stop title="${esc(t.link)}"><i class="ph ph-link"></i></a>` : ''}
        ${ghEstado}${origem}
        ${t.atribuido_a_nome ? `<span class="dvk-av" title="${esc(t.atribuido_a_nome)}">${esc(iniciais(t.atribuido_a_nome))}</span>` : ''}
      </div>
      ${t.checklist_total && t.status !== 'done' ? `<div class="dvk-bar"><i style="width:${pct}%"></i></div>` : ''}
    </article>`;
  }

  function celulasParaStatus(lista, status, h, filtrando, lane) {
    let itens = lista.filter(t => passa(t, h));
    const total = itens.length;
    let escondidas = 0;
    if (status === 'done' && !filtrando && !S.todasConcluidas) {
      const corte = Date.now() - CONCLUIDAS_DIAS * 86400000;
      const rec = itens.filter(t => !t.concluido_em || new Date(t.concluido_em).getTime() >= corte);
      escondidas = itens.length - rec.length; itens = rec;
    }
    let html = itens.length ? itens.map(t => cardHtml(t, h)).join('')
      : `<div class="dvk-vazio">${lane ? '—' : filtrando ? 'Nada com esses filtros' : 'Nada por aqui'}</div>`;
    if (status === 'done' && escondidas > 0) html += `<button class="dvk-mais-link" data-mais>Mostrar mais ${escondidas} (antes de ${CONCLUIDAS_DIAS} dias)</button>`;
    else if (status === 'done' && S.todasConcluidas && !filtrando && total > 0) html += '<button class="dvk-mais-link" data-mais>Mostrar só as recentes</button>';
    return { html, total };
  }

  function colunaHtml(s, lista, h, filtrando, comAdicionar) {
    const { html, total } = celulasParaStatus(lista, s.k, h, filtrando, !comAdicionar);
    return `
    <section class="dvk-col" data-status="${s.k}">
      <div class="dvk-col-head"><span class="dot" style="background:${s.cor}"></span><b>${s.nome}</b><span class="n">${total}</span>
        ${comAdicionar ? `<button class="mais" data-qa="${s.k}" title="Adicionar em ${s.nome}">+</button>` : ''}</div>
      ${comAdicionar ? `<div class="dvk-qa" data-qa-box="${s.k}" hidden><input maxlength="200" placeholder="Nova tarefa e Enter…"><small>Enter adiciona · Esc fecha</small></div>` : ''}
      <div class="dvk-lista">${html}</div>
    </section>`;
  }

  function renderArea() {
    const h = hoje(), filtrando = filtrosAtivos(), area = $('#dvk-area');
    if (S.visao === 'projetos') {
      const grupos = new Map();
      S.tarefas.filter(t => passa(t, h)).forEach(t => {
        const p = projetoDe(t) || '— Sem projeto';
        const k = p.toLowerCase();
        if (!grupos.has(k)) grupos.set(k, { nome: p, itens: [] });
        grupos.get(k).itens.push(t);
      });
      const ordem = [...grupos.values()].sort((a, b) => (a.nome.startsWith('—') ? 1 : 0) - (b.nome.startsWith('—') ? 1 : 0) || b.itens.filter(t => t.status !== 'done').length - a.itens.filter(t => t.status !== 'done').length);
      area.innerHTML = ordem.length ? ordem.map(g => {
        const abertas = g.itens.filter(t => t.status !== 'done').length;
        const fechada = S.lanesFechadas.has(g.nome.toLowerCase());
        return `<div class="dvk-lane ${fechada ? 'fechada' : ''}" data-lane="${esc(g.nome.toLowerCase())}">
          <div class="dvk-lane-head"><span class="dot" style="background:${g.nome.startsWith('—') ? '#94A3B8' : corTag(g.nome)}"></span><b>${esc(g.nome)}</b><small>${abertas} em aberto · ${g.itens.length - abertas} concluída${g.itens.length - abertas === 1 ? '' : 's'}</small></div>
          <div class="dvk-board">${STATUS.map(s => colunaHtml(s, g.itens.filter(t => t.status === s.k).sort((a, b) => s.k === 'done' ? (b.concluido_em || '').localeCompare(a.concluido_em || '') : a.ordem - b.ordem), h, filtrando, false)).join('')}</div>
        </div>`;
      }).join('') : '<div class="dvk-vazio">Nada com esses filtros</div>';
      $$('.dvk-lane-head', area).forEach(n => n.onclick = () => {
        const k = n.parentElement.dataset.lane;
        S.lanesFechadas.has(k) ? S.lanesFechadas.delete(k) : S.lanesFechadas.add(k);
        n.parentElement.classList.toggle('fechada');
      });
    } else {
      area.innerHTML = `<div class="dvk-board">${STATUS.map(s => colunaHtml(s, colunaOrdenada(s.k), h, filtrando, true)).join('')}</div>`;
    }
    $$('.dvk-card', area).forEach(c => c.onclick = e => { if (!e.target.closest('button,a')) abrirDrawer(+c.dataset.id); });
    $$('a[data-stop]', area).forEach(a => a.onclick = e => e.stopPropagation());
    $$('[data-mais]', area).forEach(b => b.onclick = () => { S.todasConcluidas = !S.todasConcluidas; renderArea(); });
    $$('[data-qa]', area).forEach(b => b.onclick = () => abrirQuickAdd(b.dataset.qa));
    $$('[data-qa-box] input', area).forEach(inp => {
      inp.onkeydown = e => quickAddTecla(e, inp.closest('[data-qa-box]').dataset.qaBox);
      inp.onblur = () => { if (!inp.value.trim()) inp.closest('[data-qa-box]').hidden = true; };
    });
    ligarAcoes(area);
  }

  // ── ações rápidas ─────────────────────────────────────────────
  async function acaoRapida(id, acao) {
    const t = S.tarefas.find(x => x.id === id); if (!t) return;
    const alvo = {
      iniciar: { status: 'doing', pausado: false }, pausar: { status: 'todo', pausado: true },
      concluir: { status: 'done', pausado: false }, reabrir: { status: 'todo', pausado: false },
    }[acao];
    if (!alvo) return;
    try {
      await api(`/api/dev-tarefas/${id}`, { method: 'PUT', body: corpoDe(t, alvo) });
      await carregar();
    } catch (e) { toast('Não consegui atualizar: ' + e.message, 'erro'); }
  }

  function abrirQuickAdd(status) {
    const box = $(`[data-qa-box="${status}"]`); if (!box) return;
    box.hidden = false; const i = $('input', box); i.value = ''; i.focus();
  }
  async function quickAddTecla(e, status) {
    const inp = e.target;
    if (e.key === 'Escape') { inp.closest('[data-qa-box]').hidden = true; return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const titulo = inp.value.trim(); if (!titulo) return;
    const f = S.filtros;
    inp.disabled = true;
    try {
      await api('/api/dev-tarefas', { method: 'POST', body: {
        titulo, status, tags: f.tag ? [f.tag] : [], atribuido_a: f.atrib ? parseInt(f.atrib) : null, prioridade: f.prio || 'media',
      } });
      await carregar();
      const box = $(`[data-qa-box="${status}"]`); if (box) { box.hidden = false; const novo = $('input', box); novo.value = ''; novo.focus(); }
    } catch (err) { toast('Não consegui criar: ' + err.message, 'erro'); inp.disabled = false; inp.focus(); }
  }

  // ── drag-and-drop com posição ─────────────────────────────────
  function alvoDrop(zona, y) {
    for (const c of $$('.dvk-card:not(.arrastando)', zona)) {
      const r = c.getBoundingClientRect();
      if (y < r.top + r.height / 2) return +c.dataset.id;
    }
    return null;
  }
  function limparLinha() { const l = $('#dvk-linha'); if (l) l.remove(); }
  function ligarDragDrop(area) {
    area.addEventListener('dragstart', e => {
      const c = e.target.closest && e.target.closest('.dvk-card'); if (!c) return;
      S.dragId = +c.dataset.id; c.classList.add('arrastando');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', c.dataset.id); } catch (err) { /* Firefox */ }
    });
    area.addEventListener('dragend', () => {
      limparLinha(); S.dragId = null;
      $$('.dvk-card.arrastando, .dvk-col.sobre').forEach(n => n.classList.remove('arrastando', 'sobre'));
    });
    area.addEventListener('dragover', e => {
      if (S.dragId == null) return;
      const z = e.target.closest('.dvk-col'); if (!z) return;
      e.preventDefault(); e.dataTransfer.dropEffect = 'move';
      $$('.dvk-col.sobre', area).forEach(n => { if (n !== z) n.classList.remove('sobre'); });
      z.classList.add('sobre');
      if (z.dataset.status === 'done') { limparLinha(); return; }   // Concluído é por data, sem posição manual
      const lista = $('.dvk-lista', z);
      let linha = $('#dvk-linha') || Object.assign(document.createElement('div'), { id: 'dvk-linha', className: 'dvk-linha' });
      const antes = alvoDrop(z, e.clientY);
      const ref = antes != null ? $(`.dvk-card[data-id="${antes}"]`, lista) : $('.dvk-vazio, .dvk-mais-link', lista);
      lista.insertBefore(linha, ref);
    });
    area.addEventListener('dragleave', e => {
      const z = e.target.closest('.dvk-col');
      if (z && !z.contains(e.relatedTarget)) { z.classList.remove('sobre'); limparLinha(); }
    });
    area.addEventListener('drop', e => {
      const z = e.target.closest('.dvk-col'); if (!z) return;
      e.preventDefault(); z.classList.remove('sobre');
      const antes = alvoDrop(z, e.clientY); limparLinha();
      soltar(z.dataset.status, antes);
    });
  }

  async function soltar(novoStatus, antesId) {
    const id = S.dragId; S.dragId = null;
    const t = S.tarefas.find(x => x.id === id); if (!t) return;
    const mudou = t.status !== novoStatus;
    if (!mudou && novoStatus === 'done') return;
    const coluna = colunaOrdenada(novoStatus).filter(x => x.id !== id);     // coluna INTEIRA, inclusive o que o filtro esconde
    let idx = antesId != null ? coluna.findIndex(x => x.id === antesId) : -1;
    if (novoStatus === 'done') idx = 0;
    if (idx < 0) idx = coluna.length;
    const antes = colunaOrdenada(novoStatus).map(x => x.id).join(',');
    coluna.splice(idx, 0, t);
    if (!mudou && coluna.map(x => x.id).join(',') === antes) return;
    coluna.forEach((x, i) => { x.ordem = i; });
    if (mudou) { t.status = novoStatus; t.pausado = false; t.concluido_em = novoStatus === 'done' ? new Date().toISOString() : null; }
    renderTudo();
    try { await api('/api/dev-tarefas/reordenar', { method: 'POST', body: { status: novoStatus, ids: coluna.map(x => x.id) } }); }
    catch (e) { toast('Não consegui salvar a posição — recarregando', 'erro'); }
    await carregar();
  }

  // ── drawer ────────────────────────────────────────────────────
  function novoForm(t) {
    return t ? {
      titulo: t.titulo, descricao: t.descricao || '', status: t.status, prioridade: t.prioridade, prazo: t.prazo || '',
      tags: [...(t.tags || [])], link: t.link || '', atribuido_a: t.atribuido_a || '', pausado: !!t.pausado,
    } : {
      titulo: '', descricao: '', status: 'todo', prioridade: S.filtros.prio || 'media', prazo: '',
      tags: S.filtros.tag ? [S.filtros.tag] : [], link: '', atribuido_a: S.filtros.atrib || '', pausado: false,
    };
  }

  async function abrirDrawer(id, preenchido) {
    const t = id ? S.tarefas.find(x => x.id === id) : null;
    S.drawer = { id: t ? t.id : null, form: Object.assign(novoForm(t), preenchido || {}), dirty: !!preenchido, checklist: [], comentarios: [] };
    renderDrawer();
    $('#dvk-drawer').classList.add('on'); $('#dvk-scrim').classList.add('on');
    $('#dvk-drawer').setAttribute('aria-hidden', 'false');
    setTimeout(() => { const i = $('#dvk-d-titulo'); if (i && !t) i.focus(); }, 120);
    if (t) {
      try {
        const [ck, cm] = await Promise.all([api(`/api/dev-tarefas/${t.id}/checklist`), api(`/api/dev-tarefas/${t.id}/comentarios`)]);
        if (S.drawer && S.drawer.id === t.id) { S.drawer.checklist = ck; S.drawer.comentarios = cm; renderDrawerListas(); }
      } catch (e) { /* listas ficam vazias */ }
    }
  }

  function tentarFecharDrawer() {
    if (S.drawer && S.drawer.dirty && !confirm('Descartar as alterações desta tarefa?')) return;
    fecharDrawer();
  }
  function fecharDrawer() {
    $('#dvk-drawer').classList.remove('on'); $('#dvk-scrim').classList.remove('on');
    $('#dvk-drawer').setAttribute('aria-hidden', 'true');
    S.drawer = null;
  }

  function renderDrawer() {
    const d = S.drawer, f = d.form, t = d.id ? S.tarefas.find(x => x.id === d.id) : null;
    const el = $('#dvk-drawer');
    el.innerHTML = `
      <div class="dvk-dr-head"><span class="t">${t ? 'Tarefa #' + t.id : 'Nova tarefa'}</span>
        <button class="dvk-ico" id="dvk-d-x" title="Fechar (Esc)"><i class="ph ph-x"></i></button></div>
      <div class="dvk-dr-body">
        <textarea class="dvk-titulo" id="dvk-d-titulo" rows="2" placeholder="O que precisa ser feito?" maxlength="200">${esc(f.titulo)}</textarea>
        <div class="dvk-campo"><label>Status</label>
          <div class="dvk-seg" id="dvk-d-status">${STATUS.map(s => `<button data-v="${s.k}" class="${f.status === s.k ? 'on' : ''}">${s.nome}</button>`).join('')}</div>
          ${f.status === 'doing' ? '<div><button class="dvk-btn" id="dvk-d-pausar" type="button"><i class="ph ph-pause"></i>Pausar</button></div>' : ''}
          ${f.status === 'todo' && f.pausado ? '<span class="dvk-chip warn" style="align-self:flex-start"><i class="ph ph-pause"></i>pausada — volta a Em progresso ao iniciar</span>' : ''}
        </div>
        <div class="dvk-campo"><label>Prioridade</label>
          <div class="dvk-seg" id="dvk-d-prio">${Object.keys(PRIO).map(k => `<button data-v="${k}" class="${f.prioridade === k ? 'on' : ''}">${PRIO[k]}</button>`).join('')}</div></div>
        <div class="dvk-grid2">
          <div class="dvk-campo"><label>Responsável</label><select class="dvk-in" id="dvk-d-atrib"><option value="">Ninguém</option>${S.usuarios.map(u => `<option value="${u.id}">${esc(u.nome)}</option>`).join('')}</select></div>
          <div class="dvk-campo"><label>Prazo</label><input type="date" class="dvk-in" id="dvk-d-prazo" value="${esc(f.prazo)}"></div>
        </div>
        <div class="dvk-campo"><label>Projeto e tags <span style="text-transform:none;letter-spacing:0;font-weight:500">(a primeira é o projeto)</span></label>
          <div class="dvk-tags" id="dvk-d-tags"></div><div class="dvk-sug" id="dvk-d-sug"></div></div>
        <div class="dvk-campo"><label>Descrição</label><textarea class="dvk-ta" id="dvk-d-desc" placeholder="Detalhes, contexto, links…">${esc(f.descricao)}</textarea></div>
        <div class="dvk-campo"><label>Link (PR, issue, chamado)</label><input class="dvk-in" id="dvk-d-link" placeholder="https://github.com/…" value="${esc(f.link)}"></div>
        <div id="dvk-d-ext"></div>
        ${d.id ? `<div class="dvk-sec"><span class="dvk-sec-t">Checklist</span><div id="dvk-d-ck"></div>
            <div class="dvk-add"><input class="dvk-in" id="dvk-d-ck-novo" placeholder="Adicionar item e Enter…"></div></div>
          <div class="dvk-sec"><span class="dvk-sec-t">Comentários</span><div id="dvk-d-cm" style="display:flex;flex-direction:column;gap:8px"></div>
            <div class="dvk-add"><input class="dvk-in" id="dvk-d-cm-novo" placeholder="Escrever comentário e Enter…"></div></div>
          <div class="dvk-meta-foot" id="dvk-d-meta"></div>` : ''}
      </div>
      <div class="dvk-dr-foot">
        ${d.id ? '<button class="dvk-btn" id="dvk-d-del" style="color:#BE185D"><i class="ph ph-trash"></i>Excluir</button>' : ''}
        <span style="flex:1"></span><span class="dvk-sub" style="margin-right:6px">Ctrl+Enter salva</span>
        <button class="dvk-btn" id="dvk-d-cancel">Cancelar</button><button class="dvk-btn primario" id="dvk-d-save">Salvar</button>
      </div>`;
    $('#dvk-d-atrib').value = f.atribuido_a || '';
    const marcar = () => { d.dirty = true; };
    $('#dvk-d-titulo').oninput = e => { f.titulo = e.target.value; marcar(); };
    $('#dvk-d-titulo').onkeydown = e => { if (e.key === 'Enter') e.preventDefault(); };
    $('#dvk-d-desc').oninput = e => { f.descricao = e.target.value; marcar(); };
    $('#dvk-d-link').oninput = e => { f.link = e.target.value; marcar(); };
    $('#dvk-d-prazo').onchange = e => { f.prazo = e.target.value; marcar(); };
    $('#dvk-d-atrib').onchange = e => { f.atribuido_a = e.target.value; marcar(); };
    $$('#dvk-d-status button').forEach(b => b.onclick = () => { f.status = b.dataset.v; f.pausado = false; marcar(); renderDrawer(); });
    $$('#dvk-d-prio button').forEach(b => b.onclick = () => { f.prioridade = b.dataset.v; marcar(); renderDrawer(); });
    const p = $('#dvk-d-pausar'); if (p) p.onclick = () => { f.status = 'todo'; f.pausado = true; marcar(); renderDrawer(); };
    $('#dvk-d-x').onclick = tentarFecharDrawer; $('#dvk-d-cancel').onclick = tentarFecharDrawer;
    $('#dvk-d-save').onclick = salvarDrawer;
    const del = $('#dvk-d-del'); if (del) del.onclick = excluirDrawer;
    renderTagsDrawer();
    if (d.id) {
      $('#dvk-d-ck-novo').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); addChecklist(); } };
      $('#dvk-d-cm-novo').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); addComentario(); } };
      const m = []; if (t.criado_por_nome || t.criado_em) m.push(`Criada${t.criado_por_nome ? ' por ' + esc(t.criado_por_nome) : ''}${t.criado_em ? ' em ' + esc(t.criado_em) : ''}`);
      if (t.atualizado_em) m.push('atualizada em ' + esc(t.atualizado_em));
      if (t.concluido_em) m.push('concluída em ' + esc(fmtData(t.concluido_em, hoje())));
      if (t.origem && t.origem !== 'manual') m.push('origem: ' + esc(t.origem));
      $('#dvk-d-meta').innerHTML = m.join(' · ');
      renderDrawerListas();
    }
    ext.secoesDrawer.forEach(fn => { try { fn(t, $('#dvk-d-ext'), d); } catch (e) { console.error(e); } });
  }

  function renderTagsDrawer() {
    const f = S.drawer.form, box = $('#dvk-d-tags');
    box.innerHTML = f.tags.map((tg, i) => `<span class="dvk-tagchip"><i style="background:${corTag(tg)}"></i>${esc(tg)}<button data-rm="${i}" title="Remover">×</button></span>`).join('') +
      '<input id="dvk-d-tag-in" placeholder="adicionar…" maxlength="40">';
    $$('[data-rm]', box).forEach(b => b.onclick = () => { f.tags.splice(+b.dataset.rm, 1); S.drawer.dirty = true; renderTagsDrawer(); });
    const inp = $('#dvk-d-tag-in');
    const add = v => {
      v = v.trim().replace(/,$/, '').trim(); if (!v) return;
      if (!f.tags.some(x => x.toLowerCase() === v.toLowerCase())) {
        const existente = tagsDistintas().find(x => x.toLowerCase() === v.toLowerCase());   // reaproveita a grafia que já existe
        f.tags.push(existente || v); S.drawer.dirty = true;
      }
      renderTagsDrawer(); $('#dvk-d-tag-in').focus();
    };
    inp.onkeydown = e => {
      if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(inp.value); }
      else if (e.key === 'Backspace' && !inp.value && f.tags.length) { f.tags.pop(); S.drawer.dirty = true; renderTagsDrawer(); $('#dvk-d-tag-in').focus(); }
    };
    inp.onblur = () => { if (inp.value.trim()) add(inp.value); };
    const sug = tagsDistintas().filter(x => !f.tags.some(y => y.toLowerCase() === x.toLowerCase())).slice(0, 10);
    $('#dvk-d-sug').innerHTML = sug.map(x => `<button type="button" data-s="${esc(x)}">+ ${esc(x)}</button>`).join('');
    $$('#dvk-d-sug button').forEach(b => b.onclick = () => { f.tags.push(b.dataset.s); S.drawer.dirty = true; renderTagsDrawer(); });
  }

  function renderDrawerListas() {
    const d = S.drawer; if (!d || !d.id) return;
    const ck = $('#dvk-d-ck'), cm = $('#dvk-d-cm'); if (!ck || !cm) return;
    ck.innerHTML = d.checklist.length ? d.checklist.map(i => `<div class="dvk-ck ${i.concluido ? 'on' : ''}"><input type="checkbox" ${i.concluido ? 'checked' : ''} data-ck="${i.id}"><span>${esc(i.texto)}</span><button data-ckdel="${i.id}">×</button></div>`).join('') : '<span class="dvk-sub">Sem itens</span>';
    cm.innerHTML = d.comentarios.length ? d.comentarios.map(c => `<div class="dvk-coment"><b>${esc(c.usuario_nome)}</b><small>${esc(c.criado_em)}</small><p>${esc(c.texto)}</p><button data-cmdel="${c.id}">×</button></div>`).join('') : '<span class="dvk-sub">Sem comentários</span>';
    $$('[data-ck]', ck).forEach(i => i.onchange = () => toggleChecklist(+i.dataset.ck, i.checked));
    $$('[data-ckdel]', ck).forEach(b => b.onclick = () => delChecklist(+b.dataset.ckdel));
    $$('[data-cmdel]', cm).forEach(b => b.onclick = () => delComentario(+b.dataset.cmdel));
  }

  async function recarregarListas() {
    const d = S.drawer; if (!d || !d.id) return;
    const [ck, cm] = await Promise.all([api(`/api/dev-tarefas/${d.id}/checklist`), api(`/api/dev-tarefas/${d.id}/comentarios`)]);
    d.checklist = ck; d.comentarios = cm; renderDrawerListas();
    carregar();   // atualiza os contadores nos cards
  }
  async function addChecklist() { const i = $('#dvk-d-ck-novo'), v = i.value.trim(); if (!v) return; try { await api(`/api/dev-tarefas/${S.drawer.id}/checklist`, { method: 'POST', body: { texto: v } }); i.value = ''; await recarregarListas(); $('#dvk-d-ck-novo').focus(); } catch (e) { toast(e.message, 'erro'); } }
  async function toggleChecklist(iid, ok) { const it = S.drawer.checklist.find(x => x.id === iid); try { await api(`/api/dev-tarefas/checklist/${iid}`, { method: 'PUT', body: { texto: it.texto, concluido: ok } }); await recarregarListas(); } catch (e) { toast(e.message, 'erro'); } }
  async function delChecklist(iid) { try { await api(`/api/dev-tarefas/checklist/${iid}`, { method: 'DELETE' }); await recarregarListas(); } catch (e) { toast(e.message, 'erro'); } }
  async function addComentario() { const i = $('#dvk-d-cm-novo'), v = i.value.trim(); if (!v) return; try { await api(`/api/dev-tarefas/${S.drawer.id}/comentarios`, { method: 'POST', body: { texto: v } }); i.value = ''; await recarregarListas(); $('#dvk-d-cm-novo').focus(); } catch (e) { toast(e.message, 'erro'); } }
  async function delComentario(cid) { try { await api(`/api/dev-tarefas/comentarios/${cid}`, { method: 'DELETE' }); await recarregarListas(); } catch (e) { toast(e.message, 'erro'); } }

  async function salvarDrawer() {
    const d = S.drawer; if (!d) return;
    const f = d.form;
    if (!f.titulo.trim()) { $('#dvk-d-titulo').focus(); toast('Dê um título à tarefa', 'erro'); return; }
    const corpo = {
      titulo: f.titulo.trim(), descricao: f.descricao, status: f.status, prioridade: f.prioridade, prazo: f.prazo || null,
      tags: f.tags, link: f.link.trim(), atribuido_a: f.atribuido_a ? parseInt(f.atribuido_a) : null, pausado: f.status === 'todo' && f.pausado,
    };
    const btn = $('#dvk-d-save'); btn.disabled = true;
    try {
      if (d.id) await api(`/api/dev-tarefas/${d.id}`, { method: 'PUT', body: corpo });
      else await api('/api/dev-tarefas', { method: 'POST', body: corpo });
      d.dirty = false; fecharDrawer(); await carregar();
    } catch (e) { toast('Não consegui salvar: ' + e.message, 'erro'); btn.disabled = false; }
  }
  async function excluirDrawer() {
    const d = S.drawer; if (!d || !d.id || !confirm('Excluir esta tarefa? Isso não pode ser desfeito.')) return;
    try { await api(`/api/dev-tarefas/${d.id}`, { method: 'DELETE' }); d.dirty = false; fecharDrawer(); await carregar(); }
    catch (e) { toast('Não consegui excluir: ' + e.message, 'erro'); }
  }

  // ── paleta (Ctrl/⌘ K) ─────────────────────────────────────────
  const PAL = { itens: [], sel: 0 };
  function acoesFixas() {
    const a = [
      { sec: 'Ações', ic: 'ph-plus', titulo: 'Nova tarefa', dica: 'N', executar: () => abrirDrawer(null) },
      { sec: 'Ações', ic: 'ph-squares-four', titulo: S.visao === 'quadro' ? 'Ver por projeto' : 'Ver como quadro', executar: () => { S.visao = S.visao === 'quadro' ? 'projetos' : 'quadro'; salvarLS(K_VISAO, S.visao); renderTudo(); } },
      { sec: 'Ações', ic: 'ph-warning', titulo: 'Mostrar só atrasadas', executar: () => setFiltro({ atrasadas: true }) },
    ];
    if (filtrosAtivos()) a.push({ sec: 'Ações', ic: 'ph-x-circle', titulo: 'Limpar filtros', executar: limparFiltros });
    tagsDistintas().forEach(tg => a.push({ sec: 'Projetos', ic: 'ph-folder', titulo: 'Filtrar: ' + tg, executar: () => setFiltro({ tag: tg }), chave: tg }));
    return a.concat(ext.acoesPaleta);
  }
  const norm = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  function abrirPaleta() {
    $('#dvk-palette').classList.add('on');
    const i = $('#dvk-pal-in'); i.value = ''; i.focus(); renderPaleta();
  }
  function fecharPaleta() { $('#dvk-palette').classList.remove('on'); }
  function renderPaleta() {
    const q = norm($('#dvk-pal-in').value.trim());
    let itens = [];
    const acoes = acoesFixas().filter(a => !q || norm(a.titulo + ' ' + (a.chave || '')).includes(q));
    const tarefas = q ? S.tarefas.filter(t => norm(t.titulo + ' ' + (t.tags || []).join(' ')).includes(q)).slice(0, 8)
      .map(t => ({ sec: 'Tarefas', ic: 'ph-check-square', titulo: t.titulo, dica: STATUS.find(s => s.k === t.status).nome, executar: () => abrirDrawer(t.id) })) : [];
    const textoBruto = $('#dvk-pal-in').value.trim();
    const filtroTexto = textoBruto ? [{ sec: 'Ações', ic: 'ph-funnel', titulo: `Filtrar o quadro por “${textoBruto}”`, executar: () => setFiltro({ busca: textoBruto }) }] : [];
    itens = itens.concat(filtroTexto, acoes.filter(a => a.sec === 'Ações'), tarefas, acoes.filter(a => a.sec !== 'Ações'));
    if (q.length > 2) ext.fallbacksPaleta.forEach(fn => { const r = fn($('#dvk-pal-in').value.trim()); if (r) itens.push(Object.assign({ sec: 'Perguntar à IA' }, r)); });
    PAL.itens = itens; PAL.sel = Math.min(PAL.sel, Math.max(0, itens.length - 1));
    let sec = null, html = '';
    itens.forEach((it, i) => {
      if (it.sec !== sec) { sec = it.sec; html += `<div class="dvk-pal-sec">${esc(sec)}</div>`; }
      html += `<div class="dvk-pal-it ${i === PAL.sel ? 'sel' : ''}" data-i="${i}"><span class="ic"><i class="ph ${esc(it.ic || 'ph-circle')}"></i></span><span>${esc(it.titulo)}</span>${it.dica ? `<small>${esc(it.dica)}</small>` : ''}</div>`;
    });
    $('#dvk-pal-lista').innerHTML = html || '<div class="dvk-pal-vazio">Nada encontrado</div>';
    $$('#dvk-pal-lista .dvk-pal-it').forEach(n => { n.onclick = () => executarPaleta(+n.dataset.i); n.onmousemove = () => { if (PAL.sel !== +n.dataset.i) { PAL.sel = +n.dataset.i; marcarSel(); } }; });
  }
  function marcarSel() { $$('#dvk-pal-lista .dvk-pal-it').forEach(n => n.classList.toggle('sel', +n.dataset.i === PAL.sel)); const s = $('#dvk-pal-lista .sel'); if (s) s.scrollIntoView({ block: 'nearest' }); }
  function executarPaleta(i) { const it = PAL.itens[i]; if (!it) return; fecharPaleta(); setTimeout(() => it.executar(), 30); }
  function ligarPaleta() {
    const inp = $('#dvk-pal-in');
    inp.oninput = () => { PAL.sel = 0; renderPaleta(); };
    inp.onkeydown = e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); PAL.sel = Math.min(PAL.itens.length - 1, PAL.sel + 1); marcarSel(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); PAL.sel = Math.max(0, PAL.sel - 1); marcarSel(); }
      else if (e.key === 'Enter') { e.preventDefault(); executarPaleta(PAL.sel); }
      else if (e.key === 'Escape') { e.preventDefault(); fecharPaleta(); }
    };
    $('#dvk-palette').onmousedown = e => { if (e.target.id === 'dvk-palette') fecharPaleta(); };
  }

  function planejamentoVisivel() {
    const p = $('#dev-planejamento'), m = $('#mod-areaDev');
    return !!p && !p.classList.contains('hidden') && !!m && !m.classList.contains('hidden');
  }
  function teclasGlobais(e) {
    if (!S.montado || !planejamentoVisivel()) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#dvk-palette').classList.contains('on') ? fecharPaleta() : abrirPaleta(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && S.drawer) { e.preventDefault(); salvarDrawer(); return; }
    if (e.key === 'Escape') {
      if ($('#dvk-palette').classList.contains('on')) { fecharPaleta(); return; }
      const modal = $('.dvk-modal.on'); if (modal) { modal.classList.remove('on'); return; }
      if (S.drawer) tentarFecharDrawer();
      return;
    }
    const digitando = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || '')) || e.target.isContentEditable;
    if (!digitando && !S.drawer && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === 'n') { e.preventDefault(); abrirDrawer(null); }
  }

  // ── API pública / extensões ───────────────────────────────────
  window.DVK = {
    ativar() { montar(); carregar(); },
    recarregar: carregar,
    novaTarefa: () => abrirDrawer(null),
    abrirTarefa: id => abrirDrawer(id),
    abrirNova: preenchido => abrirDrawer(null, preenchido),
    estado: () => S,
    api, toast, esc, hoje, corTag, fmtData, diasAte, STATUS, PRIO,
    fecharPaleta, abrirPaleta, setFiltro,
    registrarBotaoTopo(b) { ext.botoes.push(b); renderBotoesTopo(); },
    registrarAcaoPaleta(a) { ext.acoesPaleta.push(a); },
    registrarFallbackPaleta(fn) { ext.fallbacksPaleta.push(fn); },
    registrarSecaoDrawer(fn) { ext.secoesDrawer.push(fn); },
    registrarFocoExtra(fn) { ext.focoExtra.push(fn); },
    registrarBadgeCard(fn) { ext.badgeCard = fn; },
    registrarAposCarregar(fn) { ext.aposCarregar.push(fn); },
    rerender: renderTudo,
  };
})();
