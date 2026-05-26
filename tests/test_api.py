"""
Testes automatizados — JB Proteção Dashboard
Rodar: pytest tests/ -v
"""

import os
import pytest

os.environ.setdefault('ADMIN_USER', 'marcone')
os.environ.setdefault('ADMIN_PASS', '3209')
os.environ.setdefault('SECRET_KEY', 'test-key-for-pytest')

from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['RATELIMIT_ENABLED'] = False  # desabilita rate limit nos testes
    with flask_app.test_client() as client:
        yield client


@pytest.fixture
def autenticado(client):
    # Seta a sessão diretamente para não passar pelo rate limiter do /login
    with client.session_transaction() as sess:
        sess['logado'] = True
    return client


# ── Autenticação ────────────────────────────────────────────

class TestAuth:
    def test_raiz_redireciona_para_login(self, client):
        r = client.get('/')
        assert r.status_code == 200
        assert b'login' in r.data.lower() or b'entrar' in r.data.lower()

    def test_dashboard_sem_login_redireciona(self, client):
        for rota in ['/financeiro', '/eventos', '/vendas']:
            r = client.get(rota, follow_redirects=False)
            assert r.status_code == 302, f'{rota} deveria redirecionar'

    def test_api_sem_login_redireciona(self, client):
        for rota in ['/api/financeiro', '/api/vendas', '/api/eventos']:
            r = client.get(rota, follow_redirects=False)
            assert r.status_code == 302, f'{rota} deveria redirecionar'

    def test_login_invalido_redireciona_com_erro(self, client):
        r = client.post('/login', data={'usuario': 'errado', 'senha': 'errada'})
        assert r.status_code == 302
        assert 'erro=1' in r.headers.get('Location', '')

    def test_login_valido(self, client):
        r = client.post('/login', data={
            'usuario': os.environ['ADMIN_USER'],
            'senha':   os.environ['ADMIN_PASS'],
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/financeiro' in r.headers.get('Location', '')

    def test_logout(self, autenticado):
        r = autenticado.get('/logout', follow_redirects=False)
        assert r.status_code == 302


# ── Dashboards (autenticado) ────────────────────────────────

class TestDashboards:
    def test_financeiro_retorna_200(self, autenticado):
        assert autenticado.get('/financeiro').status_code == 200

    def test_eventos_retorna_200(self, autenticado):
        assert autenticado.get('/eventos').status_code == 200

    def test_vendas_retorna_200(self, autenticado):
        assert autenticado.get('/vendas').status_code == 200


# ── APIs (autenticado) ──────────────────────────────────────

class TestAPIFinanceiro:
    def test_retorna_200_e_json(self, autenticado):
        r = autenticado.get('/api/financeiro')
        assert r.status_code == 200
        data = r.get_json()
        assert data is not None
        assert 'cards' in data

    def test_cards_tem_campos_esperados(self, autenticado):
        data = autenticado.get('/api/financeiro').get_json()
        for campo in ['registros', 'liquidado', 'total', 'ticket', 'bases', 'pontualidade']:
            assert campo in data['cards'], f'Campo {campo!r} ausente em cards'

    def test_filtro_tipo_liquidacao(self, autenticado):
        r = autenticado.get('/api/financeiro?tipo=liquidacao')
        assert r.status_code == 200
        assert 'cards' in r.get_json()

    def test_filtro_tipo_invalido_nao_quebra(self, autenticado):
        r = autenticado.get('/api/financeiro?tipo=invalido')
        assert r.status_code == 200

    def test_export_csv(self, autenticado):
        r = autenticado.get('/api/financeiro/export')
        assert r.status_code == 200
        assert 'text/csv' in r.content_type


class TestAPIVendas:
    def test_retorna_200_e_json(self, autenticado):
        r = autenticado.get('/api/vendas')
        assert r.status_code == 200
        data = r.get_json()
        assert 'cards' in data

    def test_cards_tem_campos_esperados(self, autenticado):
        data = autenticado.get('/api/vendas').get_json()
        for campo in ['total_vendas', 'valor_liquidado', 'carteira_total', 'ticket_medio', 'regionais']:
            assert campo in data['cards'], f'Campo {campo!r} ausente'

    def test_export_csv(self, autenticado):
        r = autenticado.get('/api/vendas/export')
        assert r.status_code == 200
        assert 'text/csv' in r.content_type


class TestAPIEventos:
    def test_retorna_200_e_json(self, autenticado):
        r = autenticado.get('/api/eventos')
        assert r.status_code == 200
        data = r.get_json()
        assert 'cards' in data

    def test_cards_tem_campos_esperados(self, autenticado):
        data = autenticado.get('/api/eventos').get_json()
        for campo in ['adesoes', 'receita', 'carteira', 'ticket_medio', 'base_ativa']:
            assert campo in data['cards'], f'Campo {campo!r} ausente'

    def test_export_csv(self, autenticado):
        r = autenticado.get('/api/eventos/export')
        assert r.status_code == 200
        assert 'text/csv' in r.content_type


# ── Health ──────────────────────────────────────────────────

class TestHealth:
    def test_health_retorna_ok(self, client):
        r = client.get('/health')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'ok'
