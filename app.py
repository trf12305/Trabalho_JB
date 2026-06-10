import csv
import io
import json
import logging
import os
import re
import glob
import threading
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()
from flask import (
    Flask, render_template, request, redirect,
    session, jsonify, make_response, url_for,
)
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import db as bancodados
import db_adesoes  # fonte ISOLADA do dashboard de Vendas (relatório de adesão)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('jb_protecao')

# =========================================================
# APP
# =========================================================

app = Flask(__name__)
CORS(app)

app.secret_key                = os.environ.get('SECRET_KEY', 'dev-fallback-CHANGE-IN-PROD')
app.permanent_session_lifetime = timedelta(hours=8)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Upload de JSON do Siprov pode chegar a ~30MB; permitimos até 50MB.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ADMIN_USER = os.environ.get('ADMIN_USER', 'marcone')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '3209')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://',
)

# =========================================================
# DECORADOR DE AUTENTICAÇÃO
# =========================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logado'):
            return redirect('/')
        return f(*args, **kwargs)
    return decorated

# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================

def to_float(valor):
    try:
        if valor is None:
            return 0.0
        if isinstance(valor, (int, float)):
            return float(valor)
        s = str(valor).strip().replace('R$', '').strip()
        if ',' in s:
            s = s.replace('.', '').replace(',', '.')
        return float(s)
    except Exception:
        return 0.0


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def extrair_nome(texto):
    if not texto:
        return ''
    m = re.match(r'^\d+[-\s]+(.+)', texto.strip())
    if m:
        return m.group(1).split(' | ')[0].strip().title()
    return texto.split(' | ')[0].strip().title()


# =========================================================
# CLASSIFICADORES — Tipo de Veículo, Faixa de Valor, Nível
# =========================================================

# Palavras-chave de motos (modelos Honda/Yamaha/Shineray)
_MOTO_KEYS = (
    'MOTO', 'POP', 'BIZ', 'CG ', 'CG-', 'CG1', 'CG6', 'BROS', 'XRE',
    'FACTOR', 'FAZER', 'CROSSER', 'PCX', 'NMAX', 'MT-03', 'SHINERAY',
    'CB ', 'CB2', 'CB3', 'FAN', 'NEO', 'FLUO', 'XTZ', 'SAHARA',
    'TORNADO', 'LANDER', 'TENERE', 'ADV', 'FZ ', 'FZ1', 'FZ2',
    'YAMAHA', 'HONDA', 'TWISTER', 'TITAN', 'START', 'SHI ', 'SH ',
    '50CC',
)


def classificar_tipo_veiculo(plano, categoria=''):
    """Classifica o tipo de veículo com base no plano + categoria do JSON.
    Retorna: MOTO, CARRO LEVE, CARRO SUV, CARRO DIESEL, ESPORTIVA,
    SCOOTER, TRAIL ou OUTROS."""
    p = (plano or '').upper().strip()
    c = (categoria or '').upper().strip()

    # 1) Pela string do plano (mais específico)
    if 'SCOOTER' in p:
        return 'SCOOTER'
    if 'TRAIL' in p:
        return 'TRAIL'
    if 'ESPORTIVA' in p or 'NAKED' in p:
        return 'ESPORTIVA'
    if 'DIESEL' in p:
        return 'CARRO DIESEL'
    if 'SUV' in p or 'CAMINHONETE' in p or 'PICKUP' in p:
        return 'CARRO SUV'
    if 'CARRO - LEVE' in p or 'CARROS DE' in p or 'CARRO -' in p \
            or p.startswith('CARRO') or p.startswith('CARROS') \
            or 'PASSEIO' in p:
        return 'CARRO LEVE'
    if any(k in p for k in _MOTO_KEYS):
        return 'MOTO'

    # 2) Fallback: usa veiculo_categoria do JSON
    if c == 'MOTOCICLETA':
        return 'MOTO'
    if c == 'LEVE':
        return 'CARRO LEVE'
    if c in ('UTILITARIO', 'UTILITÁRIO'):
        return 'CARRO SUV'
    if c == 'PICKUP':
        return 'CARRO SUV'

    return 'OUTROS'


# Buckets de valor do veículo (5 faixas)
_FAIXAS_VALOR = [
    (20_000,            'ATÉ R$ 20k'),
    (50_000,            'R$ 20k a 50k'),
    (100_000,           'R$ 50k a 100k'),
    (150_000,           'R$ 100k a 150k'),
    (float('inf'),      'ACIMA DE R$ 150k'),
]
_FAIXAS_ORDEM = [f[1] for f in _FAIXAS_VALOR] + ['SEM VALOR']


def classificar_faixa_valor(valor_veiculo):
    """Classifica o valor do veículo em faixas (buckets)."""
    v = float(valor_veiculo or 0)
    if v <= 0:
        return 'SEM VALOR'
    for limite, rotulo in _FAIXAS_VALOR:
        if v <= limite:
            return rotulo
    return 'ACIMA DE R$ 150k'


# Níveis de cobertura comercial dos planos
_NIVEIS_COBERTURA_ORDEM = [
    'PREMIUM', 'LIBERTY', 'ECONOMY', 'FACILITY',
    'BÁSICO', 'ROUBO E FURTO', 'SEM NÍVEL',
]


def classificar_nivel_cobertura(plano):
    """Identifica o nível de cobertura (PREMIUM/ECONOMY/LIBERTY/BÁSICO/etc).
    Caso o plano não contenha palavra-chave, retorna 'SEM NÍVEL'."""
    p = (plano or '').upper()
    if 'PREMIUM' in p:
        return 'PREMIUM'
    if 'LIBERTY' in p:
        return 'LIBERTY'
    if 'ECONOMY' in p:
        return 'ECONOMY'
    if 'FACILITY' in p:
        return 'FACILITY'
    if 'ROUBO E FURTO' in p or 'ROUBO/FURTO' in p:
        return 'ROUBO E FURTO'
    if 'BÁSICO' in p or 'BASICO' in p:
        return 'BÁSICO'
    return 'SEM NÍVEL'


# =========================================================
# SITUAÇÕES DE LIQUIDAÇÃO
# =========================================================

SITUACOES_LIQUIDACAO = frozenset({'LIQUIDADO', 'PAGO', 'QUITADO'})

# =========================================================
# CACHE EM MEMÓRIA
# =========================================================

_cache = {
    'arquivo':           None,
    'mtime':             None,
    'dados_brutos':      None,
    'dados_processados': None,
    'dados_eventos':     None,
    'dados_vendas':      None,
}


def _cache_invalido(arquivo):
    try:
        mtime = os.path.getmtime(arquivo)
    except OSError:
        return True
    if _cache['arquivo'] != arquivo or _cache['mtime'] != mtime:
        _cache['arquivo']           = arquivo
        _cache['mtime']             = mtime
        _cache['dados_brutos']      = None
        _cache['dados_processados'] = None
        _cache['dados_eventos']     = None
        _cache['dados_vendas']      = None
        return True
    return False


# =========================================================
# ARQUIVOS
# =========================================================

def _carregar_do_json_legado():
    """Fallback: lê o JSON em data/ (usado se o banco estiver vazio)."""
    padrao = os.path.join(BASE_DIR, 'data', 'dashboard_financeiro*.json')
    arquivos = glob.glob(padrao)
    if not arquivos:
        return []
    arquivo = max(arquivos, key=os.path.getmtime)
    with open(arquivo, 'r', encoding='utf-8') as f:
        return json.load(f)


def carregar_dados_json(anos=None):
    """
    Fonte de dados do dashboard: banco SQLite (db.py).

    Por padrão (anos=None) carrega APENAS o ANO CORRENTE — assim o histórico
    congelado de anos anteriores (ex.: 2025) permanece no banco para o YoY e
    para o filtro explícito 'Ano Passado', mas NÃO infla a visão padrão.
    Passe anos=[2025] (ou múltiplos) para carregar anos específicos.

    O cache é invalidado pela chave 'ultima_sync' + conjunto de anos.
    Se o banco estiver vazio, cai para o JSON legado e o importa.
    """
    if anos is None:
        anos = [datetime.now().year]
    anos = sorted({int(a) for a in anos})

    chave_sync = bancodados.meta_get('ultima_sync') or 'vazio'
    chave = f"{chave_sync}|anos={','.join(map(str, anos))}"
    if _cache['arquivo'] != chave:
        _cache['arquivo']           = chave
        _cache['dados_brutos']      = None
        _cache['dados_processados'] = None
        _cache['dados_eventos']     = None
        _cache['dados_vendas']      = None

    if _cache['dados_brutos'] is None:
        total = bancodados.contar()
        if total == 0:
            # Banco vazio → importa o JSON legado uma vez
            legado = _carregar_do_json_legado()
            if legado:
                logger.info(f'[DADOS] Banco vazio — importando {len(legado)} registros do JSON legado.')
                bancodados.substituir_periodo(legado)
            _cache['dados_brutos'] = legado
        else:
            registros = []
            for a in anos:
                registros.extend(bancodados.ler_titulos(ano=a))
            _cache['dados_brutos'] = registros
            logger.info(f'[DADOS] Carregados {len(registros)} registros (anos={anos}) do banco SQLite.')
    return _cache['dados_brutos']


# =========================================================
# DEDUPLICAÇÃO
# =========================================================

def _dedup_raw(dados):
    """
    Dedup desativado — o Siprov é a fonte de verdade e seus totais
    consideram TODOS os registros do export, incluindo casos que
    parecem duplicatas (mesmo título com 2 liquidações idênticas,
    pagamentos parciais, estornos+repagos, etc.). Aplicar dedup aqui
    fazia o dashboard divergir dos relatórios oficiais do Siprov.

    Mantida a função (em vez de removida) para preservar a interface
    com processar_dados_para_dash() e facilitar reativação futura
    caso seja necessário (basta restaurar a lógica anterior).
    """
    return dados


# =========================================================
# PROCESSAMENTO — FINANCEIRO
# =========================================================

