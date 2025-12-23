const APP_CONFIG = (() => {
  if (typeof document === 'undefined') {
    return { managers: [] };
  }

  const body = document.body;
  const managersRaw = body?.dataset?.managers;
  let managers = [];
  if (managersRaw) {
    try {
      const parsed = JSON.parse(managersRaw);
      if (Array.isArray(parsed)) {
        managers = parsed.map(name => `${name}`.trim()).filter(Boolean);
      }
    } catch (err) {
      // Обратная совместимость: старый формат через запятую
      managers = String(managersRaw)
        .split(',')
        .map(name => name.trim())
        .filter(Boolean);
    }
  }

  const currentUser = body?.dataset?.currentUser || '';
  const currentRole = body?.dataset?.currentRole || '';

  const permissions = {
    canAccessFacades: body?.dataset?.canAccessFacades === '1',
    canAccessMetrics: body?.dataset?.canAccessMetrics === '1',
    canManageUsers: body?.dataset?.canManageUsers === '1',
    canAccessSettings: body?.dataset?.canAccessSettings === '1',
    canAccessClients: body?.dataset?.canAccessClients === '1',
    canAccessSearch: body?.dataset?.canAccessSearch === '1',
    canEditPaths: body?.dataset?.canEditPaths === '1',
    canToggleOrderOptions: body?.dataset?.canToggleOrderOptions === '1',
  };

  return { managers, currentUser, currentRole, permissions };
})();

const PREFERS_REDUCED_MOTION = typeof window !== 'undefined'
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]')?.content
  || document.body?.dataset?.csrfToken
  || '').trim();

function withCsrfHeaders(headers = {}) {
  if (!CSRF_TOKEN) return headers;
  return { ...headers, 'X-CSRFToken': CSRF_TOKEN };
}

function withCsrfBody(body = {}) {
  if (!CSRF_TOKEN) return body;
  return { ...body, csrf_token: CSRF_TOKEN };
}

const ConfirmDialog = (() => {
  let dialog;
  let messageBox;
  let resolver = null;

  function ensureDialog() {
    if (dialog) return;

    dialog = document.createElement('div');
    dialog.className = 'confirm-overlay';
    dialog.innerHTML = `
      <div class="confirm-modal" role="dialog" aria-modal="true">
        <p class="confirm-message"></p>
        <div class="confirm-actions">
          <button type="button" class="btn btn--primary" data-confirm-yes>Подтвердить</button>
          <button type="button" class="btn btn--ghost" data-confirm-no>Отмена</button>
        </div>
      </div>
    `;

    messageBox = dialog.querySelector('.confirm-message');
    dialog.addEventListener('click', event => {
      if (event.target.dataset.confirmYes !== undefined) {
        resolve(true);
      } else if (event.target.dataset.confirmNo !== undefined || event.target === dialog) {
        resolve(false);
      }
    });

    document.body.appendChild(dialog);
  }

  function resolve(result) {
    dialog?.classList.remove('is-visible');
    if (resolver) {
      resolver(result);
      resolver = null;
    }
  }

  function confirm(message) {
    ensureDialog();
    if (messageBox) messageBox.textContent = message || 'Вы уверены?';
    dialog.classList.add('is-visible');
    return new Promise(res => {
      resolver = res;
    });
  }

  return { confirm };
})();

