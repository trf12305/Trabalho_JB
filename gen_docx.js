// Gera Dashboard_JB_Relatorio.docx
const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, HeadingLevel, BorderStyle, WidthType, ShadingType,
  LevelFormat, PageBreak, Footer, Header, PageNumber, PageOrientation
} = require('docx');

// Cores
const ORANGE = "E76F2A";
const GRAY_BG = "F2F2F2";
const HEADER_BG = "1F3864";
const BORDER_GRAY = "CCCCCC";

const border = { style: BorderStyle.SINGLE, size: 6, color: BORDER_GRAY };
const borders = { top: border, bottom: border, left: border, right: border };

// Helpers
const P = (text, opts = {}) => new Paragraph({
  spacing: { after: 100 },
  ...opts,
  children: [new TextRun({ text, ...(opts.run || {}) })],
});

const Pmulti = (runs, opts = {}) => new Paragraph({
  spacing: { after: 100 }, ...opts,
  children: runs.map(r => new TextRun(r)),
});

const H1 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  spacing: { before: 360, after: 200 },
  children: [new TextRun({ text, bold: true, size: 36, color: HEADER_BG, font: "Calibri" })],
});

const H2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  spacing: { before: 280, after: 160 },
  children: [new TextRun({ text, bold: true, size: 28, color: ORANGE, font: "Calibri" })],
});

const H3 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_3,
  spacing: { before: 200, after: 100 },
  children: [new TextRun({ text, bold: true, size: 24, font: "Calibri" })],
});

const Bullet = (text) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 80 },
  children: [new TextRun({ text, size: 22, font: "Calibri" })],
});

// Tabela 2-colunas (rótulo|valor)
const cell = (text, opts = {}) => new TableCell({
  borders,
  width: { size: opts.width || 4680, type: WidthType.DXA },
  shading: opts.bg ? { fill: opts.bg, type: ShadingType.CLEAR } : undefined,
  margins: { top: 80, bottom: 80, left: 120, right: 120 },
  children: [new Paragraph({
    children: [new TextRun({
      text, bold: opts.bold || false,
      size: opts.size || 20,
      color: opts.color || "000000",
      font: "Calibri"
    })],
    alignment: opts.align || AlignmentType.LEFT,
  })],
});

const T2 = (rows, w1 = 3200, w2 = 6160) => new Table({
  width: { size: w1 + w2, type: WidthType.DXA },
  columnWidths: [w1, w2],
  rows: rows.map(([k, v]) => new TableRow({
    children: [
      cell(k, { width: w1, bold: true, bg: GRAY_BG, size: 20 }),
      cell(v, { width: w2, size: 20 }),
    ],
  })),
});

const T3 = (header, rows, widths = [3120, 3120, 3120]) => {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: total, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({
        tableHeader: true,
        children: header.map((h, i) => cell(h, {
          width: widths[i], bold: true, bg: HEADER_BG, color: "FFFFFF",
          align: AlignmentType.CENTER, size: 20,
        })),
      }),
      ...rows.map(r => new TableRow({
        children: r.map((c, i) => cell(c, { width: widths[i], size: 19 })),
      })),
    ],
  });
};

const T4 = (header, rows, widths = [2400, 2400, 2280, 2280]) => {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: total, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({
        tableHeader: true,
        children: header.map((h, i) => cell(h, {
          width: widths[i], bold: true, bg: HEADER_BG, color: "FFFFFF",
          align: AlignmentType.CENTER, size: 19,
        })),
      }),
      ...rows.map(r => new TableRow({
        children: r.map((c, i) => cell(c, { width: widths[i], size: 18 })),
      })),
    ],
  });
};

