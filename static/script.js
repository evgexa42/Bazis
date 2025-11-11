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

  return { managers, orderConfirmationEnabled };
})();

const OrdersPage = (() => {
  let allData = [];
  let currentStatus = 'all';
  let currentManager = 'Все';
  let sortKey = null;
  let sortOrder = 1;
  let refreshTimer = null;

  function init() {
    const table = document.getElementById('orders');
    if (!table) return;

    highlightActiveFilter(currentStatus);
    highlightSortButtons();
    loadData();
    refreshTimer = setInterval(loadData, 5000);
  }

  function loadData() {
    fetch(`/data?manager=${encodeURIComponent(currentManager)}`)
      .then(response => response.json())
      .then(json => {
        allData = Array.isArray(json.folders) ? json.folders : [];
        render();
        renderStats(json);
      })
      .catch(error => console.error('Ошибка при загрузке данных:', error));
  }

  function setStatusFilter(status) {
    currentStatus = status;
    highlightActiveFilter(status);
    render();
  }

  function setManagerFilter() {
    const select = document.getElementById('managerSelect');
    if (!select) return;
    currentManager = select.value;
    loadData();
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

    const doneCount = folders.filter(item => item.status === 'Готов').length;
    const newCount = folders.filter(item => item.status === 'Новый').length;

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
      .filter(item => currentStatus === 'all' || item.status === currentStatus)
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
      tr.className = item.status === 'Готов' ? 'done' : 'new';

      const statusClass = item.status === 'Готов' ? 'status-pill status-pill--success' : 'status-pill status-pill--warning';

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

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'confirm-checkbox';
        checkbox.checked = Boolean(item.confirmed);
        checkbox.disabled = Boolean(item.confirmed);
        checkbox.setAttribute('aria-label', `Подтвердить заказ ${getOrderNumber(item) || item.name || ''}`);

        if (!item.confirmed) {
          checkbox.addEventListener('change', () => handleOrderConfirmation(item, checkbox));
        }

        confirmTd.appendChild(checkbox);
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

  function handleOrderConfirmation(item, checkbox) {
    const orderNumber = getOrderNumber(item) || item.name || '';
    const message = `Точно хотите подтвердить заказ ${orderNumber}?`;
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
});