def processar_dados_para_dash(dados_originais):
    dados_sem_dup = _dedup_raw(dados_originais)
    registros = []

    for item in dados_sem_dup:
        associado = (
            item.get('pessoa_nome_razao_social')
            or item.get('associado_nome')
            or item.get('beneficio_nome')
            or item.get('titulo_associado')
            or item.get('cliente_nome')
            or 'N/A'
        )

        consultor_raw     = item.get('beneficio_consultor')    or ''
        representante_raw = item.get('beneficio_representante') or ''

        registro = {
            'beneficio_sequencial': str(item.get('beneficio_sequencial') or '').strip(),
            'titulo_parcela':       str(item.get('titulo_parcela')        or '').strip(),
            'associado':  associado,
            'unidade':    (item.get('unidade_nome_fantasia') or 'SEM UNIDADE').strip(),
            'consultor':  extrair_nome(consultor_raw)    if consultor_raw    else 'SEM CONSULTOR',
            'representante': extrair_nome(representante_raw) if representante_raw else 'SEM REPRESENTANTE',
            'cidade':    (item.get('endereco_cidade') or '').strip(),
            'uf':        (item.get('endereco_uf')     or '').strip(),
            'plano':     (item.get('beneficio_planos_principais') or '').strip() or 'SEM PLANO',
            'categoria_veiculo': (item.get('veiculo_categoria') or '').strip().upper(),
            'valor_veiculo':     to_float(item.get('veiculo_valor_veiculo')),
            'tipo_titulo': (item.get('titulo_tipo_titulo') or 'N/A').strip(),
            'situacao':  (item.get('titulo_situacao_titulo') or '').strip().upper(),
            'data_vencimento': item.get('titulo_data_vencimento'),
            'data_liquidacao': item.get('liquidacao_data_liquidacao'),
            'data_adesao':     item.get('beneficio_data_adesao'),
            'valor_titulo':    to_float(item.get('titulo_valor')),
            'valor_liquidado': to_float(item.get('liquidacao_valor_liquidado')),
            'forma_pagamento': (item.get('liquidacao_tipo_liquidacao') or 'N/A').strip(),
        }
        registros.append(registro)

    return registros


# =========================================================
# FILTROS
# =========================================================

def filtrar_dados(dados, tipo='vencimento', data_inicial=None,data_final=None, bases=None):
    di = parse_date(data_inicial)
    df = parse_date(data_final)
    filtrado = []

    for item in dados:
        if tipo == 'vencimento':
            data_str = item.get('data_vencimento')
        else:
            # Modo liquidação: usa data_liquidacao quando existe; se ausente
            # em título LIQUIDADO (inconsistência Siprov, ex.: Miguel R$50),
            # cai para data_vencimento como fallback. Assim o valor entra
            # no total (consistente com o Excel) e o caso continua
            # sinalizado no badge de auditoria para correção operacional.
            data_str = item.get('data_liquidacao') or (
                item.get('data_vencimento')
                if item.get('situacao', '') in SITUACOES_LIQUIDACAO
                else None
            )
        if not data_str:
            continue
        data_ref = parse_date(data_str)
        if data_ref is None:
            continue
        if di and data_ref < di:
            continue
        if df and data_ref > df:
            continue
        if tipo == 'liquidacao':
            if item.get('situacao', '') not in SITUACOES_LIQUIDACAO:
                continue
        if bases and 'ALL' not in bases:
            if item.get('unidade') not in bases:
                continue

        novo = dict(item)
        novo['data_filtro']  = str(data_str)
        novo['valor_filtro'] = (
            item['valor_titulo']
            if tipo == 'vencimento'
            else item['valor_liquidado']
        )
        filtrado.append(novo)

    return filtrado


def _anos_do_filtro(data_inicial, data_final):
    """Decide QUAIS anos (de vencimento) carregar do banco a partir do filtro.

    - Sem filtro de data  -> apenas o ANO CORRENTE (visão padrão limpa; o
      histórico congelado de 2025 fica no banco só para o YoY e para quando
      o usuário pedir 'Ano Passado').
    - Com filtro          -> todos os anos abrangidos pelo intervalo pedido.

    Mantém os dados históricos disponíveis sob demanda sem inflar a visão
    padrão dos dashboards."""
    di = parse_date(data_inicial)
    df = parse_date(data_final)
    if not di and not df:
        return [datetime.now().year]
    anos = set()
    if di:
        anos.add(di.year)
    if df:
        anos.add(df.year)
    if di and df and df.year >= di.year:
        anos = set(range(di.year, df.year + 1))
    return sorted(anos) if anos else [datetime.now().year]


# =========================================================
# HELPERS DE AGREGAÇÃO
# =========================================================

def _top_por_valor(dados, campo, top=10, campo_valor='valor_filtro'):
    """Agrupa por `campo` e soma `campo_valor`. Por padrão usa 'valor_filtro'
    (que muda conforme o modo: titulo_valor em Vencimento, valor_liquidado em
    Liquidação). Passe campo_valor='valor_titulo' para fixar no FACE em ambos
    os modos."""
    agg = {}
    for d in dados:
        chave = (d.get(campo) or 'N/A').strip() or 'N/A'
        agg[chave] = agg.get(chave, 0) + to_float(d.get(campo_valor, 0))
    top_items = sorted(agg.items(), key=lambda x: x[1], reverse=True)[:top]
    return (
        
        [x[0] for x in top_items],
        [round(x[1], 2) for x in top_items],
    )


def _fluxo_mensal(dados, campo_data, campo_valor):
    fluxo = {}
    for d in dados:
        data_s = str(d.get(campo_data) or '')
        if len(data_s) < 7:
            continue
        mes = data_s[:7]
        fluxo[mes] = fluxo.get(mes, 0) + to_float(d.get(campo_valor, 0))
    labels = sorted(fluxo)
    valores = [round(fluxo[m], 2) for m in labels]
    return labels, valores


def _fluxo_mensal_contagem(dados, campo_data, campo_id):
    """Conta IDs distintos por mês (usado para contar contratos únicos
    no Fluxo de Vencimento via beneficio_sequencial)."""
    fluxo = {}
    for d in dados:
        data_s = str(d.get(campo_data) or '')
        if len(data_s) < 7:
            continue
        ident = d.get(campo_id)
        if not ident:
            continue
        mes = data_s[:7]
        fluxo.setdefault(mes, set()).add(str(ident).strip())
    labels = sorted(fluxo)
    valores = [len(fluxo[m]) for m in labels]
    return labels, valores


# =========================================================
# DASHBOARD FINANCEIRO ANALÍTICO
# =========================================================

