document.addEventListener("DOMContentLoaded", function() {

    // PALETA DE CORES EXATA DAS IMAGENS
    const cor_cyan = '#34c6f3';    // Ciano do gráfico de rosca
    const cor_purple = '#4337e1';  // Roxo do gráfico de rosca
    const cor_magenta = '#e7218b'; // Rosa/Magenta das barras e da rosca
    const cor_dark = '#0b1227';    // Escuro (Pendente SLA)

    // 1. GRÁFICO: Volume de Tickets (Área)
    var optionsProducao = {
        series: [{ name: 'Tickets Resolvidos', data: [31, 40, 28, 51, 42, 109, 100] }],
        chart: { 
            height: 280, 
            type: 'area', 
            fontFamily: 'Inter, sans-serif', 
            toolbar: { show: true },
            dropShadow: { enabled: true, color: cor_cyan, top: 4, left: 0, blur: 8, opacity: 0.3 }
        },
        colors: [cor_cyan], 
        fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.02, stops: [0, 90, 100] } },
        dataLabels: { enabled: false },
        stroke: { curve: 'smooth', width: 3 },
        xaxis: { categories: ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'], axisBorder: { show: false }, axisTicks: { show: false } },
        grid: { borderColor: '#F1F5F9', strokeDashArray: 4 }
    };
    new ApexCharts(document.querySelector("#chart-producao"), optionsProducao).render();

    // 2. GRÁFICO: Status Geral (Rosca idêntica à imagem)
    var optionsStatus = {
        series: [45, 25, 20, 10], // Proporções ajustáveis
        labels: ['Resolvido', 'Em Atendimento', 'Aguardando Terceiros', 'Pendente SLA'],
        chart: { type: 'donut', height: 300, fontFamily: 'Inter, sans-serif' },
        colors: [cor_cyan, cor_purple, cor_magenta, cor_dark], // Ordem exata da sua imagem
        plotOptions: { pie: { donut: { size: '70%' } } },
        dataLabels: { enabled: false },
        legend: { position: 'bottom', markers: { radius: 12 } },
        stroke: { show: true, colors: ['#ffffff'], width: 2 } // Borda branca para separar as cores
    };
    new ApexCharts(document.querySelector("#chart-status"), optionsStatus).render();

    // 3. GRÁFICO: Funil de Atendimento
    var optionsFunil = {
        series: [{ name: 'Volume', data: [120, 85, 60, 45, 20] }],
        chart: { type: 'bar', height: 280, fontFamily: 'Inter, sans-serif', toolbar: { show: false } },
        plotOptions: {
            bar: { borderRadius: 0, horizontal: true, distributed: true, barHeight: '80%', isFunnel: true }
        },
        colors: [cor_cyan, '#0EA5E9', cor_purple, '#4F46E5', cor_dark], 
        dataLabels: {
            enabled: true,
            formatter: function (val, opt) { return opt.w.globals.labels[opt.dataPointIndex] + ': ' + val },
            dropShadow: { enabled: true, color: '#000', blur: 2, opacity: 0.5 }
        },
        xaxis: { categories: ['Abertura', 'Triagem', 'Acionamento', 'Acompanhamento', 'Fechamento'] },
        legend: { show: false }
    };
    new ApexCharts(document.querySelector("#chart-funil"), optionsFunil).render();

    // 4. GRÁFICO: Barras Horizontais (Esforço por Cliente)
    var optionsBarras = {
        series: [{ name: 'Horas Atuadas', data: [85, 62, 45, 30, 15] }], 
        chart: { type: 'bar', height: 280, fontFamily: 'Inter, sans-serif', toolbar: { show: false } },
        plotOptions: { bar: { horizontal: true, borderRadius: 6, barHeight: '55%' } },
        colors: [cor_magenta], // Mantendo a cor exata da sua imagem
        dataLabels: { enabled: false },
        xaxis: { 
            // Atualizado com a lista de clientes exata solicitada
            categories: ['NTT Sustentação', 'Arcos Dourados', 'Zamp', 'Telcoweb', 'Outros'] 
        },
        grid: { borderColor: '#F1F5F9', strokeDashArray: 4, xaxis: { lines: { show: true } }, yaxis: { lines: { show: false } } }
    };
    new ApexCharts(document.querySelector("#chart-barras"), optionsBarras).render();

});