const OrdersPage = (() => {
  let allData = [];
  let currentStatus = 'all';
  let currentManager =
    APP_CONFIG.currentRole === 'manager' && APP_CONFIG.currentUser
      ? APP_CONFIG.currentUser
      : 'Все';
  let searchTerm = '';
  let searchTimer = null;
  let sortKey = null;
  let sortOrder = 1;
  let sse = null;
  let pollingTimer = null;
  let pollingBackoff = 3000;
  let previousOrders = new Map();
  let sseStatus = { root: null, dot: null, text: null };

  function init() {
    const table = document.getElementById('orders');
    if (!table) return;

    sseStatus.root = document.getElementById('sseStatus');
    sseStatus.dot = sseStatus.root?.querySelector('.sse-status__dot') || null;
    sseStatus.text = sseStatus.root?.querySelector('.sse-status__text') || null;

    highlightActiveFilter(currentStatus);
    highlightSortButtons();

    const managerSelect = document.getElementById('managerSelect');
    if (managerSelect && currentManager !== 'Все') {
      managerSelect.value = currentManager;
    }
    const searchInput = document.getElementById('orderSearch');
    if (searchInput) {
      searchInput.addEventListener('input', () => handleSearchInput(searchInput.value));
      searchInput.addEventListener('focus', () => searchInput.classList.add('is-focused'));
      searchInput.addEventListener('blur', () => searchInput.classList.remove('is-focused'));
    }
    startSSE();
  }

  function loadData() {
    const url = `/data?${buildManagerParams().toString()}`;

    fetch(url)
      .then(response => response.json())
      .then(json => {
        updateDataFromPayload(json);
      })
      .catch(error => console.error('Ошибка при загрузке данных:', error));
  }

  function startSSE() {
    stopSSE();
    stopPollingFallback();

    updateSseIndicator('reconnect', 'Подключение…');
    const streamUrl = `/events?${buildManagerParams().toString()}`;
    sse = new EventSource(streamUrl);

    sse.onopen = () => {
      updateSseIndicator('ok', 'Онлайн');
    };

    sse.onmessage = ev => {
      pollingBackoff = 5000;
      stopPollingFallback();
      if (!ev?.data) return;
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === 'orders_snapshot') {
          const orders = Array.isArray(payload.folders) ? payload.folders : [];
          allData = orders;
          render();
          renderStats({ ...payload, folders: orders });
        }
      } catch (err) {
        console.warn('Некорректные данные SSE', err);
      }
    };

    sse.onerror = () => {
      stopSSE();
      startPollingFallback();
      updateSseIndicator('error', 'Офлайн');
    };
  }

  function stopSSE() {
    if (sse) {
      sse.close();
      sse = null;
    }
  }

  function startPollingFallback() {
    if (pollingTimer) return;

    function tick() {
      loadData();
      pollingBackoff = Math.min(pollingBackoff * 1.5, 30000);
      pollingTimer = setTimeout(tick, pollingBackoff);
    }

    pollingTimer = setTimeout(tick, pollingBackoff);
  }

  function stopPollingFallback() {
    if (!pollingTimer) return;
    clearTimeout(pollingTimer);
    pollingTimer = null;
    pollingBackoff = 5000;
  }

  function updateSseIndicator(state, label) {
    if (!sseStatus.root) return;
    sseStatus.root.classList.remove('sse-status--ok', 'sse-status--error', 'sse-status--reconnect');
    if (state === 'ok') {
      sseStatus.root.classList.add('sse-status--ok');
    } else if (state === 'error') {
      sseStatus.root.classList.add('sse-status--error');
    } else {
      sseStatus.root.classList.add('sse-status--reconnect');
    }

    if (sseStatus.text && label) {
      sseStatus.text.textContent = label;
    }
  }

  function updateDataFromPayload(payload) {
    const orders = Array.isArray(payload?.orders)
      ? payload.orders
      : Array.isArray(payload?.folders)
        ? payload.folders
        : [];

    previousOrders = new Map((allData || []).map(item => [getOrderKey(item), item]));
    allData = orders;
    render();
    renderStats({ ...payload, folders: orders });
  }

  function buildManagerParams() {
    const params = new URLSearchParams({ manager: currentManager });
    if (APP_CONFIG.currentRole === 'manager' && currentManager === 'Все') {
      params.set('show_all', '1');
    }
    return params;
  }

  function setStatusFilter(status) {
    currentStatus = status;
    highlightActiveFilter(status);
    render();
  }

  function handleSearchInput(value) {
    const normalized = (value || '').trim().toLowerCase();
    if (searchTimer) {
      clearTimeout(searchTimer);
    }
    searchTimer = setTimeout(() => {
      searchTerm = normalized;
      render();
    }, 180);
  }

  function setManagerFilter() {
    const select = document.getElementById('managerSelect');
    if (!select) return;
    const selected = select.value;

    if (APP_CONFIG.currentRole === 'manager' && !APP_CONFIG.currentUser && selected !== 'Все') {
      currentManager = 'Все';
      select.value = currentManager;
    } else {
      currentManager = selected;
    }
    stopPollingFallback();
    startSSE();
  }

  function sortBy(key) {
    if (sortKey === key) {
      sortOrder *= -1;
    } else {
      sortKey = key;
      sortOrder = 1;
    }
    highlightSortButtons();
    render();
  }

  function highlightActiveFilter(status) {
    document.querySelectorAll('[data-filter]').forEach(btn => {
      btn.classList.toggle('is-active', btn.dataset.filter === status);
    });
  }

  function highlightSortButtons() {
    document.querySelectorAll('[data-sort]').forEach(btn => {
      const isActive = btn.dataset.sort === sortKey;
      btn.classList.toggle('is-active', isActive);
      if (isActive) {
        btn.dataset.order = sortOrder === 1 ? 'asc' : 'desc';
        btn.setAttribute('aria-pressed', 'true');
      } else {
        btn.removeAttribute('data-order');
        btn.removeAttribute('aria-pressed');
      }
    });
  }

  function renderStats(data) {
    const statsDiv = document.getElementById('stats');
    if (!statsDiv) return;

    const managers = data.managers || {};
    const technologists = data.technologists || {};
    const folders = Array.isArray(data.folders) ? data.folders : [];

    const doneCount = folders.filter(item => item.status === 'Готов' || item.status === 'Подтвержден').length;
    const newCount = folders.filter(item => item.status === 'Новый').length;
    const confirmedCount = folders.filter(item => item.is_approved || item.status === 'Подтвержден').length;
    const cancelledCount = folders.filter(item => item.is_cancelled || item.status === 'ANULAT').length;

    const managerOrder = Array.from(new Set([...(APP_CONFIG.managers || []), 'Неизвестно']));
    const managerItems = managerOrder
      .map(name => {
        const value = managers[name] || 0;
        return `<li class="stat-item"><span>${name}</span><span class="stat-count">${value}</span></li>`;
      })
      .join('');

    const technologistItems = Object.entries(technologists)
      .filter(([name]) => name && name.toLowerCase() !== 'неизвестно')
      .map(([name, value]) => `<li class="stat-item"><span>${name}</span><span class="stat-count">${value}</span></li>`)
      .join('');

    statsDiv.innerHTML = `
      <div class="stats-grid">
        <article class="stat-card">
          <span class="stat-label">Всего заказов</span>
          <span class="stat-value stat-value--compact">${data.total ?? 0}</span>
          <div class="stat-meta">
            <span>Готовых: <strong>${doneCount}</strong></span>
            <span>Новых: <strong>${newCount}</strong></span>
            <span>Подтвержденных: <strong>${confirmedCount}</strong></span>
          </div>
        </article>
        <article class="stat-card">
          <span class="stat-label">Менеджеры</span>
          <ul class="stat-list stat-list--columns">
            ${managerItems}
          </ul>
        </article>
        <article class="stat-card">
          <span class="stat-label">Технологи</span>
          <ul class="stat-list">
            ${technologistItems || '<li class="stat-item"><span>Нет данных</span><span class="stat-count">0</span></li>'}
          </ul>
        </article>
      </div>
    `;
  }

  function render() {
    const table = document.querySelector('#orders tbody');
    if (!table) return;

    const filtered = allData
      .filter(item => {
        if (currentStatus === 'all') return true;
        if (currentStatus === 'Готов') {
          return item.status === 'Готов' || item.status === 'Подтвержден';
        }
        return item.status === currentStatus;
      })
      .filter(item => {
        if (!searchTerm) return true;
        const orderName = (item.display_name || item.name || '').toLowerCase();
        return orderName.includes(searchTerm);
      })
      .slice();

    if (sortKey) {
      filtered.sort((a, b) => {
        let aVal = a[sortKey];
        let bVal = b[sortKey];

        if (sortKey === 'days') {
          const aNum = Number(aVal) || 0;
          const bNum = Number(bVal) || 0;
          if (aNum === bNum) return 0;
          return aNum < bNum ? -1 * sortOrder : 1 * sortOrder;
        }

        if (typeof aVal === 'string') {
          aVal = aVal.toLowerCase();
          bVal = (bVal || '').toLowerCase();
        }
        if (aVal < bVal) return -1 * sortOrder;
        if (aVal > bVal) return 1 * sortOrder;
        return 0;
      });
    }

    const existingRows = new Map();
    table.querySelectorAll('tr').forEach(row => {
      const key = row.dataset.orderKey || row.dataset.key || row.dataset.client;
      existingRows.set(key, row);
    });

    const newRows = [];

    filtered.forEach(item => {
      const key = getOrderKey(item);
      let row = existingRows.get(key);
      const prev = previousOrders.get(key);

      if (!row) {
        row = document.createElement('tr');
        row.dataset.orderKey = key;
        row.classList.add('is-entering');
        if (!PREFERS_REDUCED_MOTION) {
          requestAnimationFrame(() => row.classList.add('is-visible'));
        } else {
          row.classList.add('is-visible');
        }
      }

      fillOrderRow(row, item);

      if (prev && hasOrderChanged(prev, item)) {
        row.classList.add('is-updated');
        setTimeout(() => row.classList.remove('is-updated'), 1100);
      }

      newRows.push(row);
      existingRows.delete(key);
    });

    existingRows.forEach(row => animateRowDeletion(row));

    newRows.forEach(row => table.appendChild(row));
    previousOrders = new Map(filtered.map(item => [getOrderKey(item), item]));
  }

  function hasOrderChanged(prev, next) {
    return prev?.status !== next?.status
      || prev?.manager !== next?.manager
      || prev?.modified !== next?.modified
      || prev?.name !== next?.name
      || prev?.display_name !== next?.display_name
      || prev?.is_approved !== next?.is_approved
      || prev?.is_cancelled !== next?.is_cancelled;
  }

  function animateRowDeletion(row) {
    if (!row) return;
    const height = row.offsetHeight;
    row.style.height = `${height}px`;
    if (!PREFERS_REDUCED_MOTION) {
      requestAnimationFrame(() => {
        row.classList.add('is-deleting');
        row.style.height = '0px';
      });
      setTimeout(() => row.remove(), 320);
    } else {
      row.remove();
    }
  }

  function fillOrderRow(tr, item) {
    tr.innerHTML = '';
    tr.className = '';
    tr.dataset.orderKey = getOrderKey(item);

    const isCancelled = !!item.is_cancelled;
    const isApproved = !!item.is_approved;
    const hasTechnologist = !!item.has_technologist;

    if (isCancelled) tr.classList.add('cancelled');
    else if (isApproved) tr.classList.add('confirmed');
    else if (hasTechnologist) tr.classList.add('done');
    else tr.classList.add('new');

    let statusClass = 'status-pill status-pill--warning';
    if (isCancelled) {
      statusClass = 'status-pill status-pill--danger';
    } else if (isApproved) {
      statusClass = 'status-pill status-pill--neutral';
    } else if (item.status === 'Готов' || hasTechnologist) {
      statusClass = 'status-pill status-pill--success';
    }

    const nameTd = document.createElement('td');
    const nameDiv = document.createElement('div');
    nameDiv.className = 'table-primary';

    const pricedInfo = getPricedInfo(item);
    if (pricedInfo?.visible) {
      nameDiv.classList.add('table-primary--with-mark');
      const pricedMark = document.createElement('span');
      pricedMark.className = 'priced-mark';
      pricedMark.textContent = pricedInfo.priced ? '✅' : '❌';
      pricedMark.title = pricedInfo.priced
        ? 'Отмечен как «Посчитан»'
        : 'Не отмечен как «Посчитан»';
      nameDiv.appendChild(pricedMark);
    }

    const nameText = document.createElement('span');
    nameText.textContent = item.display_name || item.name || '';
    nameDiv.appendChild(nameText);
    nameTd.appendChild(nameDiv);
    tr.appendChild(nameTd);

    const managerTd = document.createElement('td');
    const managerDiv = document.createElement('div');
    managerDiv.className = 'table-secondary';
    managerDiv.textContent = item.manager || '—';
    managerTd.appendChild(managerDiv);
    tr.appendChild(managerTd);

    const statusTd = document.createElement('td');
    const statusSpan = document.createElement('span');
    statusSpan.className = statusClass;
    statusSpan.textContent = item.status || '';
    statusTd.appendChild(statusSpan);
    tr.appendChild(statusTd);

    const modifiedTd = document.createElement('td');
    const modifiedDiv = document.createElement('div');
    modifiedDiv.className = 'table-secondary';
    modifiedDiv.textContent = item.modified || '';
    modifiedTd.appendChild(modifiedDiv);
    tr.appendChild(modifiedTd);

    const daysTd = document.createElement('td');
    const daysDiv = document.createElement('div');
    daysDiv.className = 'table-secondary';
    daysDiv.textContent = item.days ?? '—';
    daysTd.appendChild(daysDiv);
    tr.appendChild(daysTd);
  }

  function getPricedInfo(item) {
    if (!item?.has_technologist) return null;
    if (item.is_cancelled) return null;
    if (!item.is_priced) return { visible: true, priced: false };
    if (item.is_priced && !item.is_approved) return { visible: true, priced: true };
    return null;
  }

  function getOrderKey(item) {
    if (!item) return '';
    return item.path || item.order_key || item.name || item.id || item.modified || Math.random().toString(16).slice(2);
  }

  return {
    init,
    setStatusFilter,
    setManagerFilter,
    sortBy
  };
})();