def gerar_dashboard_analitico(dados, dados_completos, tipo_filtro='vencimento'):
    # Total de títulos = contagem direta de linhas (1 título = 1 registro),
    # batendo 1:1 com a contagem do Excel/JSON exportado do Siprov.
    # Antes deduplicava por beneficio_sequencial, o que subcontava títulos
    # quando o mesmo associado tinha múltiplas parcelas no mesmo período.
    total_registros = len(dados)

    total_face = round(
        sum(
            to_float(d.get('valor_titulo', 0))
            for d in dados
            if to_float(d.get('valor_titulo', 0)) > 0
        ), 2,
    )
    # TOTAL LIQUIDADO = SUM(titulo_valor) de TODOS os títulos com
    # situação LIQUIDADO/PAGO/QUITADO, inclusive os sem data_liquidacao
    # (Miguel R$50). Esses casos continuam aparecendo no badge de auditoria
    # para correção operacional, mas o valor entra no total — assim o
    # dashboard bate com o Excel oficial e o operacional decide o que fazer.
    total_liquidado = round(
        sum(
            to_float(d.get('valor_titulo', 0))
            for d in dados
            if d.get('situacao', '') in SITUACOES_LIQUIDACAO
            and to_float(d.get('valor_titulo', 0)) > 0
        ), 2,
    )

    base_ticket  = total_liquidado if tipo_filtro == 'liquidacao' else total_face
    ticket_medio = round(
        (base_ticket / total_registros) if total_registros > 0 else 0.0, 2,
    )

    com_ambas = [
        d for d in dados
        if parse_date(d.get('data_vencimento')) and parse_date(d.get('data_liquidacao'))
    ]
    pontuais = sum(
        1 for d in com_ambas
        if parse_date(d['data_liquidacao']) <= parse_date(d['data_vencimento'])
    )
    pct_pontualidade = round(
        (pontuais / len(com_ambas) * 100) if com_ambas else 0.0, 1
    )

    bases_no_periodo = len({
        d.get('unidade', '').strip()
        for d in dados
        if d.get('unidade')
    })

    bases_lista = sorted({
        d.get('unidade', '').strip()
        for d in dados_completos
        if d.get('unidade')
    })

    # Fluxo de Vencimento agora conta CONTRATOS ÚNICOS por mês
    # (beneficio_sequencial DISTINCT), não soma de valores.
    lbl_venc, val_venc = _fluxo_mensal_contagem(dados, 'data_vencimento', 'beneficio_sequencial')

    dados_pagos = [
        d for d in dados
        if d.get('situacao', '') in SITUACOES_LIQUIDACAO
        and d.get('data_liquidacao')
        and to_float(d.get('valor_liquidado', 0)) > 0
    ]
    # Fluxo de Recebimento: agora soma titulo_valor (FACE) dos pagos.
    # Antes usava liquidacao_valor_liquidado (caixa com juros).
    lbl_liq, val_liq = _fluxo_mensal(dados_pagos, 'data_liquidacao', 'valor_titulo')
    # Top Unidades, Consultores e Formas de Pagamento: fixados em
    # titulo_valor (FACE) — mesmo valor em Vencimento e Liquidação.
    lbl_und,   val_und   = _top_por_valor(dados,       'unidade',         top=10, campo_valor='valor_titulo')
    lbl_cons,  val_cons  = _top_por_valor(dados,       'consultor',       top=10, campo_valor='valor_titulo')
    lbl_fp,    val_fp    = _top_por_valor(dados_pagos, 'forma_pagamento', top=10, campo_valor='valor_titulo')
    lbl_rep,   val_rep   = _top_por_valor(dados,       'representante',   top=10, campo_valor='valor_titulo')

    # =====================================================
    # NOVOS GRÁFICOS — Tipo de Veículo / Faixa de Valor /
    # Nível de Cobertura (substitui Top Planos)
    # =====================================================

    # Top Planos por NÍVEL DE COBERTURA (PREMIUM/ECONOMY/LIBERTY/BÁSICO...)
    nivel_agg = {}
    for d in dados:
        nv = classificar_nivel_cobertura(d.get('plano'))
        nivel_agg[nv] = nivel_agg.get(nv, 0) + to_float(d.get('valor_titulo', 0))
    lbl_plano = [k for k in _NIVEIS_COBERTURA_ORDEM if k in nivel_agg]
    val_plano = [round(nivel_agg[k], 2) for k in lbl_plano]

    # Tipo de Veículo (MOTO / CARRO LEVE / CARRO SUV / DIESEL / ESPORTIVA / SCOOTER / TRAIL)
    tipo_v_agg = {}
    for d in dados:
        tv = classificar_tipo_veiculo(d.get('plano'), d.get('categoria_veiculo'))
        tipo_v_agg[tv] = tipo_v_agg.get(tv, 0) + to_float(d.get('valor_titulo', 0))
    tipo_v_ord = sorted(tipo_v_agg.items(), key=lambda x: x[1], reverse=True)
    lbl_tipo_v = [k for k, _ in tipo_v_ord]
    val_tipo_v = [round(v, 2) for _, v in tipo_v_ord]

    # Faixa de Valor do Veículo (5 buckets + SEM VALOR)
    faixa_agg = {}
    for d in dados:
        fx = classificar_faixa_valor(d.get('valor_veiculo'))
        faixa_agg[fx] = faixa_agg.get(fx, 0) + to_float(d.get('valor_titulo', 0))
    lbl_faixa = [k for k in _FAIXAS_ORDEM if k in faixa_agg]
    val_faixa = [round(faixa_agg[k], 2) for k in lbl_faixa]

    dados_ord = sorted(dados, key=lambda x: x.get('data_filtro') or '', reverse=True)
    tabela = [
        {
            'data_filtro':  d['data_filtro'],
            'associado':    d.get('associado'),
            'consultor':    d.get('consultor'),
            'situacao':     d.get('situacao'),
            'valor_filtro': to_float(d.get('valor_filtro', 0)),
            'unidade':      d.get('unidade'),
        }
        for d in dados_ord[:500]
    ]

    # =====================================================
    # KPIs AVANÇADOS — Fórmulas de gestão financeira
    # =====================================================
    from datetime import date as _date_today
    hoje_dt = _date_today.today()

    # Separar por situação
    SIT_ABERTO    = frozenset({'ABERTO', 'EM ABERTO'})
    SIT_PENDENTE  = frozenset({'PENDENTE', 'ATRASADO'})
    a_receber  = 0.0   # Aberto: ainda vai vencer
    inadimpl   = 0.0   # Pendente: venceu sem pagar
    qtd_inadim = 0
    aging_30 = 0.0
    aging_60 = 0.0
    aging_90 = 0.0
    aging_180 = 0.0  # acima de 90
    soma_atraso_dias = 0
    n_atrasados = 0
    soma_dso = 0
    n_dso = 0
    for d in dados:
        sit = (d.get('situacao') or '').strip().upper()
        valor = to_float(d.get('valor_titulo', 0))
        if sit in SIT_ABERTO:
            a_receber += valor
        elif sit in SIT_PENDENTE:
            inadimpl += valor
            qtd_inadim += 1
            dv = parse_date(d.get('data_vencimento'))
            if dv:
                dias_atraso = (hoje_dt - dv).days
                if dias_atraso > 0:
                    soma_atraso_dias += dias_atraso
                    n_atrasados += 1
                    if   dias_atraso <= 30:  aging_30  += valor
                    elif dias_atraso <= 60:  aging_60  += valor
                    elif dias_atraso <= 90:  aging_90  += valor
                    else:                    aging_180 += valor
        # DSO: dias entre emissão e liquidação (só dos pagos)
        if sit in SITUACOES_LIQUIDACAO:
            de = parse_date(d.get('data_adesao'))  # proxy quando não tem emissão
            dl = parse_date(d.get('data_liquidacao'))
            if de and dl and dl >= de:
                dias = (dl - de).days
                if 0 <= dias <= 365 * 2:  # corte sanidade
                    soma_dso += dias
                    n_dso += 1
    pct_conversao = round(
        (total_liquidado / total_face * 100) if total_face > 0 else 0.0, 1
    )
    pct_inadim = round(
        (inadimpl / total_face * 100) if total_face > 0 else 0.0, 1
    )
    atraso_medio = round(soma_atraso_dias / n_atrasados, 1) if n_atrasados else 0.0
    dso = round(soma_dso / n_dso, 1) if n_dso else 0.0
    # Receita por base (ranking)
    receita_por_base = {}
    for d in dados:
        u = (d.get('unidade') or 'SEM UNIDADE').strip()
        receita_por_base[u] = receita_por_base.get(u, 0) + to_float(d.get('valor_liquidado', 0))
    rank_bases = sorted(receita_por_base.items(), key=lambda x: x[1], reverse=True)
    lbl_rank_bases  = [b for b, _ in rank_bases]
    val_rank_bases  = [round(v, 2) for _, v in rank_bases]
    # Comparativo mensal — TODAS as séries usam titulo_valor (FACE)
    # pra ficar consistente com o restante do dashboard. Liquidado
    # exige data_liquidacao preenchida (exclui inconsistências).
    comparativo_mensal = {}
    for d in dados:
        mes = (d.get('data_vencimento') or '')[:7]
        if not mes:
            continue
        if mes not in comparativo_mensal:
            comparativo_mensal[mes] = {'face': 0, 'liquidado': 0, 'pendente': 0, 'aberto': 0}
        sit = (d.get('situacao') or '').strip().upper()
        valor = to_float(d.get('valor_titulo', 0))
        comparativo_mensal[mes]['face'] += valor
        if sit in SITUACOES_LIQUIDACAO:
            comparativo_mensal[mes]['liquidado'] += valor
        elif sit in SIT_PENDENTE:
            comparativo_mensal[mes]['pendente'] += valor
        elif sit in SIT_ABERTO:
            comparativo_mensal[mes]['aberto'] += valor
    cm_labels = sorted(comparativo_mensal.keys())
    cm_face      = [round(comparativo_mensal[m]['face'], 2)      for m in cm_labels]
    cm_liquidado = [round(comparativo_mensal[m]['liquidado'], 2) for m in cm_labels]
    cm_pendente  = [round(comparativo_mensal[m]['pendente'], 2)  for m in cm_labels]
    cm_aberto    = [round(comparativo_mensal[m]['aberto'], 2)    for m in cm_labels]
    # MoM (último vs penúltimo mês)
    mom = 0.0
    if len(cm_labels) >= 2 and cm_liquidado[-2] > 0:
        mom = round((cm_liquidado[-1] / cm_liquidado[-2] - 1) * 100, 1)

    # YoY: comparar último mês com mesmo mês 12 meses atrás
    yoy = 0.0
    if len(cm_labels) >= 13 and cm_liquidado[-13] > 0:
        yoy = round((cm_liquidado[-1] / cm_liquidado[-13] - 1) * 100, 1)

    # =====================================================
    # AUDITORIA — Inconsistências do Siprov
    # =====================================================
    # Títulos marcados LIQUIDADO mas sem dados de liquidação
    # (data_liquidacao OU valor_liquidado ausentes). Sinaliza
    # bugs de cadastro no Siprov para auditoria operacional.
    # Audita TODOS os processados (dados_completos), não apenas os filtrados,
    # para que o badge continue sinalizando o Miguel mesmo em modo Liquidação
    # (onde ele é excluído do filtro por falta de data_liquidacao).
    inconsistencias_lista = []
    for d in dados_completos:
        if d.get('situacao', '') in SITUACOES_LIQUIDACAO:
            if not d.get('data_liquidacao') or to_float(d.get('valor_liquidado', 0)) <= 0:
                inconsistencias_lista.append({
                    'beneficio_sequencial': d.get('beneficio_sequencial', ''),
                    'associado':            d.get('associado', ''),
                    'parcela':              d.get('titulo_parcela', ''),
                    'unidade':              d.get('unidade', ''),
                    'valor_titulo':         d.get('valor_titulo', 0),
                    'data_vencimento':      d.get('data_vencimento'),
                    'tem_data_liq':         bool(d.get('data_liquidacao')),
                    'tem_valor_liq':        to_float(d.get('valor_liquidado', 0)) > 0,
                })
    qtd_inconsistencias = len(inconsistencias_lista)
    valor_inconsistencias = round(
        sum(to_float(x['valor_titulo']) for x in inconsistencias_lista), 2
    )

    return {
        'cards': {
            'registros':    total_registros,
            'liquidado':    total_liquidado,
            'total':        total_face,
            'ticket':       ticket_medio,
            'bases':        bases_no_periodo,
            'pontualidade': pct_pontualidade,
            # ── KPIs avançados ──
            'a_receber':    round(a_receber, 2),
            'inadimplencia': round(inadimpl, 2),
            'qtd_inadim':   qtd_inadim,
            'pct_inadim':   pct_inadim,
            'pct_conversao': pct_conversao,
            'atraso_medio_dias': atraso_medio,
            'dso':          dso,
            'mom':          mom,
            'yoy':          yoy,
            # ── Auditoria de inconsistências ──
            'qtd_inconsistencias':    qtd_inconsistencias,
            'valor_inconsistencias':  valor_inconsistencias,
        },
        'inconsistencias': inconsistencias_lista[:50],  # primeiros 50 p/ tooltip/modal
        'aging': {
            'd30':  round(aging_30, 2),
            'd60':  round(aging_60, 2),
            'd90':  round(aging_90, 2),
            'd180': round(aging_180, 2),
        },
        'bases_lista': bases_lista,
        'graficos': {
            'fluxo':            {'labels': lbl_venc,  'valores': val_venc},
            'liquidacao':       {'labels': lbl_liq,   'valores': val_liq},
            'unidades':         {'labels': lbl_und,   'valores': val_und},
            'consultores':      {'labels': lbl_cons,  'valores': val_cons},
            'formas_pagamento': {'labels': lbl_fp,    'valores': val_fp},
            'planos':           {'labels': lbl_plano, 'valores': val_plano},
            'representantes':   {'labels': lbl_rep,   'valores': val_rep},
            'tipos_veiculo':    {'labels': lbl_tipo_v, 'valores': val_tipo_v},
            'faixa_valor':      {'labels': lbl_faixa,  'valores': val_faixa},
            'rank_bases':       {'labels': lbl_rank_bases,  'valores': val_rank_bases},
            'comparativo':      {
                'labels':    cm_labels,
                'face':      cm_face,
                'liquidado': cm_liquidado,
                'pendente':  cm_pendente,
                'aberto':    cm_aberto,
            },
        },
        'tabela': tabela,
    }


# =========================================================
# API — FINANCEIRO
# =========================================================

@app.route('/api/financeiro')
@login_required
def api_financeiro():
    try:
        tipo         = request.args.get('tipo', 'vencimento')
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')

        if tipo not in ('vencimento', 'liquidacao'):
            tipo = 'vencimento'
        if data_inicial and not parse_date(data_inicial):
            data_inicial = None
        if data_final and not parse_date(data_final):
            data_final = None

        anos = _anos_do_filtro(data_inicial, data_final)

        logger.info(f'[API/FIN] tipo={tipo} | {data_inicial} -> {data_final} | anos={anos} | bases={bases}')

        dados_brutos = carregar_dados_json(anos)

        if _cache['dados_processados'] is None:
            _cache['dados_processados'] = processar_dados_para_dash(dados_brutos)

        dados_processados = _cache['dados_processados']
        dados_filtrados   = filtrar_dados(
            dados_processados, tipo, data_inicial, data_final, bases
        )

        logger.info(f'[API/FIN] processados={len(dados_processados)} filtrados={len(dados_filtrados)}')

        dashboard = gerar_dashboard_analitico(dados_filtrados, dados_processados, tipo)
        return jsonify(dashboard)

    except Exception:
        logger.exception('[ERRO] api_financeiro')
        return jsonify({'erro': 'Falha ao processar os dados financeiros.'}), 500


# =========================================================
# YoY — Comparativo Year over Year
# =========================================================
# Compara o mesmo mês de dois anos consecutivos.
# Lê DIRETO do banco SQLite (não passa pelo cache de JSON),
# pois pode haver dados do ano anterior CONGELADOS no banco
# que não estão no JSON da sync atual.

