# -*- coding: utf-8 -*-
"""
Banco de dados — JB Proteção (SQLite local OU PostgreSQL no Railway).

Detecção automática:
  - Se a variável de ambiente DATABASE_URL existir (Railway injeta quando
    você adiciona um Postgres) → usa PostgreSQL (persiste sempre, 24/7).
  - Senão → usa SQLite local (data/jb_dados.db), igual antes.

A interface pública (substituir_periodo, ler_titulos, congelar_ano,
descongelar_ano, estatisticas, migrar_json, contar, meta_get) é IDÊNTICA
nos dois modos — o app.py não precisa saber qual banco está embaixo.

Estratégia "replace por mês":
  - Cada título é gravado com seu ano/mês (de titulo_data_vencimento).
  - Ao sincronizar um mês NÃO congelado, apaga os títulos daquele mês e
    reinsere os do sync. Bate exato com o Siprov, sem duplicar.
  - Meses/anos CONGELADOS (fechados) nunca são apagados — preservam o
    histórico para comparações YoY mesmo que o Siprov mude.
"""

import os
import json
import logging
import threading
from datetime import datetime

log = logging.getLogger('jb_protecao')

# =========================================================
# DETECÇÃO DO BANCO
# =========================================================
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = bool(DATABASE_URL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'data', 'jb_dados.db')   # usado só no SQLite

# Lock para escrita (SQLite aceita 1 escritor por vez; inofensivo no PG)
_write_lock = threading.Lock()

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Railway às vezes entrega "postgres://" — psycopg2 quer "postgresql://"
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    log.info('[DB] Modo PostgreSQL (Railway) detectado via DATABASE_URL.')
else:
    import sqlite3
    log.info('[DB] Modo SQLite local.')


# =========================================================
# CAMADA DE CONEXÃO / PORTABILIDADE
# =========================================================

