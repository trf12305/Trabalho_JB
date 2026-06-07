# -*- coding: utf-8 -*-
"""
siprov_adesao.py — Coletor automático das ADESÕES (fonte do dashboard de Vendas).

Caminho TOTALMENTE separado do financeiro. Usa o relatório Siprov
/ext/relatorio/adesao (layout 1393 "dashboard de venda"), grava SÓ no banco
(tabela `adesoes` via db_adesoes) — nunca no JSON live nem no financeiro.

Reusa a autenticação e a sessão HTTP do siprov_sync (mesmo usuário).

Funções principais:
  - sincronizar(meses=None)  -> coleta uma janela de meses e grava no banco
  - coletar_mes(ano, mes)    -> coleta um único mês (usado no fechamento mensal)
"""

import os
import time
import logging
from datetime import date, datetime

import siprov_sync as ss   # reusa autenticar(), _session(), _mes_inicio_fim()
import db_adesoes

log = logging.getLogger('siprov_adesao')

COD_LAYOUT = int(os.environ.get('SIPROV_ADESAO_LAYOUT', '1393'))
BASE = ss.SIPROV_BASE_URL

# Estado de progresso (lido pelo app, se quiser exibir)
progresso = {'status': 'idle', 'mensagem': '', 'adesoes': 0,
             'iniciado_em': None, 'concluido_em': None}


def _set(**kw):
    progresso.update(kw)


# ─────────────────────────────────────────────
#  RELATÓRIO DE ADESÃO (assíncrono: POST -> poll -> GET)
# ─────────────────────────────────────────────