def _resumo_mes_banco(ano, mes, situacoes_liq):
    """Retorna métricas agregadas (face, liquidado, qtd) para um (ano, mes)
    direto do banco SQLite. Não usa o cache em memória."""
    titulos = bancodados.ler_titulos(ano=ano, mes=mes)
    if not titulos:
        return None
    face = 0.0
    liquidado_face = 0.0
    qtd_total = 0
    qtd_liquidados = 0
    for t in titulos:
        qtd_total += 1
        v = to_float(t.get('titulo_valor'))
        face += v
        sit = (t.get('titulo_situacao_titulo') or '').strip().upper()
        if sit in situacoes_liq:
            liquidado_face += v
            qtd_liquidados += 1
    return {
        'ano': ano,
        'mes': mes,
        'face': round(face, 2),
        'liquidado': round(liquidado_face, 2),
        'qtd_total': qtd_total,
        'qtd_liquidados': qtd_liquidados,
    }


def _variacao_pct(atual, anterior):
    if anterior is None or anterior == 0:
        return None
    return round(((atual - anterior) / anterior) * 100, 1)


@app.route('/api/financeiro/yoy')
@login_required
def api_financeiro_yoy():
    """Comparativo YoY (Year over Year) — mesmo mês de dois anos consecutivos.

    Parâmetros:
      ano: ano atual (default = ano corrente)
      mes: mês a comparar (default = mês corrente, 1-12)

    Retorna:
      atual    — métricas do (ano, mes)
      anterior — métricas do (ano-1, mes)
      variacao_pct — % de variação atual vs anterior
      tem_anterior — True se há dados do ano anterior no banco
    """
    try:
        hoje = datetime.now()
        try:
            ano = int(request.args.get('ano') or hoje.year)
            mes = int(request.args.get('mes') or hoje.month)
        except (ValueError, TypeError):
            return jsonify({'erro': 'Parâmetros ano/mes inválidos.'}), 400

        if not (1 <= mes <= 12):
            return jsonify({'erro': 'Mês deve ser entre 1 e 12.'}), 400

        sits_liq = SITUACOES_LIQUIDACAO
        atual    = _resumo_mes_banco(ano,     mes, sits_liq)
        anterior = _resumo_mes_banco(ano - 1, mes, sits_liq)

        variacao = {}
        if atual and anterior:
            variacao = {
                'face':           _variacao_pct(atual['face'],           anterior['face']),
                'liquidado':      _variacao_pct(atual['liquidado'],      anterior['liquidado']),
                'qtd_total':      _variacao_pct(atual['qtd_total'],      anterior['qtd_total']),
                'qtd_liquidados': _variacao_pct(atual['qtd_liquidados'], anterior['qtd_liquidados']),
            }

        logger.info(f'[API/YoY] {mes:02d}/{ano} vs {mes:02d}/{ano-1} | '
                    f'atual={"OK" if atual else "vazio"} '
                    f'anterior={"OK" if anterior else "vazio"}')

        return jsonify({
            'atual':         atual,
            'anterior':      anterior,
            'variacao_pct':  variacao,
            'tem_atual':     bool(atual),
            'tem_anterior':  bool(anterior),
            'mes_nome': ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio',
                         'Junho', 'Julho', 'Agosto', 'Setembro', 'Outubro',
                         'Novembro', 'Dezembro'][mes],
        })
    except Exception:
        logger.exception('[ERRO] api_financeiro_yoy')
        return jsonify({'erro': 'Falha ao calcular YoY.'}), 500


@app.route('/api/financeiro/export')
@login_required
def api_financeiro_export():
    try:
        tipo         = request.args.get('tipo', 'vencimento')
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')

        if tipo not in ('vencimento', 'liquidacao'):
            tipo = 'vencimento'

        anos = _anos_do_filtro(data_inicial, data_final)
        dados_brutos = carregar_dados_json(anos)
        if _cache['dados_processados'] is None:
            _cache['dados_processados'] = processar_dados_para_dash(dados_brutos)

        dados_filtrados = filtrar_dados(
            _cache['dados_processados'], tipo, data_inicial, data_final, bases
        )
        dados_ord = sorted(dados_filtrados, key=lambda x: x.get('data_filtro') or '', reverse=True)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Data Ref.', 'Associado', 'Consultor', 'Situacao', 'Unidade', 'Valor'])
        for d in dados_ord:
            writer.writerow([
                d.get('data_filtro', ''),
                d.get('associado', ''),
                d.get('consultor', ''),
                d.get('situacao', ''),
                d.get('unidade', ''),
                to_float(d.get('valor_filtro', 0)),
            ])

        response = make_response('﻿' + output.getvalue())
        response.headers['Content-Disposition'] = 'attachment; filename=financeiro_export.csv'
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        logger.info(f'[EXPORT/FIN] {len(dados_ord)} linhas exportadas')
        return response

    except Exception:
        logger.exception('[ERRO] api_financeiro_export')
        return jsonify({'erro': 'Falha ao exportar.'}), 500


# =========================================================
# PROCESSAMENTO — VENDAS  (fonte: tabela `adesoes`, isolada do financeiro)
# =========================================================

# Cache próprio do Vendas — separado do _cache do financeiro.
_cache_adesoes = {'chave': None, 'brutos': None, 'processados': None}


def carregar_adesoes(anos=None):
    """Carrega adesões da tabela própria `adesoes` (db_adesoes). Fonte do
    dashboard de Vendas, TOTALMENTE separada do financeiro. Por padrão carrega
    o ano corrente; o cache é invalidado pela última sync de adesões + anos."""
    if anos is None:
        anos = [datetime.now().year]
    anos = sorted({int(a) for a in anos})
    chave = f"{db_adesoes.ultima_sync() or 'vazio'}|anos={','.join(map(str, anos))}"
    if _cache_adesoes['chave'] != chave:
        _cache_adesoes['chave'] = chave
        registros = []
        for a in anos:
            registros.extend(db_adesoes.ler(ano=a))
        _cache_adesoes['brutos'] = registros
        _cache_adesoes['processados'] = None
        logger.info(f'[ADESOES] Carregadas {len(registros)} adesões (anos={anos}).')
    return _cache_adesoes['brutos']


def processar_dados_vendas(adesoes_brutas):
    """Normaliza registros de ADESÃO (relatório Siprov 1393 'dashboard de venda')
    para o dashboard de Vendas. Cada registro = uma adesão (uma venda).
    Fonte ISOLADA do financeiro — vem da tabela `adesoes`."""
    registros = []
    for item in adesoes_brutas:
        registros.append({
            'beneficio_sequencial': str(item.get('beneficio_sequencial') or '').strip(),
            'cliente':       (item.get('associado_nome_razao_social') or 'N/A').strip(),
            'unidade':       (item.get('unidade_nome_fantasia') or item.get('unidade_razao_social') or 'SEM UNIDADE').strip(),
            'consultor':     (item.get('beneficio_nome_consultor') or '').strip() or 'SEM CONSULTOR',
            'representante': (item.get('beneficio_representante') or '').strip() or 'SEM REPRESENTANTE',
            'situacao':      (item.get('beneficio_situacao_atual') or 'N/A').strip() or 'N/A',
            'data_adesao':   str(item.get('beneficio_data_adesao') or ''),
            'mensalidade':   to_float(item.get('beneficio_valor_mensalidade')),
            'valor_veiculo': to_float(item.get('veiculo_valor_veiculo')),
            'adicionais':    to_float(item.get('beneficio_planos_adicionais_valor')),
            'placa':             (item.get('veiculo_placa_veiculo') or '').strip(),
            'veiculo_categoria': (item.get('veiculo_categoria_veiculo') or 'Outros').strip() or 'Outros',
            'veiculo_marca':     (item.get('veiculo_marca_veiculo') or '').strip(),
            'veiculo_tipo':      (item.get('veiculo_tipo_veiculo') or '').strip(),
            'uf':                (item.get('endereco_uf_padrao') or '').strip(),
            'cidade':            (item.get('endereco_cidade_padrao') or '').strip().title(),
            'idade':             item.get('associado_idade'),
            'sexo':              (item.get('associado_sexo') or '').strip().upper(),
        })
    return registros


def filtrar_dados_vendas(dados, data_inicial=None, data_final=None, bases=None):
    """Filtra adesões por DATA DE ADESÃO (data da venda) e por unidade/base."""
    di = parse_date(data_inicial)
    df = parse_date(data_final)
    filtrado = []

    for item in dados:
        data_ref = parse_date(item.get('data_adesao'))
        if data_ref is None:
            continue
        if di and data_ref < di:
            continue
        if df and data_ref > df:
            continue
        if bases and 'ALL' not in bases:
            if item.get('unidade') not in bases:
                continue
        filtrado.append(item)

    return filtrado


# =========================================================
# DASHBOARD VENDAS ANALÍTICO
# =========================================================