def _conn():
    """Retorna uma conexão pronta, com row-as-dict, no banco ativo."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
        return conn
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        return conn


def _cursor(conn):
    """Cursor que devolve linhas acessíveis por nome de coluna nos dois bancos."""
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def _q(sql):
    """Adapta placeholders: o código usa '?'; Postgres quer '%s'."""
    return sql.replace('?', '%s') if USE_POSTGRES else sql


# =========================================================
# SCHEMA
# =========================================================

def init_db():
    """Cria a tabela e índices se não existirem (nos dois bancos)."""
    if USE_POSTGRES:
        ddl_titulos = '''
            CREATE TABLE IF NOT EXISTS titulos (
                id                   BIGSERIAL PRIMARY KEY,
                ano                  INTEGER NOT NULL,
                mes                  INTEGER NOT NULL,
                congelado            INTEGER NOT NULL DEFAULT 0,
                beneficio_sequencial TEXT,
                titulo_parcela       TEXT,
                situacao             TEXT,
                data_vencimento      TEXT,
                data_liquidacao      TEXT,
                valor_titulo         DOUBLE PRECISION,
                valor_liquidado      DOUBLE PRECISION,
                unidade              TEXT,
                raw_json             TEXT NOT NULL,
                sync_em              TEXT NOT NULL
            )
        '''
    else:
        ddl_titulos = '''
            CREATE TABLE IF NOT EXISTS titulos (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ano                  INTEGER NOT NULL,
                mes                  INTEGER NOT NULL,
                congelado            INTEGER NOT NULL DEFAULT 0,
                beneficio_sequencial TEXT,
                titulo_parcela       TEXT,
                situacao             TEXT,
                data_vencimento      TEXT,
                data_liquidacao      TEXT,
                valor_titulo         REAL,
                valor_liquidado      REAL,
                unidade              TEXT,
                raw_json             TEXT NOT NULL,
                sync_em              TEXT NOT NULL
            )
        '''
    conn = _conn()
    try:
        cur = _cursor(conn)
        cur.execute(ddl_titulos)
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ano_mes   ON titulos(ano, mes);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_congelado ON titulos(congelado);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_situacao  ON titulos(situacao);')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS meta (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()
    log.info(f'[DB] Banco inicializado ({"PostgreSQL" if USE_POSTGRES else "SQLite"}).')


# =========================================================
# HELPERS
# =========================================================

def _to_float(v):
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace('R$', '').strip()
        if ',' in s:
            s = s.replace('.', '').replace(',', '.')
        return float(s)
    except Exception:
        return 0.0


def _ano_mes(registro):
    """Extrai (ano, mes) de titulo_data_vencimento (YYYY-MM-DD)."""
    dv = str(registro.get('titulo_data_vencimento') or '')
    if len(dv) >= 7:
        try:
            return int(dv[:4]), int(dv[5:7])
        except ValueError:
            pass
    return 0, 0


def _meta_set(chave, valor):
    conn = _conn()
    try:
        cur = _cursor(conn)
        if USE_POSTGRES:
            cur.execute(_q(
                'INSERT INTO meta(chave, valor) VALUES(?, ?) '
                'ON CONFLICT(chave) DO UPDATE SET valor=EXCLUDED.valor'
            ), (chave, str(valor)))
        else:
            cur.execute(
                'INSERT INTO meta(chave, valor) VALUES(?, ?) '
                'ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor',
                (chave, str(valor))
            )
        conn.commit()
    finally:
        conn.close()


def meta_get(chave, default=None):
    conn = _conn()
    try:
        cur = _cursor(conn)
        cur.execute(_q('SELECT valor FROM meta WHERE chave=?'), (chave,))
        row = cur.fetchone()
        if not row:
            return default
        return row['valor'] if USE_POSTGRES else row['valor']
    finally:
        conn.close()


# =========================================================
# GRAVAÇÃO — replace por mês
# =========================================================

def substituir_periodo(registros):
    """Grava registros (formato JSON do Siprov) usando replace-por-mês.
    Agrupa por (ano, mes); para cada grupo NÃO congelado, apaga e reinsere.
    Retorna {meses_afetados, inseridos, ignorados_congelados}."""
    if not registros:
        return {'meses_afetados': 0, 'inseridos': 0, 'ignorados_congelados': 0}

    grupos = {}
    for r in registros:
        grupos.setdefault(_ano_mes(r), []).append(r)

    agora = datetime.now().isoformat()
    inseridos = 0
    ignorados = 0
    meses_afetados = 0

    with _write_lock:
        conn = _conn()
        try:
            cur = _cursor(conn)
            for (ano, mes), itens in grupos.items():
                cur.execute(_q(
                    'SELECT COUNT(*) AS n FROM titulos '
                    'WHERE ano=? AND mes=? AND congelado=1'
                ), (ano, mes))
                row = cur.fetchone()
                n_cong = row['n'] if USE_POSTGRES else row['n']
                if n_cong and n_cong > 0:
                    ignorados += len(itens)
                    log.info(f'[DB] {mes:02d}/{ano} CONGELADO — {len(itens)} ignorados')
                    continue

                cur.execute(_q(
                    'DELETE FROM titulos WHERE ano=? AND mes=? AND congelado=0'
                ), (ano, mes))

                linhas = []
                for r in itens:
                    linhas.append((
                        ano, mes, 0,
                        str(r.get('beneficio_sequencial') or '').strip(),
                        str(r.get('titulo_parcela') or '').strip(),
                        str(r.get('titulo_situacao_titulo') or '').strip().upper(),
                        r.get('titulo_data_vencimento'),
                        r.get('liquidacao_data_liquidacao'),
                        _to_float(r.get('titulo_valor')),
                        _to_float(r.get('liquidacao_valor_liquidado')),
                        (r.get('unidade_nome_fantasia') or '').strip(),
                        json.dumps(r, ensure_ascii=False),
                        agora,
                    ))
                cur.executemany(_q('''
                    INSERT INTO titulos (
                        ano, mes, congelado, beneficio_sequencial, titulo_parcela,
                        situacao, data_vencimento, data_liquidacao, valor_titulo,
                        valor_liquidado, unidade, raw_json, sync_em
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                '''), linhas)
                inseridos += len(linhas)
                meses_afetados += 1
                log.info(f'[DB] {mes:02d}/{ano}: {len(linhas)} registros gravados')
            conn.commit()
        finally:
            conn.close()

    _meta_set('ultima_sync', agora)
    return {
        'meses_afetados': meses_afetados,
        'inseridos': inseridos,
        'ignorados_congelados': ignorados,
    }


# =========================================================
# LEITURA
# =========================================================

def ler_titulos(ano=None, mes=None, incluir_congelados=True):
    """Lê títulos e devolve no MESMO formato do JSON original
    (lista de dicts brutos do Siprov). Filtros opcionais por ano/mês."""
    sql = 'SELECT raw_json FROM titulos WHERE 1=1'
    params = []
    if ano is not None:
        sql += ' AND ano=?'
        params.append(ano)
    if mes is not None:
        sql += ' AND mes=?'
        params.append(mes)
    if not incluir_congelados:
        sql += ' AND congelado=0'
    conn = _conn()
    try:
        cur = _cursor(conn)
        cur.execute(_q(sql), params)
        rows = cur.fetchall()
    finally:
        conn.close()
    return [json.loads(r['raw_json']) for r in rows]


# =========================================================
# CONGELAMENTO
# =========================================================

def congelar_ano(ano):
    """Marca todos os títulos de um ano como congelados (imutáveis)."""
    with _write_lock:
        conn = _conn()
        try:
            cur = _cursor(conn)
            cur.execute(_q('UPDATE titulos SET congelado=1 WHERE ano=?'), (ano,))
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    log.info(f'[DB] Ano {ano} CONGELADO — {n} títulos protegidos')
    return n


def descongelar_ano(ano):
    """Reverte o congelamento de um ano (uso administrativo)."""
    with _write_lock:
        conn = _conn()
        try:
            cur = _cursor(conn)
            cur.execute(_q('UPDATE titulos SET congelado=0 WHERE ano=?'), (ano,))
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    log.info(f'[DB] Ano {ano} DESCONGELADO — {n} títulos liberados')
    return n


# =========================================================
# DIAGNÓSTICO
# =========================================================

def estatisticas():
    """Resumo do conteúdo do banco para diagnóstico/admin."""
    conn = _conn()
    try:
        cur = _cursor(conn)
        cur.execute('SELECT COUNT(*) AS n FROM titulos')
        total = cur.fetchone()['n']
        cur.execute('''
            SELECT ano, mes, congelado, COUNT(*) AS n,
                   ROUND(CAST(SUM(valor_titulo) AS NUMERIC), 2) AS face
            FROM titulos
            GROUP BY ano, mes, congelado
            ORDER BY ano, mes
        ''' if USE_POSTGRES else '''
            SELECT ano, mes, congelado, COUNT(*) AS n,
                   ROUND(SUM(valor_titulo), 2) AS face
            FROM titulos
            GROUP BY ano, mes, congelado
            ORDER BY ano, mes
        ''')
        periodos = cur.fetchall()
    finally:
        conn.close()
    return {
        'total': total,
        'banco': 'PostgreSQL' if USE_POSTGRES else 'SQLite',
        'db_path': DATABASE_URL.split('@')[-1] if USE_POSTGRES else DB_PATH,
        'ultima_sync': meta_get('ultima_sync'),
        'periodos': [
            {
                'ano': r['ano'], 'mes': r['mes'],
                'congelado': bool(r['congelado']),
                'titulos': r['n'], 'face': float(r['face'] or 0),
            }
            for r in periodos
        ],
    }


def contar():
    """Total de títulos no banco (rápido)."""
    conn = _conn()
    try:
        cur = _cursor(conn)
        cur.execute('SELECT COUNT(*) AS n FROM titulos')
        return cur.fetchone()['n']
    finally:
        conn.close()


def migrar_json(caminho_json, congelar=False):
    """Importa um arquivo JSON existente para o banco.
    Use congelar=True para marcar como histórico imutável (anos passados)."""
    with open(caminho_json, 'r', encoding='utf-8') as f:
        registros = json.load(f)
    stats = substituir_periodo(registros)
    if congelar:
        anos = {_ano_mes(r)[0] for r in registros}
        for ano in anos:
            if ano > 0:
                congelar_ano(ano)
    log.info(f'[DB] Migração de {os.path.basename(caminho_json)}: {stats}')
    return stats