const SettingsPage = (() => {
  let metricsTimer = null;

  function init() {
    const tabs = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.tab-content');
    if (!tabs.length || !panels.length) return;

    tabs.forEach(tab => {
      tab.addEventListener('click', () => activateTab(tab, tabs, panels));
    });

    const activeTab = document.querySelector('.tab-btn.is-active');
    if (activeTab) {
      activateTab(activeTab, tabs, panels, false);
    }
  }

  function activateTab(tab, tabs, panels, manageMetrics = true) {
    const target = tab.dataset.tab;

    tabs.forEach(btn => btn.classList.toggle('is-active', btn === tab));
    panels.forEach(panel => {
      panel.classList.toggle('is-active', panel.dataset.tabPanel === target);
    });

    if (!manageMetrics) return;
    if (target === 'metrics') {
      startMetricsPolling();
    } else {
      stopMetricsPolling();
    }
  }

  function startMetricsPolling() {
    if (metricsTimer) return;
    metricsTimer = setInterval(loadMetrics, 5000);
    loadMetrics();
  }

  function stopMetricsPolling() {
    if (!metricsTimer) return;
    clearInterval(metricsTimer);
    metricsTimer = null;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function formatUptime(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hrs} ч ${mins} м`;
  }

  function formatMs(value) {
    if (value === null || value === undefined) return '—';
    return value.toFixed(1);
  }

  function setStatus(text, isError = false) {
    const box = document.getElementById('metricsStatus');
    if (!box) return;
    box.textContent = text;
    box.classList.toggle('text-danger', isError);
  }

  function notifyCopied(trigger, message = 'Скопировано') {
    const scope = trigger?.closest('[data-copy-area]') || trigger?.closest('.error-entry');
    const feedback = scope?.querySelector('[data-copy-feedback]');

    if (feedback) {
      feedback.textContent = message;
      feedback.classList.add('is-visible');
      setTimeout(() => feedback.classList.remove('is-visible'), 1500);
    } else {
      window.alert(message);
    }

    if (trigger) {
      trigger.classList.add('flash-highlight');
      setTimeout(() => trigger.classList.remove('flash-highlight'), 900);
    }
  }

  async function copyTextPayload(text, trigger) {
    if (!text) return;

    const fallbackCopy = () => {
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      document.body.removeChild(area);
    };

    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        notifyCopied(trigger);
        return;
      }
    } catch (err) {
      console.warn('Clipboard API недоступен, fallback copy используется', err);
    }

    fallbackCopy();
    notifyCopied(trigger);
  }
  
  const THREAD_TTL = {
  watchdog: 5,            // должен дышать часто
  metrics: 10,            // метрики обновляются каждые несколько секунд
  snapshot_updater: 3600, // считаем живым, если шевелился за последний час
  indexer: 3600,
  orders_sync: 120,
  };

  function buildThreadsTable(threads, nowSec) {
  const tbody = document.getElementById('metrics-threads');
  if (!tbody) return;
  tbody.innerHTML = '';

  Object.entries(threads || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .forEach(([name, info]) => {
      const lastTs = info?.last_heartbeat || 0;
      const secondsAgo = lastTs ? Math.max(0, (nowSec - lastTs).toFixed(1)) : '—';

      const ttl = THREAD_TTL[name] ?? 30; // по умолчанию 30 секунд для прочих
      const alive = lastTs && nowSec - lastTs <= ttl ? 'OK' : 'DEAD';

      const row = document.createElement('tr');
      row.innerHTML = `
        <td>${name}</td>
        <td>${secondsAgo}</td>
        <td>${alive}</td>
      `;
      if (alive === 'DEAD') {
        row.classList.add('text-danger');
      }
      tbody.appendChild(row);
    });
}

  function buildEndpointsTable(perEndpoint) {
    const tbody = document.getElementById('metrics-endpoints');
    if (!tbody) return;
    tbody.innerHTML = '';

    const entries = Object.entries(perEndpoint || {})
      .sort(([, a], [, b]) => (b?.count || 0) - (a?.count || 0))
      .slice(0, 30);

    entries.forEach(([name, info]) => {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>${name}</td>
        <td>${info.count || 0}</td>
        <td>${formatMs(info.avg_ms || 0)}</td>
        <td>${formatMs(info.last_ms || 0)}</td>
        <td>${formatMs(info.max_ms || 0)}</td>
        <td>${info.last_status || 0}</td>
      `;
      tbody.appendChild(row);
    });
  }

  function buildErrorsList(errors) {
    const container = document.getElementById('metrics-errors-list');
    const copyAllBtn = document.getElementById('metrics-errors-copy-all');
    if (!container) return;
    container.innerHTML = '';

    const items = (errors || []).slice(-10).reverse();
    const allTexts = [];

    items.forEach(item => {
      const when = item?.ts ? new Date(item.ts * 1000).toLocaleString() : '—';
      const endpoint = item?.endpoint || '—';
      const errText = item?.err || 'Ошибка';
      const traceText = (item?.trace || errText || '').trim();
      const summaryText = `${when} · ${endpoint}`.trim();
      const fullText = [summaryText, traceText].filter(Boolean).join('\n');
      if (fullText) {
        allTexts.push(fullText);
      }

      const wrapper = document.createElement('article');
      wrapper.className = 'error-entry';
      wrapper.dataset.copyArea = '';

      const header = document.createElement('div');
      header.className = 'error-entry__header';
      header.innerHTML = `
        <div class="error-entry__title">
          <span class="error-entry__timestamp">${when}</span>
          <span class="error-entry__endpoint">${endpoint}</span>
        </div>
      `;

      const actions = document.createElement('div');
      actions.className = 'error-entry__actions';
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'btn btn--ghost btn--compact';
      copyBtn.textContent = 'Скопировать';
      copyBtn.addEventListener('click', () => copyTextPayload(fullText, copyBtn));
      const feedback = document.createElement('span');
      feedback.className = 'copy-feedback';
      feedback.setAttribute('data-copy-feedback', '');
      actions.appendChild(copyBtn);
      actions.appendChild(feedback);

      header.appendChild(actions);
      wrapper.appendChild(header);

      const details = document.createElement('details');
      details.className = 'error-entry__details';
      details.open = true;
      const summary = document.createElement('summary');
      summary.textContent = errText;
      const pre = document.createElement('pre');
      pre.className = 'error-entry__trace';
      pre.textContent = traceText || '—';
      details.appendChild(summary);
      details.appendChild(pre);

      wrapper.appendChild(details);
      container.appendChild(wrapper);
    });

    if (copyAllBtn) {
      copyAllBtn.disabled = allTexts.length === 0;
      copyAllBtn.onclick = () => {
        const combined = allTexts.join('\n\n');
        copyTextPayload(combined, copyAllBtn);
      };
    }
  }

  async function loadMetrics() {
    const metricsPanel = document.querySelector('[data-tab-panel="metrics"].is-active');
    if (!metricsPanel) return;

    try {
      const response = await fetch('/api/metrics', { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      if (!response.ok) throw new Error('Response not ok');

      const data = await response.json();
      const nowSec = Date.now() / 1000;
      setStatus(`Обновлено: ${new Date().toLocaleTimeString()}`);

      const uptime = Math.max(0, Math.round(nowSec - (data.started_at || nowSec)));
      setText('metric-uptime', formatUptime(uptime));
      setText('metric-started', data.started_at ? new Date(data.started_at * 1000).toLocaleString() : '—');
      setText('metric-active-requests', data?.requests?.active ?? '—');
      setText('metric-total-requests', data?.requests?.total ?? '—');
      const rpsValues = data?.requests?.last_minute_rps || [];
      const rpsAvg = rpsValues.length
        ? (rpsValues.reduce((a, b) => a + b, 0) / rpsValues.length).toFixed(2)
        : '0.00';
      setText('metric-rps', rpsAvg);
      setText('metric-cpu', data?.system?.cpu_percent ?? '—');
      setText('metric-ram', data?.system?.ram_percent ?? '—');
      setText('metric-rss', data?.system?.process_rss_mb ? `${data.system.process_rss_mb} МБ` : '—');

      setText('metric-snapshot-version', data?.snapshot?.version ?? '—');
      setText('metric-snapshot-orders', data?.snapshot?.orders_count ?? '—');
      setText('metric-snapshot-age', data?.snapshot?.seconds_ago !== undefined ? `${data.snapshot.seconds_ago} c назад` : '—');
      setText('metric-snapshot-last', formatMs(data?.snapshot?.last_build_ms));
      setText('metric-snapshot-avg', formatMs(data?.snapshot?.avg_build_ms));
      setText('metric-snapshot-count', data?.snapshot?.build_count ?? '—');

      setText('metric-sse-clients', data?.sse?.active_clients ?? '—');
      setText('metric-sse-age', data?.sse?.seconds_ago !== undefined ? `${data.sse.seconds_ago} c назад` : '—');

      const dbSize = data?.db?.size_bytes ? (data.db.size_bytes / (1024 * 1024)).toFixed(2) : '—';
      setText('metric-db-size', dbSize);
      const backupAgo = data?.db?.last_backup_hours_ago;
      setText('metric-db-backup', backupAgo !== null && backupAgo !== undefined ? `${backupAgo} ч назад` : '—');

      setText('metric-errors-total', data?.errors?.total ?? '—');
      setText('metric-errors-24h', data?.errors?.errors_last_24h ?? '—');

      buildThreadsTable(data?.threads, nowSec);
      buildEndpointsTable(data?.requests?.per_endpoint);
      buildErrorsList(data?.errors?.last_items);
    } catch (error) {
      console.error('Metrics load failed', error);
      setStatus('Не удалось обновить метрики', true);
    }
  }

  return { init };
})();

