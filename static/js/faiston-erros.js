// Sistema global de notificação de erros — Faiston
// Injeta o toast na página e expõe mostrarErroGlobal()

(function () {
    const toast = document.createElement('div');
    toast.id = 'faiston-erro-toast';
    Object.assign(toast.style, {
        display: 'none', position: 'fixed', bottom: '1.25rem', right: '1.25rem',
        zIndex: '9999', width: '400px', maxWidth: 'calc(100vw - 2.5rem)',
        background: '#fff0f0', border: '1.5px solid #fca5a5', borderRadius: '0.875rem',
        boxShadow: '0 8px 32px -4px rgba(220,38,38,.2)', padding: '1rem',
        fontFamily: 'inherit', transition: 'opacity .2s'
    });
    toast.innerHTML = `
        <div style="display:flex;align-items:flex-start;gap:.75rem">
            <span style="color:#ef4444;font-size:1.375rem;flex-shrink:0;margin-top:1px">⚠️</span>
            <div style="flex:1;min-width:0">
                <div style="font-weight:700;color:#b91c1c;font-size:.875rem;margin-bottom:.25rem"
                     id="fe-titulo">Erro no sistema</div>
                <div style="font-size:.75rem;color:#dc2626;margin-bottom:.4rem;word-break:break-word"
                     id="fe-msg"></div>
                <div style="font-size:.6875rem;color:#f87171;font-family:monospace;
                            white-space:pre-wrap;word-break:break-all;max-height:80px;overflow:auto"
                     id="fe-detalhe"></div>
            </div>
            <button onclick="faistonFecharErro()" title="Fechar"
                style="color:#f87171;background:none;border:none;cursor:pointer;
                       font-size:1.1rem;line-height:1;padding:0;flex-shrink:0">✕</button>
        </div>
        <div style="margin-top:.75rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
            <button onclick="faistonCopiarErro()" id="fe-btn-copiar"
                style="font-size:.75rem;font-weight:600;padding:.3rem .8rem;border-radius:.5rem;
                       background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5;
                       cursor:pointer">📋 Copiar detalhes para suporte</button>
            <span style="font-size:.625rem;color:#f87171;margin-left:auto;white-space:nowrap"
                  id="fe-ts"></span>
        </div>
    `;

    function montar() { document.body.appendChild(toast); }
    if (document.body) montar();
    else document.addEventListener('DOMContentLoaded', montar);

    let _erroAtual = null;

    window.mostrarErroGlobal = function (titulo, msg, detalhe) {
        const ts = new Date().toLocaleString('pt-BR', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        _erroAtual = { titulo, msg: msg || '', detalhe: detalhe || '', ts, url: window.location.pathname };
        document.getElementById('fe-titulo').textContent = titulo;
        document.getElementById('fe-msg').textContent = msg || '';
        document.getElementById('fe-detalhe').textContent = detalhe || '';
        document.getElementById('fe-ts').textContent = ts;
        toast.style.display = 'block';
        console.error('[FAISTON ERROR]', titulo, msg || '', detalhe || '');
    };

    window.faistonFecharErro = function () { toast.style.display = 'none'; };

    window.faistonCopiarErro = function () {
        if (!_erroAtual) return;
        const linhas = [
            '=== Faiston — Relatório de Erro ===',
            `Erro:     ${_erroAtual.titulo}`,
            `Mensagem: ${_erroAtual.msg}`,
            _erroAtual.detalhe ? `Detalhe:  ${_erroAtual.detalhe}` : null,
            `Hora:     ${_erroAtual.ts}`,
            `Página:   ${_erroAtual.url}`,
            `Agente:   ${navigator.userAgent}`,
        ].filter(Boolean).join('\n');

        const tentar = navigator.clipboard
            ? navigator.clipboard.writeText(linhas)
            : Promise.reject();

        tentar.catch(() => {
            // fallback sem clipboard API
            const ta = document.createElement('textarea');
            ta.value = linhas;
            ta.style.cssText = 'position:fixed;top:-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }).finally(() => {
            const btn = document.getElementById('fe-btn-copiar');
            if (btn) {
                btn.textContent = '✓ Copiado!';
                setTimeout(() => { btn.textContent = '📋 Copiar detalhes para suporte'; }, 2500);
            }
        });
    };

    // Promise rejections não tratadas (ex: fetch que jogou exceção)
    window.addEventListener('unhandledrejection', function (e) {
        const err = e.reason;
        if (!err || err.name === 'AbortError') return;
        const stack = err.stack ? err.stack.split('\n').slice(0, 3).join('\n') : '';
        window.mostrarErroGlobal(
            'Erro interno não tratado',
            err.message || String(err),
            stack
        );
    });

    // Erros JS síncronos não capturados
    window.addEventListener('error', function (e) {
        if (!e.error || e.error.name === 'AbortError') return;
        window.mostrarErroGlobal(
            'Erro JavaScript',
            e.message || 'Erro desconhecido',
            `${e.filename || 'script'}:${e.lineno}:${e.colno}`
        );
    });
})();
