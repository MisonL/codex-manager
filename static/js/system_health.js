/**
 * 导航栏系统健康状态徽章
 */

const SYSTEM_HEARTBEAT_INTERVAL_MS = 30000;
const SYSTEM_HEARTBEAT_TIMEOUT_MS = 5000;

function getSystemHealthPresentation(status) {
    const states = {
        ok: { text: 'System OK', className: 'ok' },
        degraded: { text: 'System Degraded', className: 'degraded' },
        offline: { text: 'System Offline', className: 'offline' },
        pending: { text: 'System Pending', className: 'pending' },
    };

    return states[status] || states.pending;
}

function updateSystemHealthBadge(badge, payload) {
    if (!badge) {
        return;
    }

    const status = payload?.status || 'offline';
    const presentation = getSystemHealthPresentation(status);
    const issueText = Array.isArray(payload?.issues) && payload.issues.length > 0
        ? payload.issues.join(', ')
        : 'no_issues';

    badge.textContent = presentation.text;
    badge.className = `status-badge ${presentation.className} system-badge`;
    badge.title = `uptime=${payload?.uptime_hms || '--'} issues=${issueText}`;
}

async function refreshSystemHealthBadge(badge) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), SYSTEM_HEARTBEAT_TIMEOUT_MS);

    try {
        const response = await fetch('/api/system/health', {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' },
            signal: controller.signal,
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const payload = await response.json();
        updateSystemHealthBadge(badge, payload);
    } catch (error) {
        updateSystemHealthBadge(badge, {
            status: 'offline',
            issues: ['health_endpoint_unreachable'],
            uptime_hms: '--',
        });

        if (error.name !== 'AbortError') {
            console.error('系统健康检查失败:', error);
        }
    } finally {
        window.clearTimeout(timeoutId);
    }
}

function initSystemHealthBadge() {
    const badge = document.getElementById('system-health-badge');
    if (!badge) {
        return;
    }

    refreshSystemHealthBadge(badge);

    if (window.__systemHealthHeartbeatId) {
        window.clearInterval(window.__systemHealthHeartbeatId);
    }

    window.__systemHealthHeartbeatId = window.setInterval(() => {
        refreshSystemHealthBadge(badge);
    }, SYSTEM_HEARTBEAT_INTERVAL_MS);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSystemHealthBadge);
} else {
    initSystemHealthBadge();
}