const UsersTable = (() => {
  function init() {
    const table = document.querySelector('[data-users-table]');
    if (!table) return;

    table.addEventListener('click', handleAction);
  }

  async function handleAction(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;

    const row = button.closest('tr');
    if (!row) return;

    switch (button.dataset.action) {
      case 'edit':
        toggleEdit(row, true);
        break;
      case 'cancel':
        resetRow(row);
        break;
      case 'save':
        saveRow(row);
        break;
      case 'delete':
        await deleteRow(row);
        break;
      case 'reset':
        changePassword(row);
        break;
      default:
        break;
    }
  }

  function toggleEdit(row, editing) {
    row.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('is-hidden', editing));
    row.querySelectorAll('[data-edit]').forEach(el => el.classList.toggle('is-hidden', !editing));

    if (editing) {
      const input = row.querySelector('.edit-username');
      if (input) {
        input.focus();
        input.select();
      }
    }
  }

  function resetRow(row) {
    const username = row.dataset.username || '';
    const role = row.dataset.role || '';
    const isActive = row.dataset.active === '1';

    const nameInput = row.querySelector('.edit-username');
    const roleSelect = row.querySelector('.edit-role');
    const activeCheckbox = row.querySelector('.edit-active');

    if (nameInput) nameInput.value = username;
    if (roleSelect) roleSelect.value = role;
    if (activeCheckbox) activeCheckbox.checked = isActive;

    toggleEdit(row, false);
  }

  function saveRow(row) {
    const id = Number(row.dataset.userId || 0);
    const usernameInput = row.querySelector('.edit-username');
    const roleSelect = row.querySelector('.edit-role');
    const activeCheckbox = row.querySelector('.edit-active');

    if (!usernameInput || !roleSelect || !activeCheckbox) return;

    const username = usernameInput.value.trim();
    const role = roleSelect.value;
    const isActive = !!activeCheckbox.checked;

    if (!username) {
      alert('Имя пользователя не может быть пустым.');
      return;
    }

    postJson('/settings/users/update', { id, username, role, is_active: isActive })
      .then(payload => {
        if (payload?.status !== 'ok') throw new Error(payload?.message || 'Не удалось сохранить пользователя.');
        updateRowView(row, { username, role, isActive });
        toggleEdit(row, false);
      })
      .catch(error => alert(error.message));
  }

  async function deleteRow(row) {
    const id = Number(row.dataset.userId || 0);
    const username = row.dataset.username || '';

    const approved = await ConfirmDialog.confirm(`Удалить пользователя "${username}"?`);
    if (!approved) return;

    postJson('/settings/users/delete', { id })
      .then(payload => {
        if (payload?.status !== 'ok') throw new Error(payload?.message || 'Не удалось удалить пользователя.');
        row.remove();
      })
      .catch(error => alert(error.message));
  }

  function changePassword(row) {
    const id = Number(row.dataset.userId || 0);
    const username = row.dataset.username || '';

    const newPassword = prompt(`Введите новый пароль для "${username}":`);
    if (newPassword === null) return;

    const trimmedPassword = newPassword.trim();
    if (!trimmedPassword) {
      alert('Пароль не может быть пустым.');
      return;
    }

    postJson('/settings/users/reset_password', { id, new_password: trimmedPassword })
      .then(payload => {
        if (payload?.status !== 'ok') throw new Error(payload?.message || 'Не удалось изменить пароль.');
        showPasswordNotice(username, payload.password || trimmedPassword);
      })
      .catch(error => alert(error.message));
  }

  function updateRowView(row, { username, role, isActive }) {
    row.dataset.username = username;
    row.dataset.role = role;
    row.dataset.active = isActive ? '1' : '0';

    const nameCell = row.querySelector('.user-cell');
    const roleBadge = row.querySelector('.role-badge');
    const statusContainer = row.querySelector('[data-view] .status-pill')?.parentElement;

    if (nameCell) nameCell.textContent = username;
    if (roleBadge) roleBadge.textContent = role;

    const statusPill = document.createElement('span');
    statusPill.className = `status-pill ${isActive ? 'status-pill--success' : 'status-pill--neutral'}`;
    statusPill.textContent = isActive ? 'Активен' : 'Заблокирован';

    if (statusContainer) {
      statusContainer.innerHTML = '';
      statusContainer.appendChild(statusPill);
    }
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: withCsrfHeaders({ 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }),
      body: JSON.stringify(withCsrfBody(body))
    }).then(async response => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.message || 'Запрос завершился с ошибкой.');
      }
      return payload;
    });
  }

  function showPasswordNotice(username, password) {
    const box = document.querySelector('[data-password-notice]');
    const text = document.querySelector('[data-password-text]');
    if (!box || !text) return;

    text.textContent = `Новый пароль для ${username}: ${password}`;
    box.classList.remove('is-hidden');
  }

  return { init };
})();

