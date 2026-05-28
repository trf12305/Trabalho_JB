"""
siprov_sync.py
==============
Integração automática entre a Siprov API e o dashboard JB Proteção.
Campos mapeados diretamente do OpenAPI oficial da Siprov (v1.81).

Usa o endpoint de RELATÓRIO (POST /ext/relatorio/financeiro) para
obter dados idênticos ao "Relatório de Contas a Receber" nativo do Siprov.

Instale:  pip install requests schedule python-dotenv
Configure no .env:
  SIPROV_USUARIO=seu.email@empresa.com
  SIPROV_SENHA=sua_senha
  SIPROV_COD_LOJA=          (opcional)
  SIPROV_DATA_INICIAL=      (opcional, ex: 2026-01-01)
Execute:  python siprov_sync.py
"""

import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import schedule
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURAÇÃO
# ─────────────────────────────────────────────

SIPROV_BASE_URL     = "https://acesso.siprov.com.br/siprov-api"
SIPROV_USUARIO      = os.environ.get("SIPROV_USUARIO", "ti@jbclube.com")
SIPROV_SENHA        = os.environ.get("SIPROV_SENHA",   "Melo3209.")
SIPROV_COD_LOJA     = os.environ.get("SIPROV_COD_LOJA", "")
SIPROV_DATA_INICIAL = os.environ.get("SIPROV_DATA_INICIAL", "")
# Layout 496 = "dashboard financeiro" — campos idênticos ao formato do dashboard.
# Os dados retornados pelo relatório não precisam de conversão adicional.
SIPROV_COD_LAYOUT   = int(os.environ.get("SIPROV_COD_LAYOUT", "496"))
HORARIOS            = ["09:00", "18:00"]

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "siprov_sync.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("siprov_sync")

# ─────────────────────────────────────────────
#  PROGRESSO COMPARTILHADO (lido pelo app.py)
# ─────────────────────────────────────────────

progresso = {
    "status":          "idle",  # idle | autenticando | titulos | associados | salvando | ok | erro
    "mensagem":        "",
    "titulos_atual":   0,
    "titulos_total":   0,
    "associados":      0,
    "iniciado_em":     None,
    "concluido_em":    None,
    "duracao_segundos": 0,
}


def _set(**kwargs):
    """Atualiza o dict de progresso (thread-safe enough para Python GIL)."""
    progresso.update(kwargs)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _safe(valor, default=""):
    return valor if valor is not None else default


def _float(valor) -> float:
    try:
        if valor is None:
            return 0.0
        if isinstance(valor, (int, float)):
            return float(valor)
        s = str(valor).strip().replace("R$", "").strip()
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        return float(s)
    except Exception:
        return 0.0


