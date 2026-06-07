# -*- coding: utf-8 -*-
"""
backfill_adesoes.py — Busca um intervalo de meses de ADESÃO (layout 1393) no
Siprov, grava na tabela `adesoes` e CONGELA cada mês do intervalo.

Uso:
    python backfill_adesoes.py ANO MES_INI MES_FIM
    ex.:  python backfill_adesoes.py 2025 1 12
          python backfill_adesoes.py 2026 1 5

Loga os codRelatorio solicitados (para retomar timeouts pelos cods, sem regerar).
NÃO passe o mês corrente (ele deve ficar vivo).
"""
import sys
import time
import logging

import siprov_sync as ss
import siprov_adesao as sa
import db_adesoes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backfill_adesoes")

TIMEOUT_GLOBAL = 2400


def coletar(token, meses):
    fila = {}
    for (ano, mes) in meses:
        di, df = ss._mes_inicio_fim(ano, mes)
        try:
            cod = sa._solicitar(token, di, df)
            if cod:
                fila[(ano, mes)] = cod
                log.info(f"  solicitado {mes:02d}/{ano} -> cod={cod}")
            else:
                log.error(f"  sem codRelatorio para {mes:02d}/{ano}")
        except Exception as e:
            log.error(f"  erro ao solicitar {mes:02d}/{ano}: {e}")

    log.info(f"CODS: {dict((f'{m:02d}/{a}', c) for (a, m), c in fila.items())}")
    todos = []
    pendentes = dict(fila)
    t0 = time.time()
    while pendentes and (time.time() - t0) < TIMEOUT_GLOBAL:
        prontos = []
        for chave, cod in list(pendentes.items()):
            try:
                sit = sa._situacao(token, cod)
                if sit not in ("PENDENTE", "PROCESSANDO", "EM PROCESSAMENTO"):
                    prontos.append((chave, cod, sit))
            except Exception as e:
                log.warning(f"  erro verificar cod={cod}: {e}")
        for chave, cod, sit in prontos:
            pendentes.pop(chave)
            ano, mes = chave
            if sit == "FINALIZADO":
                try:
                    itens = sa._baixar(token, cod)
                    todos.extend(itens)
                    log.info(f"  baixado {mes:02d}/{ano}: {len(itens)} adesoes")
                except Exception as e:
                    log.error(f"  erro baixar {mes:02d}/{ano}: {e}")
            else:
                log.warning(f"  {mes:02d}/{ano} situacao={sit} (ignorado)")
        if pendentes:
            time.sleep(15)
    if pendentes:
        for (ano, mes), cod in pendentes.items():
            log.error(f"  TIMEOUT {mes:02d}/{ano} cod={cod} nao finalizou")
    return todos


def main():
    if len(sys.argv) < 4:
        print("uso: python backfill_adesoes.py ANO MES_INI MES_FIM")
        return 1
    ano = int(sys.argv[1]); mi = int(sys.argv[2]); mf = int(sys.argv[3])
    meses = [(ano, m) for m in range(mi, mf + 1)]
    log.info(f"=== BACKFILL ADESOES {ano} meses {mi}..{mf} ===")

    db_adesoes.init_db()
    token = ss.autenticar()
    adesoes = coletar(token, meses)
    log.info(f"TOTAL coletado: {len(adesoes)} adesoes")
    if adesoes:
        log.info(f"Gravado: {db_adesoes.substituir_periodo(adesoes)}")

    # Congela os meses do intervalo que estao presentes no banco
    import db
    e = db.estatisticas  # noqa
    conn = db._conn(); cur = db._cursor(conn)
    cur.execute("SELECT DISTINCT ano, mes FROM adesoes WHERE ano=?", (ano,))
    presentes = {(r['ano'], r['mes']) for r in cur.fetchall()}
    conn.close()
    for (a, m) in meses:
        if (a, m) in presentes:
            n = db_adesoes.congelar_mes(a, m)
            log.info(f"  CONGELADO {m:02d}/{a}: {n} adesoes")
        else:
            log.warning(f"  {m:02d}/{a} ausente — NAO congelado (reexecute pelo cod).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