const UIEffects = (() => {
  function init() {
    renderToasts();
    highlightJournal();
    enhanceAuthForms();
    rememberClients();
  }

  function renderToasts() {
    const stack = document.getElementById('toastStack');
    const page = document.body?.dataset?.page || '';
    if (!stack || page !== 'settings') return;

    const notices = document.querySelectorAll('.notice-card');
    notices.forEach(notice => {
      const text = notice.textContent.trim();
      const type = notice.classList.contains('notice-card--error')
        ? 'error'
        : notice.classList.contains('notice-card--warning')
          ? 'warning'
          : 'success';
      pushToast(stack, { title: type === 'error' ? 'Ошибка' : 'Состояние', desc: text, type });
      notice.remove();
    });
  }

  function pushToast(stack, { title, desc, type }) {
    const toast = document.createElement('div');
    toast.className = `toast toast--${type || 'info'}`;

    toast.innerHTML = `
      <div class="toast__title">${title || 'Сообщение'}</div>
      <div class="toast__desc">${desc || ''}</div>
      <button class="toast__close" aria-label="Закрыть">✕</button>
    `;

    toast.querySelector('.toast__close')?.addEventListener('click', () => toast.remove());
    stack.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('is-visible'));
    setTimeout(() => toast.remove(), 4200);
  }

  function highlightJournal() {
    if (!window.location.pathname.includes('journal')) return;
    document.querySelectorAll('tbody tr').forEach((row, index) => {
      if (index < 5) {
        row.classList.add('flash-highlight');
        setTimeout(() => row.classList.remove('flash-highlight'), 1100);
      }
    });
  }

  function enhanceAuthForms() {
    const page = document.body?.dataset?.page || '';
    if (page !== 'login' && page !== 'setup') return;
    const hasError = document.querySelector('.notice-card--error');
    if (!hasError) return;
    document.querySelectorAll('input').forEach(input => {
      input.classList.add('shake');
      setTimeout(() => input.classList.remove('shake'), 320);
    });
  }

  function rememberClients() {
    if (!window.location.pathname.includes('clients')) return;
    const addForm = document.querySelector('form[action="/add_client"]');
    const nameInput = addForm?.querySelector('input[name="client"]');

    addForm?.addEventListener('submit', () => {
      if (nameInput?.value) {
        sessionStorage.setItem('recentClientName', nameInput.value.trim());
      }
    });

    const recentName = sessionStorage.getItem('recentClientName');
    if (!recentName) return;
    const row = document.querySelector(`tr[data-client="${CSS.escape(recentName)}"]`);
    if (row) {
      row.classList.add('flash-highlight');
      setTimeout(() => row.classList.remove('flash-highlight'), 1200);
      sessionStorage.removeItem('recentClientName');
    }
  }

  return { init };
})();

