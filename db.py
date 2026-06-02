# -*- coding: utf-8 -*-
"""
Banco de dados local (SQLite) — JB Proteção.

Estratégia "replace por mês":
  - Cada título é gravado com seu ano/mês (derivado de titulo_data_vencimento).
  - Ao sincronizar um mês, apaga os títulos daquele mês (se NÃO congelado) e
    reinsere todos do sync. Bate exato com o Siprov, sem duplicar.
  - Meses/anos CONGELADOS (fechados) nunca são apagados — preservam histórico
    para comparações YoY mesmo que o Siprov mude.

O dashboard lê do banco no MESMO formato que lia do JSON (lista de dicts),
então o restante do app.py não precisa mudar de lógica.
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime

log = logging.getLogger('jb_protecao')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'jb_dados.db')

# Lock para escrita (SQLite aceita 1 escritor por vez)
_write_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')      # melhor concorrência leitura/escrita
    conn.execute('PRAGMA synchronous=NORMAL;')
    return conn


def init_db():
    """Cria a tabela e índices se não existirem."""
    with _conn() as conn:
        conn.execute('''
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
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ano_mes   ON titulos(ano, mes);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_congelado ON titulos(congelado);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_situacao  ON titulos(situacao);')
        # Metadados (última sync, etc.)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS meta (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
        ''')
    log.info(f'[DB] Banco inicializado em {DB_PATH}')


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
    return 0, 0  # sem data de vencimento → grupo "0/0"


def _meta_set(chave, valor):
    with _conn() as conn:
        conn.execute(
            'INSERT INTO meta(chave, valor) VALUES(?, ?) '
            'ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor',
            (chave, str(valor))
        )


def meta_get(chave, default=None):
    with _conn() as conn:
        row = conn.execute('SELECT valor FROM meta WHERE chave=?', (chave,)).fetchone()
        return row['valor'] if row else default


def substituir_periodo(registros):
    """
    Grava uma lista de registros (formato JSON do Siprov) no banco usando
    a estratégia replace-por-mês. Agrupa por (ano, mes) e, para cada grupo
    NÃO congelado, apaga o que existe e reinsere.

    Retorna dict com estatísticas: {meses_afetados, inseridos, ignorados_congelados}
    """
    if not registros:
        return {'meses_afetados': 0, 'inseridos': 0, 'ignorados_congelados': 0}

    # Agrupa por (ano, mes)
    grupos = {}
    for r in registros:
        chave = _ano_mes(r)
        grupos.setdefault(chave, []).append(r)

    agora = datetime.now().isoformat()
    inseridos = 0
    ignorados = 0
    meses_afetados = 0

    with _write_lock, _conn() as conn:
        for (ano, mes), itens in grupos.items():
            # Verifica se o período está congelado
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM titulos '
                'WHERE ano=? AND mes=? AND congelado=1',
                (ano, mes)
            ).fetchone()
            if row['n'] > 0:
                ignorados += len(itens)
                log.info(f'[DB] {mes:02d}/{ano} está CONGELADO — {len(itens)} registros ignorados')
                continue

            # Apaga período não congelado e reinsere
            conn.execute(
                'DELETE FROM titulos WHERE ano=? AND mes=? AND congelado=0',
                (ano, mes)
            )
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
            conn.executemany('''
                INSERT INTO titulos (
                    ano, mes, congelado, beneficio_sequencial, titulo_parcela,
                    situacao, data_vencimento, data_liquidacao, valor_titulo,
                    valor_liquidado, unidade, raw_json, sync_em
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', linhas)
            inseridos += len(linhas)
            meses_afetados += 1
            log.info(f'[DB] {mes:02d}/{ano}: {len(linhas)} registros gravados')

    _meta_set('ultima_sync', agora)
    return {
        'meses_afetados': meses_afetados,
        'inseridos': inseridos,
        'ignorados_congelados': ignorados,
    }


def ler_titulos(ano=None, mes=None, incluir_congelados=True):
    """
    Lê títulos do banco e devolve no MESMO formato do JSON original
    (lista de dicts brutos do Siprov). Filtros opcionais por ano/mês.
    """
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
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(row['raw_json']) for row in rows]


def congelar_ano(ano):
    """Marca todos os títulos de um ano como congelados (imutáveis)."""
    with _write_lock, _conn() as conn:
        cur = conn.execute(
            'UPDATE titulos SET congelado=1 WHERE ano=?', (ano,)
        )
        n = cur.rowcount
    log.info(f'[DB] Ano {ano} CONGELADO — {n} títulos protegidos')
    return n


def descongelar_ano(ano):
    """Reverte o congelamento de um ano (uso administrativo)."""
    with _write_lock, _conn() as conn:
        cur = conn.execute(
            'UPDATE titulos SET congelado=0 WHERE ano=?', (ano,)
        )
        n = cur.rowcount
    log.info(f'[DB] Ano {ano} DESCONGELADO — {n} títulos liberados')
    return n


def estatisticas():
    """Resumo do conteúdo do banco para diagnóstico/admin."""
    with _conn() as conn:
        total = conn.execute('SELECT COUNT(*) AS n FROM titulos').fetchone()['n']
        por_periodo = conn.execute('''
            SELECT ano, mes, congelado, COUNT(*) AS n,
                   ROUND(SUM(valor_titulo), 2) AS face
            FROM titulos
            GROUP BY ano, mes, congelado
            ORDER BY ano, mes
        ''').fetchall()
    return {
        'total': total,
        'db_path': DB_PATH,
        'ultima_sync': meta_get('ultima_sync'),
        'periodos': [
            {
                'ano': r['ano'], 'mes': r['mes'],
                'congelado': bool(r['congelado']),
                'titulos': r['n'], 'face': r['face'],
            }
            for r in por_periodo
        ],
    }


def contar():
    """Total de títulos no banco (rápido)."""
    with _conn() as conn:
        return conn.execute('SELECT COUNT(*) AS n FROM titulos').fetchone()['n']


def migrar_json(caminho_json, congelar=False):
    """
    Importa um arquivo JSON existente para o banco.
    Use congelar=True para marcar como histórico imutável (anos passados).
    """
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
