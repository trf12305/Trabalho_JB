# -*- coding: utf-8 -*-
"""
db_adesoes.py — Armazenamento das ADESÕES (fonte do dashboard de Vendas).

Caminho TOTALMENTE separado do financeiro: usa a mesma camada de conexão do
db.py (SQLite local ou PostgreSQL no Railway), mas grava numa tabela própria
`adesoes`. Não toca em nada da tabela `titulos`.

Fonte: relatório Siprov /ext/relatorio/adesao (layout 1393 "dashboard de venda").
Cada registro = uma adesão (uma venda). Agrupa por mês de beneficio_data_adesao.
"""

import json
import logging
from datetime import datetime

import db  # reusa _conn, _cursor, _q, _to_float, _write_lock, USE_POSTGRES

log = logging.getLogger('jb_protecao')


def _ano_mes(reg):
    """(ano, mes) a partir de beneficio_data_adesao (YYYY-MM-DD)."""
    d = str(reg.get('beneficio_data_adesao') or '')
    if len(d) >= 7:
        try:
            return int(d[:4]), int(d[5:7])
        except ValueError:
            pass
    return 0, 0


def init_db():
    """Cria a tabela `adesoes` e índices (SQLite e PostgreSQL)."""
    if db.USE_POSTGRES:
        ddl = '''
            CREATE TABLE IF NOT EXISTS adesoes (
                id                   BIGSERIAL PRIMARY KEY,
                ano                  INTEGER NOT NULL,
                mes                  INTEGER NOT NULL,
                congelado            INTEGER NOT NULL DEFAULT 0,
                beneficio_sequencial TEXT,
                situacao             TEXT,
                data_adesao          TEXT,
                valor_mensalidade    DOUBLE PRECISION,
                valor_veiculo        DOUBLE PRECISION,
                valor_adicionais     DOUBLE PRECISION,
                unidade              TEXT,
                consultor            TEXT,
                representante        TEXT,
                raw_json             TEXT NOT NULL,
                sync_em              TEXT NOT NULL
            )
        '''
    else:
        ddl = '''
            CREATE TABLE IF NOT EXISTS adesoes (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ano                  INTEGER NOT NULL,
                mes                  INTEGER NOT NULL,
                congelado            INTEGER NOT NULL DEFAULT 0,
                beneficio_sequencial TEXT,
                situacao             TEXT,
                data_adesao          TEXT,
                valor_mensalidade    REAL,
                valor_veiculo        REAL,
                valor_adicionais     REAL,
                unidade              TEXT,
                consultor            TEXT,
                representante        TEXT,
                raw_json             TEXT NOT NULL,
                sync_em              TEXT NOT NULL
            )
        '''
    conn = db._conn()
    try:
        cur = db._cursor(conn)
        cur.execute(ddl)
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ad_ano_mes ON adesoes(ano, mes);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ad_unidade ON adesoes(unidade);')
        conn.commit()
    finally:
        conn.close()

    # Migração: adiciona a coluna `congelado` se a tabela já existia sem ela.
    conn = db._conn()
    try:
        cur = db._cursor(conn)
        cur.execute('ALTER TABLE adesoes ADD COLUMN congelado INTEGER NOT NULL DEFAULT 0')
        conn.commit()
        log.info('[ADESOES] Coluna `congelado` adicionada (migração).')
    except Exception:
        conn.rollback()  # coluna já existe — ok
    finally:
        conn.close()

    log.info(f'[ADESOES] Tabela inicializada ({"PostgreSQL" if db.USE_POSTGRES else "SQLite"}).')