const ClientsPage = (() => {
  function init() {
    const table = document.querySelector('[data-clients-table]');
    if (!table) return;

    table.addEventListener('click', handleAction);
  }

  async function handleAction(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;

    const row = button.closest('tr');
    if (!row) return;

    const action = button.dataset.action;

    switch (action) {
      case 'edit':
        toggleEdit(row, true);
        break;
      case 'save':
        await saveClient(row);
        break;
      case 'delete':
        await deleteClient(row);
        break;
      default:
        break;
    }
  }

  function toggleEdit(row, editing) {
    row.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('is-hidden', editing));
    row.querySelectorAll('[data-edit]').forEach(el => el.classList.toggle('is-hidden', !editing));

    if (editing) {
      const input = row.querySelector('.edit-name');
      if (input) {
        input.focus();
        input.select();
      }
    }
  }

  async function saveClient(row) {
    const oldName = row.dataset.client;
    const nameInput = row.querySelector('.edit-name');
    const managerSelect = row.querySelector('.edit-manager');

    if (!nameInput || !managerSelect) return;

    const newName = nameInput.value.trim();
    const newManager = managerSelect.value;

    if (!newName) {
      alert('Имя клиента не может быть пустым!');
      return;
    }

    const approved = await ConfirmDialog.confirm('Сохранить изменения?');
    if (!approved) return;

    fetch('/update_client', {
      method: 'POST',
      headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(withCsrfBody({ old_name: oldName, new_name: newName, new_manager: newManager }))
    })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok || (data && data.status !== 'ok')) {
          alert((data && data.message) || 'Не удалось обновить клиента.');
          return;
        }
        window.location.reload();
      })
      .catch(() => alert('Не удалось обновить клиента.'));
  }

  async function deleteClient(row) {
    const client = row.dataset.client;
    if (!client) return;

    const approved = await ConfirmDialog.confirm(`Удалить клиента "${client}"?`);
    if (!approved) return;

    fetch('/delete_client', {
      method: 'POST',
      headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(withCsrfBody({ old_name: oldName, new_name: newName, new_manager: newManager }))
    })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok || (data && data.status !== 'ok')) {
          alert((data && data.message) || 'Не удалось удалить клиента.');
          return;
        }
        window.location.reload();
      })
      .catch(() => alert('Не удалось удалить клиента.'));
  }

  return { init };
})();

