// =========================================================
// SYNC SIPROV — Componente compartilhado entre os 3 dashboards
// =========================================================
// Espera que o template tenha estes elementos:
//   #btn-sync-siprov   #sync-icon   #sync-label   #sync-status-text
// Opcional: window.recarregarDashboard()  ← chamada após sync OK
// =========================================================

(function () {
    'use strict';

    const btn        = document.getElementById('btn-sync-siprov');
    const icon       = document.getElementById('sync-icon');
    const label      = document.getElementById('sync-label');
    const statusText = document.getElementById('sync-status-text');

    if (!btn || !statusText) return;

    let polling          = false;
    let lastSyncFilename = null;

    function fmtData(iso) {
        if (!iso) return '—';
        try {
            // O servidor (Render) grava o horário em UTC, sem informar o fuso.
            // Acrescenta 'Z' para o navegador interpretar como UTC e exibe no
            // fuso de Brasília — senão apareceria 3h adiantado (10:34 vs 07:34).
            let s = String(iso);
            if (!/([zZ]|[+\-]\d\d:?\d\d)$/.test(s)) s = s + 'Z';
            const d = new Date(s);
            return d.toLocaleString('pt-BR', {
                day: '2-digit', month: '2-digit', year: 'numeric',
                hour: '2-digit', minute: '2-digit',
                timeZone: 'America/Sao_Paulo',
            });
        } catch { return iso; }
    }

    function aplicarStatus(s) {
        const p = s.progresso || {};
        const emAndamento = s.em_andamento || ['autenticando', 'titulos', 'associados', 'salvando'].includes(p.status);

        if (emAndamento) {
            btn.disabled = true;
            btn.style.opacity = '0.7';
            btn.style.cursor = 'wait';
            icon.style.display = 'inline-block';
            icon.style.animation = 'spin 1s linear infinite';
            label.textContent = 'Sincronizando…';

            const msg = p.mensagem || 'Sincronizando…';
            const detalhes = [];
            if (p.titulos_atual) detalhes.push(`${p.titulos_atual} títulos`);
            if (p.associados)    detalhes.push(`${p.associados} associados`);
            statusText.innerHTML = `<span class="text-orange-400 font-semibold">⟳ ${msg}</span>` +
                                   (detalhes.length ? ` <span class="text-slate-500">·</span> ${detalhes.join(' · ')}` : '');
        } else {
            btn.disabled = false;
            btn.style.opacity = '';
            btn.style.cursor = '';
            icon.style.animation = '';
            label.textContent = 'Sync Siprov';

            const ult = s.ultimo_sync ? fmtData(s.ultimo_sync) : '—';
            const status = p.status === 'ok'  ? '✓ ' :
                           p.status === 'erro' ? '✗ ' : '';
            const cor    = p.status === 'erro' ? 'text-red-400' :
                           p.status === 'ok'   ? 'text-emerald-400' : 'text-slate-400';
            statusText.innerHTML = `<span class="${cor}">${status}Última sincronização: <strong>${ult}</strong></span>` +
                                   (p.mensagem ? ` <span class="text-slate-500">· ${p.mensagem}</span>` : '');

            // Se sync acabou de terminar com sucesso, recarrega o dashboard
            if (s.arquivo && lastSyncFilename && s.arquivo !== lastSyncFilename && p.status === 'ok') {
                if (typeof window.recarregarDashboard === 'function') {
                    window.recarregarDashboard();
                }
            }
            lastSyncFilename = s.arquivo;

            if (polling && p.status !== 'autenticando' && p.status !== 'titulos' && p.status !== 'associados' && p.status !== 'salvando') {
                polling = false;
            }
        }
    }

    async function buscarStatus() {
        try {
            const r = await fetch('/api/admin/sync/status');
            if (!r.ok) return;
            const s = await r.json();
            aplicarStatus(s);
        } catch (e) {
            statusText.textContent = 'Não foi possível ler o status da sincronização.';
        }
    }

    function iniciarPolling() {
        if (polling) return;
        polling = true;
        const tick = async () => {
            if (!polling) return;
            await buscarStatus();
            if (polling) setTimeout(tick, 3000);
        };
        tick();
    }

    btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        btn.disabled = true;
        label.textContent = 'Disparando…';
        try {
            const r = await fetch('/api/admin/sync', { method: 'POST' });
            let data = {};
            try { data = await r.json(); } catch (_) { /* corpo vazio/não-JSON */ }
            if (!r.ok) {
                let msg;
                if (r.status === 503) {
                    msg = data.status || 'Sincronização desativada neste ambiente.';
                } else if (r.status === 401 || r.status === 403) {
                    msg = 'Sessão expirada — faça login novamente.';
                } else {
                    msg = data.erro || data.status || ('Erro ao iniciar sync (HTTP ' + r.status + ').');
                }
                statusText.innerHTML = '<span class="text-red-400 font-semibold">✗ ' + msg + '</span>';
                label.textContent = 'Sync Siprov';
                btn.disabled = false;
                return;
            }
            iniciarPolling();
        } catch (e) {
            statusText.innerHTML = '<span class="text-red-400 font-semibold">✗ Erro de conexão ao iniciar sync.</span>';
            label.textContent = 'Sync Siprov';
            btn.disabled = false;
        }
    });

    // CSS pra animação do ícone
    const css = document.createElement('style');
    css.textContent = '@keyframes spin { to { transform: rotate(360deg); } } #sync-icon { display:inline-block; }';
    document.head.appendChild(css);

    // Carga inicial + polling leve a cada 30s pra refletir syncs do scheduler
    buscarStatus();
    setInterval(buscarStatus, 30000);
})();
