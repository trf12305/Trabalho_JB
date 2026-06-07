# -*- coding: utf-8 -*-
"""
backfill_meses.py — Busca um intervalo de meses do FINANCEIRO (CREDITO) no
Siprov, grava no banco e CONGELA cada mês buscado (histórico só p/ pesquisa).

Uso:
    python backfill_meses.py ANO MES_INI MES_FIM
    ex.:  python backfill_meses.py 2026 1 5     # Jan..Mai/2026 (fechados)

Reexecutável: meses já congelados são preservados pelo replace-por-mês; este
script congela explicitamente cada mês do intervalo ao final.
NÃO congela o mês corrente — passe apenas meses já FECHADOS.
"""
import sys
import time
import logging

import siprov_sync as ss
import db as bancodados

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("backfill_meses")

TIPO = "CREDITO"
SITUACOES = ["ABERTO", "PENDENTE", "LIQUIDADO"]
FILTRO = "vencimento"
TIMEOUT_GLOBAL = 2400


def coletar(token, meses):
    fila = {}
    for (ano, mes) in meses:
        di, df = ss._mes_inicio_fim(ano, mes)
        try:
            cod = ss._solicitar_relatorio(token, TIPO, SITUACOES, di, df, FILTRO)
            if cod:
                fila[(ano, mes)] = cod
                log.info(f"  solicitado {mes:02d}/{ano} -> cod={cod}")
            else:
                log.error(f"  sem codRelatorio para {mes:02d}/{ano}")
        except Exception as e:
            log.error(f"  erro ao solicitar {mes:02d}/{ano}: {e}")

    todos = []
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
                    log.info(f"  baixado {mes:02d}/{ano}: {len(itens)} titulos")
                except Exception as e:
                    log.error(f"  erro baixar {mes:02d}/{ano}: {e}")
            else:
                log.warning(f"  {mes:02d}/{ano} encerrou com situacao={sit} (ignorado)")
        if pendentes:
            time.sleep(15)
    if pendentes:
        for (ano, mes), cod in pendentes.items():
            log.error(f"  TIMEOUT {mes:02d}/{ano} cod={cod} nao finalizou")
    return todos


def main():
    if len(sys.argv) < 4:
        print("uso: python backfill_meses.py ANO MES_INI MES_FIM")
        return 1
    ano = int(sys.argv[1]); mi = int(sys.argv[2]); mf = int(sys.argv[3])
    meses = [(ano, m) for m in range(mi, mf + 1)]
    log.info(f"=== BACKFILL {ano} meses {mi}..{mf} (CREDITO) ===")

    bancodados.init_db()
    token = ss.autenticar()
    titulos = coletar(token, meses)
    log.info(f"TOTAL coletado: {len(titulos)} titulos")
    if not titulos:
        log.error("Nada retornado pelo Siprov. Nada gravado. Reexecute.")
        return 2

    stats = bancodados.substituir_periodo(titulos)
    log.info(f"Gravado: {stats}")

    # Congela cada mês fechado do intervalo (somente os que de fato chegaram).
    e = bancodados.estatisticas()
    presentes = {(p['ano'], p['mes']) for p in e['periodos']}
    for (ano, mes) in meses:
        if (ano, mes) in presentes:
            n = bancodados.congelar_mes(ano, mes)
            log.info(f"  CONGELADO {mes:02d}/{ano}: {n} titulos")
        else:
            log.warning(f"  {mes:02d}/{ano} ausente no banco — NAO congelado (reexecute).")

    e = bancodados.estatisticas()
    print("\n--- PERIODOS NO BANCO ---")
    for p in e['periodos']:
        flag = "[CONG]" if p['congelado'] else ""
        print(f"  {p['mes']:02d}/{p['ano']}  titulos={p['titulos']:>6} {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