def _solicitar(token, di, df):
    """POST /ext/relatorio/adesao -> codRelatorio. Datas em dd/MM/yyyy."""
    body = {
        'codLayout': COD_LAYOUT,
        'formato': 'JSON',
        'dataAdesaoInicial': di,
        'dataAdesaoFinal': df,
    }
    r = ss._session().post(
        f'{BASE}/ext/relatorio/adesao',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=body, timeout=(15, 60),
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    return data.get('codRelatorio') if isinstance(data, dict) else None


def _situacao(token, cod):
    r = ss._session().options(
        f'{BASE}/ext/relatorio/adesao/{cod}',
        headers={'Authorization': f'Bearer {token}'}, timeout=(10, 30),
    )
    r.raise_for_status()
    try:
        return (r.json().get('situacao', 'PENDENTE') if r.content else 'PENDENTE').upper()
    except Exception:
        return 'PENDENTE'


def _baixar(token, cod):
    r = ss._session().get(
        f'{BASE}/ext/relatorio/adesao/{cod}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        timeout=(15, 300),
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ('itens', 'dados', 'registros', 'content', 'items'):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def coletar(token, meses, timeout_global=1800):
    """Solicita todos os relatórios da janela, aguarda e baixa. Devolve lista
    de adesões (formato bruto do Siprov)."""
    fila = {}
    for (ano, mes) in meses:
        di, df = ss._mes_inicio_fim(ano, mes)
        try:
            cod = _solicitar(token, di, df)
            if cod:
                fila[(ano, mes)] = cod
                log.info(f'  solicitado {mes:02d}/{ano} -> cod={cod}')
            else:
                log.error(f'  sem codRelatorio para {mes:02d}/{ano}')
        except Exception as e:
            log.error(f'  erro ao solicitar {mes:02d}/{ano}: {e}')

    todos = []
    pendentes = dict(fila)
    t0 = time.time()
    while pendentes and (time.time() - t0) < timeout_global:
        prontos = []
        for chave, cod in list(pendentes.items()):
            try:
                sit = _situacao(token, cod)
                if sit not in ('PENDENTE', 'PROCESSANDO', 'EM PROCESSAMENTO'):
                    prontos.append((chave, cod, sit))
            except Exception as e:
                log.warning(f'  erro verificar cod={cod}: {e}')
        for chave, cod, sit in prontos:
            pendentes.pop(chave)
            ano, mes = chave
            if sit == 'FINALIZADO':
                try:
                    itens = _baixar(token, cod)
                    todos.extend(itens)
                    log.info(f'  baixado {mes:02d}/{ano}: {len(itens)} adesões')
                    _set(adesoes=len(todos), mensagem=f'{len(todos)} adesões baixadas…')
                except Exception as e:
                    log.error(f'  erro baixar {mes:02d}/{ano}: {e}')
            else:
                log.warning(f'  {mes:02d}/{ano} encerrou com situacao={sit} (ignorado)')
        if pendentes:
            time.sleep(15)
    if pendentes:
        for (ano, mes), cod in pendentes.items():
            log.error(f'  TIMEOUT {mes:02d}/{ano} cod={cod} não finalizou')
    return todos


def _meses_janela():
    """Janela padrão de sync: mês corrente + SIPROV_ADESAO_MESES_BACK anteriores."""
    back = int(os.environ.get('SIPROV_ADESAO_MESES_BACK', '1'))
    hoje = date.today()
    y, m = hoje.year, hoje.month
    meses = []
    for _ in range(back + 1):
        meses.append((y, m))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    meses.reverse()
    return meses


# ─────────────────────────────────────────────
#  API PÚBLICA
# ─────────────────────────────────────────────

def sincronizar(meses=None):
    """Coleta a janela de meses (default: corrente + anteriores) e grava no
    banco (tabela `adesoes`). Meses congelados são preservados automaticamente."""
    inicio = datetime.now()
    _set(status='coletando', mensagem='Coletando adesões…',
         iniciado_em=inicio.isoformat(), concluido_em=None, adesoes=0)
    log.info('=' * 50)
    log.info(f'SYNC ADESÕES — {inicio.strftime("%d/%m/%Y %H:%M:%S")}')

    db_adesoes.init_db()
    try:
        token = ss.autenticar()
    except Exception as e:
        log.error(f'Falha na autenticação: {e}')
        _set(status='erro', mensagem=f'Falha na autenticação: {e}',
             concluido_em=datetime.now().isoformat())
        return None

    if meses is None:
        meses = _meses_janela()
    # Pula meses já CONGELADOS (fechados) — busca só o(s) mês(es) vivo(s).
    congelados = [(a, m) for (a, m) in meses if db_adesoes.is_congelado(a, m)]
    meses = [(a, m) for (a, m) in meses if not db_adesoes.is_congelado(a, m)]
    if congelados:
        log.info(f'  Meses CONGELADOS pulados: {[f"{m:02d}/{a}" for (a, m) in congelados]}')
    log.info(f'  Janela (vivos): {meses}')
    if not meses:
        log.info('  Nenhum mes vivo a buscar (todos congelados).')
        _set(status='ok', mensagem='Nada a buscar (todos congelados).',
             concluido_em=datetime.now().isoformat())
        return None

    adesoes = coletar(token, meses)
    if not adesoes:
        log.warning('Nenhuma adesão retornada pela API.')
        _set(status='erro', mensagem='Nenhuma adesão retornada.',
             concluido_em=datetime.now().isoformat())
        return None

    stats = db_adesoes.substituir_periodo(adesoes)
    dur = round((datetime.now() - inicio).total_seconds(), 1)
    log.info(f'  [DB] {stats} — {dur}s')
    _set(status='ok', mensagem=f'{len(adesoes)} adesões sincronizadas ({dur}s).',
         concluido_em=datetime.now().isoformat(), adesoes=len(adesoes))
    return stats


def coletar_mes(ano, mes):
    """Coleta UM mês de adesões e grava no banco. Usado pelo fechamento mensal
    (busca final do mês antes de congelar). Retorna stats ou None."""
    db_adesoes.init_db()
    token = ss.autenticar()
    adesoes = coletar(token, [(ano, mes)])
    if not adesoes:
        log.warning(f'[ADESÕES] {mes:02d}/{ano}: nada retornado pela API.')
        return None
    return db_adesoes.substituir_periodo(adesoes)