def gerar_dashboard_vendas(dados, dados_completos):
    # Cada adesão = uma venda. Conta TODAS as adesões do período.
    total_vendas = len(dados)

    total_mensalidade = round(sum(to_float(d.get('mensalidade', 0)) for d in dados), 2)
    total_veiculos    = round(sum(to_float(d.get('valor_veiculo', 0)) for d in dados), 2)

    ticket_medio = round(
        (total_mensalidade / total_vendas) if total_vendas > 0 else 0.0, 2
    )

    regionais = len({d.get('unidade', '').strip() for d in dados if d.get('unidade')})

    total_adicionais = round(sum(to_float(d.get('adicionais', 0)) for d in dados), 2)

    n_cancelados = sum(1 for d in dados if 'CANCEL' in (d.get('situacao') or '').upper())
    taxa_cancelamento = round((n_cancelados / total_vendas * 100) if total_vendas > 0 else 0.0, 1)

    def _top_count(registros, campo, top=10):
        cnt = {}
        for d in registros:
            k = (d.get(campo) or 'N/A').strip() or 'N/A'
            cnt[k] = cnt.get(k, 0) + 1
        items = sorted(cnt.items(), key=lambda x: x[1], reverse=True)[:top]
        return [x[0] for x in items], [x[1] for x in items]

    # Evolução: nº de adesões por mês (data de adesão)
    evolucao = {}
    for d in dados:
        mes = (d.get('data_adesao') or '')[:7]
        if len(mes) == 7:
            evolucao[mes] = evolucao.get(mes, 0) + 1
    lbl_evol = sorted(evolucao)
    val_evol = [evolucao[m] for m in lbl_evol]

    # Situação atual da adesão (Ativo / Cancelado / ...)
    sit_cnt = {}
    for d in dados:
        s = (d.get('situacao') or 'OUTROS').strip().upper() or 'OUTROS'
        sit_cnt[s] = sit_cnt.get(s, 0) + 1
    lbl_sit = list(sit_cnt.keys())
    val_sit = list(sit_cnt.values())

    lbl_cons, val_cons = _top_count(dados, 'consultor',         top=10)
    lbl_rep,  val_rep  = _top_count(dados, 'representante',     top=10)
    lbl_reg,  val_reg  = _top_count(dados, 'unidade',           top=10)
    lbl_cat,  val_cat  = _top_count(dados, 'veiculo_categoria', top=8)
    lbl_uf,   val_uf   = _top_count(dados, 'uf',                top=12)
    lbl_cidade, val_cidade = _top_count(dados, 'cidade',        top=10)
    lbl_marca, val_marca = _top_count(dados, 'veiculo_marca',   top=10)

    # Carro × Moto — derivado de veiculo_categoria (campo limpo):
    # MOTOCICLETA -> Moto; LEVE/UTILITARIO/PESADO -> Carro.
    tipo_cnt = {'Carro': 0, 'Moto': 0, 'Outros': 0}
    for d in dados:
        cat = (d.get('veiculo_categoria') or '').upper()
        if 'MOTO' in cat:
            tipo_cnt['Moto'] += 1
        elif any(k in cat for k in ('LEVE', 'UTIL', 'PESAD', 'CARRO', 'CAMINH')):
            tipo_cnt['Carro'] += 1
        else:
            tipo_cnt['Outros'] += 1
    lbl_tipo = [k for k in ('Carro', 'Moto', 'Outros') if tipo_cnt[k] > 0]
    val_tipo = [tipo_cnt[k] for k in lbl_tipo]

    # Perfil — faixa etária
    faixas_idade = ['18-24', '25-34', '35-44', '45-54', '55+', 'N/A']
    idade_cnt = {f: 0 for f in faixas_idade}
    for d in dados:
        try:
            i = int(d.get('idade'))
        except (TypeError, ValueError):
            idade_cnt['N/A'] += 1
            continue
        if   i < 25: idade_cnt['18-24'] += 1
        elif i < 35: idade_cnt['25-34'] += 1
        elif i < 45: idade_cnt['35-44'] += 1
        elif i < 55: idade_cnt['45-54'] += 1
        else:        idade_cnt['55+']   += 1
    lbl_idade = [f for f in faixas_idade if idade_cnt[f] > 0]
    val_idade = [idade_cnt[f] for f in lbl_idade]

    # Perfil — sexo
    sexo_map = {'M': 'Masculino', 'F': 'Feminino'}
    sexo_cnt = {}
    for d in dados:
        s = sexo_map.get((d.get('sexo') or '').upper(), 'Outros')
        sexo_cnt[s] = sexo_cnt.get(s, 0) + 1
    lbl_sexo = list(sexo_cnt.keys())
    val_sexo = list(sexo_cnt.values())

    # Perfil — faixa de mensalidade
    faixas_mens = ['< 80', '80–100', '100–150', '150–200', '200+']
    mens_cnt = {f: 0 for f in faixas_mens}
    for d in dados:
        v = to_float(d.get('mensalidade', 0))
        if   v < 80:  mens_cnt['< 80']    += 1
        elif v < 100: mens_cnt['80–100']  += 1
        elif v < 150: mens_cnt['100–150'] += 1
        elif v < 200: mens_cnt['150–200'] += 1
        else:         mens_cnt['200+']    += 1
    lbl_mens = [f for f in faixas_mens if mens_cnt[f] > 0]
    val_mens = [mens_cnt[f] for f in lbl_mens]

    bases_lista = sorted({
        d.get('unidade', '').strip()
        for d in dados_completos
        if d.get('unidade')
    })

    dados_ord = sorted(dados, key=lambda x: x.get('data_adesao') or '', reverse=True)
    tabela = [
        {
            # Mantém as chaves 'data_emissao' e 'valor_titulo' que o JS já lê,
            # preenchidas com a semântica de adesão (data da venda / mensalidade).
            'data_emissao': d.get('data_adesao'),
            'cliente':      d.get('cliente'),
            'consultor':    d.get('consultor'),
            'placa':        d.get('placa'),
            'unidade':      d.get('unidade'),
            'situacao':     d.get('situacao'),
            'valor_titulo': to_float(d.get('mensalidade', 0)),
        }
        for d in dados_ord[:500]
    ]

    return {
        'cards': {
            'total_vendas':    total_vendas,
            'valor_liquidado': total_mensalidade,   # rótulo: Mensalidades (R$)
            'carteira_total':  total_veiculos,       # rótulo: Valor Segurado (Veículos)
            'ticket_medio':    ticket_medio,         # rótulo: Ticket Médio (Mensalidade)
            'regionais':       regionais,
            'adicionais':      total_adicionais,     # rótulo: Adicionais (R$)
            'taxa_cancelamento': taxa_cancelamento,  # rótulo: Cancelamento (%)
        },
        'graficos': {
            'evolucao':       {'labels': lbl_evol,  'valores': val_evol},
            'situacao':       {'labels': lbl_sit,   'valores': val_sit},
            'consultores':    {'labels': lbl_cons,  'valores': val_cons},
            'representantes': {'labels': lbl_rep,   'valores': val_rep},
            'por_regional':   {'labels': lbl_reg,   'valores': val_reg},
            'categorias':     {'labels': lbl_cat,   'valores': val_cat},
            'por_uf':         {'labels': lbl_uf,     'valores': val_uf},
            'por_cidade':     {'labels': lbl_cidade, 'valores': val_cidade},
            'por_marca':      {'labels': lbl_marca,  'valores': val_marca},
            'por_tipo':       {'labels': lbl_tipo,  'valores': val_tipo},
            'perfil_idade':   {'labels': lbl_idade, 'valores': val_idade},
            'perfil_sexo':    {'labels': lbl_sexo,  'valores': val_sexo},
            'perfil_mensalidade': {'labels': lbl_mens, 'valores': val_mens},
        },
        'tabela':      tabela,
        'bases_lista': bases_lista,
    }


# =========================================================
# API — VENDAS
# =========================================================

@app.route('/api/vendas')
@login_required
def api_vendas():
    try:
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')

        if data_inicial and not parse_date(data_inicial):
            data_inicial = None
        if data_final and not parse_date(data_final):
            data_final = None

        anos = _anos_do_filtro(data_inicial, data_final)

        logger.info(f'[API/VENDAS] {data_inicial} -> {data_final} | anos={anos} | bases={bases}')

        adesoes_brutas = carregar_adesoes(anos)

        if _cache_adesoes['processados'] is None:
            _cache_adesoes['processados'] = processar_dados_vendas(adesoes_brutas)

        dados_vendas    = _cache_adesoes['processados']
        dados_filtrados = filtrar_dados_vendas(dados_vendas, data_inicial, data_final, bases)

        logger.info(f'[API/VENDAS] total={len(dados_vendas)} filtrados={len(dados_filtrados)}')

        dashboard = gerar_dashboard_vendas(dados_filtrados, dados_vendas)
        return jsonify(dashboard)

    except Exception:
        logger.exception('[ERRO] api_vendas')
        return jsonify({'erro': 'Falha ao processar dados de vendas.'}), 500


@app.route('/api/vendas/export')
@login_required
def api_vendas_export():
    try:
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')

        anos = _anos_do_filtro(data_inicial, data_final)
        adesoes_brutas = carregar_adesoes(anos)
        if _cache_adesoes['processados'] is None:
            _cache_adesoes['processados'] = processar_dados_vendas(adesoes_brutas)

        dados_filtrados = filtrar_dados_vendas(
            _cache_adesoes['processados'], data_inicial, data_final, bases
        )
        dados_ord = sorted(dados_filtrados, key=lambda x: x.get('data_emissao') or '', reverse=True)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Data Emissao', 'Cliente', 'Consultor', 'Placa', 'Regional', 'Situacao', 'Valor'])
        for d in dados_ord:
            writer.writerow([
                d.get('data_emissao', ''),
                d.get('cliente', ''),
                d.get('consultor', ''),
                d.get('placa', ''),
                d.get('unidade', ''),
                d.get('situacao', ''),
                to_float(d.get('valor_titulo', 0)),
            ])

        response = make_response('﻿' + output.getvalue())
        response.headers['Content-Disposition'] = 'attachment; filename=vendas_export.csv'
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        logger.info(f'[EXPORT/VENDAS] {len(dados_ord)} linhas exportadas')
        return response

    except Exception:
        logger.exception('[ERRO] api_vendas_export')
        return jsonify({'erro': 'Falha ao exportar.'}), 500


# =========================================================
# PROCESSAMENTO — EVENTOS / ADESÕES
# =========================================================

def processar_dados_eventos(dados_originais):
    dados_sem_dup = _dedup_raw(dados_originais)
    registros = []

    for item in dados_sem_dup:
        data_venc = parse_date(item.get('titulo_data_vencimento'))
        data_liq  = parse_date(item.get('liquidacao_data_liquidacao'))

        tempo_resposta = None
        if data_venc and data_liq:
            tempo_resposta = (data_liq - data_venc).days

        registro = {
            'beneficio_sequencial': str(item.get('beneficio_sequencial') or '').strip(),
            'titulo_parcela':       str(item.get('titulo_parcela')        or '').strip(),
            'associado':     (item.get('pessoa_nome_razao_social') or 'N/A').strip(),
            'unidade':       (item.get('unidade_nome_fantasia')    or 'SEM UNIDADE').strip(),
            'consultor':     extrair_nome(item.get('beneficio_consultor')    or '') or 'SEM CONSULTOR',
            'representante': extrair_nome(item.get('beneficio_representante') or '') or 'SEM REPRESENTANTE',
            'uf':    (item.get('endereco_uf')     or '').strip(),
            'cidade':(item.get('endereco_cidade') or '').strip(),
            'plano':  (item.get('beneficio_planos_principais') or '').strip() or 'SEM PLANO',
            'situacao':(item.get('titulo_situacao_titulo') or '').strip().upper(),
            'data_adesao':     str(item.get('beneficio_data_adesao')      or ''),
            'data_vencimento': str(item.get('titulo_data_vencimento')     or ''),
            'data_liquidacao': str(item.get('liquidacao_data_liquidacao') or ''),
            'mensalidade':    to_float(item.get('beneficio_valor_mensalidade')),
            'valor_titulo':   to_float(item.get('titulo_valor')),
            'valor_liquidado':to_float(item.get('liquidacao_valor_liquidado')),
            'valor_veiculo':  to_float(item.get('veiculo_valor_veiculo')),
            'veiculo_marca':  (item.get('veiculo_marca_veiculo') or '').strip(),
            'veiculo_categoria': (item.get('veiculo_categoria') or '').strip(),
            'tempo_resposta': tempo_resposta,
            'pontual': tempo_resposta is not None and tempo_resposta <= 0,
        }
        registros.append(registro)

    return registros


# =========================================================
# DASHBOARD EVENTOS ANALÍTICO
# =========================================================

