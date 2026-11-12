/* Kanban da Área de Dev — Conexões (GitHub, webhooks, Outlook/calendário).
 * Pendura-se no núcleo por DVK.registrar*(). Segredos e tokens de webhook só aparecem
 * aqui pra perfil 'dev' (o backend barra o resto). */
(function () {
  'use strict';
  const { api, toast, esc } = window.DVK;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  let cfg = null;                                   // /api/dev-integracoes/status
  const gh = {};                                    // url -> { s: status, t: timestamp }
  const GH_TTL_MS = 5 * 60 * 1000;
  let ghBuscando = false;

  async function carregarCfg() {
    try { cfg = await api('/api/dev-integracoes/status'); } catch (e) { cfg = null; }
    return cfg;
  }

  // ── GitHub: selo no card + seção no drawer ───────────────────
  const ehGithub = url => /^https?:\/\/(www\.)?github\.com\/[\w.-]+\/[\w.-]+\/(pull|issues)\/\d+/i.test(url || '');

  function seloGh(s) {
    if (!s || s.erro) return '';
    const cls = { aberto: 'ok', mergeado: 'merged', fechado: 'late', rascunho: '' }[s.estado] || '';
    const chk = s.checks === 'falhando' ? ' ✕' : s.checks === 'ok' ? ' ✓' : s.checks === 'pendente' ? ' …' : '';
    const tit = `${s.repo} #${s.numero} — ${s.estado}${s.checks ? ' · checks ' + s.checks : ''}`;
    return `<span class="dvk-chip ${cls}" title="${esc(tit)}"><i class="ph ph-${s.tipo === 'pr' ? 'git-pull-request' : 'circle-dashed'}"></i>#${s.numero} ${esc(s.estado)}${chk}</span>`;
  }

  async function buscarGh(urls) {
    if (!urls.length || ghBuscando) return;
    ghBuscando = true;
    try {
      const r = await api('/api/dev-integracoes/github/status', { method: 'POST', body: { urls } });
      const agora = Date.now();
      Object.keys(r).forEach(u => { gh[u] = { s: r[u], t: agora }; });
      window.DVK.rerender();
    } catch (e) { urls.forEach(u => { gh[u] = { s: { erro: 'indisponivel' }, t: Date.now() }; }); }
    ghBuscando = false;
  }

  window.DVK.registrarBadgeCard(t => ehGithub(t.link) && gh[t.link] ? seloGh(gh[t.link].s) : '');
  window.DVK.registrarAposCarregar(tarefas => {
    const agora = Date.now();
    const urls = [...new Set(tarefas.filter(t => t.status !== 'done' && ehGithub(t.link) && (!gh[t.link] || agora - gh[t.link].t > GH_TTL_MS)).map(t => t.link))];
    buscarGh(urls.slice(0, 30));
  });

  async function secaoDrawer(t, el) {
    if (!el) return;
    el.innerHTML = '';
    if (!t) return;
    // GitHub
    if (ehGithub(t.link)) {
      const box = document.createElement('div'); box.className = 'dvk-campo'; box.innerHTML = '<label>GitHub</label><div class="dvk-gh"><span class="dvk-mini">Consultando…</span></div>';
      el.appendChild(box);
      try {
        const r = await api('/api/dev-integracoes/github/status', { method: 'POST', body: { urls: [t.link] } });
        const s = r[t.link] || r[Object.keys(r)[0]];
        $('.dvk-gh', box).innerHTML = !s || s.erro
          ? `<span class="dvk-mini">${s && s.erro === 'sem_acesso' ? esc(s.dica || 'Sem acesso a este repositório.') : 'Não consegui consultar o GitHub agora.'}</span>`
          : `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${seloGh(s)}<b style="font-size:13px">${esc(s.titulo)}</b></div>
             <span class="dvk-mini">${esc(s.repo)} · por ${esc(s.autor || '—')} · atualizado ${esc(s.atualizado || '—')}${s.checks ? ' · checks: ' + esc(s.checks) : ''}</span>
             <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Abrir no GitHub ↗</a>`;
      } catch (e) { $('.dvk-gh', box).innerHTML = '<span class="dvk-mini">Não consegui consultar o GitHub agora.</span>'; }
    }
    // Calendário
    if (!cfg) await carregarCfg();
    if (cfg && cfg.graph.configurado && t.prazo) {
      const box = document.createElement('div'); box.className = 'dvk-campo'; box.innerHTML = '<label>Calendário (Outlook)</label><div id="dvk-cal-box"></div>';
      el.appendChild(box);
      const desenhar = () => {
        const c = $('#dvk-cal-box', box);
        c.innerHTML = t.evento_id
          ? `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="dvk-chip ok"><i class="ph ph-calendar-check"></i>Prazo no seu calendário</span><button class="dvk-btn" id="dvk-cal-rm">Remover</button></div><span class="dvk-mini">O evento acompanha mudanças de prazo e some ao concluir.</span>`
          : `<button class="dvk-btn" id="dvk-cal-add"><i class="ph ph-calendar-plus"></i>Adicionar prazo ao calendário</button>`;
        const add = $('#dvk-cal-add', c), rm = $('#dvk-cal-rm', c);
        if (add) add.onclick = async () => { add.disabled = true; try { const r = await api('/api/dev-integracoes/calendario/' + t.id, { method: 'POST' }); t.evento_id = r.evento_id; toast('Evento criado no calendário de ' + r.mailbox); desenhar(); window.DVK.recarregar(); } catch (e) { toast(e.message, 'erro'); add.disabled = false; } };
        if (rm) rm.onclick = async () => { try { await api('/api/dev-integracoes/calendario/' + t.id, { method: 'DELETE' }); t.evento_id = null; toast('Evento removido'); desenhar(); window.DVK.recarregar(); } catch (e) { toast(e.message, 'erro'); } };
      };
      desenhar();
    }
  }
  window.DVK.registrarSecaoDrawer((t, el) => { secaoDrawer(t, el); });

  // ── painel Conexões ───────────────────────────────────────────
  const copiar = async texto => { try { await navigator.clipboard.writeText(texto); toast('Copiado'); } catch (e) { toast('Selecione e copie (Ctrl+C)'); } };
  const origem = () => location.origin;
  const EVENTOS_ROTULO = { 'tarefa.criada': 'criada', 'tarefa.atualizada': 'atualizada', 'tarefa.movida': 'movida', 'tarefa.concluida': 'concluída', 'tarefa.excluida': 'excluída' };

  function abrirConexoes(aba) {
    let m = $('#dvk-m-conexoes');
    if (!m) { m = document.createElement('div'); m.id = 'dvk-m-conexoes'; m.className = 'dvk-modal'; document.body.appendChild(m); }
    m.innerHTML = `<div class="dvk-modal-box" style="width:min(820px,100%)" role="dialog" aria-label="Conexões">
      <div class="dvk-modal-head"><h3>Conexões</h3><button class="dvk-ico" data-fechar title="Fechar"><i class="ph ph-x"></i></button></div>
      <div class="dvk-abas" id="dvk-cx-abas">${[['geral', 'Visão geral'], ['entrada', 'Entrada'], ['saida', 'Saída'], ['outlook', 'Outlook']].map(([k, n]) => `<button data-a="${k}">${n}</button>`).join('')}</div>
      <div class="dvk-modal-body" id="dvk-cx-corpo"></div></div>`;
    m.onmousedown = e => { if (e.target === m) m.classList.remove('on'); };
    $('[data-fechar]', m).onclick = () => m.classList.remove('on');
    $$('#dvk-cx-abas button', m).forEach(b => b.onclick = () => trocarAba(b.dataset.a));
    m.classList.add('on');
    trocarAba(aba || 'geral');
  }

  async function trocarAba(a) {
    $$('#dvk-cx-abas button').forEach(b => b.classList.toggle('on', b.dataset.a === a));
    const corpo = $('#dvk-cx-corpo'); corpo.innerHTML = '<span class="dvk-mini">Carregando…</span>';
    try { await ({ geral: abaGeral, entrada: abaEntrada, saida: abaSaida, outlook: abaOutlook }[a])(corpo); }
    catch (e) { corpo.innerHTML = `<span class="dvk-mini" style="color:#BE185D">${esc(e.message)}</span>`; }
  }

  async function abaGeral(corpo) {
    await carregarCfg();
    const c = cfg || { ia: {}, github: {}, graph: {} };
    const cartao = (nome, ok, ok_txt, off_txt) => `<div class="dvk-cartao"><b><span class="pt ${ok ? 'on' : ''}"></span>${nome}</b><small>${ok ? ok_txt : off_txt}</small></div>`;
    corpo.innerHTML = `<div class="dvk-cartoes">
        ${cartao('IA', c.ia.configurado, `Ativa · modelo ${esc(c.ia.modelo || '')}`, 'Sem chave. Defina <code>LLM_API_KEY</code> (ou <code>GROQ_API_KEY</code>). Briefing e standup seguem por regras.')}
        ${cartao('GitHub', c.github.token, 'Token configurado — repositórios privados liberados.', 'Sem token: só repositórios públicos. Defina <code>GITHUB_TOKEN</code> (somente leitura).')}
        ${cartao('Microsoft Graph', c.graph.configurado, 'Calendário e e-mails do Outlook ativos.', 'Defina <code>GRAPH_TENANT_ID</code>, <code>GRAPH_CLIENT_ID</code> e <code>GRAPH_CLIENT_SECRET</code> (ou as do Loop).')}
      </div>
      <div class="dvk-linha-hook"><b style="font-size:13.5px">Como as peças se encaixam</b>
        <span class="dvk-mini"><b>Entrada</b> — n8n, TiFlux, Faiston Hub e GitHub mandam eventos para o quadro (criam, movem e comentam tarefas).<br>
        <b>Saída</b> — o quadro avisa o n8n (e daí Teams, e-mail, planilha…) quando uma tarefa é criada, movida ou concluída.<br>
        <b>Outlook</b> — e-mail sinalizado vira tarefa com IA, e o prazo da tarefa vira evento no calendário.<br>
        <b>Teams</b> — use um fluxo do Power Automate/n8n (“quando chegar mensagem…” → HTTP POST) para uma URL de <b>Entrada</b>.<br>
        <b>TiFlux / Hub</b> — ligue pelo n8n a uma URL de <b>Entrada</b> (use <code>origem_ref</code> = número do chamado/tarefa para não duplicar).</span></div>`;
  }

  // ---- Entrada ----
  const PRESETS = [
    { rotulo: 'n8n', tipo: 'generico', nome: 'n8n', origem: 'n8n', tag: '', status: 'todo' },
    { rotulo: 'TiFlux (via n8n)', tipo: 'generico', nome: 'TiFlux', origem: 'tiflux', tag: 'Suporte', status: 'backlog' },
    { rotulo: 'Faiston Hub (via n8n)', tipo: 'generico', nome: 'Faiston Hub', origem: 'hub', tag: 'Hub', status: 'backlog' },
    { rotulo: 'Teams (via Power Automate)', tipo: 'generico', nome: 'Teams', origem: 'teams', tag: '', status: 'todo' },
    { rotulo: 'GitHub', tipo: 'github', nome: 'GitHub', origem: 'github', tag: '', status: 'todo' },
  ];

  async function abaEntrada(corpo) {
    const hooks = await api('/api/dev-integracoes/hooks-entrada');
    corpo.innerHTML = `
      <p class="dvk-aviso" style="font-size:12.5px">Cada webhook tem uma URL secreta própria — quem tem a URL pode criar tarefas, então trate como senha. Sem cookie nem login: a autenticação é o token na URL (e a assinatura, no caso do GitHub).</p>
      ${hooks.length ? hooks.map(h => hookEntradaHtml(h)).join('') : '<span class="dvk-mini">Nenhum webhook de entrada ainda.</span>'}
      <div class="dvk-linha-hook"><b style="font-size:13.5px">Novo webhook de entrada</b>
        <div class="dvk-presets">${PRESETS.map((p, i) => `<button class="dvk-btn" data-preset="${i}">${esc(p.rotulo)}</button>`).join('')}</div>
        <div class="dvk-grid2"><input class="dvk-in" id="dvk-he-nome" placeholder="Nome (ex.: TiFlux)"><select class="dvk-in" id="dvk-he-tipo"><option value="generico">Genérico (JSON)</option><option value="github">GitHub (assinado)</option></select></div>
        <div class="dvk-grid2"><input class="dvk-in" id="dvk-he-origem" placeholder="Origem (ex.: tiflux)" value="n8n"><input class="dvk-in" id="dvk-he-tag" placeholder="Tag/projeto padrão (opcional)"></div>
        <div class="dvk-grid2"><select class="dvk-in" id="dvk-he-status">${window.DVK.STATUS.map(s => `<option value="${s.k}" ${s.k === 'todo' ? 'selected' : ''}>Entra em: ${s.nome}</option>`).join('')}</select><span></span></div>
        <div><button class="dvk-btn primario" id="dvk-he-criar"><i class="ph ph-plus"></i>Criar webhook</button></div></div>`;
    $$('[data-preset]', corpo).forEach(b => b.onclick = () => {
      const p = PRESETS[+b.dataset.preset];
      $('#dvk-he-nome', corpo).value = p.nome; $('#dvk-he-tipo', corpo).value = p.tipo; $('#dvk-he-origem', corpo).value = p.origem;
      $('#dvk-he-tag', corpo).value = p.tag; $('#dvk-he-status', corpo).value = p.status;
    });
    $('#dvk-he-criar', corpo).onclick = async () => {
      const nome = $('#dvk-he-nome', corpo).value.trim(); if (!nome) { toast('Dê um nome ao webhook', 'erro'); return; }
      try {
        await api('/api/dev-integracoes/hooks-entrada', { method: 'POST', body: { nome, tipo: $('#dvk-he-tipo', corpo).value, origem: $('#dvk-he-origem', corpo).value, tag_padrao: $('#dvk-he-tag', corpo).value, status_padrao: $('#dvk-he-status', corpo).value } });
        toast('Webhook criado'); trocarAba('entrada');
      } catch (e) { toast(e.message, 'erro'); }
    };
    $$('[data-copiar]', corpo).forEach(b => b.onclick = () => copiar(b.dataset.copiar));
    $$('[data-he-ativo]', corpo).forEach(b => b.onclick = async () => { try { await api('/api/dev-integracoes/hooks-entrada/' + b.dataset.heAtivo, { method: 'PUT', body: { ativo: b.dataset.v === '1' } }); trocarAba('entrada'); } catch (e) { toast(e.message, 'erro'); } });
    $$('[data-he-del]', corpo).forEach(b => b.onclick = async () => { if (!confirm('Excluir este webhook? Quem usa a URL vai começar a receber erro 404.')) return; try { await api('/api/dev-integracoes/hooks-entrada/' + b.dataset.heDel, { method: 'DELETE' }); trocarAba('entrada'); } catch (e) { toast(e.message, 'erro'); } });
  }

  function hookEntradaHtml(h) {
    const url = origem() + h.caminho;
    const exemplo = h.tipo === 'github'
      ? `No GitHub: Settings → Webhooks → Add webhook\nPayload URL: ${url}\nContent type: application/json\nSecret: ${h.segredo}\nEventos: Pull requests e Issues\n\nRegras: PR mergeado → Concluído · PR aberto → Em progresso · issue fechada → Concluído. A tarefa precisa ter o link do PR/issue no campo Link.`
      : `curl -X POST '${url}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{"titulo":"Chamado #123: VPN caindo","origem_ref":"TKT-123","prioridade":"alta","link":"https://…"}'\n\nAções: "upsert" (padrão; mesmo origem_ref atualiza), "mover" + "status", "concluir", "comentar" + "comentario".\nCampos: titulo, descricao, prioridade (urgente|alta|media|baixa), prazo (AAAA-MM-DD), tags[], status, link, checklist[].`;
    return `<div class="dvk-linha-hook ${h.ativo ? '' : 'off'}">
      <div class="topo"><b>${esc(h.nome)}</b><span class="dvk-chip">${h.tipo === 'github' ? 'GitHub' : 'JSON'}</span><span class="dvk-chip">origem: ${esc(h.origem)}</span>${h.tag_padrao ? `<span class="dvk-chip">tag: ${esc(h.tag_padrao)}</span>` : ''}<span class="dvk-chip">entra em: ${esc((window.DVK.STATUS.find(s => s.k === h.status_padrao) || {}).nome || h.status_padrao)}</span>
        <span style="flex:1"></span><button class="dvk-btn" data-he-ativo="${h.id}" data-v="${h.ativo ? 0 : 1}">${h.ativo ? 'Desativar' : 'Ativar'}</button><button class="dvk-btn" style="color:#BE185D" data-he-del="${h.id}">Excluir</button></div>
      <div style="display:flex;gap:6px;align-items:center"><div class="dvk-codigo" style="flex:1">${esc(url)}</div><button class="dvk-btn" data-copiar="${esc(url)}"><i class="ph ph-copy"></i>URL</button>${h.tipo === 'github' ? `<button class="dvk-btn" data-copiar="${esc(h.segredo)}"><i class="ph ph-key"></i>Secret</button>` : ''}</div>
      <details><summary class="dvk-mini" style="cursor:pointer">Como usar</summary><div class="dvk-codigo" style="white-space:pre-wrap;margin-top:6px;user-select:text">${esc(exemplo)}</div></details>
      <span class="dvk-mini">${h.ultimo_uso ? `Último uso: ${esc(h.ultimo_uso)} — ${esc(h.ultimo_resultado || '')}` : 'Nunca usado'}</span></div>`;
  }

  // ---- Saída ----
  async function abaSaida(corpo) {
    const d = await api('/api/dev-integracoes/hooks-saida');
    corpo.innerHTML = `
      <p class="dvk-aviso" style="font-size:12.5px">O quadro envia um POST JSON para a URL quando algo acontece. Cada envio vem assinado: o cabeçalho <code>X-Faiston-Signature</code> é <code>sha256=</code> + HMAC-SHA256 do corpo com o secret do webhook. Só https e só endereços públicos. Tarefas criadas por webhooks de entrada genéricos não disparam saída (evita laço).</p>
      ${d.hooks.length ? d.hooks.map(h => `<div class="dvk-linha-hook ${h.ativo ? '' : 'off'}">
        <div class="topo"><b>${esc(h.nome)}</b>${h.ultimo_em ? (h.ultimo_status && h.ultimo_status < 400 ? `<span class="dvk-chip ok">✓ ${h.ultimo_status} · ${esc(h.ultimo_em)}</span>` : `<span class="dvk-chip late">✕ ${esc(h.ultimo_erro || 'falhou')} · ${esc(h.ultimo_em)}</span>`) : '<span class="dvk-chip">nunca enviado</span>'}
          <span style="flex:1"></span><button class="dvk-btn" data-hs-testar="${h.id}">Testar</button><button class="dvk-btn" data-hs-ativo="${h.id}" data-v="${h.ativo ? 0 : 1}">${h.ativo ? 'Desativar' : 'Ativar'}</button><button class="dvk-btn" style="color:#BE185D" data-hs-del="${h.id}">Excluir</button></div>
        <div class="dvk-codigo">${esc(h.url)}</div>
        <div class="dvk-chips">${(h.eventos.length ? h.eventos : Object.keys(EVENTOS_ROTULO)).map(e => `<span class="dvk-chip">${esc(EVENTOS_ROTULO[e] || e)}</span>`).join('')}</div>
        <div style="display:flex;gap:6px;align-items:center"><span class="dvk-mini">Secret:</span><div class="dvk-codigo" style="flex:1">${esc(h.segredo)}</div><button class="dvk-btn" data-copiar="${esc(h.segredo)}"><i class="ph ph-copy"></i></button></div></div>`).join('') : '<span class="dvk-mini">Nenhum webhook de saída ainda.</span>'}
      <div class="dvk-linha-hook"><b style="font-size:13.5px">Novo webhook de saída</b>
        <div class="dvk-grid2"><input class="dvk-in" id="dvk-hs-nome" placeholder="Nome (ex.: n8n → Teams)"><input class="dvk-in" id="dvk-hs-url" placeholder="https://n8n.suaempresa.com/webhook/…"></div>
        <div class="dvk-chips">${Object.keys(EVENTOS_ROTULO).map(e => `<label class="dvk-toggle"><input type="checkbox" data-ev="${e}"> ${EVENTOS_ROTULO[e]}</label>`).join('&nbsp;&nbsp;')}</div>
        <span class="dvk-mini">Sem nenhum evento marcado, o webhook recebe todos.</span>
        <div><button class="dvk-btn primario" id="dvk-hs-criar"><i class="ph ph-plus"></i>Criar webhook</button></div></div>`;
    $$('[data-copiar]', corpo).forEach(b => b.onclick = () => copiar(b.dataset.copiar));
    $('#dvk-hs-criar', corpo).onclick = async () => {
      const nome = $('#dvk-hs-nome', corpo).value.trim(), url = $('#dvk-hs-url', corpo).value.trim();
      if (!nome || !url) { toast('Informe nome e URL', 'erro'); return; }
      try { await api('/api/dev-integracoes/hooks-saida', { method: 'POST', body: { nome, url, eventos: $$('[data-ev]:checked', corpo).map(c => c.dataset.ev) } }); toast('Webhook criado'); trocarAba('saida'); }
      catch (e) { toast(e.message, 'erro'); }
    };
    $$('[data-hs-testar]', corpo).forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await api(`/api/dev-integracoes/hooks-saida/${b.dataset.hsTestar}/testar`, { method: 'POST' }); toast(r.erro ? 'Falhou: ' + r.erro : 'Teste enviado — resposta ' + r.status, r.erro ? 'erro' : ''); trocarAba('saida'); } catch (e) { toast(e.message, 'erro'); b.disabled = false; } });
    $$('[data-hs-ativo]', corpo).forEach(b => b.onclick = async () => { try { await api('/api/dev-integracoes/hooks-saida/' + b.dataset.hsAtivo, { method: 'PUT', body: { ativo: b.dataset.v === '1' } }); trocarAba('saida'); } catch (e) { toast(e.message, 'erro'); } });
    $$('[data-hs-del]', corpo).forEach(b => b.onclick = async () => { if (!confirm('Excluir este webhook de saída?')) return; try { await api('/api/dev-integracoes/hooks-saida/' + b.dataset.hsDel, { method: 'DELETE' }); trocarAba('saida'); } catch (e) { toast(e.message, 'erro'); } });
  }

  // ---- Outlook ----
  async function abaOutlook(corpo) {
    await carregarCfg();
    if (!cfg || !cfg.graph.configurado) {
      corpo.innerHTML = '<p class="dvk-aviso" style="font-size:13px">O Microsoft Graph não está configurado neste ambiente. Defina <code>GRAPH_TENANT_ID</code>, <code>GRAPH_CLIENT_ID</code> e <code>GRAPH_CLIENT_SECRET</code> (permissões de aplicativo <b>Mail.Read</b> e <b>Calendars.ReadWrite</b>, com consentimento do administrador). Recomenda-se restringir as caixas acessíveis por uma Application Access Policy.</p>';
      return;
    }
    const emails = await api('/api/dev-integracoes/outlook/emails');
    corpo.innerHTML = `<p class="dvk-aviso" style="font-size:12.5px">E-mails que você sinalizou (bandeirinha) no Outlook. “Criar tarefa” lê o e-mail com a IA e sugere as tarefas pra você revisar — e não duplica se você repetir.</p>` +
      (emails.length ? emails.map(e => `<div class="dvk-linha-hook"><div class="topo"><b>${esc(e.assunto)}</b><span class="dvk-mini">${esc(e.de)} · ${esc(e.recebido)}</span><span style="flex:1"></span><button class="dvk-btn ia" data-mail="${esc(e.id)}"><i class="ph ph-sparkle"></i>Criar tarefa</button></div><span class="dvk-mini">${esc(e.previa)}</span></div>`).join('')
        : '<span class="dvk-mini">Nenhum e-mail sinalizado agora.</span>');
    $$('[data-mail]', corpo).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        const e = await api('/api/dev-integracoes/outlook/email?id=' + encodeURIComponent(b.dataset.mail));
        $('#dvk-m-conexoes').classList.remove('on');
        window.DVKIA.abrirCaptura({ texto: e.texto, autoGerar: true, origem: 'email', origem_ref: b.dataset.mail, link: e.link });
      } catch (err) { toast(err.message, 'erro'); b.disabled = false; }
    });
  }

  window.DVK.registrarBotaoTopo({ id: 'conexoes', titulo: 'Conexões', icone: 'ph-plugs-connected', dica: 'GitHub, webhooks, Outlook', onclick: () => abrirConexoes('geral') });
  window.DVK.registrarAcaoPaleta({ sec: 'Conexões', ic: 'ph-plugs-connected', titulo: 'Abrir conexões', executar: () => abrirConexoes('geral') });
  window.DVK.registrarAcaoPaleta({ sec: 'Conexões', ic: 'ph-envelope', titulo: 'E-mails sinalizados do Outlook', executar: () => abrirConexoes('outlook') });
  window.DVKCX = { abrirConexoes };
})();
