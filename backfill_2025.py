# -*- coding: utf-8 -*-
"""
backfill_2025.py
================
Importa o ano de 2025 INTEIRO (so CREDITO / contas a receber) do Siprov
direto para o BANCO DE DADOS, marcando como CONGELADO (historico imutavel
para o YoY). NAO escreve no dashboard_financeiro_live.json e NAO altera a
visao atual do dashboard.

Uso:
    python backfill_2025.py            # ano todo (2025-01 a 2025-12)
    python backfill_2025.py 1 6        # apenas meses 1..6 de 2025 (retry parcial)

Pode ser re-executado a qualquer momento: o replace-por-mes regrava o mes
e, no fim, congela 2025. Meses que o Siprov devolver vazios sao apenas
pulados (nao apagam o que ja existe).
"""

import sys
import time
import logging

import siprov_sync as ss
import db as bancodados

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_2025")

ANO = 2025
TIPO = "CREDITO"                       # so contas a receber
SITUACOES = ["ABERTO", "PENDENTE", "LIQUIDADO"]
FILTRO = "vencimento"
TIMEOUT_GLOBAL = 2400                  # 40 min no total para os relatorios


def coletar_ano(token, meses):
    """Solicita todos os relatorios do periodo, aguarda e baixa em paralelo.
    Devolve lista de titulos (formato Siprov)."""
    fila = {}   # (ano, mes) -> codRelatorio
    for mes in meses:
        di, df = ss._mes_inicio_fim(ANO, mes)
        try:
            cod = ss._solicitar_relatorio(token, TIPO, SITUACOES, di, df, FILTRO)
            if cod:
                fila[(ANO, mes)] = cod
                log.info(f"  solicitado {mes:02d}/{ANO} -> cod={cod}")
            else:
                log.error(f"  sem codRelatorio para {mes:02d}/{ANO}")
        except Exception as e:
            log.error(f"  erro ao solicitar {mes:02d}/{ANO}: {e}")

    log.info(f"{len(fila)} relatorios solicitados. Aguardando...")
    todos = []
    por_mes = {}
    pendentes = dict(fila)
    t0 = time.time()
    while pendentes and (time.time() - t0) < TIMEOUT_GLOBAL:
        prontos = []
        for chave, cod in list(pendentes.items()):
            try:
                data = ss._options(token, f"/ext/relatorio/financeiro/{cod}")
                sit = (data.get("situacao", "PENDENTE") if data else "PENDENTE").upper()
                if sit not in ("PENDENTE", "PROCESSANDO", "EM PROCESSAMENTO"):
                    prontos.append((chave, cod, sit))
            except Exception as e:
                log.warning(f"  erro verificar cod={cod}: {e}")
        for chave, cod, sit in prontos:
            pendentes.pop(chave)
            ano, mes = chave
            if sit == "FINALIZADO":
                try:
                    itens = ss._baixar_relatorio(token, cod)
                    todos.extend(itens)
                    por_mes[mes] = len(itens)
                    log.info(f"  baixado {mes:02d}/{ANO}: {len(itens)} titulos")
                except Exception as e:
                    log.error(f"  erro baixar {mes:02d}/{ANO}: {e}")
            else:
                log.warning(f"  {mes:02d}/{ANO} encerrou com situacao={sit} (ignorado)")
        if pendentes:
            time.sleep(15)
    if pendentes:
        for (ano, mes), cod in pendentes.items():
            log.error(f"  TIMEOUT {mes:02d}/{ANO} cod={cod} nunca finalizou")
    return todos, por_mes


def main():
    meses = list(range(1, 13))
    if len(sys.argv) >= 3:
        meses = list(range(int(sys.argv[1]), int(sys.argv[2]) + 1))
    log.info(f"=== BACKFILL {ANO} (CREDITO) meses={meses} ===")

    bancodados.init_db()
    token = ss.autenticar()
    titulos, por_mes = coletar_ano(token, meses)
    log.info(f"TOTAL coletado: {len(titulos)} titulos | por mes: {por_mes}")

    if not titulos:
        log.error("Nenhum titulo retornado pelo Siprov. Nada gravado. "
                  "Tente novamente (instabilidade da API).")
        return 2

    stats = bancodados.substituir_periodo(titulos)
    log.info(f"Gravado no banco: {stats}")

    # So congela 2025 quando TODOS os 12 meses estiverem presentes — assim
    # uma execucao parcial (retry) nao bloqueia a insercao dos meses faltantes.
    e = bancodados.estatisticas()
    meses_2025 = {p["mes"] for p in e["periodos"] if p["ano"] == ANO}
    faltam = sorted(set(range(1, 13)) - meses_2025)
    if not faltam:
        n = bancodados.congelar_ano(ANO)
        log.info(f"Ano {ANO} COMPLETO -> CONGELADO: {n} titulos protegidos")
    else:
        log.warning(f"Ano {ANO} ainda INCOMPLETO (faltam meses {faltam}) — "
                    f"NAO congelado. Rode de novo para os meses faltantes.")

    # Conferencia
    e = bancodados.estatisticas()
    print("\n--- PERIODOS NO BANCO APOS BACKFILL ---")
    for p in e["periodos"]:
        flag = "[CONG]" if p["congelado"] else ""
        print(f"  {p['mes']:02d}/{p['ano']}  titulos={p['titulos']:>6}  face=R$ {p['face']:>14,.2f} {flag}")
    print(f"  total no banco: {e['total']}")
    return 0 if not faltam else 3


if __name__ == "__main__":
    sys.exit(main())