def _data(s: str) -> str:
    """
    Normaliza datas para yyyy-MM-dd (formato que o app.py usa em parse_date).
    Aceita dd/MM/yyyy ou yyyy-MM-dd.
    """
    if not s:
        return ""
    s = str(s).strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if len(s) >= 10 and s[2] == "/":
        try:
            return datetime.strptime(s[:10], "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


# ─────────────────────────────────────────────
#  AUTENTICAÇÃO
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  HTTP SESSION (keep-alive + gzip + connection pooling)
#  Cada thread tem sua própria session via thread-local.
# ─────────────────────────────────────────────

_session_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_session_local, "session", None)
    if s is None:
        s = requests.Session()
        # Aumenta connection pool e habilita retry leve em 502/503/504
        retry = Retry(
            total=3, backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://",  adapter)
        s.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        _session_local.session = s
    return s


def autenticar() -> str:
    credencial = base64.b64encode(f"{SIPROV_USUARIO}:{SIPROV_SENHA}".encode()).decode()
    resp = _session().post(
        f"{SIPROV_BASE_URL}/ext/autenticacao",
        headers={"Authorization": f"Basic {credencial}"},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("authorizationToken")
    if not token:
        raise ValueError("Token não retornado pela Siprov.")
    log.info("Autenticação OK.")
    return token


def _get(token: str, path: str, params: dict = None, timeout=(15, 180)) -> dict | list:
    resp = _session().get(
        f"{SIPROV_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _post(token: str, path: str, body: dict = None, timeout=(15, 60)) -> dict | list:
    """HTTP POST autenticado com Bearer token."""
    resp = _session().post(
        f"{SIPROV_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _options(token: str, path: str, timeout=(10, 30)) -> dict:
    """HTTP OPTIONS autenticado (Siprov usa para verificar status de relatório)."""
    resp = _session().options(
        f"{SIPROV_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {}


# ─────────────────────────────────────────────
#  MAPEAMENTO — TituloOutputTO → formato do dashboard
#
#  Campos reais confirmados pelo OpenAPI da Siprov:
#
#  TituloOutputTO:
#    codTitulo, codPessoa, codLoja, codTipoTitulo,
#    nomePessoa, cpfCnpjPessoa, nomeLoja, nomeTipoTitulo,
#    dataEmissao, dataVencimento, dataCancelamento, dataLancamento,
#    valor, situacao, descricao, tipoLancamento, tipoPessoa,
#    nomeUsuarioCadastro, observacao, urlFatura,
#    beneficios[] → TituloBeneficioOutputTO:
#      codBeneficio, codConsultor, codRepresentante,
#      nomeConsultor, nomeRepresentante, nomeSituacao, tipoSituacao, descricao
#    liquidacoes[] → TituloLiquidacaoOutputTO:
#      codLiquidacao, codTipoLiquidacao, nomeTipoLiquidacao,
#      dataLiquidacao, dataCredito, valor, total,
#      desconto, acrescimo, juros, multa, despesas,
#      banco, agencia, codConta, codCaixa,
#      descricaoConta, descricaoCaixa, nomeLiquidante, nomeEstornador
#
#  PesquisaAssociadoOutputTO (GET /ext/associado):
#    codPessoa, codBeneficio, nomePessoa, cpfCnpj, sequencial,
#    dataAdesao, dataAtivacao, dataCadastro, dataNascimento,
#    placa, chassi, sexo, situacao, tipoBeneficio, tipoSituacao,
#    planos[] → { codPlano, nome, valor }
#    endereco → { cidade, uf, ... }
#    telefoneCelular, telefoneFixo, email,
#    nomeEmpresa, identidade, numeroCnh, categoriaCnh
# ─────────────────────────────────────────────

def titulo_para_registro(titulo: dict, associado: dict = None) -> dict:
    """
    Converte um registro de título para o formato esperado pelo app.py.

    Suporta dois formatos de entrada (com fallback automático):
      • ItemTituloOutputTO  — endpoint de lista GET /ext/financeiro/titulo
        campos: nomeDevedorCredor, sequencialBeneficio[], tipo,
                valorLiquidado, dataLiquidacao (campos diretos)
      • TituloOutputTO  — endpoint de relatório POST /ext/relatorio/financeiro
        campos: nomePessoa, beneficios[].codBeneficio, tipoLancamento,
                liquidacoes[].valor / .dataLiquidacao (nested)
    """
    a = associado or {}

    # Endereço do associado (EnderecoTO: cidade, uf, bairro, cep, logradouro...)
    endereco = a.get("endereco") or {}

    # Plano principal do associado (PlanoOutputTO: nome, valor)
    planos = a.get("planos") or []
    plano_principal = ""
    mensalidade = 0.0
    if planos and isinstance(planos[0], dict):
        plano_principal = planos[0].get("nome", "")
        mensalidade = _float(planos[0].get("valor", 0))
    elif planos:
        plano_principal = str(planos[0])

    # Sequencial do benefício:
    #   ItemTituloOutputTO: sequencialBeneficio[] (array de ints)
    #   TituloOutputTO:     beneficios[].codBeneficio (array de objetos)
    seq_lista = titulo.get("sequencialBeneficio") or []
    if not seq_lista:
        bens = titulo.get("beneficios") or []
        seq_lista = [
            b.get("codBeneficio")
            for b in bens
            if isinstance(b, dict) and b.get("codBeneficio")
        ]
    beneficio_seq = seq_lista[0] if seq_lista else _safe(
        a.get("sequencial") or a.get("codBeneficio")
    )

    # Nome da pessoa:
    #   ItemTituloOutputTO: nomeDevedorCredor
    #   TituloOutputTO:     nomePessoa
    nome = _safe(
        titulo.get("nomeDevedorCredor")
        or titulo.get("nomePessoa")
        or a.get("nomePessoa")
    )

    # Tipo de lançamento:
    #   ItemTituloOutputTO: tipo → "Crédito", "Débito", "Rateio"
    #   TituloOutputTO:     tipoLancamento → "CREDITO", "DEBITO", "RATEIO"
    tipo = _safe(titulo.get("tipo") or titulo.get("tipoLancamento"))

    # Liquidação:
    #   ItemTituloOutputTO: valorLiquidado e dataLiquidacao como campos diretos
    #   TituloOutputTO:     liquidacoes[] → TituloLiquidacaoOutputTO
    data_liq    = _data(_safe(titulo.get("dataLiquidacao")))
    valor_liq   = _float(titulo.get("valorLiquidado", 0))
    tipo_liq    = ""
    conta_liq   = ""
    desconto    = 0.0
    usuario_liq = ""

    liquidacoes = titulo.get("liquidacoes") or []
    if liquidacoes and isinstance(liquidacoes[0], dict):
        liq = liquidacoes[0]  # usa a primeira liquidação
        if not data_liq:
            data_liq = _data(_safe(
                liq.get("dataLiquidacao") or liq.get("dataCredito")
            ))
        if not valor_liq:
            valor_liq = _float(liq.get("valor", 0))
        tipo_liq    = _safe(liq.get("nomeTipoLiquidacao"))
        conta_liq   = _safe(liq.get("descricaoConta") or liq.get("descricaoCaixa"))
        desconto    = _float(liq.get("desconto", 0))
        usuario_liq = _safe(liq.get("nomeLiquidante"))

    # Consultor e Representante — TituloOutputTO (layout 496): beneficios[].nomeConsultor/.nomeRepresentante
    consultor     = ""
    representante = ""
    bens_obj = titulo.get("beneficios") or []
    if bens_obj and isinstance(bens_obj[0], dict):
        consultor     = _safe(bens_obj[0].get("nomeConsultor"))
        representante = _safe(bens_obj[0].get("nomeRepresentante"))

    return {
        # ── Benefício ──────────────────────────────────────
        "beneficio_sequencial":          beneficio_seq,
        "beneficio_data_adesao":         _data(_safe(a.get("dataAdesao"))),
        "beneficio_consultor":           consultor,
        "beneficio_representante":       representante,
        "beneficio_valor_mensalidade":   mensalidade,
        "beneficio_planos_principais":   plano_principal,

        # ── Pessoa ─────────────────────────────────────────
        "pessoa_nome_razao_social":      nome,
        "pessoa_cpf_cnpj":               _safe(titulo.get("cpfCnpjPessoa") or a.get("cpfCnpj")),
        "pessoa_data_nascimento":        _data(_safe(a.get("dataNascimento"))),
        "pessoa_sexo":                   _safe(a.get("sexo")),

        # ── Endereço ───────────────────────────────────────
        "endereco_cidade":               _safe(endereco.get("cidade")),
        "endereco_uf":                   _safe(endereco.get("uf")),

        # ── Unidade ────────────────────────────────────────
        "unidade_nome_fantasia":         _safe(titulo.get("nomeLoja")),
        "unidade_razao_social":          "",

        # ── Título ─────────────────────────────────────────
        "titulo_parcela":                _safe(titulo.get("codTitulo")),
        "titulo_data_emissao":           _data(_safe(titulo.get("dataEmissao"))),
        "titulo_data_vencimento":        _data(_safe(titulo.get("dataVencimento"))),
        "titulo_situacao_titulo":        _safe(titulo.get("situacao")),
        "titulo_tipo_titulo":            tipo,
        "titulo_descricao":              _safe(titulo.get("descricao")),
        "titulo_valor":                  _float(titulo.get("valor")),
        "titulo_conta":                  "",
        "titulo_usuario_cadastro":       _safe(titulo.get("nomeUsuarioCadastro")),

        # ── Liquidação ─────────────────────────────────────
        "liquidacao_data_liquidacao":    data_liq,
        "liquidacao_valor_liquidado":    valor_liq,
        "liquidacao_tipo_liquidacao":    tipo_liq,
        "liquidacao_conta_liquidacao":   conta_liq,
        "liquidacao_desconto":           desconto,
        "liquidacao_usuario_liquidante": usuario_liq,

        # ── Veículo ────────────────────────────────────────
        "veiculo_placa_veiculo":         _safe(a.get("placa")),
        "veiculo_categoria":             _safe(a.get("tipoBeneficio")),
        "veiculo_marca_veiculo":         "",
        "veiculo_valor_veiculo":         0.0,
    }


# ─────────────────────────────────────────────
#  COLETA — TÍTULOS via /ext/relatorio/financeiro
#
#  Fluxo assíncrono:
#    1. POST /ext/relatorio/financeiro  → recebe codRelatorio (situacao: "Pendente")
#    2. OPTIONS /ext/relatorio/financeiro/{cod}  → poll até situacao != "Pendente"
#    3. GET /ext/relatorio/financeiro/{cod}  → baixa o JSON com os títulos
# ─────────────────────────────────────────────

def _mes_inicio_fim(ano: int, mes: int) -> tuple[str, str]:
    """Retorna (01/MM/yyyy, último_dia/MM/yyyy) em formato dd/MM/yyyy."""
    from calendar import monthrange
    ultimo_dia = monthrange(ano, mes)[1]
    return f"01/{mes:02d}/{ano}", f"{ultimo_dia:02d}/{mes:02d}/{ano}"


def _atualizar_dashboard_live(registros: list, _ignorado=None, lock=None) -> None:
    """
    Salva os registros em data/dashboard_financeiro_live.json.

    Com o layout 496 os dados já chegam no formato do dashboard —
    não é necessária conversão adicional.
    Chamado após cada mês completar para atualização em tempo real.
    """
    snapshot = list(registros) if lock is not None else registros

    arquivo = DATA_DIR / "dashboard_financeiro_live.json"
    tmp = arquivo.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)
    tmp.replace(arquivo)
    log.info(f"  [LIVE] {len(snapshot)} registros disponíveis no dashboard")


def _dedup_key(item: dict) -> str:
    """Chave única de um título para deduplicação entre filtros vencimento+liquidacao."""
    return str(item.get("codTitulo") or item.get("titulo_parcela") or id(item))


def _solicitar_relatorio(token: str, tipo_lancamento: str,
                          situacoes: list[str], di: str, df: str,
                          filtro_data: str = "vencimento") -> int | None:
    """
    Solicita a geração assíncrona de um relatório financeiro.

    Args:
        tipo_lancamento: "CREDITO" ou "DEBITO"
        situacoes: lista de situações, ex. ["ABERTO", "LIQUIDADO"]
        di, df: datas no formato dd/MM/yyyy (início e fim)
        filtro_data: "vencimento" (dataVencimento) ou "liquidacao" (dataLiquidacao)

    Retorna codRelatorio (int) ou None se a API não retornar o código.
    """
    body: dict = {
        "codLayout":      SIPROV_COD_LAYOUT,
        "formato":        "JSON",
        "tipoLancamento": tipo_lancamento.upper(),
        # A API exige situações em MAIÚSCULO: "ABERTO", "LIQUIDADO"
        "situacaoTitulo": [s.upper() for s in situacoes],
    }
    # Fusão vencimento + liquidacao: cada relatório usa apenas UM tipo de data.
    if filtro_data == "liquidacao":
        body["dataLiquidacaoInicial"] = di
        body["dataLiquidacaoFinal"]   = df
    else:
        body["dataVencimentoInicial"] = di
        body["dataVencimentoFinal"]   = df

    if SIPROV_COD_LOJA:
        try:
            body["codLoja"] = [int(SIPROV_COD_LOJA)]
        except ValueError:
            body["codLoja"] = [SIPROV_COD_LOJA]

    log.info(f"    POST /ext/relatorio/financeiro "
             f"tipo={tipo_lancamento} filtro={filtro_data} sits={situacoes} {di}→{df}")
    data = _post(token, "/ext/relatorio/financeiro", body, timeout=(15, 60))
    if not isinstance(data, dict):
        log.error(f"    Resposta inesperada do POST relatório: {type(data)}")
        return None
    cod = data.get("codRelatorio")
    situacao = data.get("situacao", "?")
    mensagem = data.get("mensagem", "")
    log.info(f"    Relatório solicitado: cod={cod}, situacao={situacao}"
             + (f", msg={mensagem}" if mensagem else ""))
    return cod


def _aguardar_relatorio(token: str, cod_relatorio: int,
                         timeout_s: int = 1200) -> bool:
    """
    Aguarda o relatório ficar pronto via OPTIONS /ext/relatorio/financeiro/{cod}.

    Faz polling com backoff exponencial (5s → 30s máx).
    Retorna True se pronto, False se timeout.
    """
    # Estados que indicam "ainda processando" — a API retorna em MAIÚSCULO
    _PENDENTES = {"PENDENTE", "PROCESSANDO", "EM PROCESSAMENTO"}

    inicio = time.time()
    intervalo = 5.0
    while time.time() - inicio < timeout_s:
        try:
            data = _options(token, f"/ext/relatorio/financeiro/{cod_relatorio}")
            situacao = (data.get("situacao", "PENDENTE") if data else "PENDENTE").upper()
            decorrido = int(time.time() - inicio)
            log.info(f"    Relatório {cod_relatorio}: situacao={situacao} ({decorrido}s)")
            if situacao not in _PENDENTES:
                # FINALIZADO, ERRO, ou qualquer outro estado terminal
                return situacao == "FINALIZADO"
        except requests.HTTPError as e:
            log.warning(f"    HTTP {e.response.status_code} ao verificar "
                        f"relatório {cod_relatorio}")
        except Exception as e:
            log.warning(f"    Erro ao verificar relatório {cod_relatorio}: {e}")
        time.sleep(intervalo)
        intervalo = min(intervalo * 1.5, 30.0)   # backoff gradual, máx 30 s

    log.error(f"    Timeout ({timeout_s}s) aguardando relatório {cod_relatorio}")
    return False


def _baixar_relatorio(token: str, cod_relatorio: int) -> list[dict]:
    """
    Baixa o conteúdo JSON do relatório via GET /ext/relatorio/financeiro/{cod}.
    Retorna lista de dicts com os registros de título.
    """
    resp = _session().get(
        f"{SIPROV_BASE_URL}/ext/relatorio/financeiro/{cod_relatorio}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=(15, 300),
    )
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception as e:
        log.error(f"    Relatório {cod_relatorio}: resposta não é JSON — {e}")
        log.debug(f"    Conteúdo raw: {resp.text[:500]}")
        return []

    # A resposta pode ser: lista direta ou dict com uma chave de array
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("itens", "dados", "titulos", "registros", "content", "items"):
            if key in data and isinstance(data[key], list):
                log.info(f"    Relatório {cod_relatorio}: {len(data[key])} itens em '{key}'")
                return data[key]
        log.warning(f"    Relatório {cod_relatorio}: estrutura inesperada — "
                    f"chaves={list(data.keys())}")
    return []


def coletar_titulos(token: str) -> list[dict]:
    """
    Coleta os títulos financeiros via relatório assíncrono, fatiando por mês.

    Estratégia de janela:
      - Se SIPROV_DATA_INICIAL setada (YYYY-MM-DD): puxa do mês dessa data até o
        mês de SIPROV_DATA_FINAL (ou hoje+SIPROV_MESES_FUTUROS, default 6 meses).
      - Senão: puxa SIPROV_MESES_BACK meses para trás a partir de hoje (default 3).
    """
    log.info("Coletando títulos financeiros via /ext/relatorio/financeiro "
             "(tipos=CREDITO+DEBITO, situacoes=Aberto+Pendente+Liquidado)...")

    hoje = date.today()
    data_ini_str = os.environ.get("SIPROV_DATA_INICIAL", "").strip()
    data_fim_str = os.environ.get("SIPROV_DATA_FINAL", "").strip()
    meses_futuros = int(os.environ.get("SIPROV_MESES_FUTUROS", "6"))

    meses = []
    if data_ini_str:
        try:
            di = datetime.strptime(data_ini_str, "%Y-%m-%d").date()
        except ValueError:
            log.warning(f"SIPROV_DATA_INICIAL invalida ({data_ini_str}), usando hoje-3meses")
            di = hoje
        if data_fim_str:
            try:
                df = datetime.strptime(data_fim_str, "%Y-%m-%d").date()
            except ValueError:
                df = hoje
                # Adiciona meses_futuros
                for _ in range(meses_futuros):
                    df = df.replace(day=1)
                    df = (df.replace(month=12, day=1) if df.month == 1
                          else df.replace(month=df.month, day=1))
                    # avança 1 mês manualmente
                    if df.month == 12:
                        df = df.replace(year=df.year + 1, month=1)
                    else:
                        df = df.replace(month=df.month + 1)
        else:
            df = hoje
            for _ in range(meses_futuros):
                if df.month == 12:
                    df = df.replace(year=df.year + 1, month=1)
                else:
                    df = df.replace(month=df.month + 1)
        log.info(f"  Janela: {di.strftime('%m/%Y')} ate {df.strftime('%m/%Y')}")
        y, m = di.year, di.month
        while (y, m) <= (df.year, df.month):
            meses.append((y, m))
            if m == 12:
                m = 1; y += 1
            else:
                m += 1
    else:
        meses_back = int(os.environ.get("SIPROV_MESES_BACK", "3"))
        y, m = hoje.year, hoje.month
        for _ in range(meses_back):
            meses.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        meses.reverse()

    # Diretório de cache por mês — sobrevive reinício do app
    cache_dir = DATA_DIR / "mes_cache"
    cache_dir.mkdir(exist_ok=True)

    # Fase 1 — meses que JÁ estão em cache (carrega rápido, sem rede)
    todos = []
    meses_a_buscar = []
    for ano, mes in meses:
        cache_file = cache_dir / f"{ano}_{mes:02d}.json"
        mes_passado = (ano, mes) < (hoje.year, hoje.month)
        idade_max_horas = 24 * 30 if mes_passado else 1
        if cache_file.exists():
            idade_h = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
            if idade_h < idade_max_horas:
                try:
                    with open(cache_file, encoding="utf-8") as f:
                        itens_cache = json.load(f)
                    log.info(f"  Cache {mes:02d}/{ano}: {len(itens_cache)} títulos (idade {idade_h:.1f}h)")
                    todos.extend(itens_cache)
                    continue
                except Exception as e:
                    log.warning(f"  Cache {mes:02d}/{ano} corrompido ({e}), refazendo")
        meses_a_buscar.append((ano, mes))

    _set(titulos_atual=len(todos),
         mensagem=f"Cache carregado: {len(todos)} títulos. {len(meses_a_buscar)} meses a buscar.")

    # Fase 2 — buscar meses faltantes em PARALELO
    if meses_a_buscar:
        # Lock para proteger 'todos' e progresso
        lock = threading.Lock()
        # Situações — a API exige MAIÚSCULO.
        # ABERTO=em aberto, PENDENTE=vencido não pago, LIQUIDADO=pago.
        # Com filtro por dataVencimento, as 3 situações cobrem TODO o universo do período.
        situacoes = os.environ.get("SIPROV_SITUACOES", "ABERTO,PENDENTE,LIQUIDADO").split(",")
        situacoes = [s.strip().upper() for s in situacoes if s.strip()]
        # Tipos de lançamento — a API aceita "CREDITO" ou "DEBITO" (um por relatório)
        # Mapeamento para compatibilidade com configurações antigas ("Crédito"/"Débito")
        _tipo_map = {
            "CREDITO": "CREDITO",
            "CRÉDITO": "CREDITO",   # "CRÉDITO" com acento
            "DEBITO": "DEBITO",
            "DÉBITO": "DEBITO",     # "DÉBITO" com acento
            "RATEIO": "RATEIO",
        }
        tipos_raw = os.environ.get("SIPROV_TIPOS", "CREDITO,DEBITO").split(",")
        tipos = [
            _tipo_map.get(t.strip().upper(), t.strip().upper())
            for t in tipos_raw if t.strip()
        ]
        log.info(f"  Situações: {situacoes} | Tipos: {tipos}")
        log.info(f"  Estratégia: solicitar {len(meses_a_buscar)*len(tipos)} relatórios "
                 f"de uma vez, depois aguardar e baixar em paralelo.")

        # ── Fase 2a: solicita TODOS os relatórios de uma vez ──────────────────
        # Fusão vencimento + liquidacao: para cada mês/tipo, 2 relatórios são solicitados.
        # Assim capturamos tanto quem pagou antecipado (vencimento no período, liquidacao antes)
        # quanto quem pagou atrasado (vencimento antes do período, liquidacao no período).
        # Chave: (ano, mes, tipo, filtro_data)  →  codRelatorio
        filtros_data = ["vencimento"]  # fusão com liquidacao desativada por ora
        fila: dict[tuple, int] = {}
        for ano, mes in meses_a_buscar:
            di, df = _mes_inicio_fim(ano, mes)
            for tipo in tipos:
                for filtro_data in filtros_data:
                    try:
                        cod = _solicitar_relatorio(token, tipo, situacoes, di, df, filtro_data)
                        if cod:
                            fila[(ano, mes, tipo, filtro_data)] = cod
                            log.info(f"  Solicitado: {mes:02d}/{ano} {tipo}/{filtro_data} → cod={cod}")
                        else:
                            log.error(f"  Sem codRelatorio para {tipo}/{filtro_data} {mes:02d}/{ano}")
                    except Exception as e:
                        log.error(f"  Erro ao solicitar {tipo}/{filtro_data} {mes:02d}/{ano}: {e}")

        log.info(f"  {len(fila)} relatórios solicitados. Aguardando conclusão…")
        _set(mensagem=f"Aguardando {len(fila)} relatórios do Siprov…")

        # ── Fase 2b: polling paralelo de todos até ficarem prontos ───────────
        # Timeout global: 30 min. Intervalo de poll: 15 s.
        TIMEOUT_GLOBAL = int(os.environ.get("SIPROV_TIMEOUT_RELATORIO", "1800"))
        pendentes: dict[tuple, int] = dict(fila)  # cópia mutável
        concluidos: dict[tuple, list] = {}         # (ano,mes,tipo,filtro_data) → itens
        t_inicio = time.time()

        while pendentes and (time.time() - t_inicio) < TIMEOUT_GLOBAL:
            prontos_agora = []
            for chave, cod in list(pendentes.items()):
                try:
                    data = _options(token, f"/ext/relatorio/financeiro/{cod}")
                    sit = (data.get("situacao", "PENDENTE") if data
                           else "PENDENTE").upper()
                    decorrido = int(time.time() - t_inicio)
                    ano, mes, tipo, filtro_data = chave
                    log.info(f"  [{decorrido}s] {mes:02d}/{ano} {tipo}/{filtro_data} cod={cod}: {sit}")
                    if sit not in ("PENDENTE", "PROCESSANDO", "EM PROCESSAMENTO"):
                        prontos_agora.append((chave, cod, sit))
                except Exception as e:
                    log.warning(f"  Erro ao verificar cod={cod}: {e}")

            for chave, cod, sit in prontos_agora:
                pendentes.pop(chave)
                ano, mes, tipo, filtro_data = chave
                if sit == "FINALIZADO":
                    try:
                        itens = _baixar_relatorio(token, cod)
                        log.info(f"  Baixado {mes:02d}/{ano} {tipo}/{filtro_data}: {len(itens)} itens")
                        concluidos[chave] = itens

                        # Salva no cache do mês — deduplica por codTitulo antes de gravar
                        cache_file = cache_dir / f"{ano}_{mes:02d}.json"
                        existentes: list = []
                        if cache_file.exists():
                            try:
                                with open(cache_file, encoding="utf-8") as f:
                                    existentes = json.load(f)
                            except Exception:
                                existentes = []
                        # Deduplicação: vencimento + liquidacao podem retornar o mesmo título
                        _vistos: dict = {_dedup_key(r): r for r in existentes}
                        for item in itens:
                            _vistos.setdefault(_dedup_key(item), item)
                        merged = list(_vistos.values())
                        tmp = cache_file.with_suffix(".json.tmp")
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(merged, f, ensure_ascii=False)
                        tmp.replace(cache_file)

                        # Live update acumulado (dedup será feito ao final)
                        with lock:
                            todos.extend(itens)
                            _set(titulos_atual=len(todos),
                                 mensagem=f"{len(todos)} títulos (baixando…)")
                        _atualizar_dashboard_live(todos, None, lock)
                    except Exception as e:
                        log.error(f"  Erro ao baixar {mes:02d}/{ano} {tipo}/{filtro_data}: {e}")
                else:
                    log.warning(f"  Relatório {cod} encerrou com situacao={sit} — ignorado")

            if pendentes:
                time.sleep(15)  # aguarda 15 s antes do próximo round de polling

        if pendentes:
            for chave, cod in pendentes.items():
                ano, mes, tipo, filtro_data = chave
                log.error(f"  Timeout global ({TIMEOUT_GLOBAL}s): "
                          f"{mes:02d}/{ano} {tipo}/{filtro_data} cod={cod} nunca finalizou")

    # Deduplicação final: fusão vencimento+liquidacao pode duplicar títulos pagos no prazo
    if todos:
        _vistos_final: dict = {}
        for item in todos:
            _vistos_final.setdefault(_dedup_key(item), item)
        n_antes = len(todos)
        todos = list(_vistos_final.values())
        n_dup = n_antes - len(todos)
        if n_dup:
            log.info(f"  Deduplicação final: {n_dup} registros removidos (mesmo codTitulo em vencimento+liquidacao)")

    log.info(f"TOTAL: {len(todos)} títulos coletados (após deduplicação)")
    _set(titulos_total=len(todos))
    return todos


# ─────────────────────────────────────────────
#  COLETA — ASSOCIADOS (para enriquecer)
# ─────────────────────────────────────────────

def coletar_associados(token: str) -> dict:
    """
    Retorna dict { str(codPessoa): dados_associado }.
    A API exige pelo menos um dos params: codPessoa, nome, cpf/cnpj,
    dataCadastroInicial+dataCadastroFinal, dataNascimento, situacaoBeneficio,
    placa, chassi. Tentamos buscar todos os ATIVOS via situacaoBeneficio.
    """
    log.info("Coletando associados (situacaoBeneficio=ATIVO)...")
    mapa = {}
    pagina = 1
    while True:
        params = {"situacaoBeneficio": "ATIVO", "pagina": pagina}
        if SIPROV_COD_LOJA:
            params["codLoja"] = SIPROV_COD_LOJA
        try:
            resp = _get(token, "/ext/associado", params, timeout=(15, 120))
        except requests.HTTPError as e:
            log.warning(f"Erro ao buscar associados pagina {pagina}: {e}")
            break
        except requests.Timeout:
            log.warning(f"Timeout pagina {pagina} de associados")
            break

        itens = resp.get("itens", []) if isinstance(resp, dict) else (resp or [])
        if not itens:
            break
        for a in itens:
            cod = a.get("codPessoa")
            if cod:
                mapa[str(cod)] = a
        log.info(f"  Associados pag {pagina}: +{len(itens)} (acum {len(mapa)})")
        _set(associados=len(mapa), mensagem=f"Buscando associados ({len(mapa)})…")
        if len(itens) < 100:
            break
        pagina += 1

    log.info(f"Total de associados ATIVOS: {len(mapa)}")
    return mapa


# ─────────────────────────────────────────────
#  CICLO PRINCIPAL
# ─────────────────────────────────────────────

def sincronizar():
    inicio = datetime.now()
    log.info("=" * 60)
    log.info(f"SINCRONIZAÇÃO — {inicio.strftime('%d/%m/%Y %H:%M:%S')}")
    log.info("=" * 60)
    _set(status="autenticando", mensagem="Autenticando no Siprov…",
         titulos_atual=0, titulos_total=0, associados=0,
         iniciado_em=inicio.isoformat(), concluido_em=None, duracao_segundos=0)

    try:
        token = autenticar()
    except Exception as e:
        log.error(f"Falha na autenticação: {e}")
        _set(status="erro", mensagem=f"Falha na autenticação: {e}",
             concluido_em=datetime.now().isoformat())
        return

    try:
        _set(status="titulos", mensagem="Coletando títulos…")
        titulos = coletar_titulos(token)
    except Exception as e:
        log.error(f"Falha ao coletar títulos: {e}")
        _set(status="erro", mensagem=f"Falha ao coletar títulos: {e}",
             concluido_em=datetime.now().isoformat())
        return

    if not titulos:
        log.warning("Nenhum título retornado.")
        _set(status="erro", mensagem="Nenhum título retornado pela API.",
             concluido_em=datetime.now().isoformat())
        return

    # O layout 496 já devolve os dados no formato do dashboard —
    # não é necessário buscar associados nem converter registros.
    log.info(f"Salvando {len(titulos)} registros (dados já no formato do dashboard)...")
    _set(status="salvando", mensagem="Salvando registros…")
    try:
        _atualizar_dashboard_live(titulos, None, None)
    except Exception as e:
        log.warning(f"Falha no live update final: {e}")

    registros = titulos  # uso direto, sem conversão

    # Salva
    ts = inicio.strftime("%Y%m%d_%H%M%S")
    arquivo = DATA_DIR / f"dashboard_financeiro_{ts}.json"
    with open(arquivo, "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=2)

    kb = arquivo.stat().st_size // 1024
    duracao = round((datetime.now() - inicio).total_seconds(), 1)

    log.info(f"✓ {len(registros)} registros salvos em {arquivo.name} ({kb} KB) — {duracao}s")
    _set(status="ok",
         mensagem=f"{len(registros)} registros sincronizados ({duracao}s).",
         concluido_em=datetime.now().isoformat(),
         duracao_segundos=duracao)

    # Mantém só os 5 arquivos mais recentes
    arquivos = sorted(DATA_DIR.glob("dashboard_financeiro_*.json"), key=lambda p: p.stat().st_mtime)
    for antigo in arquivos[:-5]:
        antigo.unlink()
        log.info(f"Arquivo antigo removido: {antigo.name}")


# ─────────────────────────────────────────────
#  AGENDAMENTO
# ─────────────────────────────────────────────

def iniciar():
    if not SIPROV_USUARIO or not SIPROV_SENHA:
        print("\n❌ Configure SIPROV_USUARIO e SIPROV_SENHA no .env antes de rodar!\n")
        return

    for h in HORARIOS:
        schedule.every().day.at(h).do(sincronizar)
        log.info(f"Agendado: {h} todo dia")

    log.info("Executando sincronização inicial...")
    sincronizar()

    log.info(f"Aguardando... próximos horários: {HORARIOS} (Ctrl+C para sair)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    iniciar()