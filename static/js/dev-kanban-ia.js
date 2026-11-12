/* Kanban da Área de Dev — IA (captura, briefing, standup, comando).
 * Pendura-se no núcleo (dev-kanban.js) por DVK.registrar*(). A IA só PROPÕE:
 * rascunhos e ações passam por revisão/confirmação aqui e são revalidados
 * no servidor (/api/dev-ia/*). */
(function () {
  'use strict';
  const { api, toast, esc } = window.DVK;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const estado = { briefing: null, carregandoBriefing: false, escopo: 'minhas' };

  // ── modal genérico ────────────────────────────────────────────
  function modal(id, titulo, corpo, rodape) {
    let m = $('#' + id);
    if (!m) { m = document.createElement('div'); m.id = id; m.className = 'dvk-modal'; document.body.appendChild(m); }
    m.innerHTML = `<div class="dvk-modal-box" role="dialog" aria-label="${esc(titulo)}">
      <div class="dvk-modal-head"><h3>${esc(titulo)}</h3><button class="dvk-ico" data-fechar title="Fechar"><i class="ph ph-x"></i></button></div>
      <div class="dvk-modal-body">${corpo}</div><div class="dvk-modal-foot">${rodape || ''}</div></div>`;
    m.onmousedown = e => { if (e.target === m) m.classList.remove('on'); };
    $('[data-fechar]', m).onclick = () => m.classList.remove('on');
    m.classList.add('on');
    return m;
  }
  const fechar = id => { const m = $('#' + id); if (m) m.classList.remove('on'); };
  const AVISO_LGPD = '<p class="dvk-aviso">O texto é enviado ao provedor de IA configurado. Não cole dados pessoais sensíveis (CPF, senhas, tokens).</p>';

  // ── captura inteligente ───────────────────────────────────────
  function abrirCaptura(opc) {
    opc = opc || {};
    const m = modal('dvk-m-captura', 'Capturar tarefas com IA',
      `<p class="dvk-aviso" style="font-size:13px">Cole um e-mail, ata, mensagem… A IA sugere tarefas (título, prioridade, prazo, projeto e checklist) e você revisa antes de criar.</p>
       <textarea class="dvk-ta" id="dvk-cap-texto" rows="8" placeholder="Cole aqui o texto…">${esc(opc.texto || '')}</textarea>${AVISO_LGPD}
       <div id="dvk-cap-resultado"></div>`,
      '<button class="dvk-btn" data-fechar2>Cancelar</button><button class="dvk-btn primario" id="dvk-cap-gerar"><i class="ph ph-sparkle"></i>Gerar rascunhos</button>');
    $('[data-fechar2]', m).onclick = () => fechar('dvk-m-captura');
    const gerar = $('#dvk-cap-gerar', m);
    gerar.onclick = async () => {
      const texto = $('#dvk-cap-texto', m).value.trim();
      if (texto.length < 8) { toast('Cole um texto um pouco maior', 'erro'); return; }
      gerar.disabled = true; gerar.innerHTML = '<i class="ph ph-circle-notch"></i>Lendo…';
      try {
        const { rascunhos } = await api('/api/dev-ia/capturar', { method: 'POST', body: { texto } });
        renderRascunhos(m, rascunhos, opc);
      } catch (e) { toast(e.message, 'erro'); }
      gerar.disabled = false; gerar.innerHTML = '<i class="ph ph-sparkle"></i>Gerar de novo';
    };
    if (opc.texto && opc.autoGerar) gerar.click();
  }

  function renderRascunhos(m, rascunhos, opc) {
    const box = $('#dvk-cap-resultado', m);
    if (!rascunhos.length) { box.innerHTML = '<p class="dvk-aviso" style="font-size:13px">Não encontrei nada acionável nesse texto.</p>'; return; }
    box.innerHTML = rascunhos.map((r, i) => `
      <div class="dvk-rascunho" data-i="${i}">
        <label class="dvk-toggle"><input type="checkbox" data-f="on" checked> Criar esta tarefa</label>
        <input class="dvk-in" data-f="titulo" value="${esc(r.titulo)}" maxlength="200">
        <div class="dvk-grid2">
          <select class="dvk-in" data-f="prioridade">${['urgente', 'alta', 'media', 'baixa'].map(p => `<option value="${p}" ${r.prioridade === p ? 'selected' : ''}>${window.DVK.PRIO[p]}</option>`).join('')}</select>
          <input class="dvk-in" data-f="prazo" type="date" value="${esc(r.prazo || '')}">
        </div>
        <input class="dvk-in" data-f="tags" value="${esc(r.tags.join(', '))}" placeholder="Projeto, tag, tag">
        <textarea class="dvk-ta" data-f="descricao" rows="2" placeholder="Descrição">${esc(r.descricao)}</textarea>
        <textarea class="dvk-ta" data-f="checklist" rows="${Math.min(5, Math.max(2, r.checklist.length))}" placeholder="Checklist — um item por linha">${esc(r.checklist.join('\n'))}</textarea>
      </div>`).join('') + '<div style="text-align:right"><button class="dvk-btn primario" id="dvk-cap-criar"><i class="ph ph-check"></i>Criar tarefas selecionadas</button></div>';
    $$('.dvk-rascunho [data-f="on"]', box).forEach(c => c.onchange = () => c.closest('.dvk-rascunho').classList.toggle('off', !c.checked));
    $('#dvk-cap-criar', box).onclick = async () => {
      const tarefas = $$('.dvk-rascunho', box).filter(n => $('[data-f="on"]', n).checked).map(n => {
        const v = f => $(`[data-f="${f}"]`, n).value;
        return {
          titulo: v('titulo'), prioridade: v('prioridade'), prazo: v('prazo') || null, descricao: v('descricao'),
          tags: v('tags').split(',').map(s => s.trim()).filter(Boolean), checklist: v('checklist').split('\n').map(s => s.trim()).filter(Boolean),
          origem_ref: opc.origem_ref || undefined, link: opc.link || '',
        };
      }).filter(t => t.titulo.trim());
      if (!tarefas.length) { toast('Selecione pelo menos uma tarefa', 'erro'); return; }
      try {
        const r = await api('/api/dev-ia/capturar/confirmar', { method: 'POST', body: { tarefas, origem: opc.origem || 'ia' } });
        fechar('dvk-m-captura');
        toast(r.criadas.length ? `${r.criadas.length} tarefa${r.criadas.length > 1 ? 's' : ''} criada${r.criadas.length > 1 ? 's' : ''}` + (r.duplicadas ? ` (${r.duplicadas} já existia)` : '') : 'Essa tarefa já existia no quadro');
        window.DVK.recarregar();
      } catch (e) { toast(e.message, 'erro'); }
    };
  }

  // ── briefing (dentro do Foco de hoje) ─────────────────────────
  async function pedirBriefing() {
    if (estado.carregandoBriefing) return;
    estado.carregandoBriefing = true; window.DVK.rerender();
    try { estado.briefing = await api('/api/dev-ia/briefing?escopo=' + estado.escopo); }
    catch (e) { toast(e.message, 'erro'); }
    estado.carregandoBriefing = false; window.DVK.rerender();
  }

  function focoExtra(el) {
    if (!el) return;
    const b = estado.briefing;
    el.innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap">
        <button class="dvk-btn ia" id="dvk-brief-btn" ${estado.carregandoBriefing ? 'disabled' : ''}><i class="ph ph-sparkle"></i>${estado.carregandoBriefing ? 'Pensando…' : (b ? 'Atualizar briefing' : 'Briefing do dia')}</button>
        <select class="dvk-sel" id="dvk-brief-escopo" title="Escopo do briefing"><option value="minhas" ${estado.escopo === 'minhas' ? 'selected' : ''}>Só o que é meu / sem dono</option><option value="todas" ${estado.escopo === 'todas' ? 'selected' : ''}>Time todo</option></select>
      </div>
      ${b ? `<div class="dvk-briefing">${esc(b.texto)}<small>${b.gerado_com === 'ia' ? 'Gerado por IA a partir do ranking do quadro' : 'Gerado pelas regras do quadro'}${b.aviso ? ' — ' + esc(b.aviso) : ''}</small></div>` : ''}`;
    $('#dvk-brief-btn', el).onclick = pedirBriefing;
    $('#dvk-brief-escopo', el).onchange = e => { estado.escopo = e.target.value; };
  }

  // ── standup ───────────────────────────────────────────────────
  function abrirStandup(periodo) {
    const m = modal('dvk-m-standup', 'Standup',
      `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
         <div class="dvk-seg" id="dvk-st-per"><button data-v="ontem" class="${periodo !== 'semana' ? 'on' : ''}">Desde ontem</button><button data-v="semana" class="${periodo === 'semana' ? 'on' : ''}">Semana</button></div>
         <select class="dvk-sel" id="dvk-st-esc"><option value="minhas">Só o que é meu / sem dono</option><option value="todas">Time todo</option></select>
       </div>
       <textarea class="dvk-ta" id="dvk-st-texto" rows="14" placeholder="Gerando…"></textarea>
       <p class="dvk-aviso" id="dvk-st-info"></p>`,
      '<button class="dvk-btn" id="dvk-st-copiar"><i class="ph ph-copy"></i>Copiar</button><button class="dvk-btn" id="dvk-st-diario"><i class="ph ph-notebook"></i>Salvar no Diário</button><button class="dvk-btn primario" id="dvk-st-gerar"><i class="ph ph-sparkle"></i>Gerar de novo</button>');
    let per = periodo === 'semana' ? 'semana' : 'ontem';
    $$('#dvk-st-per button', m).forEach(b => b.onclick = () => { per = b.dataset.v; $$('#dvk-st-per button', m).forEach(x => x.classList.toggle('on', x === b)); gerar(); });
    $('#dvk-st-esc', m).onchange = () => gerar();
    const gerar = async () => {
      const ta = $('#dvk-st-texto', m); ta.value = ''; ta.placeholder = 'Gerando…'; $('#dvk-st-info', m).textContent = '';
      try {
        const d = await api('/api/dev-ia/standup', { method: 'POST', body: { periodo: per, escopo: $('#dvk-st-esc', m).value } });
        ta.value = d.texto;
        $('#dvk-st-info', m).textContent = d.gerado_com === 'ia' ? 'Gerado por IA a partir das tarefas concluídas, em andamento e priorizadas.' : 'Gerado pelas regras do quadro' + (d.aviso ? ' — ' + d.aviso : '') + '.';
      } catch (e) { ta.placeholder = e.message; toast(e.message, 'erro'); }
    };
    $('#dvk-st-gerar', m).onclick = gerar;
    $('#dvk-st-copiar', m).onclick = async () => { try { await navigator.clipboard.writeText($('#dvk-st-texto', m).value); toast('Copiado'); } catch (e) { $('#dvk-st-texto', m).select(); toast('Selecione e copie (Ctrl+C)'); } };
    $('#dvk-st-diario', m).onclick = async () => {
      const texto = $('#dvk-st-texto', m).value.trim(); if (!texto) return;
      const d = new Date().toLocaleDateString('pt-BR');
      try { await api('/api/dev-diario', { method: 'POST', body: { titulo: `Standup ${per === 'semana' ? 'da semana ' : ''}${d}`, descricao: texto } }); toast('Salvo no Diário da equipe'); }
      catch (e) { toast(e.message, 'erro'); }
    };
    gerar();
  }

  // ── comando em linguagem natural ──────────────────────────────
  async function executarComando(texto) {
    toast('Pensando…');
    let r;
    try { r = await api('/api/dev-ia/comando', { method: 'POST', body: { texto } }); }
    catch (e) { toast(e.message, 'erro'); return; }
    const filtro = (r.acoes.find(a => a.tipo === 'filtrar') || {}).filtro;
    if (filtro) window.DVK.setFiltro(Object.assign({ busca: '', tag: '', atrib: '', prio: '', atrasadas: false }, filtro));
    const mut = r.acoes.filter(a => a.mutavel);
    if (!mut.length) {
      if (r.resposta) modal('dvk-m-resp', 'Resposta', `<p style="margin:0;line-height:1.55;white-space:pre-line">${esc(r.resposta)}</p>`, '<button class="dvk-btn primario" data-fechar2>Ok</button>').querySelector('[data-fechar2]').onclick = () => fechar('dvk-m-resp');
      else if (!filtro) toast('Não entendi o que fazer com isso — tente reformular', 'erro');
      else toast('Filtro aplicado');
      return;
    }
    const m = modal('dvk-m-acoes', 'Confirmar ações',
      `${r.resposta ? `<p style="margin:0;line-height:1.5">${esc(r.resposta)}</p>` : ''}
       <p class="dvk-aviso">A IA sugeriu as ações abaixo. Nada foi alterado ainda — marque o que quer aplicar.</p>
       ${mut.map((a, i) => `<label class="dvk-rascunho" style="cursor:pointer"><span style="display:flex;gap:10px;align-items:flex-start"><input type="checkbox" data-i="${i}" checked style="margin-top:3px"><span style="font-size:13px;line-height:1.45">${esc(a.resumo)}</span></span></label>`).join('')}`,
      '<button class="dvk-btn" data-fechar2>Cancelar</button><button class="dvk-btn primario" id="dvk-ac-aplicar"><i class="ph ph-check"></i>Aplicar selecionadas</button>');
    $('[data-fechar2]', m).onclick = () => fechar('dvk-m-acoes');
    $('#dvk-ac-aplicar', m).onclick = async () => {
      const marcadas = $$('input[data-i]', m).filter(c => c.checked).map(c => mut[+c.dataset.i]);
      if (!marcadas.length) { toast('Nada selecionado', 'erro'); return; }
      try {
        const d = await api('/api/dev-ia/comando/aplicar', { method: 'POST', body: { acoes: marcadas } });
        fechar('dvk-m-acoes');
        const partes = []; if (d.movidas) partes.push(`${d.movidas} movida${d.movidas > 1 ? 's' : ''}`); if (d.alteradas) partes.push(`${d.alteradas} alterada${d.alteradas > 1 ? 's' : ''}`); if (d.criadas) partes.push(`${d.criadas} criada${d.criadas > 1 ? 's' : ''}`);
        toast('Pronto: ' + (partes.join(', ') || 'nada mudou'));
        window.DVK.recarregar();
      } catch (e) { toast(e.message, 'erro'); }
    };
  }

  // ── registro no núcleo ────────────────────────────────────────
  window.DVK.registrarBotaoTopo({ id: 'ia-capturar', titulo: 'Capturar', icone: 'ph-sparkle', classe: 'ia', dica: 'Transformar um texto em tarefas (IA)', onclick: () => abrirCaptura() });
  window.DVK.registrarBotaoTopo({ id: 'ia-standup', titulo: 'Standup', icone: 'ph-clipboard-text', dica: 'Resumo do que foi feito e do que vem', onclick: () => abrirStandup('ontem') });
  window.DVK.registrarFocoExtra(focoExtra);
  window.DVK.registrarAcaoPaleta({ sec: 'IA', ic: 'ph-sparkle', titulo: 'Capturar tarefas de um texto', executar: () => abrirCaptura() });
  window.DVK.registrarAcaoPaleta({ sec: 'IA', ic: 'ph-sun', titulo: 'Briefing do dia', executar: pedirBriefing });
  window.DVK.registrarAcaoPaleta({ sec: 'IA', ic: 'ph-clipboard-text', titulo: 'Standup — desde ontem', executar: () => abrirStandup('ontem') });
  window.DVK.registrarAcaoPaleta({ sec: 'IA', ic: 'ph-calendar', titulo: 'Standup — da semana', executar: () => abrirStandup('semana') });
  window.DVK.registrarFallbackPaleta(texto => ({ ic: 'ph-sparkle', titulo: `Perguntar à IA: “${texto}”`, dica: 'ex.: o que está atrasado?', executar: () => executarComando(texto) }));
  window.DVKIA = { abrirCaptura, abrirStandup, executarComando, pedirBriefing };
})();