const SearchPage = (() => {
  function init() {
    document.querySelectorAll('[data-copy-path]').forEach(button => {
      button.addEventListener('click', () => copyPath(button));
    });

    const periodSelect = document.getElementById('period');
    if (periodSelect && periodSelect.form) {
      periodSelect.addEventListener('change', () => periodSelect.form.submit());
    }
  }

  function copyPath(button) {
    const text = button.dataset.copyPath;
    if (!text) return;

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(() => showCopied(button))
        .catch(() => fallbackCopy(text, button));
    } else {
      fallbackCopy(text, button);
    }
  }

  function fallbackCopy(text, button) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    try {
      document.execCommand('copy');
      showCopied(button);
    } catch (error) {
      alert('Ошибка копирования');
    }

    document.body.removeChild(textarea);
  }

  function showCopied(button) {
    const status = button.parentElement?.querySelector('.copy-status');
    if (!status) return;
    status.classList.add('is-visible');
    setTimeout(() => status.classList.remove('is-visible'), 2000);
  }

  return { init };
})();

const SetupPage = (() => {
  function init() {
    const form = document.querySelector('[data-setup-form]');
    if (!form) return;

    bindRepeater('manager');
    bindRepeater('technologist');
  }

  function bindRepeater(prefix) {
    const container = document.querySelector(`[data-${prefix}-rows]`);
    const addBtn = document.querySelector(`[data-add-${prefix}]`);
    if (!container || !addBtn) return;

    addBtn.addEventListener('click', (event) => {
      event.preventDefault();
      addRow(prefix, container);
    });

    container.addEventListener('click', (event) => {
      const removeBtn = event.target.closest('[data-remove-row]');
      if (!removeBtn) return;
      const row = removeBtn.closest('[data-repeater-row]');
      const rows = container.querySelectorAll('[data-repeater-row]');
      if (row && rows.length > 1) {
        row.remove();
      }
    });
  }

  function addRow(prefix, container) {
    const row = document.createElement('div');
    row.className = 'repeater-row';
    row.setAttribute('data-repeater-row', '');

    if (prefix === 'technologist') {
      row.innerHTML = `
        <input type="text" class="input" name="technologist_marker[]" placeholder="Маркер">
        <input type="text" class="input" name="technologist_name[]" placeholder="Имя">
        <input type="text" class="input" name="technologist_username[]" placeholder="Логин">
        <input type="password" class="input" name="technologist_password[]" placeholder="Пароль">
        <button type="button" class="btn btn--ghost" data-remove-row>✖</button>
      `;
    } else {
      row.innerHTML = `
        <input type="text" class="input" name="manager_name[]" placeholder="Имя для отображения">
        <input type="text" class="input" name="manager_username[]" placeholder="Логин">
        <input type="password" class="input" name="manager_password[]" placeholder="Пароль">
        <button type="button" class="btn btn--ghost" data-remove-row>✖</button>
      `;
    }

    container.appendChild(row);
  }

  return { init };
})();

