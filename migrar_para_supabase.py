# -*- coding: utf-8 -*-
"""
migrar_para_supabase.py
=======================
Copia TODOS os dados do SQLite LOCAL (data/jb_dados.db) para o PostgreSQL da
Supabase (o banco que o Render usa), PRESERVANDO o estado congelado.
Migra: tabela `titulos` (financeiro), `adesoes` (vendas) e `meta` (ultima_sync).
NÃO altera o SQLite local. Faz REPLACE total no Postgres (apaga e recarrega),
então pode rodar de novo sem duplicar.
------------------------------------------------------------------
COMO RODAR (no seu notebook):
1) Pegue a string de conexão da Supabase. No painel da Supabase:
Project Settings -> Database -> Connection string -> "URI"
(algo como: postgresql://postgres:SENHA@aws-1-sa-east-1.pooler.supabase.com:5432/postgres)
OBS: é a MESMA que está no Render em Settings -> Environment -> DATABASE_URL.

2) No PowerShell, dentro da pasta do projeto:
$env:DATABASE_URL = "postgresql://postgres:SENHA@....supabase.com:5432/postgres"
python migrar_para_supabase.py
(Se faltar a lib:  pip install psycopg2-binary)
------------------------------------------------------------------
"""
import os
import sys
import sqlite3
DB_URL = os.environ.get('DATABASE_URL', '').strip()
if not DB_URL:
    print('ERRO: defina a variavel DATABASE_URL (string da Supabase) antes de rodar.')
    print('  PowerShell:  $env:DATABASE_URL="postgresql://postgres:SENHA@host:5432/postgres"')
    sys.exit(1)
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql://', 1)
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print('ERRO: psycopg2 nao instalado. Rode:  pip install psycopg2-binary')
    sys.exit(1)
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'jb_dados.db')
if not os.path.exists(SQLITE_PATH):
    print(f'ERRO: SQLite local nao encontrado em {SQLITE_PATH}')
    sys.exit(1)
DDL_TITULOS = '''
CREATE TABLE IF NOT EXISTS titulos (
    id BIGSERIAL PRIMARY KEY, ano INTEGER NOT NULL, mes INTEGER NOT NULL,
    congelado INTEGER NOT NULL DEFAULT 0, beneficio_sequencial TEXT,
    titulo_parcela TEXT, situacao TEXT, data_vencimento TEXT, data_liquidacao TEXT,
    valor_titulo DOUBLE PRECISION, valor_liquidado DOUBLE PRECISION, unidade TEXT,
    raw_json TEXT NOT NULL, sync_em TEXT NOT NULL )'''
DDL_ADESOES = '''
CREATE TABLE IF NOT EXISTS adesoes (
    id BIGSERIAL PRIMARY KEY, ano INTEGER NOT NULL, mes INTEGER NOT NULL,
    congelado INTEGER NOT NULL DEFAULT 0, beneficio_sequencial TEXT, situacao TEXT,
    data_adesao TEXT, valor_mensalidade DOUBLE PRECISION, valor_veiculo DOUBLE PRECISION,
    valor_adicionais DOUBLE PRECISION, unidade TEXT, consultor TEXT, representante TEXT,
    raw_json TEXT NOT NULL, sync_em TEXT NOT NULL )'''
DDL_META = 'CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT)'
COLS_TITULOS = ['ano', 'mes', 'congelado', 'beneficio_sequencial', 'titulo_parcela',
                'situacao', 'data_vencimento', 'data_liquidacao', 'valor_titulo',
                'valor_liquidado', 'unidade', 'raw_json', 'sync_em']
COLS_ADESOES = ['ano', 'mes', 'congelado', 'beneficio_sequencial', 'situacao',
                'data_adesao', 'valor_mensalidade', 'valor_veiculo', 'valor_adicionais',
                'unidade', 'consultor', 'representante', 'raw_json', 'sync_em']
INDICES = [
    'CREATE INDEX IF NOT EXISTS idx_ano_mes   ON titulos(ano, mes)',
    'CREATE INDEX IF NOT EXISTS idx_congelado ON titulos(congelado)',
    'CREATE INDEX IF NOT EXISTS idx_situacao  ON titulos(situacao)',
    'CREATE INDEX IF NOT EXISTS idx_ad_ano_mes ON adesoes(ano, mes)',
    'CREATE INDEX IF NOT EXISTS idx_ad_unidade ON adesoes(unidade)',
]
def migrar(scur, pcur, tabela, cols, ddl):
    pcur.execute(ddl)
    pcur.execute(f'DELETE FROM {tabela}')               # replace total
    scur.execute(f'SELECT {",".join(cols)} FROM {tabela}')
    sql = f'INSERT INTO {tabela} ({",".join(cols)}) VALUES %s'
    total = 0
    while True:
        lote = scur.fetchmany(2000)
        if not lote:
            break
        psycopg2.extras.execute_values(pcur, sql, [tuple(r) for r in lote], page_size=500)
        total += len(lote)
        print(f'    {tabela}: {total} linhas...', end='\r')
    print(f'    {tabela}: {total} linhas migradas.   ')
    return total
def main():
    print('=' * 60)
    print('MIGRACAO SQLite local  ->  Supabase (PostgreSQL)')
    print('=' * 60)
    print('Origem :', SQLITE_PATH)
    print('Destino:', DB_URL.split('@')[-1])   # mostra host, sem a senha
    print('-' * 60)
    s = sqlite3.connect(SQLITE_PATH)
    s.row_factory = sqlite3.Row
    p = psycopg2.connect(DB_URL, connect_timeout=30)
    try:
        scur = s.cursor()
        pcur = p.cursor()
        n_t = migrar(scur, pcur, 'titulos', COLS_TITULOS, DDL_TITULOS)
        p.commit()                       # commit por tabela (mais seguro)
        n_a = migrar(scur, pcur, 'adesoes', COLS_ADESOES, DDL_ADESOES)
        p.commit()
        # meta (ultima_sync, ultima_sync_adesoes)
        pcur.execute(DDL_META)
        pcur.execute('DELETE FROM meta')
        scur.execute('SELECT chave, valor FROM meta')
        metas = [(r['chave'], r['valor']) for r in scur.fetchall()]
        if metas:
            psycopg2.extras.execute_values(
                pcur, 'INSERT INTO meta (chave, valor) VALUES %s', metas)
        print(f'    meta: {len(metas)} chaves migradas.')
        for ddl in INDICES:
            pcur.execute(ddl)
        p.commit()
        print('-' * 60)
        print(f'OK! Migrado: {n_t} titulos + {n_a} adesoes + {len(metas)} meta.')
        print('Abra o dashboard do Render e confira (incl. filtro Ano Passado/2025).')
    except Exception as e:
        p.rollback()
        print('\nERRO na migracao (nada foi commitado):', e)
        raise
    finally:
        s.close()
        p.close()
if __name__ == '__main__':
    main()