"""
siprov_sync.py
==============
Integração automática entre a Siprov API e o dashboard JB Proteção.
Campos mapeados diretamente do OpenAPI oficial da Siprov (v1.81).

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
HORARIOS            = ["06:00", "18:00"]

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
    Converte ItemTituloOutputTO (lista) + PesquisaAssociadoOutputTO
    para o formato exato esperado pelo app.py.

    Schema real confirmado no SwaggerHub v1.81:
      ItemTituloOutputTO: celular, codPessoa, codTitulo, cpfCnpjPessoa,
        dataEmissao, dataLiquidacao, dataVencimento, descricao,
        nomeDevedorCredor, nomeLoja, sequencialBeneficio[], situacao, tipo,
        valor, valorLiquidado
      (sem beneficios[] nem liquidacoes[] — esses só existem no endpoint /titulo/{id})
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

    # Sequencial do benefício: vem do array sequencialBeneficio[] no titulo,
    # ou do campo sequencial do associado
    seq_lista = titulo.get("sequencialBeneficio") or []
    beneficio_seq = seq_lista[0] if seq_lista else _safe(a.get("sequencial") or a.get("codBeneficio"))

    return {
        # ── Benefício ──────────────────────────────────────
        "beneficio_sequencial":        beneficio_seq,
        "beneficio_data_adesao":       _data(_safe(a.get("dataAdesao"))),
        # consultor/representante não disponíveis no endpoint de lista
        "beneficio_consultor":         "",
        "beneficio_representante":     "",
        "beneficio_valor_mensalidade": mensalidade,
        "beneficio_planos_principais": plano_principal,

        # ── Pessoa ─────────────────────────────────────────
        # campo correto: nomeDevedorCredor (não nomePessoa)
        "pessoa_nome_razao_social":    _safe(titulo.get("nomeDevedorCredor") or a.get("nomePessoa")),
        "pessoa_cpf_cnpj":             _safe(titulo.get("cpfCnpjPessoa") or a.get("cpfCnpj")),
        "pessoa_data_nascimento":      _data(_safe(a.get("dataNascimento"))),
        "pessoa_sexo":                 _safe(a.get("sexo")),

        # ── Endereço ───────────────────────────────────────
        "endereco_cidade":             _safe(endereco.get("cidade")),
        "endereco_uf":                 _safe(endereco.get("uf")),

        # ── Unidade ────────────────────────────────────────
        "unidade_nome_fantasia":       _safe(titulo.get("nomeLoja")),
        "unidade_razao_social":        "",

        # ── Título ─────────────────────────────────────────
        # codTitulo usado como parcela (único por título, garante dedup correto)
        "titulo_parcela":              _safe(titulo.get("codTitulo")),
        "titulo_data_emissao":         _data(_safe(titulo.get("dataEmissao"))),
        "titulo_data_vencimento":      _data(_safe(titulo.get("dataVencimento"))),
        "titulo_situacao_titulo":      _safe(titulo.get("situacao")),
        # campo correto: tipo (Débito/Crédito/Rateio), não nomeTipoTitulo
        "titulo_tipo_titulo":          _safe(titulo.get("tipo")),
        "titulo_descricao":            _safe(titulo.get("descricao")),
        "titulo_valor":                _float(titulo.get("valor")),
        "titulo_conta":                "",
        "titulo_usuario_cadastro":     "",

        # ── Liquidação — campos diretos no ItemTituloOutputTO ──
        "liquidacao_data_liquidacao":  _data(_safe(titulo.get("dataLiquidacao"))),
        "liquidacao_valor_liquidado":  _float(titulo.get("valorLiquidado")),
        # campos de detalhe de liquidação só existem em /titulo/{id}
        "liquidacao_tipo_liquidacao":  "",
        "liquidacao_conta_liquidacao": "",
        "liquidacao_desconto":         0.0,
        "liquidacao_usuario_liquidante": "",

        # ── Veículo ────────────────────────────────────────
        "veiculo_placa_veiculo":       _safe(a.get("placa")),
        # tipoBeneficio do associado indica categoria (ex: "Carro", "Moto")
        "veiculo_categoria":           _safe(a.get("tipoBeneficio")),
        "veiculo_marca_veiculo":       "",
        "veiculo_valor_veiculo":       0.0,
    }


# ─────────────────────────────────────────────
#  COLETA — TÍTULOS (paginado)
# ─────────────────────────────────────────────

def _mes_inicio_fim(ano: int, mes: int) -> tuple[str, str]:
    """Retorna (01/MM/yyyy, último_dia/MM/yyyy) em formato dd/MM/yyyy."""
    from calendar import monthrange
    ultimo_dia = monthrange(ano, mes)[1]
    return f"01/{mes:02d}/{ano}", f"{ultimo_dia:02d}/{mes:02d}/{ano}"


def _atualizar_dashboard_live(titulos_brutos: list, mapa_assoc: dict, lock=None) -> None:
    """
    Converte títulos brutos -> formato dashboard e salva em data/dashboard_financeiro_live.json.
    Chamado após cada mês completar — assim o dashboard reflete o progresso em tempo real.
    """
    if lock is not None:
        snapshot = list(titulos_brutos)  # snapshot rápido sob o lock
    else:
        snapshot = titulos_brutos

    registros = []
    for t in snapshot:
        cod = str(t.get("codPessoa") or "")
        assoc = mapa_assoc.get(cod) if mapa_assoc else {}
        try:
            registros.append(titulo_para_registro(t, assoc or {}))
        except Exception:
            pass

    arquivo = DATA_DIR / "dashboard_financeiro_live.json"
    tmp = arquivo.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False)
    tmp.replace(arquivo)
    log.info(f"  [LIVE] {len(registros)} registros disponíveis no dashboard")


def _coletar_pagina_mes(token: str, tipo: str, situacao: str, di: str, df: str,
                         total_acumulado_inicial: int = 0) -> list[dict]:
    """Pagina um único mês para um (tipo, situacao). Retorna todos os itens do período."""
    todos = []
    while True:
        params = {
            "tipo": tipo,
            "situacao": situacao,
            "dataVencimentoInicial": di,
            "dataVencimentoFinal":   df,
            "inicio": len(todos),
        }
        if SIPROV_COD_LOJA:
            params["codLoja"] = SIPROV_COD_LOJA

        try:
            resp = _get(token, "/ext/financeiro/titulo", params, timeout=(15, 180))
        except requests.HTTPError as e:
            log.error(f"    Erro HTTP no offset {params['inicio']}: {e}")
            break
        except requests.Timeout:
            log.warning(f"    Timeout no offset {params['inicio']} — pulando lote")
            break

        itens = resp.get("itens", []) if isinstance(resp, dict) else (resp or [])
        if not itens:
            break
        todos.extend(itens)
        total = resp.get("quantidade", 0) if isinstance(resp, dict) else 0
        log.info(f"    [{tipo}/{situacao} {di}–{df}] offset {params['inicio']}: +{len(itens)} (acum {len(todos)}/{total or '?'})")
        _set(titulos_atual=total_acumulado_inicial + len(todos),
             mensagem=f"Baixando {di}–{df} ({len(todos)}/{total or '?'})")
        if total and len(todos) >= total:
            break
        if len(itens) < 100:  # API retorna 100 por página; menos = última página
            break
    return todos


def coletar_titulos(token: str) -> list[dict]:
    """
    Coleta os títulos LIQUIDADOS fatiando por mês.
    A API tem cache: primeira chamada lenta (~50s), as seguintes (~5-7s).

    Estratégia de janela:
      - Se SIPROV_DATA_INICIAL setada (YYYY-MM-DD): puxa do mês dessa data até o
        mês de SIPROV_DATA_FINAL (ou hoje+SIPROV_MESES_FUTUROS, default 6 meses).
      - Senão: puxa SIPROV_MESES_BACK meses para trás a partir de hoje (default 3).
    """
    log.info("Coletando títulos financeiros (tipo=Crédito + situacao=Liquidado)...")

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
        # Quantos meses simultâneos (default 4, configurável)
        max_workers = int(os.environ.get("SIPROV_PARALELISMO", "4"))
        max_workers = min(max_workers, len(meses_a_buscar))
        # Status a coletar (Aberto + Pendente + Liquidado por padrão)
        situacoes = os.environ.get("SIPROV_SITUACOES", "Aberto,Pendente,Liquidado").split(",")
        situacoes = [s.strip() for s in situacoes if s.strip()]
        log.info(f"  Iniciando coleta paralela de {len(meses_a_buscar)} meses x {len(situacoes)} status com {max_workers} threads…")

        def _buscar_mes(ano_mes):
            ano, mes = ano_mes
            di, df = _mes_inicio_fim(ano, mes)
            log.info(f"  [thread] Iniciando {mes:02d}/{ano}…")
            with lock:
                _set(mensagem=f"Buscando títulos de {mes:02d}/{ano}…")

            itens_mes = []
            for sit in situacoes:
                itens_sit = _coletar_pagina_mes(token, "Crédito", sit, di, df,
                                                  total_acumulado_inicial=0)
                log.info(f"  [thread] {mes:02d}/{ano} {sit}: {len(itens_sit)}")
                itens_mes.extend(itens_sit)

            log.info(f"  [thread] {mes:02d}/{ano} TOTAL: {len(itens_mes)} títulos")

            # Salva imediatamente no cache
            cache_file = cache_dir / f"{ano}_{mes:02d}.json"
            try:
                tmp = cache_file.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(itens_mes, f, ensure_ascii=False)
                tmp.replace(cache_file)
            except Exception as e:
                log.warning(f"  Falha ao salvar cache de {mes:02d}/{ano}: {e}")

            with lock:
                todos.extend(itens_mes)
                _set(titulos_atual=len(todos))

            # LIVE UPDATE: atualiza o JSON do dashboard com o que tem até agora
            try:
                _atualizar_dashboard_live(todos, {}, lock)
            except Exception as e:
                log.warning(f"  Falha no live update após {mes:02d}/{ano}: {e}")

            return (ano, mes, len(itens_mes))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_buscar_mes, ym) for ym in meses_a_buscar]
            for fut in as_completed(futures):
                try:
                    ano, mes, n = fut.result()
                    log.info(f"  ✓ {mes:02d}/{ano} completo: {n} títulos (acum {len(todos)})")
                except Exception as e:
                    log.error(f"  Falha em thread de mês: {e}")

    log.info(f"TOTAL: {len(todos)} títulos liquidados coletados")
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

    try:
        _set(status="associados", mensagem="Coletando associados…")
        mapa_assoc = coletar_associados(token)
    except Exception as e:
        log.warning(f"Falha ao coletar associados (continuando sem enriquecimento): {e}")
        mapa_assoc = {}

    log.info("Convertendo registros (com enriquecimento de associados)...")
    _set(status="salvando", mensagem="Convertendo com enriquecimento…")
    # Atualiza o LIVE agora com associados (versão final enriquecida)
    try:
        _atualizar_dashboard_live(titulos, mapa_assoc, None)
    except Exception as e:
        log.warning(f"Falha no live update final: {e}")

    registros = []
    for t in titulos:
        cod = str(t.get("codPessoa") or "")
        assoc = mapa_assoc.get(cod) or {}
        try:
            registros.append(titulo_para_registro(t, assoc))
        except Exception as e:
            log.warning(f"Erro ao converter título {t.get('codTitulo')}: {e}")

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