const FacadesPage = (() => {
  function init() {
    const container = document.getElementById('facadeList');
    const addRowBtn = document.getElementById('addRow');
    const clearBtn = document.getElementById('clearAll');
    const generateBtn = document.getElementById('generate');

    if (!container || !addRowBtn || !clearBtn || !generateBtn) return;

    addRowBtn.addEventListener('click', () => {
      clearStatus();
      addRow();
    });

    clearBtn.addEventListener('click', () => {
      clearStatus();
      resetForm();
    });

    generateBtn.addEventListener('click', () => {
      clearStatus();
      const items = gatherItems();
      if (!items.length) {
        setStatus('Добавьте хотя бы одну строку перед генерацией.', 'error');
        return;
      }

      fetch('/facades/generate', {
        method: 'POST',
        headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(withCsrfBody({ items }))
      })
        .then(async response => {
          const data = await response.json().catch(() => ({}));
          if (!response.ok) {
            const errors = (data.errors || ['Не удалось создать файл.']).join('\n');
            throw new Error(errors);
          }
          return data;
        })
        .then(data => {
          const folder = data.folder ? `в папке ${data.folder}` : 'в заданной папке';
          setStatus(`Файл ${data.file} успешно создан ${folder}.`, 'success');
        })
        .catch(error => {
          setStatus(error.message, 'error');
        });
    });

    resetForm();
  }

  function createNumberInput(min, placeholder, value = '') {
    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(min);
    input.placeholder = placeholder;
    input.value = value;
    return input;
  }

  function createTextInput(placeholder, value = '') {
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = placeholder;
    input.value = value;
    return input;
  }

  function createSelect(options, selected) {
    const select = document.createElement('select');
    options.forEach(opt => {
      const option = document.createElement('option');
      option.value = opt.value;
      option.textContent = opt.label;
      if (opt.value === selected) option.selected = true;
      select.appendChild(option);
    });
    return select;
  }

  function updateRowNumbers() {
    document.querySelectorAll('.facade-row').forEach((row, index) => {
      const indexEl = row.querySelector('.row-index');
      if (indexEl) {
        indexEl.textContent = index + 1;
      }
    });
  }

  function addRow(data = {}) {
    const list = document.getElementById('facadeList');
    if (!list) return;

    const row = document.createElement('div');
    row.className = 'facade-row facade-grid';

    const indexEl = document.createElement('div');
    indexEl.className = 'row-index';
    row.appendChild(indexEl);

    const positionInput = createTextInput('позиция', data.position || '');
    positionInput.dataset.field = 'position';
    row.appendChild(positionInput);

    const heightInput = createNumberInput(1, 'высота', data.height || '');
    heightInput.dataset.field = 'height';
    row.appendChild(heightInput);

    const widthInput = createNumberInput(1, 'ширина', data.width || '');
    widthInput.dataset.field = 'width';
    row.appendChild(widthInput);

    const countInput = createNumberInput(1, 'кол-во', data.count || '1');
    countInput.classList.add('quantity-input');
    countInput.dataset.field = 'count';
    row.appendChild(countInput);

    const hingesSelect = createSelect([
      { value: '2', label: '2' },
      { value: '3', label: '3' },
      { value: '4', label: '4' },
      { value: '5', label: '5' },
      { value: '6', label: '6' }
    ], data.hinges || '2');
    hingesSelect.classList.add('hinges-select');
    hingesSelect.dataset.field = 'hinges';
    row.appendChild(hingesSelect);

    const sideSelect = createSelect([
      { value: 'left', label: 'Левая' },
      { value: 'right', label: 'Правая' }
    ], data.side || 'right');
    sideSelect.dataset.field = 'side';
    row.appendChild(sideSelect);

    const removeWrapper = document.createElement('div');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'btn btn--danger btn--small remove-btn';
    removeBtn.textContent = '✖';
    removeBtn.title = 'Удалить строку';
    removeBtn.addEventListener('click', () => {
      row.remove();
      updateRowNumbers();
    });
    removeWrapper.appendChild(removeBtn);
    row.appendChild(removeWrapper);

    list.appendChild(row);
    updateRowNumbers();
  }

  function gatherItems() {
    const items = [];
    document.querySelectorAll('.facade-row').forEach(row => {
      const position = row.querySelector('[data-field="position"]');
      const width = row.querySelector('[data-field="width"]');
      const height = row.querySelector('[data-field="height"]');
      const count = row.querySelector('[data-field="count"]');
      const hinges = row.querySelector('[data-field="hinges"]');
      const side = row.querySelector('[data-field="side"]');

      if (!position || !width || !height || !count || !hinges || !side) return;
      if (!width.value && !height.value && !count.value) return;

      items.push({
        position: position.value.trim(),
        width: width.value.trim(),
        height: height.value.trim(),
        count: count.value.trim(),
        hinges: hinges.value.trim(),
        side: side.value
      });
    });
    return items;
  }

  function setStatus(message, type) {
    const status = document.getElementById('status');
    if (!status) return;
    status.textContent = message;
    status.className = `status ${type}`;
  }

  function clearStatus() {
    const status = document.getElementById('status');
    if (!status) return;
    status.textContent = '';
    status.className = 'status';
  }

  function resetForm() {
    const list = document.getElementById('facadeList');
    if (!list) return;
    list.innerHTML = '';
    addRow();
    updateRowNumbers();
  }

  return {
    init
  };
})();

window.setStatusFilter = OrdersPage.setStatusFilter;
window.setManagerFilter = OrdersPage.setManagerFilter;
window.sortBy = OrdersPage.sortBy;

document.addEventListener('DOMContentLoaded', () => {
  OrdersPage.init();
  ClientsPage.init();
  SearchPage.init();
  SetupPage.init();
  FacadesPage.init();
  SettingsPage.init();
  UsersTable.init();
  UIEffects.init();
});

const ClientsTable = (() => {

  function init() {
    const table = document.querySelector('[data-clients-table]');
    if (!table) return;

    table.addEventListener('click', handleClick);
  }

  function handleClick(e) {
    const row = e.target.closest('tr');
    if (!row) return;

    if (e.target.closest('[data-edit-btn]')) {
      switchToEdit(row);
    } 
    else if (e.target.closest('[data-delete-btn]')) {
      deleteClient(row);
    }
    else if (e.target.closest('[data-save-btn]')) {
      saveClient(row);
    }
    else if (e.target.closest('[data-cancel-btn]')) {
      cancelEdit(row);
    }
  }

  function switchToEdit(row) {
    row.querySelectorAll('[data-view]').forEach(el => el.classList.add('is-hidden'));
    row.querySelectorAll('[data-edit]').forEach(el => el.classList.remove('is-hidden'));
  }

  function cancelEdit(row) {
    row.querySelectorAll('[data-edit]').forEach(el => el.classList.add('is-hidden'));
    row.querySelectorAll('[data-view]').forEach(el => el.classList.remove('is-hidden'));
  }

  function saveClient(row) {
    const oldName = row.dataset.client;
    const newName = row.querySelector('.edit-name').value.trim();
    const newManager = row.querySelector('.edit-manager').value.trim();

    if (newName) {
      sessionStorage.setItem('recentClientName', newName);
    }

    fetch('/update_client', {
      method: 'POST',
      headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(withCsrfBody({
        old_name: oldName,
        new_name: newName,
        manager: newManager,
		new_manager: newManager
      }))
    })
      .then(r => r.json())
      .then(res => {
        if (res.status === 'ok') {
          row.dataset.client = newName;
          row.querySelector('[data-view]').textContent = newName;
          location.reload();
        } else {
          alert(res.message || 'Ошибка сохранения');
        }
      });
  }

  async function deleteClient(row) {
    const name = row.dataset.client;
    const approved = await ConfirmDialog.confirm(`Удалить клиента "${name}"?`);
    if (!approved) return;

    fetch('/delete_client', {
      method: 'POST',
      headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(withCsrfBody({ name }))
    })
      .then(r => r.json())
      .then(res => {
        if (res.status === 'ok') {
          row.remove();
        } else {
          alert(res.message || 'Не удалось удалить');
        }
      });
  }

  return { init };

})();

document.addEventListener('DOMContentLoaded', () => {
  ClientsTable.init();
});