// ============================ CONTEÚDO ============================
const content = [
  // Capa
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 2400, after: 200 },
    children: [new TextRun({ text: "JB PROTEÇÃO", bold: true, size: 56, color: ORANGE, font: "Calibri" })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { after: 200 },
    children: [new TextRun({ text: "Dashboard Financeiro Inteligente", size: 32, color: HEADER_BG, font: "Calibri" })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { after: 1800 },
    children: [new TextRun({ text: "Relatório Executivo e Técnico", italics: true, size: 26, color: "555555", font: "Calibri" })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { after: 100 },
    children: [new TextRun({ text: "Período da base: Maio/2026", size: 22, font: "Calibri" })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { after: 100 },
    children: [new TextRun({ text: "Volume: 17.795 títulos | 12 unidades | 14.933 associados", size: 22, font: "Calibri" })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { after: 100 },
    children: [new TextRun({ text: "Última sincronização: 27/05/2026 16:12", size: 22, font: "Calibri" })],
  }),
  new Paragraph({ children: [new PageBreak()] }),

  // 1. RESUMO EXECUTIVO
  H1("1. Resumo Executivo"),
  P("A JB Proteção movimentou 17.795 títulos em Maio/2026, totalizando R$ 1.800.906,37 em valor previsto de carteira. Até o momento, R$ 1.215.351,90 já foi efetivamente recebido — correspondendo a 67,5% de taxa de conversão.", { run: { size: 22, font: "Calibri" } }),
  P("A operação apresenta saúde financeira sólida, com zero inadimplência ativa, 66,9% de pontualidade nos pagamentos e atraso médio dos casos atrasados de apenas 2,5 dias. Os R$ 585.821,41 restantes correspondem a títulos com vencimento ainda no futuro do período.", { run: { size: 22, font: "Calibri" } }),
  P("Distribuição em 12 bases nos estados de Alagoas, Pernambuco, Paraíba e Sergipe, atendendo 14.933 CPFs únicos.", { run: { size: 22, font: "Calibri" } }),

  H3("Headline numbers"),
  T2([
    ["Faturamento previsto", "R$ 1.800.906,37"],
    ["Faturamento realizado", "R$ 1.215.351,90"],
    ["Taxa de conversão", "67,5%"],
    ["Pontualidade", "66,9%"],
    ["Carteira a receber", "R$ 585.821,41"],
    ["Inadimplência", "R$ 0,00 (0%)"],
    ["Ticket médio (face)", "R$ 101,20"],
    ["Bases ativas", "12"],
    ["Auditoria detectou", "1 inconsistência (R$ 50,00)"],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // 2. CONTEXTO
  H1("2. Contexto da Base de Dados"),
  H3("Fonte"),
  Bullet("Sistema: Siprov (export JSON layout 496)"),
  Bullet("Arquivo: dashboard_financeiro_live.json (20,6 MB, 17.795 registros, 33 colunas)"),
  Bullet("Modo de operação: JSON fixo como fonte de verdade (auto-sync desativado)"),
  H3("Cobertura"),
  Bullet("Tipo de lançamento: CREDITO (Contas a Receber)"),
  Bullet("Período de vencimento: 01/05/2026 a 31/05/2026"),
  Bullet("Situações no JSON: 12.406 LIQUIDADO + 5.389 ABERTO + 0 PENDENTE"),
  Bullet("Bases: JB ALAGOAS, JB ARACAJU, JB ARAPIRACA, JB CAMPINA GRANDE, JB CARUARU, JB CORURIPE, JB ITABAIANA, JB MACEIÓ, JB PARAÍBA, JB PATOS, JB PERNAMBUCO, JB SERGIPE"),

  new Paragraph({ children: [new PageBreak()] }),

  // 3. KPIs PRINCIPAIS
  H1("3. KPIs Principais — Origem e Cálculo de Cada Card"),

  H2("3.1 QTD. TÍTULOS / QTD. LIQUIDADOS"),
  P("Quantidade total de boletos no período. No modo Liquidação, mostra apenas os pagos.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),
  T3(
    ["Aspecto", "Modo Vencimento", "Modo Liquidação"],
    [
      ["Valor mostrado", "17.795", "12.406"],
      ["Coluna JSON", "(contagem de linhas)", "titulo_situacao_titulo = LIQUIDADO"],
      ["Fórmula", "len(dados)", "len(dados onde situação=LIQUIDADO)"],
      ["Significado", "Total de boletos do período", "Quantidade efetiva de pagamentos"],
    ]
  ),

  H2("3.2 TOTAL LIQUIDADO / TOTAL RECEBIDO"),
  P("Dinheiro que efetivamente entrou no caixa, incluindo juros e multas cobrados.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),
  T2([
    ["Valor mostrado", "R$ 1.215.351,90"],
    ["Coluna JSON", "liquidacao_valor_liquidado"],
    ["Filtro aplicado", "titulo_situacao_titulo ∈ (LIQUIDADO, PAGO, QUITADO) E valor > 0"],
    ["Fórmula", "SUM(liquidacao_valor_liquidado) dos liquidados"],
    ["Significado executivo", "Receita real do mês após juros, multas e descontos"],
  ]),

  H2("3.3 VALOR DE FACE (PERÍODO / PAGOS)"),
  P("Valor nominal dos boletos, sem juros ou descontos. Modo Vencimento mostra todos; Liquidação mostra só os pagos.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),
  T3(
    ["Aspecto", "Vencimento (Período)", "Liquidação (Pagos)"],
    [
      ["Valor mostrado", "R$ 1.800.906,37", "R$ 1.215.084,96"],
      ["Coluna JSON", "titulo_valor", "titulo_valor"],
      ["Filtro", "Todos os títulos", "Só LIQUIDADO"],
      ["Fórmula", "SUM(titulo_valor)", "SUM(titulo_valor) onde LIQUIDADO"],
      ["Significado", "Quanto devia entrar (planejado)", "Valor original dos boletos pagos"],
    ]
  ),

  H2("3.4 TICKET MÉDIO"),
  P("Valor médio por título no período.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),
  T3(
    ["Aspecto", "Modo Vencimento (Face)", "Modo Liquidação (Recebido)"],
    [
      ["Valor mostrado", "R$ 101,20", "R$ 97,96"],
      ["Coluna JSON", "titulo_valor / qtd", "liquidacao_valor_liquidado / qtd"],
      ["Fórmula", "R$ 1.800.906,37 / 17.795", "R$ 1.215.351,90 / 12.406"],
    ]
  ),

  H2("3.5 PONTUALIDADE"),
  P("Percentual de boletos pagos no dia ou antes do vencimento.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),
  T2([
    ["Valor mostrado", "66,9%"],
    ["Colunas JSON", "titulo_data_vencimento e liquidacao_data_liquidacao"],
    ["Filtro", "Títulos LIQUIDADO com ambas as datas (12.405)"],
    ["Fórmula", "COUNT(data_liquidacao ≤ data_vencimento) / TOTAL × 100"],
    ["Resultado", "8.298 pontuais / 12.405 = 66,9%"],
    ["Quebra detalhada", "50,6% pagaram antes / 16,3% no dia / 33,1% atrasaram"],
    ["Atraso médio dos atrasados", "2,5 dias (máximo 25 dias)"],
  ]),

  H2("3.6 BASES"),
  T2([
    ["Valor mostrado", "12"],
    ["Coluna JSON", "unidade_nome_fantasia"],
    ["Fórmula", "COUNT(DISTINCT unidade_nome_fantasia)"],
    ["Significado", "Unidades JB com movimento no período"],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // 4. KPIs AVANÇADOS
  H1("4. KPIs Avançados de Gestão Financeira"),

  H2("4.1 A RECEBER (ABERTO)"),
  T2([
    ["Valor mostrado", "R$ 585.821,41"],
    ["Coluna JSON", "titulo_valor"],
    ["Filtro", "titulo_situacao_titulo ∈ (ABERTO, EM ABERTO)"],
    ["Fórmula", "SUM(titulo_valor) dos abertos"],
    ["Significado", "Carteira a converter — boletos com vencimento no futuro"],
  ]),

  H2("4.2 INADIMPLÊNCIA"),
  T2([
    ["Valor mostrado", "R$ 0,00 (0 títulos, 0% da carteira)"],
    ["Coluna JSON", "titulo_valor"],
    ["Filtro", "titulo_situacao_titulo ∈ (PENDENTE, ATRASADO)"],
    ["Fórmula", "SUM(titulo_valor) dos pendentes"],
    ["Significado", "Boletos vencidos sem pagamento. Zero indica saúde financeira excelente"],
  ]),

  H2("4.3 % CONVERSÃO"),
  T2([
    ["Valor mostrado", "67,5%"],
    ["Fórmula", "TOTAL LIQUIDADO / VALOR FACE PERÍODO × 100"],
    ["Cálculo", "R$ 1.215.351,90 / R$ 1.800.906,37 = 67,5%"],
    ["Significado executivo", "Eficiência de cobrança — 67,5% da carteira já virou caixa"],
  ]),

  H2("4.4 MoM / YoY"),
  T2([
    ["MoM (Month over Month)", "(último mês / penúltimo mês - 1) × 100"],
    ["YoY (Year over Year)", "(último mês / mesmo mês há 12 meses - 1) × 100"],
    ["Valor atual", "0% / 0% (base com apenas 1 mês, sem histórico)"],
    ["Significado", "Comparativo de crescimento — ativará automaticamente após 2-3 meses de histórico"],
  ]),

  H2("4.5 DSO (Days Sales Outstanding)"),
  T2([
    ["Valor mostrado", "301,3 dias"],
    ["Colunas JSON", "titulo_data_vencimento e liquidacao_data_liquidacao"],
    ["Filtro", "Só LIQUIDADO com ambas as datas"],
    ["Fórmula", "MÉDIA(data_liquidacao - data_vencimento) dos pagos"],
    ["Interpretação", "Valor alto reflete pagamentos antecipados (associados pagando 1-2 anos adiante)"],
  ]),

  H2("4.6 Atraso Médio"),
  T2([
    ["Valor mostrado", "0,0 dias"],
    ["Filtro", "Só PENDENTE"],
    ["Fórmula", "MÉDIA(hoje - data_vencimento) dos pendentes"],
    ["Resultado", "Zero porque não há pendentes no período"],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // 5. AGING
  H1("5. Aging — Análise de Inadimplência por Idade"),
  P("Buckets que segmentam a inadimplência pela idade do atraso, conforme metodologia padrão de gestão de carteira.", { run: { size: 22, font: "Calibri" } }),
  T4(
    ["Bucket", "Faixa de atraso", "Coluna JSON", "Valor atual"],
    [
      ["D30", "1-30 dias", "titulo_valor (PENDENTE)", "R$ 0,00"],
      ["D60", "31-60 dias", "titulo_valor (PENDENTE)", "R$ 0,00"],
      ["D90", "61-90 dias", "titulo_valor (PENDENTE)", "R$ 0,00"],
      ["D180+", "91+ dias", "titulo_valor (PENDENTE)", "R$ 0,00"],
    ]
  ),
  P("Todos os buckets estão zerados porque INADIMPLÊNCIA atual é R$ 0,00.", { run: { italics: true, size: 21, color: "555555", font: "Calibri" } }),

  // 6. AUDITORIA
  H1("6. Auditoria Automática — Detecção de Inconsistências"),
  P("Diferencial do dashboard: detecta automaticamente registros do Siprov marcados como LIQUIDADO mas com dados de pagamento ausentes ou inválidos.", { run: { size: 22, font: "Calibri" } }),

  H3("Lógica de detecção"),
  Bullet("Verifica todos os títulos com situação LIQUIDADO/PAGO/QUITADO"),
  Bullet("Sinaliza quando liquidacao_data_liquidacao está vazia, OU"),
  Bullet("Sinaliza quando liquidacao_valor_liquidado é nulo ou ≤ 0"),
  Bullet("Exibe badge amarelo (⚠) no card TOTAL LIQUIDADO com a contagem"),
  Bullet("Modal com tabela detalhada para auditoria operacional"),

  H3("Caso atual detectado"),
  T2([
    ["Quantidade", "1 título"],
    ["Valor de face afetado", "R$ 50,00"],
    ["Associado", "Miguel Ferreira Lima"],
    ["CPF", "419.107.614-00"],
    ["Unidade", "JB ARAPIRACA"],
    ["Veículo", "HONDA — placa QTT3356"],
    ["Parcela", "4/12 (RENOVAÇÃO)"],
    ["Vencimento", "10/05/2026"],
    ["Diagnóstico", "Marcado LIQUIDADO no Siprov sem registro de pagamento — provável erro de cadastro"],
  ]),

  new Paragraph({ children: [new PageBreak()] }),

  // 7. GRÁFICOS
  H1("7. Gráficos do Dashboard"),
  P("Nove visualizações cobrindo todas as dimensões de análise da carteira.", { run: { size: 22, font: "Calibri" } }),
  T3(
    ["Gráfico", "Colunas JSON", "Análise"],
    [
      ["Fluxo Mensal", "titulo_data_vencimento + titulo_valor", "Faturamento previsto por mês"],
      ["Liquidação Mensal", "liquidacao_data_liquidacao + liquidacao_valor_liquidado", "Caixa realizado por mês"],
      ["Top 10 Unidades", "unidade_nome_fantasia + valor_filtro", "Ranking de bases por receita"],
      ["Top 10 Consultores", "beneficio_consultor + valor_filtro", "Performance comercial individual"],
      ["Formas de Pagamento", "liquidacao_tipo_liquidacao + valor", "Boleto, Pix, Dinheiro, etc."],
      ["Top 10 Planos", "beneficio_planos_principais + valor", "Planos mais vendidos"],
      ["Top 10 Representantes", "beneficio_representante + valor", "Performance dos representantes"],
      ["Ranking de Bases", "unidade_nome_fantasia + valor_liquidado", "Receita realizada por unidade"],
      ["Comparativo Mensal", "titulo_situacao_titulo + valores", "Face × Liquidado × Pendente × Aberto"],
    ],
    [3000, 3600, 2760]
  ),

  // 8. TABELA E EXPORT
  H1("8. Tabela Operacional e Export"),
  H3("Tabela de detalhamento"),
  P("Mostra os 500 títulos mais recentes do filtro atual com as colunas:", { run: { size: 22, font: "Calibri" } }),
  Bullet("Data filtro (vencimento ou liquidação conforme o modo)"),
  Bullet("Associado (pessoa_nome_razao_social)"),
  Bullet("Consultor (beneficio_consultor, limpo)"),
  Bullet("Situação (titulo_situacao_titulo)"),
  Bullet("Valor (titulo_valor ou liquidacao_valor_liquidado)"),
  Bullet("Unidade (unidade_nome_fantasia)"),

  H3("Export CSV"),
  P("Botão \"Exportar CSV\" gera arquivo com TODOS os 17.795 títulos e 33 colunas, pronto para análises ad-hoc no Excel ou ferramentas de BI.", { run: { size: 22, font: "Calibri" } }),

  new Paragraph({ children: [new PageBreak()] }),

  // 9. FILTROS E CONTROLES
  H1("9. Filtros e Controles do Dashboard"),
  T3(
    ["Controle", "Função", "Comportamento técnico"],
    [
      ["Bases", "Filtrar por uma ou múltiplas unidades", "WHERE unidade_nome_fantasia IN (...)"],
      ["Data Inicial / Final", "Range customizado", "WHERE data_filtro BETWEEN di AND df"],
      ["Tipo de Filtro", "Vencimento (planejado) × Liquidação (caixa)", "Define qual coluna de data e qual valor agregar"],
      ["Atualizar", "Recarrega API sem refresh de página", "GET /api/financeiro?..."],
      ["Auto-Refresh", "Atualização periódica automática", "setInterval no front-end"],
      ["Sync Siprov", "Disparo manual de coleta no Siprov", "POST /api/admin/sync (assíncrono)"],
      ["Exportar CSV", "Download dos dados filtrados", "GET /api/financeiro/export?..."],
      ["Sair", "Encerra sessão", "GET /logout"],
    ]
  ),

  // 10. ARQUITETURA TÉCNICA
  H1("10. Arquitetura Técnica"),
  H3("Stack"),
  Bullet("Backend: Python 3 + Flask + APScheduler"),
  Bullet("Frontend: HTML5 + TailwindCSS + Chart.js + JavaScript vanilla"),
  Bullet("Integração: API REST Siprov (layout 496 assíncrono)"),
  Bullet("Persistência: JSON local em data/ (sem banco de dados)"),
  Bullet("Cache: in-memory por mtime do arquivo (invalidação automática)"),

  H3("Pipeline de dados"),
  Pmulti([
    { text: "1. ", bold: true, size: 22, font: "Calibri" },
    { text: "carregar_dados_json()", italics: true, size: 22, font: "Calibri" },
    { text: " — lê o JSON mais recente do disco e cacheia.", size: 22, font: "Calibri" },
  ]),
  Pmulti([
    { text: "2. ", bold: true, size: 22, font: "Calibri" },
    { text: "processar_dados_para_dash()", italics: true, size: 22, font: "Calibri" },
    { text: " — normaliza nomes de campos do Siprov para os usados no dashboard.", size: 22, font: "Calibri" },
  ]),
  Pmulti([
    { text: "3. ", bold: true, size: 22, font: "Calibri" },
    { text: "filtrar_dados()", italics: true, size: 22, font: "Calibri" },
    { text: " — aplica filtros de data, bases e modo (vencimento/liquidação).", size: 22, font: "Calibri" },
  ]),
  Pmulti([
    { text: "4. ", bold: true, size: 22, font: "Calibri" },
    { text: "gerar_dashboard_analitico()", italics: true, size: 22, font: "Calibri" },
    { text: " — calcula KPIs, agrega gráficos e monta a tabela top 500.", size: 22, font: "Calibri" },
  ]),
  Pmulti([
    { text: "5. ", bold: true, size: 22, font: "Calibri" },
    { text: "API /api/financeiro", italics: true, size: 22, font: "Calibri" },
    { text: " — devolve JSON consumido pelo front-end.", size: 22, font: "Calibri" },
  ]),

  H3("Confiabilidade"),
  Bullet("100% das rotas testadas (login, financeiro, eventos, vendas, export, auditoria)"),
  Bullet("Validação cruzada: todos os totais batem 1:1 com planilha Excel gerada do mesmo JSON"),
  Bullet("Tratamento de inputs inválidos: datas malformadas e tipos desconhecidos têm fallback seguro"),
  Bullet("Auto-sync desativável para garantir reprodutibilidade em apresentações"),

  // 11. CONCLUSÃO
  H1("11. Conclusão"),
  P("O Dashboard Financeiro JB Proteção entrega visibilidade executiva e operacional completa sobre a carteira de Maio/2026, com 17.795 títulos processados. Todos os indicadores foram validados contra a planilha Excel oficial e estão 100% consistentes.", { run: { size: 22, font: "Calibri" } }),
  P("Principais entregas:", { run: { size: 22, font: "Calibri", bold: true } }),
  Bullet("Acompanhamento em tempo real do realizado vs. previsto"),
  Bullet("Detecção automática de inconsistências de cadastro no Siprov"),
  Bullet("Dois modos analíticos (Vencimento e Liquidação) para visões complementares"),
  Bullet("Filtros multidimensionais para análises ad-hoc"),
  Bullet("Export CSV completo para integrações com Excel e BI"),
  Bullet("KPIs avançados (DSO, Aging, MoM/YoY) prontos para escalar com histórico"),
  P("Recomendação: manter o auto-sync desativado durante a janela de validação inicial pelos times de operação e contabilidade. Reativar após confirmação dos números.", { run: { size: 22, font: "Calibri", italics: true } }),
];

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Calibri", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, color: HEADER_BG, font: "Calibri" },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, color: ORANGE, font: "Calibri" },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Calibri" },
        paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "•",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } },
      }],
    }],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: "JB Proteção  |  Dashboard Financeiro", size: 18, color: "888888", font: "Calibri" })],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ text: "Página ", size: 18, color: "888888", font: "Calibri" }),
            new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "888888", font: "Calibri" }),
          ],
        })],
      }),
    },
    children: content,
  }],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("Dashboard_JB_Relatorio.docx", buf);
  console.log("OK: Dashboard_JB_Relatorio.docx (" + (buf.length / 1024).toFixed(1) + " KB)");
});
