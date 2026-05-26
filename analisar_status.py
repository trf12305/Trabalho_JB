import json
import os
from collections import Counter

ARQUIVO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'data',
    'dashboard_financeiro.json'
)

# =========================================================
# CARREGAR JSON
# =========================================================

with open(ARQUIVO, 'r', encoding='utf-8') as f:
    dados = json.load(f)

# =========================================================
# LOCALIZAR TABELA
# =========================================================

tabela = []

if isinstance(dados, dict):

    if 'tabela' in dados:
        tabela = dados['tabela']

    else:
        for chave, valor in dados.items():
            if isinstance(valor, list):
                tabela = valor
                break

elif isinstance(dados, list):
    tabela = dados

# =========================================================
# ANÁLISE
# =========================================================

contador_status = Counter()

total_registros = 0
total_valor = 0

total_liquidados = 0
total_pendentes = 0

valor_liquidado = 0
valor_pendente = 0

for item in tabela:

    total_registros += 1

    # STATUS
    status = (
        item.get('titulo_situacao_titulo')
        or 'SEM STATUS'
    ).upper()

    contador_status[status] += 1

    # VALOR
    valor = float(
        item.get('titulo_valor') or 0
    )

    total_valor += valor

    # CONTADORES
    if status == 'LIQUIDADO':

        total_liquidados += 1
        valor_liquidado += valor

    else:

        total_pendentes += 1
        valor_pendente += valor

# =========================================================
# EXIBIÇÃO
# =========================================================

print('\n' + '=' * 60)
print('RESUMO GERAL')
print('=' * 60)

print(f'TOTAL DE REGISTROS: {total_registros:,}')
print(f'VALOR TOTAL: R$ {total_valor:,.2f}')

print('\n' + '=' * 60)
print('STATUS ENCONTRADOS')
print('=' * 60)

for status, qtd in contador_status.items():

    percentual = (
        qtd / total_registros
    ) * 100

    print(f'\nSTATUS: {status}')
    print(f'QUANTIDADE: {qtd}')
    print(f'PERCENTUAL: {percentual:.2f}%')

print('\n' + '=' * 60)
print('ANÁLISE FINANCEIRA')
print('=' * 60)

print(f'''
TOTAL GERAL:
{total_registros}

LIQUIDADOS:
{total_liquidados}

PENDENTES/OUTROS:
{total_pendentes}
''')

print('=' * 60)

print(f'''
VALOR LIQUIDADO:
R$ {valor_liquidado:,.2f}

VALOR PENDENTE:
R$ {valor_pendente:,.2f}
''')

print('=' * 60)