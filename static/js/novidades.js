/* Aviso de novidades do sistema (2026-09-28).
 *
 * Na primeira vez que a pessoa abre o sistema depois de uma novidade
 * publicada, mostra um card com o que mudou e um botão pra página
 * /novidades. "Já vi" fica no servidor (POST /api/novidades/vistas), então
 * fechar aqui vale pra qualquer computador. Também acende um pontinho em
 * todo link marcado com [data-novidades-link] enquanto houver novidade não vista.
 */
(function () {
    if (window.self !== window.top) return;                 // quadro embutido no painel do gestor
    if (location.pathname.indexOf('/novidades') === 0) return;

    var CAT = {
        novidade: { rotulo: 'Novidade', cor: '#5B2EE0' },
        melhoria: { rotulo: 'Melhoria', cor: '#0891A0' },
        correcao: { rotulo: 'Correção', cor: '#D0307E' }
    };

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function acenderPontos() {
        var links = document.querySelectorAll('[data-novidades-link]');
        for (var i = 0; i < links.length; i++) {
            if (links[i].querySelector('.novidades-ponto')) continue;
            var p = document.createElement('span');
            p.className = 'novidades-ponto';
            p.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#EC4899;margin-left:6px;box-shadow:0 0 0 3px rgba(236,72,153,0.18);vertical-align:middle';
            links[i].appendChild(p);
        }
    }

    function apagarPontos() {
        var pts = document.querySelectorAll('.novidades-ponto');
        for (var i = 0; i < pts.length; i++) pts[i].remove();
    }

    function marcarVisto() {
        apagarPontos();
        return fetch('/api/novidades/vistas', { method: 'POST', credentials: 'same-origin' }).catch(function () {});
    }

    function mostrar(d) {
        var escuro = document.documentElement.getAttribute('data-theme') === 'dark';
        var fundoCard = escuro ? '#11111E' : '#FFFFFF';
        var txtForte = escuro ? '#F1F2F8' : '#0B0D1F';
        var txtMedio = escuro ? '#B4B8C8' : '#5E647A';
        var borda = escuro ? 'rgba(255,255,255,0.08)' : '#E5E8F0';

        var itens = d.itens.map(function (n, i) {
            var c = CAT[n.categoria] || CAT.novidade;
            return '<div style="padding:12px 0;' + (i ? 'border-top:1px solid ' + borda : '') + '">' +
                '<span style="font-size:10.5px;font-weight:700;color:' + c.cor + ';text-transform:uppercase;letter-spacing:.06em">' + c.rotulo + '</span>' +
                '<p style="margin:3px 0 0;font-size:14px;font-weight:700;color:' + txtForte + ';line-height:1.35">' + esc(n.titulo) + '</p>' +
                (n.resumo ? '<p style="margin:4px 0 0;font-size:13px;color:' + txtMedio + ';line-height:1.5">' + esc(n.resumo) + '</p>' : '') +
                '</div>';
        }).join('');
        var outras = d.total - d.itens.length;

        var fundo = document.createElement('div');
        fundo.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(7,7,15,0.45);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:16px;font-family:Inter,system-ui,sans-serif';
        fundo.innerHTML =
            '<div role="dialog" aria-modal="true" aria-labelledby="nov-titulo" style="background:' + fundoCard + ';border-radius:18px;max-width:440px;width:100%;overflow:hidden;box-shadow:0 24px 60px rgba(7,7,15,0.35)">' +
              '<div style="background:linear-gradient(135deg,#5B2EE0,#B826C9);padding:20px 22px;color:#fff">' +
                '<p style="margin:0;font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;opacity:.85">Faiston OPS</p>' +
                '<p id="nov-titulo" style="margin:4px 0 0;font-size:20px;font-weight:800;letter-spacing:-.01em">Tem novidade no sistema 🎉</p>' +
              '</div>' +
              '<div style="padding:6px 22px 4px;max-height:50vh;overflow-y:auto">' + itens +
                (outras > 0 ? '<p style="margin:0;padding:10px 0;border-top:1px solid ' + borda + ';font-size:12.5px;color:' + txtMedio + '">+ ' + outras + ' outra' + (outras > 1 ? 's' : '') + ' na página de novidades</p>' : '') +
              '</div>' +
              '<div style="display:flex;gap:8px;padding:14px 22px 20px">' +
                '<button type="button" data-acao="fechar" style="flex:1;border:1px solid ' + borda + ';background:transparent;color:' + txtMedio + ';border-radius:10px;padding:11px;font-size:13px;font-weight:600;cursor:pointer">Fechar</button>' +
                '<button type="button" data-acao="ver" style="flex:1.4;border:0;background:linear-gradient(135deg,#5B2EE0,#B826C9);color:#fff;border-radius:10px;padding:11px;font-size:13px;font-weight:700;cursor:pointer">Ver todas as novidades →</button>' +
              '</div>' +
            '</div>';

        function fechar() { marcarVisto(); fundo.remove(); document.removeEventListener('keydown', onEsc); }
        function onEsc(e) { if (e.key === 'Escape') fechar(); }
        fundo.addEventListener('click', function (e) {
            var acao = e.target.getAttribute && e.target.getAttribute('data-acao');
            // A própria página marca como visto depois de carregar -- assim ela
            // ainda consegue destacar o que é "Novo pra você".
            if (acao === 'ver') { location.href = '/novidades'; }
            else if (acao === 'fechar' || e.target === fundo) fechar();
        });
        document.addEventListener('keydown', onEsc);
        document.body.appendChild(fundo);
    }

    function iniciar() {
        fetch('/api/novidades/nao-vistas', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d || !d.total) return;
                acenderPontos();
                mostrar(d);
            })
            .catch(function () { /* aviso é opcional: falha em silêncio */ });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