def substituir_periodo(registros):
    """Grava adesões usando replace-por-mês (agrupado por mês de adesão).
    Para cada mês presente, apaga e reinsere. Retorna estatística simples."""
    if not registros:
        return {'meses_afetados': 0, 'inseridos': 0}

    grupos = {}
    for r in registros:
        grupos.setdefault(_ano_mes(r), []).append(r)

    agora = datetime.now().isoformat()
    inseridos = 0
    ignorados = 0
    meses = 0
    with db._write_lock:
        conn = db._conn()
        try:
            cur = db._cursor(conn)
            for (ano, mes), itens in grupos.items():
                # Mês CONGELADO (fechado) é imutável — não apaga nem regrava.
                cur.execute(db._q(
                    'SELECT COUNT(*) AS n FROM adesoes WHERE ano=? AND mes=? AND congelado=1'
                ), (ano, mes))
                row = cur.fetchone()
                if (row['n'] if row else 0):
                    ignorados += len(itens)
                    log.info(f'[ADESOES] {mes:02d}/{ano} CONGELADO — {len(itens)} ignorados')
                    continue
                cur.execute(db._q('DELETE FROM adesoes WHERE ano=? AND mes=? AND congelado=0'), (ano, mes))
                linhas = []
                for r in itens:
                    linhas.append((
                        ano, mes,
                        str(r.get('beneficio_sequencial') or '').strip(),
                        str(r.get('beneficio_situacao_atual') or '').strip(),
                        r.get('beneficio_data_adesao'),
                        db._to_float(r.get('beneficio_valor_mensalidade')),
                        db._to_float(r.get('veiculo_valor_veiculo')),
                        db._to_float(r.get('beneficio_planos_adicionais_valor')),
                        (r.get('unidade_nome_fantasia') or r.get('unidade_razao_social') or '').strip(),
                        (r.get('beneficio_nome_consultor') or '').strip(),
                        (r.get('beneficio_representante') or '').strip(),
                        json.dumps(r, ensure_ascii=False),
                        agora,
                    ))
                cur.executemany(db._q('''
                    INSERT INTO adesoes (
                        ano, mes, beneficio_sequencial, situacao, data_adesao,
                        valor_mensalidade, valor_veiculo, valor_adicionais,
                        unidade, consultor, representante, raw_json, sync_em
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                '''), linhas)
                inseridos += len(linhas)
                meses += 1
                log.info(f'[ADESOES] {mes:02d}/{ano}: {len(linhas)} adesões gravadas')
            conn.commit()
        finally:
            conn.close()

    db._meta_set('ultima_sync_adesoes', agora)
    return {'meses_afetados': meses, 'inseridos': inseridos, 'ignorados_congelados': ignorados}


def congelar_mes(ano, mes):
    """Congela (torna imutável) todas as adesões de um (ano, mes).
    Usado no fechamento mensal — vira retrato fixo só para pesquisa."""
    with db._write_lock:
        conn = db._conn()
        try:
            cur = db._cursor(conn)
            cur.execute(db._q('UPDATE adesoes SET congelado=1 WHERE ano=? AND mes=?'), (ano, mes))
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    log.info(f'[ADESOES] {mes:02d}/{ano} CONGELADO — {n} adesões protegidas')
    return n


def descongelar_mes(ano, mes):
    """Reverte o congelamento de um (ano, mes) (uso administrativo)."""
    with db._write_lock:
        conn = db._conn()
        try:
            cur = db._cursor(conn)
            cur.execute(db._q('UPDATE adesoes SET congelado=0 WHERE ano=? AND mes=?'), (ano, mes))
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    log.info(f'[ADESOES] {mes:02d}/{ano} DESCONGELADO — {n} adesões liberadas')
    return n


def ler(ano=None, mes=None):
    """Lê adesões (lista de dicts brutos do Siprov). Filtros opcionais."""
    sql = 'SELECT raw_json FROM adesoes WHERE 1=1'
    params = []
    if ano is not None:
        sql += ' AND ano=?'
        params.append(ano)
    if mes is not None:
        sql += ' AND mes=?'
        params.append(mes)
    conn = db._conn()
    try:
        cur = db._cursor(conn)
        cur.execute(db._q(sql), params)
        rows = cur.fetchall()
    finally:
        conn.close()
    return [json.loads(r['raw_json']) for r in rows]


def is_congelado(ano, mes):
    """True se o (ano, mes) de adesões está CONGELADO (mês fechado)."""
    conn = db._conn()
    try:
        cur = db._cursor(conn)
        cur.execute(db._q(
            'SELECT COUNT(*) AS n FROM adesoes WHERE ano=? AND mes=? AND congelado=1'
        ), (ano, mes))
        row = cur.fetchone()
        return bool(row['n']) if row else False
    finally:
        conn.close()


def contar():
    conn = db._conn()
    try:
        cur = db._cursor(conn)
        cur.execute('SELECT COUNT(*) AS n FROM adesoes')
        return cur.fetchone()['n']
    finally:
        conn.close()


def ultima_sync():
    return db.meta_get('ultima_sync_adesoes')


def migrar_json(caminho_json):
    """Importa um JSON de adesões (já baixado do Siprov) para a tabela."""
    with open(caminho_json, 'r', encoding='utf-8') as f:
        registros = json.load(f)
    init_db()
    stats = substituir_periodo(registros)
    log.info(f'[ADESOES] Migração de {caminho_json}: {stats}')
    return stats
