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
            const d = new Date(iso);
            return d.toLocaleString('pt-BR', {
                day: '2-digit', month: '2-digit', year: 'numeric',
                hour: '2-digit', minute: '2-digit',
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

    // =====================================================
    // MODO JSON-ONLY ATIVO
    // =====================================================
    // Endpoint /api/admin/sync está bloqueado no backend.
    // Botão fica em modo informativo: avisa que a sync está
    // desativada em vez de tentar disparar requisição.
    btn.classList.remove('bg-orange-600', 'hover:bg-orange-500');
    btn.classList.add('bg-slate-700', 'hover:bg-slate-600', 'cursor-not-allowed');
    btn.title = 'Sincronização desativada — sistema em modo JSON local.';
    label.textContent = 'JSON LOCAL';
    if (icon) icon.textContent = '🔒';

    btn.addEventListener('click', async (e) => {
        e.preventDefault();
        // Visual feedback de bloqueio
        const original = label.textContent;
        label.textContent = 'Modo JSON ativo';
        if (statusText) {
            statusText.innerHTML = '<span class="text-yellow-400">ⓘ Sincronização desativada. Sistema usa apenas o JSON local como fonte de dados.</span>';
        }
        setTimeout(() => { label.textContent = original; }, 2500);
    });

    // CSS pra animação do ícone
    const css = document.createElement('style');
    css.textContent = '@keyframes spin { to { transform: rotate(360deg); } } #sync-icon { display:inline-block; }';
    document.head.appendChild(css);

    // Carga inicial + polling leve a cada 30s pra refletir syncs do scheduler
    buscarStatus();
    setInterval(buscarStatus, 30000);
})();
