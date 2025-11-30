const APP_CONFIG = (() => {
  if (typeof document === 'undefined') {
    return { managers: [], orderConfirmationEnabled: false };
  }

  const body = document.body;
  const managersRaw = body?.dataset?.managers || '';
  const managers = managersRaw
    .split(',')
    .map(name => name.trim())
    .filter(Boolean);

  const orderConfirmationEnabled = body?.dataset?.orderConfirmation === 'true';
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

  return { managers, orderConfirmationEnabled, currentUser, currentRole, permissions };
})();

const OrdersPage = (() => {
  let allData = [];
  let currentStatus = 'all';
  let currentManager =
    APP_CONFIG.currentRole === 'manager' && APP_CONFIG.currentUser
      ? APP_CONFIG.currentUser
      : 'Все';
  let sortKey = null;
  let sortOrder = 1;
  let sse = null;
  let pollingTimer = null;
  let pollingBackoff = 5000;

  function init() {
    const table = document.getElementById('orders');
    if (!table) return;

    highlightActiveFilter(currentStatus);
    highlightSortButtons();

    const managerSelect = document.getElementById('managerSelect');
    if (managerSelect && currentManager !== 'Все') {
      managerSelect.value = currentManager;
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

    const streamUrl = `/events?${buildManagerParams().toString()}`;
    sse = new EventSource(streamUrl);

    sse.onmessage = ev => {
      pollingBackoff = 5000;
      stopPollingFallback();
      if (!ev?.data) return;
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === 'orders_snapshot') {
          allData = Array.isArray(payload.folders) ? payload.folders : [];
          render();
          renderStats(payload);
        }
      } catch (err) {
        console.warn('Некорректные данные SSE', err);
      }
    };

    sse.onerror = () => {
      stopSSE();
      startPollingFallback();
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

  function updateDataFromPayload(payload) {
    const orders = Array.isArray(payload?.orders)
      ? payload.orders
      : Array.isArray(payload?.folders)
        ? payload.folders
        : [];

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
	const confirmedCount = folders.filter(item => item.status === 'Подтвержден').length;

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

  function canConfirm(item) {
    const role = APP_CONFIG.currentRole;
    const user = APP_CONFIG.currentUser || '';
    const manager = (item.manager || 'Неизвестно').trim() || 'Неизвестно';

    if (!APP_CONFIG.orderConfirmationEnabled) return false;

    if (role === 'admin' || role === 'technologist') {
      return true;
    }

    if (role === 'manager') {
      if (manager === user) return true;
      if (manager === 'Неизвестно') return true;
      return false;
    }

    return false;
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

    table.innerHTML = '';

    filtered.forEach(item => {
      const tr = document.createElement('tr');
      if (item.status === 'Готов') {
        tr.classList.add('done');
      } else if (item.status === 'Подтвержден') {
        tr.classList.add('confirmed');
      } else {
        tr.classList.add('new');
      }

      let statusClass = 'status-pill status-pill--warning';
      if (item.status === 'Готов') {
        statusClass = 'status-pill status-pill--success';
      } else if (item.status === 'Подтвержден') {
        statusClass = 'status-pill status-pill--neutral';
      }

      const nameTd = document.createElement('td');
      const nameDiv = document.createElement('div');
      nameDiv.className = 'table-primary';
      nameDiv.textContent = item.name || '';
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

      if (APP_CONFIG.orderConfirmationEnabled) {
        const confirmTd = document.createElement('td');
        confirmTd.className = 'table-checkbox';

        if (item.status === 'Готов' && canConfirm(item)) {
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.className = 'confirm-checkbox';
          checkbox.checked = false;
          const orderLabel = getOrderLabel(item);
          const ariaLabel = orderLabel ? `Подтвердить заказ ${orderLabel}` : 'Подтвердить заказ';
          checkbox.setAttribute('aria-label', ariaLabel);
          checkbox.addEventListener('change', () => handleOrderConfirmation(item, checkbox));
          confirmTd.appendChild(checkbox);
        } else if (item.status === 'Подтвержден') {
          const orderLabel = getOrderLabel(item);
          const confirmedMark = document.createElement('span');
          confirmedMark.className = 'confirm-status';
          confirmedMark.textContent = '✔';
          const ariaLabel = orderLabel ? `Заказ подтвержден ${orderLabel}` : 'Заказ подтвержден';
          confirmedMark.setAttribute('aria-label', ariaLabel);
          confirmedMark.title = orderLabel ? `Подтвержден: ${orderLabel}` : 'Заказ подтвержден';
          confirmTd.appendChild(confirmedMark);
        } else {
          const placeholder = document.createElement('span');
          placeholder.className = 'table-muted';
          placeholder.textContent = '—';
          confirmTd.appendChild(placeholder);
        }

        tr.appendChild(confirmTd);
      }

      table.appendChild(tr);
    });
  }

  function getOrderNumber(item) {
    if (!item) return '';
    if (typeof item.order_number === 'string' && item.order_number.trim()) {
      return item.order_number.trim();
    }
    return extractOrderNumber(item.name);
  }

  function extractOrderNumber(name) {
    if (!name) return '';
    const firstPart = name.trim().split(/\s+/)[0] || '';
    return firstPart.replace(/\+$/, '');
  }

  function getClientName(item) {
    const name = item?.name || '';
    if (!name.trim()) return '';
    const withoutPlus = name.replace(/\s+\+$/, '').trim();
    const firstSpaceIndex = withoutPlus.indexOf(' ');
    if (firstSpaceIndex === -1) return '';
    let clientPart = withoutPlus.slice(firstSpaceIndex + 1).trim();
    clientPart = clientPart.replace(/\[[^\]]*\]/g, '').trim();
    return clientPart;
  }

  function getOrderLabel(item) {
    const orderNumber = getOrderNumber(item);
    const clientName = getClientName(item);
    const label = [orderNumber, clientName].filter(Boolean).join(' ');
    return label || item?.name || '';
  }

  function handleOrderConfirmation(item, checkbox) {
    const orderLabel = getOrderLabel(item);
    const message = orderLabel ? `Подтвердить заказ ${orderLabel}?` : 'Подтвердить заказ?';
    if (!window.confirm(message)) {
      checkbox.checked = false;
      return;
    }

    checkbox.disabled = true;

    fetch('/confirm_order', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ folder: item.name })
    })
      .then(async response => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.status !== 'ok') {
          const errorMessage = payload.message || 'Не удалось подтвердить заказ.';
          throw new Error(errorMessage);
        }
        return payload;
      })
      .then(() => {
        loadData();
      })
      .catch(error => {
        checkbox.checked = false;
        checkbox.disabled = false;
        window.alert(error.message || 'Не удалось подтвердить заказ.');
      });
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

  function buildThreadsTable(threads, nowSec) {
    const tbody = document.getElementById('metrics-threads');
    if (!tbody) return;
    tbody.innerHTML = '';

    Object.entries(threads || {})
      .sort(([a], [b]) => a.localeCompare(b))
      .forEach(([name, info]) => {
        const lastTs = info?.last_heartbeat || 0;
        const secondsAgo = lastTs ? Math.max(0, (nowSec - lastTs).toFixed(1)) : '—';
        const alive = lastTs && nowSec - lastTs <= 5 ? 'OK' : 'DEAD';

        const row = document.createElement('tr');
        row.innerHTML = `
          <td>${name}</td>
          <td>${secondsAgo} c назад</td>
          <td>
            <span class="status-pill ${alive === 'OK' ? 'status-pill--success' : 'status-pill--error'}">${alive}</span>
          </td>
        `;
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
    if (!container) return;
    container.innerHTML = '';

    (errors || []).slice(-10).reverse().forEach(item => {
      const wrapper = document.createElement('details');
      const when = item?.ts ? new Date(item.ts * 1000).toLocaleString() : '';
      wrapper.innerHTML = `
        <summary>${when} · ${item?.endpoint || ''} · ${item?.err || ''}</summary>
        <pre>${item?.trace || ''}</pre>
      `;
      container.appendChild(wrapper);
    });
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

  function handleAction(event) {
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
        deleteRow(row);
        break;
      case 'reset':
        resetPassword(row);
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

  function deleteRow(row) {
    const id = Number(row.dataset.userId || 0);
    const username = row.dataset.username || '';

    if (!confirm(`Удалить пользователя "${username}"?`)) return;

    postJson('/settings/users/delete', { id })
      .then(payload => {
        if (payload?.status !== 'ok') throw new Error(payload?.message || 'Не удалось удалить пользователя.');
        row.remove();
      })
      .catch(error => alert(error.message));
  }

  function resetPassword(row) {
    const id = Number(row.dataset.userId || 0);
    const username = row.dataset.username || '';

    if (!confirm(`Сбросить пароль для "${username}"?`)) return;

    postJson('/settings/users/reset_password', { id })
      .then(payload => {
        if (payload?.status !== 'ok') throw new Error(payload?.message || 'Не удалось сбросить пароль.');
        showPasswordNotice(username, payload.password);
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
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify(body)
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

const ClientsPage = (() => {
  function init() {
    const table = document.querySelector('[data-clients-table]');
    if (!table) return;

    table.addEventListener('click', handleAction);
  }

  function handleAction(event) {
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
        saveClient(row);
        break;
      case 'delete':
        deleteClient(row);
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

  function saveClient(row) {
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

    if (!confirm('Сохранить изменения?')) return;

    fetch('/update_client', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_name: oldName, new_name: newName, new_manager: newManager })
    }).then(() => window.location.reload());
  }

  function deleteClient(row) {
    const client = row.dataset.client;
    if (!client) return;

    if (!confirm(`Удалить клиента "${client}"?`)) return;

    fetch('/delete_client', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: client })
    }).then(() => window.location.reload());
  }

  return { init };
})();

const SearchPage = (() => {
  function init() {
    document.querySelectorAll('[data-copy-path]').forEach(button => {
      button.addEventListener('click', () => copyPath(button));
    });
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
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items })
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
  FacadesPage.init();
  SettingsPage.init();
  UsersTable.init();
});