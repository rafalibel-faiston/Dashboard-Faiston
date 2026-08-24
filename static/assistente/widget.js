/* Widget do assistente OPS — Faiston OPS.
 * Fase 1: caixa de perguntas com resposta em streaming (SSE) e log no
 * servidor. Sem localStorage de histórico (o histórico, se existir,
 * vem do log — regra da especificação). Autocontido: não depende do
 * patch de CSRF que algumas telas já têm em window.fetch, porque nem
 * toda tela tem esse patch (ex.: relatorio.html, onde o widget nem é
 * incluído, mas por segurança o script não presume nada do host).
 */
(function () {
    "use strict";

    function csrfCookie() {
        var m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : null;
    }

    function carregarCss() {
        if (document.getElementById("nexo-css")) return;
        var link = document.createElement("link");
        link.id = "nexo-css";
        link.rel = "stylesheet";
        link.href = "/assistente/widget.css";
        document.head.appendChild(link);
    }

    function montarDom() {
        var launcher = document.createElement("button");
        launcher.id = "nexo-launcher";
        launcher.type = "button";
        launcher.title = "Assistente OPS (Alt+A)";
        launcher.setAttribute("aria-label", "Abrir assistente OPS");
        launcher.innerHTML =
            '<img src="/assistente/avatar.svg" alt="">' +
            '<span class="nexo-badge" id="nexo-badge"></span>';

        var painel = document.createElement("div");
        painel.id = "nexo-panel";
        painel.innerHTML =
            '<div id="nexo-header">' +
            '  <img src="/assistente/avatar.svg" alt="">' +
            '  <div><div class="nexo-titulo">OPS</div><div class="nexo-sub">Assistente Faiston</div></div>' +
            '  <button type="button" id="nexo-fechar" aria-label="Fechar">✕</button>' +
            "</div>" +
            '<div id="nexo-mensagens">' +
            '  <div id="nexo-sugestoes">' +
            '    <button type="button" class="nexo-chip" data-pergunta="Resumo da semana">📊 Resumo da semana</button>' +
            "  </div>" +
            "</div>" +
            '<div class="nexo-dica">Em construção — hoje só monta o resumo semanal e responde de forma genérica. Ainda não acessa documentos nem outros dados do sistema.</div>' +
            '<form id="nexo-form">' +
            '  <textarea id="nexo-input" rows="1" placeholder="Pergunte algo..." maxlength="2000"></textarea>' +
            '  <button type="submit" id="nexo-enviar" aria-label="Enviar">➤</button>' +
            "</form>";

        document.body.appendChild(launcher);
        document.body.appendChild(painel);
        return { launcher: launcher, painel: painel };
    }

    function bolhaAssistente(mensagens, { erro } = {}) {
        var wrap = document.createElement("div");
        wrap.className = "nexo-msg nexo-msg-assistente" + (erro ? " nexo-erro" : "");
        var bolha = document.createElement("div");
        bolha.className = "nexo-bolha";
        bolha.textContent = "";
        wrap.appendChild(bolha);
        mensagens.appendChild(wrap);
        mensagens.scrollTop = mensagens.scrollHeight;
        return { wrap: wrap, bolha: bolha };
    }

    function bolhaUsuario(mensagens, texto) {
        var wrap = document.createElement("div");
        wrap.className = "nexo-msg nexo-msg-usuario";
        var bolha = document.createElement("div");
        bolha.className = "nexo-bolha";
        bolha.textContent = texto;
        wrap.appendChild(bolha);
        mensagens.appendChild(wrap);
        mensagens.scrollTop = mensagens.scrollHeight;
    }

    function montarFeedback(wrap, logId) {
        if (!logId) return;
        var box = document.createElement("div");
        box.className = "nexo-feedback";
        var up = document.createElement("button");
        up.type = "button";
        up.textContent = "👍";
        up.setAttribute("aria-label", "Útil");
        var down = document.createElement("button");
        down.type = "button";
        down.textContent = "👎";
        down.setAttribute("aria-label", "Não útil");

        function enviar(util) {
            up.disabled = down.disabled = true;
            up.classList.toggle("nexo-ativo", util);
            down.classList.toggle("nexo-ativo", !util);
            var headers = { "Content-Type": "application/json" };
            var token = csrfCookie();
            if (token) headers["X-CSRF-Token"] = token;
            fetch("/assistente/feedback", {
                method: "POST",
                credentials: "same-origin",
                headers: headers,
                body: JSON.stringify({ log_id: logId, util: util }),
            }).catch(function () {});
            var obrigado = document.createElement("span");
            obrigado.className = "nexo-obrigado";
            obrigado.textContent = "Obrigado!";
            box.appendChild(obrigado);
        }

        up.addEventListener("click", function () { enviar(true); });
        down.addEventListener("click", function () { enviar(false); });
        box.appendChild(up);
        box.appendChild(down);
        wrap.appendChild(box);
    }

    async function enviarPergunta(pergunta, mensagens) {
        bolhaUsuario(mensagens, pergunta);
        var { wrap, bolha } = bolhaAssistente(mensagens);
        bolha.innerHTML = '<span class="nexo-cursor"></span>';

        var headers = { "Content-Type": "application/json" };
        var token = csrfCookie();
        if (token) headers["X-CSRF-Token"] = token;

        var resp;
        try {
            resp = await fetch("/assistente/pergunta", {
                method: "POST",
                credentials: "same-origin",
                headers: headers,
                body: JSON.stringify({
                    pergunta: pergunta,
                    contexto_tela: (document.body && document.body.dataset && document.body.dataset.nexoTela) || location.pathname,
                }),
            });
        } catch (e) {
            wrap.classList.add("nexo-erro");
            bolha.textContent = "Não consegui falar com o assistente. Verifique sua conexão.";
            return;
        }

        if (!resp.ok || !resp.body) {
            wrap.classList.add("nexo-erro");
            bolha.textContent = resp.status === 403
                ? "O assistente ainda não está disponível para o seu perfil."
                : "Não consegui obter resposta agora. Tente de novo em instantes.";
            return;
        }

        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var texto = "";
        var logId = null;
        var primeiraLinha = true;

        function processarEvento(blocoBruto) {
            var evento = "message";
            var dadosLinhas = [];
            blocoBruto.split("\n").forEach(function (linha) {
                if (linha.indexOf("event:") === 0) evento = linha.slice(6).trim();
                else if (linha.indexOf("data:") === 0) dadosLinhas.push(linha.slice(5).trim());
            });
            var dados = {};
            try { dados = JSON.parse(dadosLinhas.join("\n")); } catch (e) { /* ignora linha malformada */ }

            if (evento === "inicio") {
                logId = dados.log_id;
            } else if (evento === "texto") {
                if (primeiraLinha) { bolha.innerHTML = ""; primeiraLinha = false; }
                texto += dados.delta || "";
                bolha.textContent = texto;
                var cursor = document.createElement("span");
                cursor.className = "nexo-cursor";
                bolha.appendChild(cursor);
                mensagens.scrollTop = mensagens.scrollHeight;
            } else if (evento === "erro") {
                wrap.classList.add("nexo-erro");
                bolha.textContent = dados.mensagem || "Algo deu errado.";
            } else if (evento === "fim") {
                bolha.textContent = texto;
                if (texto) montarFeedback(wrap, logId);
            }
        }

        while (true) {
            var { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            var partes = buffer.split("\n\n");
            buffer = partes.pop();
            for (var i = 0; i < partes.length; i++) {
                if (partes[i].trim()) processarEvento(partes[i]);
            }
        }
        if (buffer.trim()) processarEvento(buffer);
    }

    function iniciar() {
        carregarCss();
        var mensagens = null;
        var { launcher, painel } = montarDom();
        mensagens = painel.querySelector("#nexo-mensagens");
        var form = painel.querySelector("#nexo-form");
        var input = painel.querySelector("#nexo-input");
        var enviarBtn = painel.querySelector("#nexo-enviar");
        var fechar = painel.querySelector("#nexo-fechar");
        var sugestoes = painel.querySelector("#nexo-sugestoes");

        async function enviar(pergunta) {
            if (!pergunta) return;
            if (sugestoes) { sugestoes.remove(); sugestoes = null; }
            enviarBtn.disabled = true;
            try {
                await enviarPergunta(pergunta, mensagens);
            } finally {
                enviarBtn.disabled = false;
                input.focus();
            }
        }

        painel.querySelectorAll(".nexo-chip").forEach(function (chip) {
            chip.addEventListener("click", function () {
                enviar(chip.getAttribute("data-pergunta"));
            });
        });

        function abrir() {
            painel.classList.add("aberto");
            input.focus();
        }
        function fecharPainel() {
            painel.classList.remove("aberto");
        }
        function alternar() {
            painel.classList.contains("aberto") ? fecharPainel() : abrir();
        }

        launcher.addEventListener("click", alternar);
        fechar.addEventListener("click", fecharPainel);
        document.addEventListener("keydown", function (ev) {
            if (ev.altKey && (ev.key === "a" || ev.key === "A")) {
                ev.preventDefault();
                alternar();
            } else if (ev.key === "Escape" && painel.classList.contains("aberto")) {
                fecharPainel();
            }
        });

        input.addEventListener("input", function () {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 90) + "px";
        });
        input.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                form.requestSubmit();
            }
        });

        form.addEventListener("submit", function (ev) {
            ev.preventDefault();
            var pergunta = input.value.trim();
            if (!pergunta) return;
            input.value = "";
            input.style.height = "auto";
            enviar(pergunta);
        });
    }

    function verificarElegibilidadeEIniciar() {
        fetch("/assistente/elegivel", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : { elegivel: false }; })
            .then(function (d) { if (d && d.elegivel) iniciar(); })
            .catch(function () { /* offline ou rota indisponível: widget simplesmente não aparece */ });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", verificarElegibilidadeEIniciar);
    } else {
        verificarElegibilidadeEIniciar();
    }
})();