def gerar_dashboard_eventos(dados_todos, data_inicial=None, data_final=None, bases=None):
    if bases and 'ALL' not in bases:
        dados_base = [d for d in dados_todos if d.get('unidade') in bases]
    else:
        dados_base = dados_todos

    contratos_carteira = set()
    carteira_total = 0.0
    for d in dados_base:
        bid = d.get('beneficio_sequencial', '')
        if bid and bid not in contratos_carteira:
            contratos_carteira.add(bid)
            carteira_total += d.get('valor_veiculo', 0.0)

    base_ativa = len({
        d['beneficio_sequencial']
        for d in dados_base
        if d.get('beneficio_sequencial')
    })

    tempos = [
        d['tempo_resposta'] for d in dados_base
        if d.get('tempo_resposta') is not None
    ]
    tempo_medio    = round(sum(tempos) / len(tempos), 1) if tempos else 0.0
    total_eventos  = len(dados_base)
    total_pontuais = sum(1 for d in dados_base if d.get('pontual'))
    pct_pontual    = round(
        (total_pontuais / total_eventos * 100) if total_eventos else 0.0, 1
    )

    di = parse_date(data_inicial)
    df = parse_date(data_final)

    adesoes_ids   = set()
    adesoes_dados = []

    for d in dados_base:
        data_a = parse_date(d.get('data_adesao'))
        if not data_a:
            continue
        if di and data_a < di:
            continue
        if df and data_a > df:
            continue
        bid = d.get('beneficio_sequencial', '')
        if bid and bid not in adesoes_ids:
            adesoes_ids.add(bid)
            adesoes_dados.append(d)

    qtd_adesoes  = len(adesoes_ids)
    receita_nova = round(sum(d.get('mensalidade', 0.0) for d in adesoes_dados), 2)
    ticket_medio = round(
        (receita_nova / qtd_adesoes) if qtd_adesoes > 0 else 0.0, 2
    )

    hoje = datetime.now().date()

    if di and df:
        total_dias      = max((df - di).days + 1, 1)
        dias_decorridos = max((min(hoje, df) - di).days + 1, 0)
    elif di:
        total_dias      = max((hoje - di).days + 1, 1)
        dias_decorridos = total_dias
    else:
        import calendar
        _, ultimo_dia   = calendar.monthrange(hoje.year, hoje.month)
        total_dias      = ultimo_dia
        dias_decorridos = hoje.day

    ades_mes = {}
    for d in adesoes_dados:
        mes = d.get('data_adesao', '')[:7]
        if len(mes) == 7:
            ades_mes[mes] = ades_mes.get(mes, 0) + 1
    lbl_ades_mes = sorted(ades_mes)
    val_ades_mes = [ades_mes[m] for m in lbl_ades_mes]

    rec_mes = {}
    for d in adesoes_dados:
        mes = d.get('data_adesao', '')[:7]
        if len(mes) == 7:
            rec_mes[mes] = rec_mes.get(mes, 0.0) + d.get('mensalidade', 0.0)
    lbl_rec_mes = sorted(rec_mes)
    val_rec_mes = [round(rec_mes[m], 2) for m in lbl_rec_mes]

    def _top_count(registros, campo, top=10):
        cnt = {}
        for d in registros:
            k = (d.get(campo) or 'N/A').strip()
            cnt[k] = cnt.get(k, 0) + 1
        items = sorted(cnt.items(), key=lambda x: x[1], reverse=True)[:top]
        return [x[0] for x in items], [x[1] for x in items]

    lbl_und,   val_und   = _top_count(adesoes_dados, 'unidade',   top=10)
    lbl_cons,  val_cons  = _top_count(adesoes_dados, 'consultor', top=10)
    lbl_uf,    val_uf    = _top_count(adesoes_dados, 'uf',        top=10)
    lbl_plano, val_plano = _top_count(adesoes_dados, 'plano',     top=8)

    bases_lista = sorted({
        d.get('unidade', '').strip()
        for d in dados_todos
        if d.get('unidade')
    })

    tabela = [
        {
            'data_adesao': d.get('data_adesao'),
            'associado':   d.get('associado'),
            'unidade':     d.get('unidade'),
            'plano':       d.get('plano'),
            'uf':          d.get('uf'),
            'mensalidade': to_float(d.get('mensalidade', 0)),
        }
        for d in sorted(
            adesoes_dados,
            key=lambda x: x.get('data_adesao') or '',
            reverse=True
        )[:500]
    ]

    return {
        'ritmo': {
            'realizado':       qtd_adesoes,
            'dias_decorridos': dias_decorridos,
            'total_dias':      total_dias,
        },
        'cards': {
            'adesoes':      qtd_adesoes,
            'receita':      receita_nova,
            'carteira':     round(carteira_total, 2),
            'ticket_medio': ticket_medio,
            'base_ativa':   base_ativa,
        },
        'performance': {
            'tempo_medio':   tempo_medio,
            'pontualidade':  pct_pontual,
            'total_eventos': total_eventos,
        },
        'graficos': {
            'adesoes_mes':     {'labels': lbl_ades_mes, 'valores': val_ades_mes},
            'receita_mes':     {'labels': lbl_rec_mes,  'valores': val_rec_mes},
            'top_unidades':    {'labels': lbl_und,      'valores': val_und},
            'top_consultores': {'labels': lbl_cons,     'valores': val_cons},
            'por_uf':          {'labels': lbl_uf,       'valores': val_uf},
            'top_planos':      {'labels': lbl_plano,    'valores': val_plano},
        },
        'tabela':      tabela,
        'bases_lista': bases_lista,
    }


# =========================================================
# API — EVENTOS
# =========================================================

@app.route('/api/eventos')
@login_required
def api_eventos():
    try:
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')

        if data_inicial and not parse_date(data_inicial):
            data_inicial = None
        if data_final and not parse_date(data_final):
            data_final = None

        anos = _anos_do_filtro(data_inicial, data_final)

        logger.info(f'[API/EVENTOS] {data_inicial} -> {data_final} | anos={anos} | bases={bases}')

        dados_brutos = carregar_dados_json(anos)

        if _cache['dados_eventos'] is None:
            _cache['dados_eventos'] = processar_dados_eventos(dados_brutos)

        dados_eventos = _cache['dados_eventos']
        dashboard = gerar_dashboard_eventos(dados_eventos, data_inicial, data_final, bases)

        logger.info(
            f'[API/EVENTOS] eventos={len(dados_eventos)} '
            f'adesoes={dashboard["cards"]["adesoes"]}'
        )
        return jsonify(dashboard)

    except Exception:
        logger.exception('[ERRO] api_eventos')
        return jsonify({'erro': 'Falha ao processar dados de eventos.'}), 500
@app.route('/api/eventos/export')
@login_required
def api_eventos_export():
    try:
        data_inicial = request.args.get('data_inicial') or None
        data_final   = request.args.get('data_final')   or None
        bases        = request.args.getlist('bases')
        anos = _anos_do_filtro(data_inicial, data_final)
        dados_brutos = carregar_dados_json(anos)
        if _cache['dados_eventos'] is None:
            _cache['dados_eventos'] = processar_dados_eventos(dados_brutos)
        dashboard = gerar_dashboard_eventos(
            _cache['dados_eventos'], data_inicial, data_final, bases
        )
        tabela = dashboard.get('tabela', [])
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Data Adesao', 'Associado', 'Unidade', 'Plano', 'UF', 'Mensalidade'])
        for d in tabela:
            writer.writerow([
                d.get('data_adesao', ''),
                d.get('associado', ''),
                d.get('unidade', ''),
                d.get('plano', ''),
                d.get('uf', ''),
                to_float(d.get('mensalidade', 0)),
            ])
        response = make_response('﻿' + output.getvalue())
        response.headers['Content-Disposition'] = 'attachment; filename=eventos_export.csv'
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        logger.info(f'[EXPORT/EVENTOS] {len(tabela)} linhas exportadas')
        return response
    except Exception:
        logger.exception('[ERRO] api_eventos_export')
        return jsonify({'erro': 'Falha ao exportar.'}), 500
# =========================================================
# HEALTH CHECK
# =========================================================
@app.route('/health')
def health():
    return jsonify({
        'status':    'ok',
        'timestamp': datetime.now().isoformat(),
        'versao':    '2.0.0',
    })
# =========================================================
# LOGIN / ROTAS
# =========================================================
@app.route('/')
def home():
    if session.get('logado'):
        return redirect('/financeiro')
    return render_template('login.html')
@app.route('/login', methods=['POST'])
@limiter.limit('10 per minute')
def login():
    usuario = (request.form.get('usuario') or '').strip()
    senha   = (request.form.get('senha')   or '').strip()
    if usuario == ADMIN_USER and senha == ADMIN_PASS:
        session.permanent = True
        session['logado'] = True
        logger.info(f'[LOGIN] Acesso concedido: usuario={usuario!r}')
        return redirect('/financeiro')
    logger.warning(f'[LOGIN] Falha de autenticacao: usuario={usuario!r}')
    return redirect('/?erro=1')
@app.route('/financeiro')
@login_required
def financeiro():
    return render_template('dash_financeiro.html')
@app.route('/eventos')
@login_required
def eventos():
    return render_template('dash_eventos.html')
@app.route('/vendas')
@login_required
def vendas():
    return render_template('dash_vendas.html')
@app.route('/logout')
def logout():
    session.clear()
    logger.info('[LOGOUT] Sessao encerrada')
    return redirect('/')
# =========================================================
# SYNC SIPROV — scheduler automático
# =========================================================
_sync_lock = threading.Lock()
def _ha_internet(host='acesso.siprov.com.br', porta=443, timeout=5):
    """Testa se o Siprov é alcançável (DNS + conexão). Usado para o retry
    pós-boot, quando o Wi-Fi pode ainda não ter subido."""
    import socket
    try:
        socket.create_connection((host, porta), timeout=timeout).close()
        return True
    except OSError:
        return False
# Horários agendados da sync (America/Recife). Editável aqui.
# O chefe abre os dashboards às 08h e 18h → puxamos os relatórios 30min antes,
# para já estar tudo pronto quando ele abrir. Financeiro e adesão no MESMO horário.
SYNC_SLOTS = ((7, 30), (17, 30))  # 07:30 e 17:30
def _slot_ja_coberto(ultima_iso, slots=SYNC_SLOTS):
    """True se JÁ houve sync desde o último horário agendado (ex.: 07:30/17:30)
    que já passou hoje. Serve para NÃO re-sincronizar a cada restart/abertura do
    dashboard — só deixa rodar quando um slot novo chega (ou foi perdido com o
    note desligado, e aí ele roda assim que liga). Não afeta a sync manual."""
    if not ultima_iso:
        return False
    try:
        ult = datetime.fromisoformat(ultima_iso)
    except (ValueError, TypeError):
        return False
    agora = datetime.now()
    slot_dt = None
    for (h, m) in sorted(slots, reverse=True):
        cand = agora.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= agora:
            slot_dt = cand
            break
    if slot_dt is None:                       # antes do 1º slot do dia
        h, m = max(slots)
        slot_dt = (agora - timedelta(days=1)).replace(
            hour=h, minute=m, second=0, microsecond=0)
    return ult >= slot_dt
