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

    /* Versão do próprio <script src="...widget.js?v=N"> que carregou este
     * arquivo, repassada pro CSS. Assim os dois sobem juntos: bumpar o ?v=
     * no HTML troca JS e CSS de uma vez, e nunca acontece de o navegador
     * ficar com um novo e outro velho -- combinação que já deu bolha vazia
     * na tela (JS antigo criava um elemento que o CSS novo não estilizava
     * mais). */
    var _versao = (function () {
        var src = (document.currentScript && document.currentScript.src) || "";
        var m = src.match(/[?&]v=([^&]+)/);
        return m ? m[1] : "";
    })();

    /* Mesma versão do CSS vale pro avatar: trocar o mascote sem isso
     * deixaria o ícone antigo em cache no navegador de quem já usou. */
    var _sufixoVersao = _versao ? "?v=" + encodeURIComponent(_versao) : "";
    var _avatarSrc = "/assistente/avatar.svg" + _sufixoVersao;

    function carregarCss() {
        if (document.getElementById("ops-css")) return;
        var link = document.createElement("link");
        link.id = "ops-css";
        link.rel = "stylesheet";
        link.href = "/assistente/widget.css" + _sufixoVersao;
        document.head.appendChild(link);
    }

    function montarDom() {
        var launcher = document.createElement("button");
        launcher.id = "ops-launcher";
        launcher.type = "button";
        launcher.title = "Assistente OPS (Alt+A)";
        launcher.setAttribute("aria-label", "Abrir assistente OPS");
        launcher.innerHTML =
            '<img src="' + _avatarSrc + '" alt="">' +
            '<span class="ops-badge" id="ops-badge"></span>';

        var painel = document.createElement("div");
        painel.id = "ops-panel";
        painel.innerHTML =
            '<div id="ops-header">' +
            '  <img src="' + _avatarSrc + '" alt="">' +
            '  <div><div class="ops-titulo">OPS</div><div class="ops-sub">Assistente Faiston</div></div>' +
            '  <button type="button" id="ops-sino" title="Sinalizações" aria-label="Sinalizações">' +
            '    🔔<span class="ops-badge" id="ops-sino-badge" hidden></span>' +
            "  </button>" +
            '  <a href="/assistente/documentos" id="ops-gerenciar" title="Gerenciar base de procedimentos" hidden>⚙</a>' +
            '  <button type="button" id="ops-fechar" aria-label="Fechar">✕</button>' +
            "</div>" +
            '<div id="ops-mensagens">' +
            '  <div id="ops-sugestoes">' +
            '    <div class="ops-boas-vindas">' +
            '      <div class="ops-boas-vindas-titulo">Como posso ajudar?</div>' +
            '      <div class="ops-boas-vindas-sub">Pergunte sobre um procedimento, suas tarefas ou o resumo da sua semana.</div>' +
            "    </div>" +
            '    <div class="ops-chips-linha">' +
            '      <button type="button" class="ops-chip" data-pergunta="Resumo da semana">Resumo da semana</button>' +
            '      <button type="button" class="ops-chip" data-pergunta="Quero começar o onboarding">Começar onboarding</button>' +
            "    </div>" +
            "  </div>" +
            "</div>" +
            '<div id="ops-sinalizacoes" hidden>' +
            '  <div id="ops-sinalizacoes-lista"></div>' +
            '  <div id="ops-sinalizacoes-vazio" hidden>Nada por aqui — nenhuma sinalização até agora.</div>' +
            "</div>" +
            '<form id="ops-form">' +
            '  <textarea id="ops-input" rows="1" placeholder="Pergunte algo..." maxlength="2000"></textarea>' +
            '  <button type="submit" id="ops-enviar" aria-label="Enviar">➤</button>' +
            "</form>";

        document.body.appendChild(launcher);
        document.body.appendChild(painel);
        return { launcher: launcher, painel: painel };
    }

    function bolhaAssistente(mensagens, { erro } = {}) {
        var wrap = document.createElement("div");
        wrap.className = "ops-msg ops-msg-assistente" + (erro ? " ops-erro" : "");
        var bolha = document.createElement("div");
        bolha.className = "ops-bolha";
        bolha.textContent = "";
        wrap.appendChild(bolha);
        mensagens.appendChild(wrap);
        mensagens.scrollTop = mensagens.scrollHeight;
        return { wrap: wrap, bolha: bolha };
    }

    function bolhaUsuario(mensagens, texto) {
        var wrap = document.createElement("div");
        wrap.className = "ops-msg ops-msg-usuario";
        var bolha = document.createElement("div");
        bolha.className = "ops-bolha";
        bolha.textContent = texto;
        wrap.appendChild(bolha);
        mensagens.appendChild(wrap);
        mensagens.scrollTop = mensagens.scrollHeight;
    }

    function montarFontes(wrap, fontes) {
        if (!fontes || !fontes.length) return;
        var box = document.createElement("div");
        box.className = "ops-fontes";
        fontes.forEach(function (fonte) {
            var chip = document.createElement("button");
            chip.type = "button";
            chip.className = "ops-fonte-chip";
            chip.textContent = "📄 " + fonte.titulo;
            var previa = document.createElement("div");
            previa.className = "ops-fonte-previa";
            previa.textContent = fonte.trecho || "";
            previa.hidden = true;
            chip.addEventListener("click", function () {
                previa.hidden = !previa.hidden;
            });
            var item = document.createElement("div");
            item.className = "ops-fonte-item";
            item.appendChild(chip);
            item.appendChild(previa);
            box.appendChild(item);
        });
        wrap.appendChild(box);
    }

    function montarFeedback(wrap, logId) {
        if (!logId) return;
        var box = document.createElement("div");
        box.className = "ops-feedback";
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
            up.classList.toggle("ops-ativo", util);
            down.classList.toggle("ops-ativo", !util);
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
            obrigado.className = "ops-obrigado";
            obrigado.textContent = "Obrigado!";
            box.appendChild(obrigado);
        }

        up.addEventListener("click", function () { enviar(true); });
        down.addEventListener("click", function () { enviar(false); });
        box.appendChild(up);
        box.appendChild(down);
        wrap.appendChild(box);
    }

    /* Resposta da capacidade "achar" (buscar_carimbo) sempre chega no
     * formato 'Carimbo "TÍTULO" (CATEGORIA):\n\n{conteúdo}' — ver
     * formatar_resposta() em app/assistente/capacidade_achar.py. Copia só
     * o conteúdo depois dos dois-pontos, nunca o cabeçalho com título/
     * categoria (isso é metadado nosso, não faz parte do texto que a
     * pessoa vai colar no atendimento). */
    var _RE_CARIMBO = /^Carimbo "[^"]*"\s*\([^)]*\):\n\n([\s\S]+)$/;

    function extrairConteudoCarimbo(texto) {
        var m = _RE_CARIMBO.exec(texto);
        return m ? m[1] : null;
    }

    function copiarTexto(texto) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(texto);
        }
        // Fallback pra navegador sem Clipboard API (ou contexto não
        // seguro) -- textarea temporário fora da tela + execCommand.
        return new Promise(function (resolve, reject) {
            var ta = document.createElement("textarea");
            ta.value = texto;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            try {
                document.execCommand("copy") ? resolve() : reject();
            } catch (e) {
                reject(e);
            } finally {
                document.body.removeChild(ta);
            }
        });
    }

    function montarBotaoCopiarCarimbo(wrap, conteudo) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ops-copiar";
        btn.textContent = "📋 Copiar carimbo";
        btn.setAttribute("aria-label", "Copiar texto do carimbo");
        btn.addEventListener("click", function () {
            copiarTexto(conteudo).then(function () {
                btn.textContent = "✓ Copiado!";
                btn.classList.add("ops-copiado");
            }, function () {
                btn.textContent = "Não foi possível copiar";
            }).then(function () {
                setTimeout(function () {
                    btn.textContent = "📋 Copiar carimbo";
                    btn.classList.remove("ops-copiado");
                }, 2000);
            });
        });
        wrap.appendChild(btn);
    }

    function tempoRelativo(iso) {
        if (!iso) return "";
        var diffMs = Date.now() - new Date(iso).getTime();
        var minutos = Math.floor(diffMs / 60000);
        if (minutos < 60) return minutos <= 1 ? "agora há pouco" : minutos + " min atrás";
        var horas = Math.floor(minutos / 60);
        if (horas < 24) return horas === 1 ? "há 1 hora" : "há " + horas + " horas";
        var dias = Math.floor(horas / 24);
        return dias === 1 ? "há 1 dia" : "há " + dias + " dias";
    }

    function postJson(url, corpo) {
        var headers = { "Content-Type": "application/json" };
        var token = csrfCookie();
        if (token) headers["X-CSRF-Token"] = token;
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: headers,
            body: JSON.stringify(corpo || {}),
        });
    }

    function montarFeedbackSinalizacao(box, sinalizacaoId) {
        var opcoes = [
            { rotulo: "👍", valor: 1, titulo: "Útil" },
            { rotulo: "👎", valor: -1, titulo: "Não útil" },
            { rotulo: "🔕", valor: -2, titulo: "Não me avise mais assim" },
        ];
        opcoes.forEach(function (op) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = op.rotulo;
            btn.title = op.titulo;
            btn.setAttribute("aria-label", op.titulo);
            btn.addEventListener("click", function () {
                box.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
                btn.classList.add("ops-ativo");
                postJson("/assistente/sinalizacoes/" + sinalizacaoId + "/feedback", { feedback: op.valor }).catch(function () {});
                var obrigado = document.createElement("span");
                obrigado.className = "ops-obrigado";
                obrigado.textContent = "Obrigado!";
                box.appendChild(obrigado);
            });
            box.appendChild(btn);
        });
    }

    function montarItemSinalizacao(item) {
        var wrap = document.createElement("div");
        wrap.className = "ops-sinal-item";
        var texto = document.createElement("div");
        texto.className = "ops-sinal-texto";
        texto.textContent = item.texto;
        var quando = document.createElement("div");
        quando.className = "ops-sinal-quando";
        quando.textContent = tempoRelativo(item.criado_em);
        var feedback = document.createElement("div");
        feedback.className = "ops-feedback";
        wrap.appendChild(texto);
        wrap.appendChild(quando);
        if (item.feedback === null || item.feedback === undefined) {
            montarFeedbackSinalizacao(feedback, item.id);
            wrap.appendChild(feedback);
        }
        return wrap;
    }

    function atualizarBadge(launcherBadge, sinoBadge) {
        fetch("/assistente/sinalizacoes/contagem", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : { nao_vistas: 0 }; })
            .then(function (d) {
                var n = (d && d.nao_vistas) || 0;
                [launcherBadge, sinoBadge].forEach(function (el) {
                    if (!el) return;
                    if (n > 0) {
                        el.textContent = n > 9 ? "9+" : String(n);
                        el.hidden = false;
                        el.style.display = "inline-block";
                    } else {
                        el.hidden = true;
                        el.style.display = "none";
                    }
                });
                var launcher = launcherBadge && launcherBadge.closest("#ops-launcher");
                if (launcher) launcher.classList.toggle("ops-tem-novidade", n > 0);
            })
            .catch(function () {});
    }

    function carregarSinalizacoes(lista, vazio, launcherBadge, sinoBadge) {
        lista.innerHTML = "";
        vazio.hidden = true;
        fetch("/assistente/sinalizacoes", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : { sinalizacoes: [] }; })
            .then(function (d) {
                var itens = (d && d.sinalizacoes) || [];
                if (!itens.length) {
                    vazio.hidden = false;
                    return;
                }
                itens.forEach(function (item) {
                    lista.appendChild(montarItemSinalizacao(item));
                    if (!item.vista_em) {
                        postJson("/assistente/sinalizacoes/" + item.id + "/visualizar", {}).catch(function () {});
                    }
                });
                atualizarBadge(launcherBadge, sinoBadge);
            })
            .catch(function () {
                vazio.hidden = false;
            });
    }

    /* Indicador de "está digitando": três pontinhos com bounce defasado,
     * o gesto que todo mundo já reconhece de app de mensagem. Fica só
     * enquanto a resposta não começou a chegar -- assim que o primeiro
     * pedaço de texto entra, some e dá lugar ao texto de verdade. */
    function mostrarDigitando(wrap, bolha) {
        var pontos = document.createElement("span");
        pontos.className = "ops-digitando";
        pontos.setAttribute("aria-label", "escrevendo");
        for (var i = 0; i < 3; i++) pontos.appendChild(document.createElement("span"));
        bolha.appendChild(pontos);
        // Enquanto são só os pontinhos, a bolha encolhe pra caber neles --
        // uma bolha larga e vazia com três pontos perdidos no canto fica
        // estranha. Volta ao normal quando o texto começa a chegar.
        wrap.classList.add("ops-aguardando");
    }

    /* Tempo mínimo que os pontinhos ficam na tela. Sem isso, resposta
     * rápida faz eles piscarem por uns 100ms — o olho não registra, e a
     * resposta parece surgir do nada. Segurar meio segundo faz a coisa
     * ler como "ele parou pra pensar" em vez de um flash. Não atrasa o
     * fim da resposta: o texto continua chegando por trás, só a primeira
     * pintura na tela é que espera. */
    var MIN_DIGITANDO_MS = 500;

    async function consumirStreamSSE(resp, wrap, bolha, mensagens) {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var texto = "";
        var logId = null;
        var comecouEm = Date.now();
        var mostrandoTexto = false;  // já trocou os pontinhos pelo texto?
        var trocaAgendada = null;

        function pintar(comCaret) {
            bolha.textContent = texto;
            if (comCaret) {
                var caret = document.createElement("span");
                caret.className = "ops-caret";
                bolha.appendChild(caret);
            }
            mensagens.scrollTop = mensagens.scrollHeight;
        }

        function trocarPontosPorTexto() {
            if (trocaAgendada) { clearTimeout(trocaAgendada); trocaAgendada = null; }
            bolha.innerHTML = "";
            wrap.classList.remove("ops-aguardando");
            mostrandoTexto = true;
        }

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
                texto += dados.delta || "";
                if (mostrandoTexto) {
                    pintar(true);
                } else if (!trocaAgendada) {
                    // Primeiro pedaço chegou: agenda a troca dos pontinhos
                    // pelo texto respeitando o tempo mínimo. O que chegar
                    // enquanto isso vai se acumulando em `texto`.
                    trocaAgendada = setTimeout(function () {
                        trocaAgendada = null;
                        trocarPontosPorTexto();
                        pintar(true);
                    }, Math.max(0, MIN_DIGITANDO_MS - (Date.now() - comecouEm)));
                }
            } else if (evento === "fontes") {
                montarFontes(wrap, dados.fontes);
            } else if (evento === "erro") {
                trocarPontosPorTexto();
                wrap.classList.add("ops-erro");
                bolha.textContent = dados.mensagem || "Algo deu errado.";
            } else if (evento === "fim") {
                // Resposta inteira pode chegar antes do tempo mínimo (cache,
                // resposta curta). Respeita o mínimo aqui também, senão os
                // pontinhos piscam e somem -- justamente o que a gente quer
                // evitar. Sem caret no fim, pra não deixar o traço piscando
                // pra sempre depois que a resposta acabou.
                function fecharNaTela() {
                    trocarPontosPorTexto();
                    pintar(false);
                    if (texto) {
                        montarFeedback(wrap, logId);
                        var conteudoCarimbo = extrairConteudoCarimbo(texto);
                        if (conteudoCarimbo) montarBotaoCopiarCarimbo(wrap, conteudoCarimbo);
                    }
                }
                var restante = Math.max(0, MIN_DIGITANDO_MS - (Date.now() - comecouEm));
                if (mostrandoTexto || restante === 0) {
                    fecharNaTela();
                } else {
                    if (trocaAgendada) { clearTimeout(trocaAgendada); trocaAgendada = null; }
                    setTimeout(fecharNaTela, restante);
                }
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

    async function enviarPergunta(pergunta, mensagens) {
        bolhaUsuario(mensagens, pergunta);
        var { wrap, bolha } = bolhaAssistente(mensagens);
        mostrarDigitando(wrap, bolha);

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
                    contexto_tela: (document.body && document.body.dataset && document.body.dataset.opsTela) || location.pathname,
                }),
            });
        } catch (e) {
            wrap.classList.add("ops-erro");
            bolha.textContent = "Não consegui falar com o assistente. Verifique sua conexão.";
            return;
        }

        if (!resp.ok || !resp.body) {
            wrap.classList.add("ops-erro");
            bolha.textContent = resp.status === 403
                ? "O assistente ainda não está disponível para o seu perfil."
                : "Não consegui obter resposta agora. Tente de novo em instantes.";
            return;
        }

        await consumirStreamSSE(resp, wrap, bolha, mensagens);
    }

    async function iniciarCheckinDiario(mensagens) {
        var { wrap, bolha } = bolhaAssistente(mensagens);
        mostrarDigitando(wrap, bolha);

        var headers = {};
        var token = csrfCookie();
        if (token) headers["X-CSRF-Token"] = token;

        var resp;
        try {
            resp = await fetch("/assistente/checkin/iniciar", {
                method: "POST",
                credentials: "same-origin",
                headers: headers,
            });
        } catch (e) {
            wrap.remove();
            return;
        }
        if (!resp.ok || !resp.body) {
            // Checkin é proativo -- se falhar, some em silêncio em vez de
            // abrir com uma bolha de erro sem a pessoa ter pedido nada.
            wrap.remove();
            return;
        }
        await consumirStreamSSE(resp, wrap, bolha, mensagens);
    }

    function iniciar(admin) {
        carregarCss();
        var mensagens = null;
        var { launcher, painel } = montarDom();
        mensagens = painel.querySelector("#ops-mensagens");
        var form = painel.querySelector("#ops-form");
        var input = painel.querySelector("#ops-input");
        var enviarBtn = painel.querySelector("#ops-enviar");
        var fechar = painel.querySelector("#ops-fechar");
        var sugestoes = painel.querySelector("#ops-sugestoes");
        if (admin) {
            var gerenciar = painel.querySelector("#ops-gerenciar");
            if (gerenciar) gerenciar.hidden = false;
        }

        var sino = painel.querySelector("#ops-sino");
        var sinoBadge = painel.querySelector("#ops-sino-badge");
        var launcherBadge = launcher.querySelector("#ops-badge");
        var painelSinalizacoes = painel.querySelector("#ops-sinalizacoes");
        var listaSinalizacoes = painel.querySelector("#ops-sinalizacoes-lista");
        var vazioSinalizacoes = painel.querySelector("#ops-sinalizacoes-vazio");
        var opsForm = painel.querySelector("#ops-form");
        var vendoSinalizacoes = false;

        sino.addEventListener("click", function () {
            vendoSinalizacoes = !vendoSinalizacoes;
            painelSinalizacoes.hidden = !vendoSinalizacoes;
            mensagens.hidden = vendoSinalizacoes;
            opsForm.hidden = vendoSinalizacoes;
            if (vendoSinalizacoes) {
                carregarSinalizacoes(listaSinalizacoes, vazioSinalizacoes, launcherBadge, sinoBadge);
            }
        });

        atualizarBadge(launcherBadge, sinoBadge);
        setInterval(function () { atualizarBadge(launcherBadge, sinoBadge); }, 120000);

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

        painel.querySelectorAll(".ops-chip").forEach(function (chip) {
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

        // Fase 7 (checkin diário): só dispara na 1a vez que a pessoa loga
        // no dia (o servidor decide isso, não o front -- recarregar a
        // página nunca reabre sozinho de novo hoje).
        fetch("/assistente/checkin/pendente", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : { pendente: false }; })
            .then(function (d) {
                if (!d || !d.pendente) return;
                if (sugestoes) { sugestoes.remove(); sugestoes = null; }
                abrir();
                iniciarCheckinDiario(mensagens);
            })
            .catch(function () { /* checkin é proativo -- falha em silêncio */ });

        fetch("/api/avisos-ops", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(montarAvisoGlobal)
            .catch(function () {});
    }

    /* Aviso global de novidade (ex.: lançamento do Assistente OPS),
     * disparado pelo admin em GET/POST /api/avisos-ops (main.py). Não é
     * uma sinalização da capacidade D (essa é pessoal, por usuário) --
     * é um recado único pra empresa toda, então "visto" fica só no
     * localStorage de quem já fechou, comparado por `disparado_em` (um
     * novo disparo com timestamp diferente reaparece pra todo mundo,
     * inclusive quem já tinha fechado o anterior). */
    function montarAvisoGlobal(aviso) {
        if (!aviso || !aviso.disparado_em) return;
        var vistoKey = "ops_aviso_visto";
        try {
            if (localStorage.getItem(vistoKey) === aviso.disparado_em) return;
        } catch (e) { /* localStorage indisponível: mostra sempre, sem persistir */ }

        var caixa = document.createElement("div");
        caixa.className = "ops-aviso";
        var fechar = document.createElement("button");
        fechar.type = "button";
        fechar.className = "ops-aviso-fechar";
        fechar.setAttribute("aria-label", "Fechar aviso");
        fechar.textContent = "✕";
        var texto = document.createElement("p");
        texto.className = "ops-aviso-texto";
        texto.textContent = aviso.mensagem || "";
        var link = document.createElement("a");
        link.className = "ops-aviso-link";
        link.href = aviso.link || "/ajuda";
        link.textContent = "Saiba mais →";
        caixa.appendChild(fechar);
        caixa.appendChild(texto);
        caixa.appendChild(link);

        function marcarVisto() {
            try { localStorage.setItem(vistoKey, aviso.disparado_em); } catch (e) {}
            caixa.remove();
        }
        fechar.addEventListener("click", marcarVisto);
        link.addEventListener("click", marcarVisto);

        document.body.appendChild(caixa);
    }

    function verificarElegibilidadeEIniciar() {
        fetch("/assistente/elegivel", { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : { elegivel: false }; })
            .then(function (d) { if (d && d.elegivel) iniciar(!!d.admin); })
            .catch(function () { /* offline ou rota indisponível: widget simplesmente não aparece */ });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", verificarElegibilidadeEIniciar);
    } else {
        verificarElegibilidadeEIniciar();
    }
})();
