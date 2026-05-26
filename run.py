"""
Servidor de produção usando Waitress (Windows-compatível).
Uso: python run.py
"""

import logging
import os
from waitress import serve
from app import app

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

HOST    = '0.0.0.0'
PORT    = int(os.environ.get('PORT', 5000))  # Railway injeta PORT automaticamente
THREADS = 4

if __name__ == '__main__':
    print(f'JB Protecao iniciado em http://{HOST}:{PORT}')
    print(f'Threads: {THREADS}')
    serve(app, host=HOST, port=PORT, threads=THREADS)