def _executar_sync(max_retries=10, retry_delay=120, forcar=False):
    """Executa a sync. Se falhar por REDE (sem internet, comum logo após o
    boot), aguarda e tenta de novo — até max_retries vezes, com retry_delay
    segundos entre tentativas. Falhas que não são de rede não fazem retry.

    forcar=False (agendador/startup): só roda se o slot 09h/17h ainda não foi
    coberto — evita re-baixar relatório a cada restart/abertura do dashboard.
    forcar=True (botão manual): roda sempre."""
    if not forcar and _slot_ja_coberto(bancodados.meta_get('ultima_sync')):
        logger.info('[SIPROV] Slot 09h/17h já coberto — sem re-sync (restart/abertura).')
        return
    if not _sync_lock.acquire(blocking=False):
        logger.info('[SIPROV] Sync ja em andamento, ignorando.')
        return
    import time as _time
    try:
        from siprov_sync import sincronizar
        for tentativa in range(1, max_retries + 1):
            # Espera a rede ficar disponível (Wi-Fi pode demorar a subir no boot)
            if not _ha_internet():
                if tentativa < max_retries:
                    logger.warning(
                        f'[SIPROV] Sem internet (tentativa {tentativa}/{max_retries}). '
                        f'Aguardando {retry_delay}s para tentar de novo...'
                    )
                    _time.sleep(retry_delay)
                    continue
                else:
                    logger.error('[SIPROV] Sem internet apos todas as tentativas. Sync abortada.')
                    return
            # Há internet → executa a sync
            try:
                sincronizar()
                _cache['dados_brutos']      = None
                _cache['dados_processados'] = None
                _cache['dados_eventos']     = None
                _cache['dados_vendas']      = None
                logger.info('[SIPROV] Cache invalidado apos sync.')
                return  # sucesso
            except Exception as e:
                # Erro de rede no meio da sync → retry; outros erros → aborta
                msg = str(e).lower()
                eh_rede = any(k in msg for k in (
                    'resolve', 'getaddrinfo', 'connection', 'timed out',
                    'timeout', 'max retries', 'network', 'unreachable',
                ))
                if eh_rede and tentativa < max_retries:
                    logger.warning(
                        f'[SIPROV] Falha de rede na sync (tentativa {tentativa}/{max_retries}): '
                        f'{str(e)[:120]}. Retry em {retry_delay}s...'
                    )
                    _time.sleep(retry_delay)
                    continue
                logger.exception('[SIPROV] Falha na sincronizacao automatica')
                return
    finally:
        _sync_lock.release()
def _congelar_ano_anterior():
    """Job anual (1º de Janeiro): congela o ano que acabou de fechar,
    protegendo-o de alterações futuras (histórico para YoY)."""
    ano_fechado = datetime.now().year - 1
    try:
        n = bancodados.congelar_ano(ano_fechado)
        logger.info(f'[DB] Congelamento anual: ano {ano_fechado} protegido ({n} títulos).')
    except Exception:
        logger.exception('[DB] Falha ao congelar ano anterior')
def _sync_adesoes(forcar=False):
    """Sync da fonte de Vendas (adesões). Separada do financeiro.
    Mesma guarda por slot: não re-sincroniza a cada restart/abertura."""
    if not forcar and _slot_ja_coberto(db_adesoes.ultima_sync()):
        logger.info('[ADESOES] Slot 09h/17h já coberto — sem re-sync.')
        return
    try:
        import siprov_adesao
        siprov_adesao.sincronizar()
        _cache_adesoes['chave'] = None  # invalida cache do Vendas
        logger.info('[ADESOES] Cache invalidado apos sync.')
    except Exception:
        logger.exception('[ADESOES] Falha na sync de adesoes')
# =========================================================
# FECHAMENTO MENSAL — geral, para TODOS os dashboards (atuais e futuros)
# =========================================================
# Cada fonte de dados registra aqui COMO buscar o mês final e COMO congelá-lo.
# Para cobrir um dashboard novo no futuro, basta adicionar uma entrada nesta
# lista — o fechamento mensal passa a arquivá-lo automaticamente.
def _fontes_arquivaveis():
    import siprov_adesao
    return [
        {
            'nome': 'financeiro/titulos',
            'sync_mes': lambda ano, mes: _executar_sync(),  # janela cobre o mês que fechou
            'congelar_mes': lambda ano, mes: bancodados.congelar_mes(ano, mes),
        },
        {
            'nome': 'vendas/adesoes',
            'sync_mes': lambda ano, mes: siprov_adesao.coletar_mes(ano, mes),
            'congelar_mes': lambda ano, mes: db_adesoes.congelar_mes(ano, mes),
        },
    ]
def _fechamento_mensal(ano=None, mes=None):
    """Roda no dia 1º de cada mês: para o mês que FECHOU, em TODAS as fontes:
       (1) busca os dados finais do mês no Siprov,
       (2) congela o mês (imutável — guardado só para pesquisa/YoY).
    Mês congelado fica fora da visão padrão (escopo por ano), aparecendo só
    em consultas históricas / 'Ano Passado'."""
    if ano is None or mes is None:
        hoje = datetime.now()
        if hoje.month == 1:
            ano, mes = hoje.year - 1, 12
        else:
            ano, mes = hoje.year, hoje.month - 1
    logger.info(f'[FECHAMENTO] === Fechando {mes:02d}/{ano} em todas as fontes ===')
    for fonte in _fontes_arquivaveis():
        nome = fonte['nome']
        # 1) Busca final do mês (best-effort — se falhar, ainda congela o que há).
        #    No modo JB_NO_SYNC só congela (não toca na API).
        try:
            if fonte.get('sync_mes') and os.environ.get('JB_NO_SYNC') != '1':
                logger.info(f'[FECHAMENTO] {nome}: buscando dados finais de {mes:02d}/{ano}…')
                fonte['sync_mes'](ano, mes)
        except Exception:
            logger.exception(f'[FECHAMENTO] {nome}: falha na busca final (vai congelar mesmo assim)')
        # 2) Congela (ação garantida)
        try:
            n = fonte['congelar_mes'](ano, mes)
            logger.info(f'[FECHAMENTO] {nome}: {mes:02d}/{ano} CONGELADO ({n} registros).')
        except Exception:
            logger.exception(f'[FECHAMENTO] {nome}: falha ao congelar')
    # Invalida todos os caches para refletir o congelamento
    _cache['dados_brutos'] = None
    _cache['dados_processados'] = None
    _cache['dados_eventos'] = None
    _cache['dados_vendas'] = None
    _cache_adesoes['chave'] = None
    logger.info(f'[FECHAMENTO] Concluído para {mes:02d}/{ano}.')


# Estado do agendador, exposto em /admin/debug para diagnosticar produção.
# Valores: 'nao_iniciado' | 'JB_NO_SYNC' | 'sem_credenciais' |
#          'sem_apscheduler' | 'ativo'
_SCHEDULER_STATUS = 'nao_iniciado'


def _iniciar_scheduler():
    global _SCHEDULER_STATUS
    # Garante que o banco existe antes de qualquer operação
    try:
        bancodados.init_db()
        db_adesoes.init_db()  # tabela isolada do dashboard de Vendas
    except Exception:
        logger.exception('[DB] Falha ao inicializar banco')

    # Interruptor: JB_NO_SYNC=1 roda o app SEM nenhuma sincronização com o
    # Siprov (sem scheduler 09h/18h e sem sync no startup). Serve só os dados
    # já existentes no banco. Útil para rodar offline/local sem tocar na API.
    if os.environ.get('JB_NO_SYNC') == '1':
        _SCHEDULER_STATUS = 'JB_NO_SYNC'
        logger.warning('[SIPROV] JB_NO_SYNC=1 — sync DESATIVADO (sem scheduler e sem startup sync).')
        return

    if not os.environ.get('SIPROV_USUARIO') or not os.environ.get('SIPROV_SENHA'):
        _SCHEDULER_STATUS = 'sem_credenciais'
        logger.warning('[SIPROV] Credenciais nao configuradas — sync automatico desativado.')
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import atexit
    except ImportError:
        _SCHEDULER_STATUS = 'sem_apscheduler'
        logger.warning('[SIPROV] APScheduler nao instalado — sync automatico desativado. Execute: pip install APScheduler')
        return

    scheduler = BackgroundScheduler(daemon=True, timezone='America/Recife')
    # misfire_grace_time=6h + coalesce: se o note estava DESLIGADO no horário
    # agendado (ex.: perdeu as 09:00), o job roda assim que o serviço subir,
    # desde que dentro de 6h. coalesce=True junta execuções perdidas em 1.
    _job_defaults = {'misfire_grace_time': 6 * 3600, 'coalesce': True}
    # Sync às 07:30 e 17:30 (America/Recife) — 30min antes do chefe abrir (08h/18h).
    # Financeiro E adesão no MESMO horário (cada um puxa só o mês corrente = 1
    # relatório, então rodar juntos não sobrecarrega a API).
    scheduler.add_job(_executar_sync, 'cron', hour='7,17', minute=30,
                      id='sync_horario', **_job_defaults)
    scheduler.add_job(_sync_adesoes, 'cron', hour='7,17', minute=30,
                      id='sync_adesoes', **_job_defaults)
    # FECHAMENTO MENSAL: 1º de cada mês às 03:00 — busca + congela o mês fechado
    # em TODAS as fontes (financeiro, vendas e futuras).
    scheduler.add_job(_fechamento_mensal, 'cron', day=1, hour=3, minute=0,
                      id='fechamento_mensal', **_job_defaults)
    # Congelamento anual: 1º de Janeiro às 02:00 — rede de segurança (YoY)
    scheduler.add_job(_congelar_ano_anterior, 'cron', month=1, day=1, hour=2, minute=0,
                      id='congelar_anual', **_job_defaults)
    scheduler.start()
    _SCHEDULER_STATUS = 'ativo'
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info('[SIPROV] Scheduler: financeiro+adesoes 07:30 e 17:30, '
                'fechamento mensal dia 1 03h, congelamento anual 01/Jan.')

    # Startup: tenta sincronizar UMA vez ao subir — mas a guarda por slot
    # interna (_slot_ja_coberto) faz NADA acontecer se o slot 09h/17h já foi
    # coberto. Assim: NÃO baixa relatório a cada restart/abertura do dashboard;
    # só roda se um horário foi perdido (note estava desligado).
    threading.Thread(target=_executar_sync, daemon=True).start()
    threading.Thread(target=_sync_adesoes, daemon=True).start()

# =========================================================
# AUTO-SYNC ATIVADO
# =========================================================
# Reativada: sync no startup (se dados desatualizados) +
# scheduler cron 09:00 e 18:00 (America/Recife).
# Para voltar ao modo JSON-only, comente a linha abaixo.
_iniciar_scheduler()

@app.route('/api/admin/sync', methods=['POST'])
@login_required
def api_admin_sync():
    if os.environ.get('JB_NO_SYNC') == '1':
        return jsonify({'status': 'sync desativado (JB_NO_SYNC=1)'}), 503
    # Dispara FINANCEIRO e ADESÕES juntos, em paralelo — mesmos dados que o
    # scheduler puxa às 07:30/17:30. Assim o botão manual atualiza as duas
    # fontes de uma vez (relatório financeiro + relatório de adesão no Siprov).
    threading.Thread(target=lambda: _executar_sync(forcar=True), daemon=True).start()
    threading.Thread(target=lambda: _sync_adesoes(forcar=True), daemon=True).start()
    return jsonify({'status': 'sync iniciado em background (financeiro + adesoes)'})


@app.route('/api/admin/sync/adesoes', methods=['POST'])
@login_required
def api_admin_sync_adesoes():
    """Dispara a sincronização de ADESÕES (fonte do Vendas) em background."""
    if os.environ.get('JB_NO_SYNC') == '1':
        return jsonify({'status': 'sync desativado (JB_NO_SYNC=1)'}), 503
    threading.Thread(target=lambda: _sync_adesoes(forcar=True), daemon=True).start()
    return jsonify({'status': 'sync de adesoes iniciado em background'})


@app.route('/api/admin/fechamento', methods=['POST'])
@login_required
def api_admin_fechamento():
    """Dispara o FECHAMENTO MENSAL manualmente (busca final + congela o mês
    em todas as fontes). Body opcional: {"ano": 2026, "mes": 5}. Sem body,
    fecha o mês ANTERIOR ao corrente."""
    ano = mes = None
    if request.is_json:
        ano = request.json.get('ano')
        mes = request.json.get('mes')
    try:
        ano = int(ano) if ano is not None else None
        mes = int(mes) if mes is not None else None
    except (TypeError, ValueError):
        return jsonify({'erro': 'ano/mes inválidos'}), 400
    threading.Thread(target=lambda: _fechamento_mensal(ano, mes), daemon=True).start()
    return jsonify({'status': 'fechamento mensal iniciado em background',
                    'ano': ano, 'mes': mes})
# =========================================================
# UPLOAD DE JSON — modo JSON-only (deploy Railway/produção)
# =========================================================
# Permite ao admin substituir o arquivo data/dashboard_financeiro_live.json
# diretamente via navegador, sem precisar de FTP/SSH/redeploy.
# Salva no diretório data/ e invalida o cache em memória.
@app.route('/admin/upload', methods=['GET'])
@login_required
def admin_upload_page():
    return render_template('admin_upload.html')

@app.route('/admin/debug', methods=['GET'])
@login_required
def admin_debug():
    """Diagnóstico do filesystem — útil pra verificar mount de volume no Railway."""
    import subprocess
    info = {
        'BASE_DIR': BASE_DIR,
        'cwd': os.getcwd(),
        '__file__': os.path.abspath(__file__),
        'FLASK_ENV': os.environ.get('FLASK_ENV'),
        'PORT': os.environ.get('PORT'),
        'RAILWAY_ENV': {k: v for k, v in os.environ.items() if 'RAILWAY' in k},
        # Diagnóstico do banco (mascarado por segurança)
        'banco_ativo': 'PostgreSQL' if bancodados.USE_POSTGRES else 'SQLite',
        'tem_DATABASE_URL': bool(os.environ.get('DATABASE_URL')),
        'database_url_prefixo': (os.environ.get('DATABASE_URL', '')[:25] + '...') if os.environ.get('DATABASE_URL') else None,
        'vars_banco': sorted([k for k in os.environ if any(
            t in k.upper() for t in ('DATABASE', 'PG', 'POSTGRES'))]),
    }
    # ---- Diagnóstico do SYNC automático (por que pega ou não pega) ----
    _usr = os.environ.get('SIPROV_USUARIO', '')
    info['sync'] = {
        'scheduler_status': _SCHEDULER_STATUS,   # ativo | sem_credenciais | JB_NO_SYNC | sem_apscheduler
        'JB_NO_SYNC': os.environ.get('JB_NO_SYNC'),
        'tem_SIPROV_USUARIO': bool(_usr),
        'SIPROV_USUARIO': (_usr[:3] + '***@' + _usr.split('@')[-1]) if '@' in _usr else ('***' if _usr else None),
        'tem_SIPROV_SENHA': bool(os.environ.get('SIPROV_SENHA')),
        'janela': {
            'SIPROV_DATA_INICIAL': os.environ.get('SIPROV_DATA_INICIAL'),
            'SIPROV_DATA_FINAL':   os.environ.get('SIPROV_DATA_FINAL'),
            'SIPROV_MESES_FUTUROS': os.environ.get('SIPROV_MESES_FUTUROS'),
            'SIPROV_MESES_BACK':    os.environ.get('SIPROV_MESES_BACK'),
            'SIPROV_SITUACOES':     os.environ.get('SIPROV_SITUACOES'),
            'SIPROV_TIPOS':         os.environ.get('SIPROV_TIPOS'),
        },
        'ultima_sync': bancodados.meta_get('ultima_sync'),
        'horarios_agendados': '07:30 e 17:30 (America/Recife)',
    }
    data_dir = os.path.join(BASE_DIR, 'data')
    info['data_dir'] = data_dir
    info['data_dir_exists'] = os.path.exists(data_dir)
    if info['data_dir_exists']:
        try:
            arquivos = []
            for f in os.listdir(data_dir):
                full = os.path.join(data_dir, f)
                arquivos.append({
                    'nome': f,
                    'tamanho_bytes': os.path.getsize(full) if os.path.isfile(full) else None,
                    'mtime': datetime.fromtimestamp(os.path.getmtime(full)).isoformat() if os.path.exists(full) else None,
                    'is_dir': os.path.isdir(full),
                })
            info['data_dir_contents'] = arquivos
        except Exception as e:
            info['data_dir_error'] = str(e)
    try:
        mounts = subprocess.check_output(['mount'], stderr=subprocess.STDOUT, timeout=3).decode()
        info['mounts_with_data'] = [l for l in mounts.split('\n') if '/data' in l or '/app' in l]
    except Exception as e:
        info['mounts_error'] = str(e)
    try:
        df = subprocess.check_output(['df', '-h', data_dir], stderr=subprocess.STDOUT, timeout=3).decode()
        info['df_data_dir'] = df.strip().split('\n')
    except Exception as e:
        info['df_error'] = str(e)
    return jsonify(info)

@app.route('/api/admin/upload-json', methods=['POST'])
@login_required
def api_admin_upload_json():
    if 'arquivo' not in request.files:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400

    arquivo = request.files['arquivo']
    if not arquivo or not arquivo.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Arquivo vazio.'}), 400

    if not arquivo.filename.lower().endswith('.json'):
        return jsonify({'status': 'erro', 'mensagem': 'O arquivo deve ter extensão .json'}), 400

    try:
        conteudo = arquivo.read()
        try:
            dados = json.loads(conteudo)
        except json.JSONDecodeError as e:
            return jsonify({
                'status': 'erro',
                'mensagem': f'JSON inválido: {str(e)[:200]}',
            }), 400

        if not isinstance(dados, list):
            return jsonify({
                'status': 'erro',
                'mensagem': 'O JSON deve ser uma lista de registros.',
            }), 400

        if len(dados) == 0:
            return jsonify({
                'status': 'erro',
                'mensagem': 'A lista está vazia.',
            }), 400

        primeiro = dados[0]
        campos_chave = ('titulo_situacao_titulo', 'titulo_valor', 'beneficio_sequencial')
        campos_presentes = [c for c in campos_chave if c in primeiro]
        if len(campos_presentes) < 2:
            return jsonify({
                'status': 'erro',
                'mensagem': (
                    'O JSON não parece ser do dashboard financeiro. '
                    f'Esperava campos como {campos_chave}. '
                    f'Encontrei só: {campos_presentes}.'
                ),
            }), 400

        data_dir = os.path.join(BASE_DIR, 'data')
        os.makedirs(data_dir, exist_ok=True)
        destino = os.path.join(data_dir, 'dashboard_financeiro_live.json')
        tmp = destino + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(conteudo)
        if os.path.exists(destino):
            os.replace(tmp, destino)
        else:
            os.rename(tmp, destino)

        for k in ('arquivo', 'mtime', 'dados_brutos','dados_processados', 'dados_eventos', 'dados_vendas'):
            _cache[k] = None

        logger.info(
            f'[UPLOAD] JSON substituído: {len(dados):,} registros '
            f'({len(conteudo)/1024/1024:.1f} MB) por {session.get("usuario", "?")}'
        )
        return jsonify({
            'status': 'ok',
            'registros': len(dados),
            'tamanho_mb': round(len(conteudo) / 1024 / 1024, 2),
            'mensagem': (
                f'Arquivo carregado com sucesso: {len(dados):,} registros. '
                'O dashboard já está usando os novos dados.'
            ),
        })
    except Exception:
        logger.exception('[UPLOAD] Falha ao processar arquivo')
        return jsonify({
            'status': 'erro',
            'mensagem': 'Erro interno ao processar o arquivo. Veja os logs.',
        }), 500

@app.route('/api/admin/sync/status')
@login_required
def api_admin_sync_status():
    padrao = os.path.join(BASE_DIR, 'data', 'dashboard_financeiro_*.json')
    arquivos_sync = glob.glob(padrao)
    em_andamento = not _sync_lock.acquire(blocking=False)
    if not em_andamento:
        _sync_lock.release()
    try:
        from siprov_sync import progresso as siprov_progresso
        prog = dict(siprov_progresso)
    except Exception:
        prog = {}
    if not arquivos_sync:
        return jsonify({
            'ultimo_sync': None,
            'arquivo': None,
            'em_andamento': em_andamento,
            'progresso': prog,
        })
    mais_recente = max(arquivos_sync, key=os.path.getmtime)
    mtime = os.path.getmtime(mais_recente)
    return jsonify({
        'ultimo_sync': datetime.fromtimestamp(mtime).isoformat(),
        'arquivo': os.path.basename(mais_recente),
        'em_andamento': em_andamento,
        'progresso': prog,
    })

@app.route('/api/admin/db/status')
@login_required
def api_admin_db_status():
    """Resumo do banco SQLite: períodos armazenados, congelados, totais."""
    try:
        return jsonify(bancodados.estatisticas())
    except Exception:
        logger.exception('[DB] Falha ao ler estatisticas')
        return jsonify({'erro': 'Falha ao ler o banco.'}), 500

@app.route('/api/admin/db/congelar', methods=['POST'])
@login_required
def api_admin_db_congelar():
    """Congela um ano (protege de alterações). Body: {"ano": 2025}"""
    ano = request.json.get('ano') if request.is_json else request.form.get('ano')
    try:
        ano = int(ano)
    except (TypeError, ValueError):
        return jsonify({'erro': 'Informe um ano válido (ex: 2025).'}), 400
    n = bancodados.congelar_ano(ano)
    # Invalida cache para refletir mudança
    _cache['arquivo'] = None
    return jsonify({'status': 'ok', 'ano': ano, 'titulos_congelados': n})
# =========================================================
# START
# =========================================================
if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)