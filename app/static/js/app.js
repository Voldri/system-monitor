/**
 * Головний контролер веб-дашборду System Monitor (Етап 3).
 * - Опитування поточних метрик: раз на 1 с (плитки, смужки ядер, диски)
 * - Графік "Наживо": раз на 1 с (зчитується з in-memory буфера)
 * - Опитування процесів: раз на 5 с
 * - Графіки "Година" / "День": при кліку та раз на 60 с
 * - Моніторинг та спливаючі сповіщення (toasts) про нові тривоги
 * - Аналітика та індекс здоров'я системи (1h, 24h, 7d)
 * - Характеристики ПК (Windows 11, CPU, RAM, диски, GPU)
 * - Експорт в Excel (.xlsx) через системний діалог pywebview або браузер
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Ініціалізація компонентів графіка та вкладок
  const chart = new TelemetryChart('mainTelemetryChart', 'chartContainer', 'chartTooltip');
  let currentSort = 'cpu';
  let currentAnalyticsPeriod = '24h';
  let currentView = 'monitoring';

  // Відомі ID тривог для запобігання повторних toast-сповіщень
  const seenActiveAlertIds = new Set();
  let isFirstAlertsPoll = true;

  // Буфери спарклайнів (до 60 останніх точок)
  const sparkBuffers = {
    cpu: [],
    ram: [],
    gpu: [],
    net: [],
  };

  const tabsController = new TabsController((newRange) => {
    fetchChartHistory(newRange);
  });

  // 2. Допоміжні утиліти форматування
  function formatBytesGB(bytes) {
    if (!bytes || bytes <= 0) return '0.0 ГБ';
    return `${(bytes / (1024 ** 3)).toFixed(1)} ГБ`;
  }

  function formatBytesMB(bytes) {
    if (!bytes || bytes <= 0) return '0 МБ';
    return `${Math.round(bytes / (1024 ** 2))} МБ`;
  }

  function formatSpeed(bytesPerSec) {
    const b = Number(bytesPerSec) || 0;
    if (b < 1024) return `${Math.round(b)} Б/с`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)} КБ/с`;
    return `${(b / (1024 * 1024)).toFixed(1)} МБ/с`;
  }

  function getValueStateClass(pct) {
    const val = Number(pct) || 0;
    if (val > 90) return 'val-red';
    if (val >= 70) return 'val-yellow';
    return 'val-green';
  }

  function applyValueState(element, pct) {
    if (!element) return;
    element.classList.remove('val-green', 'val-yellow', 'val-red');
    element.classList.add(getValueStateClass(pct));
  }

  function formatAlertsPlural(count) {
    const n = Math.abs(count);
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) {
      return `${count} активна тривога`;
    }
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) {
      return `${count} активні тривоги`;
    }
    return `${count} активних тривог`;
  }

  // 3. Навігація по головних розділах
  const navButtons = document.querySelectorAll('.nav-tab-btn');
  const viewPanels = document.querySelectorAll('.view-panel');

  function switchView(viewName) {
    currentView = viewName;
    navButtons.forEach((btn) => {
      if (btn.getAttribute('data-view') === viewName) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    viewPanels.forEach((panel) => {
      if (panel.id === `view${viewName.charAt(0).toUpperCase() + viewName.slice(1)}`) {
        panel.classList.add('active');
      } else {
        panel.classList.remove('active');
      }
    });

    if (viewName === 'analytics') {
      fetchAnalytics(currentAnalyticsPeriod);
    } else if (viewName === 'alerts') {
      fetchAlerts();
    } else if (viewName === 'specs') {
      fetchSpecs();
    } else if (viewName === 'monitoring') {
      chart.resize();
      chart.render();
    }
  }

  navButtons.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const view = e.currentTarget.getAttribute('data-view');
      if (view) switchView(view);
    });
  });

  const activeAlertsBadge = document.getElementById('activeAlertsBadge');
  if (activeAlertsBadge) {
    activeAlertsBadge.addEventListener('click', () => switchView('alerts'));
  }

  // 4. Оновлення статусу сервера
  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const data = await res.json();

      const statusBadge = document.getElementById('collectorStatusBadge');
      const statusText = document.getElementById('collectorStatusText');
      const coresCount = document.getElementById('cpuCoresCount');
      const coresHeaderCount = document.getElementById('coresHeaderCount');

      if (data.collector_active) {
        if (statusBadge) statusBadge.style.display = 'inline-flex';
        if (statusText) statusText.textContent = 'Збір даних активний';
      } else {
        if (statusText) statusText.textContent = 'Збір призупинено';
      }

      const coresStr = `${data.logical_cores || '--'} логічних ядер`;
      if (coresCount) coresCount.textContent = coresStr;
      if (coresHeaderCount) coresHeaderCount.textContent = `${data.logical_cores || '--'} ядер`;
    } catch (e) {
      console.warn('Не вдалося отримати /api/status:', e);
    }
  }

  // 5. Оновлення поточних метрик (раз на 1 с)
  async function fetchCurrentMetrics() {
    try {
      const res = await fetch('/api/metrics/current');
      if (!res.ok) return;
      const data = await res.json();

      const timeDisplay = document.getElementById('localTimeDisplay');
      if (timeDisplay && data.local_time) {
        timeDisplay.textContent = data.local_time;
      }

      // 5.1 Плитка CPU
      const cpuVal = document.getElementById('cpuTotalVal');
      if (cpuVal) {
        cpuVal.textContent = `${data.cpu_total.toFixed(1)}%`;
        applyValueState(cpuVal, data.cpu_total);
      }
      pushSparklinePoint('cpu', data.cpu_total, 100);

      // 5.2 Плитка RAM
      const ramVal = document.getElementById('ramPercentVal');
      const ramCaption = document.getElementById('ramUsedCaption');
      const ramTotalInfo = document.getElementById('ramTotalInfo');
      if (ramVal) {
        ramVal.textContent = `${data.ram_percent.toFixed(1)}%`;
        applyValueState(ramVal, data.ram_percent);
      }
      if (ramCaption) {
        ramCaption.textContent = `${formatBytesGB(data.ram_used_bytes)} / ${formatBytesGB(data.ram_total_bytes)}`;
      }
      if (ramTotalInfo) {
        ramTotalInfo.textContent = formatBytesGB(data.ram_total_bytes);
      }
      pushSparklinePoint('ram', data.ram_percent, 100);

      // 5.3 Плитка GPU (NVIDIA RTX)
      const gpuVal = document.getElementById('gpuPercentVal');
      const gpuCaption = document.getElementById('gpuCaption');
      const gpuNameInfo = document.getElementById('gpuNameInfo');

      if (data.gpu_percent !== null && data.gpu_percent !== undefined) {
        if (gpuVal) {
          gpuVal.textContent = `${data.gpu_percent.toFixed(1)}%`;
          applyValueState(gpuVal, data.gpu_percent);
        }
        if (gpuCaption) {
          const usedStr = formatBytesGB(data.gpu_mem_used_bytes);
          const totalStr = formatBytesGB(data.gpu_mem_total_bytes);
          const tempStr = data.gpu_temp_c !== null ? `${Math.round(data.gpu_temp_c)}°C` : '--°C';
          gpuCaption.textContent = `VRAM: ${usedStr} / ${totalStr} • ${tempStr}`;
        }
        if (gpuNameInfo && data.gpu_name) {
          gpuNameInfo.textContent = data.gpu_name;
        }
        pushSparklinePoint('gpu', data.gpu_percent, 100);
      } else {
        if (gpuVal) gpuVal.textContent = '--%';
        if (gpuCaption) gpuCaption.textContent = 'GPU не виявлено';
      }

      // 5.4 Плитка Мережі
      const netRecv = document.getElementById('netRecvVal');
      const netSent = document.getElementById('netSentVal');
      if (netRecv) netRecv.textContent = `↓ ${formatSpeed(data.net_recv_bytes_sec)}`;
      if (netSent) netSent.textContent = `↑ ${formatSpeed(data.net_sent_bytes_sec)}`;
      const totalSpeed = (Number(data.net_recv_bytes_sec) || 0) + (Number(data.net_sent_bytes_sec) || 0);
      pushSparklinePoint('net', totalSpeed, null);

      // 5.5 Сітка ядер процесора
      renderCpuCores(data.cpu_cores || []);

      // 5.6 Розділи дисків зі спільною швидкістю
      renderDisks(data.disks_info || [], data.disk_read_bytes_sec, data.disk_write_bytes_sec);

    } catch (e) {
      console.warn('Помилка при завантаженні /api/metrics/current:', e);
    }
  }

  // 6. Спарклайни для плиток
  function pushSparklinePoint(type, val, maxVal) {
    const buf = sparkBuffers[type];
    if (!buf) return;
    buf.push(Number(val) || 0);
    if (buf.length > 60) buf.shift();

    const canvasId = `sparkline${type.charAt(0).toUpperCase() + type.slice(1)}`;
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    drawSparkline(canvas, buf, maxVal, type);
  }

  function drawSparkline(canvas, data, fixedMax, type) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 90;
    const h = canvas.clientHeight || 38;

    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
    }

    ctx.resetTransform();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (data.length < 2) return;

    const max = fixedMax !== null ? fixedMax : (Math.max(...data, 1024) * 1.1);
    const min = 0;
    const range = Math.max(max - min, 1);

    const stepX = w / (data.length - 1);

    let strokeColor = '#4c8dff';
    if (type === 'ram') {
      strokeColor = '#a78bfa';
    } else if (type === 'gpu') {
      strokeColor = '#f0883e';
    } else if (type === 'net') {
      strokeColor = '#3fb950';
    }

    ctx.beginPath();
    data.forEach((v, idx) => {
      const x = idx * stepX;
      const normalized = Math.max(0, Math.min(1, (v - min) / range));
      const y = h - (normalized * (h - 6)) - 3;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    ctx.strokeStyle = strokeColor;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  }

  // 7. Рендеринг сітки ядер процесора
  function renderCpuCores(cores) {
    const grid = document.getElementById('coresGrid');
    if (!grid) return;

    if (!cores || cores.length === 0) {
      grid.innerHTML = '<div class="empty-state">Дані ядер відсутні</div>';
      return;
    }

    if (grid.children.length !== cores.length || grid.querySelector('.empty-state')) {
      grid.innerHTML = '';
      const cols = Math.ceil(cores.length / 2);
      grid.style.setProperty('--core-cols', cols);
      cores.forEach((_, idx) => {
        const item = document.createElement('div');
        item.className = 'core-bar-wrapper';
        item.innerHTML = `
          <div class="core-pct-label" id="coreVal${idx}">--%</div>
          <div class="core-bar-track">
            <div class="core-bar-fill" id="coreFill${idx}"></div>
          </div>
          <div class="core-label">C${idx + 1}</div>
        `;
        grid.appendChild(item);
      });
    }

    cores.forEach((val, idx) => {
      const fill = document.getElementById(`coreFill${idx}`);
      const text = document.getElementById(`coreVal${idx}`);
      if (fill && text) {
        const safeVal = Math.min(Math.max(val, 0), 100);
        fill.style.height = `${safeVal}%`;
        text.textContent = `${Math.round(safeVal)}%`;
        const stateClass = getValueStateClass(safeVal);
        fill.classList.remove('val-green', 'val-yellow', 'val-red');
        fill.classList.add(stateClass);
        text.classList.remove('val-green', 'val-yellow', 'val-red');
        text.classList.add(stateClass);
      }
    });
  }

  // 8. Рендеринг розділів дисків зі спільною швидкістю
  function renderDisks(disks, readSpeed, writeSpeed) {
    const speedRead = document.getElementById('diskSharedRead');
    const speedWrite = document.getElementById('diskSharedWrite');
    if (speedRead) speedRead.textContent = `↓ ${formatSpeed(readSpeed)}`;
    if (speedWrite) speedWrite.textContent = `↑ ${formatSpeed(writeSpeed)}`;

    const list = document.getElementById('disksList');
    if (!list) return;

    if (!disks || disks.length === 0) {
      list.innerHTML = '<div class="empty-state">Розділи не знайдено</div>';
      return;
    }

    let parsedDisks = disks;
    if (typeof disks === 'string') {
      try { parsedDisks = JSON.parse(disks); } catch (e) { parsedDisks = []; }
    }

    list.innerHTML = '';
    parsedDisks.forEach((d) => {
      const mount = d.mount || d.mountpoint || d.device || 'Диск';
      const totalB = Number(d.total_bytes) || 0;
      const usedB = Number(d.used_bytes) || 0;
      const freeB = Number(d.free_bytes) || (totalB - usedB);
      const usedPct = d.percent !== undefined ? Number(d.percent) : (totalB > 0 ? (usedB / totalB * 100) : 0);
      const safePct = Math.min(Math.max(usedPct, 0), 100);
      const stateClass = getValueStateClass(safePct);

      const totalGB = formatBytesGB(totalB);
      const usedGB = formatBytesGB(usedB);
      const freeGB = formatBytesGB(freeB);

      const row = document.createElement('div');
      row.className = 'disk-item';
      row.innerHTML = `
        <div class="disk-item-header">
          <div style="display: flex; align-items: baseline; gap: 10px;">
            <span class="disk-item-name">${mount}</span>
            <span class="disk-item-stats">Використано ${usedGB} / Вільно ${freeGB} / Всього ${totalGB}</span>
          </div>
          <span class="disk-item-pct ${stateClass}">${safePct.toFixed(0)}%</span>
        </div>
        <div class="disk-bar-track">
          <div class="disk-bar-fill ${stateClass}" style="width: ${safePct}%;"></div>
        </div>
      `;
      list.appendChild(row);
    });
  }

  // 9. Опитування процесів (раз на 5 с)
  async function fetchProcesses() {
    try {
      const res = await fetch(`/api/processes?sort=${currentSort}&limit=12`);
      if (!res.ok) return;
      const data = await res.json();
      renderProcessesTable(data.processes || []);
    } catch (e) {
      console.warn('Помилка при завантаженні /api/processes:', e);
    }
  }

  function renderProcessesTable(procs) {
    const tbody = document.getElementById('processesTableBody');
    if (!tbody) return;

    if (!procs || procs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state">Немає активних процесів</div></td></tr>';
      return;
    }

    tbody.innerHTML = '';
    procs.forEach((p) => {
      const tr = document.createElement('tr');
      const cpuVal = Number(p.cpu_percent) || 0;
      const ramVal = Number(p.memory_percent) || 0;
      const ramMb = formatBytesMB(p.memory_rss_bytes);

      tr.innerHTML = `
        <td>
          <div class="proc-name-cell">
            <span>${p.name || 'невідомо'}</span>
          </div>
        </td>
        <td><span class="proc-pid">${p.pid}</span></td>
        <td>
          <div class="proc-bar-cell">
            <span class="${getValueStateClass(cpuVal)}">${cpuVal.toFixed(1)}%</span>
            <div class="proc-cpu-bar-track">
              <div class="proc-cpu-bar-fill" style="width: ${Math.min(cpuVal, 100)}%;"></div>
            </div>
          </div>
        </td>
        <td>${ramVal.toFixed(1)}%</td>
        <td>${ramMb}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // 10. Завантаження історії для графіка
  async function fetchChartHistory(range) {
    try {
      const res = await fetch(`/api/metrics/history?range=${range}`);
      if (!res.ok) return;
      const json = await res.json();
      chart.setData(json.data || [], range);
    } catch (e) {
      console.warn(`Помилка при завантаженні /api/metrics/history?range=${range}:`, e);
    }
  }

  // 11. Сортування процесів
  const sortCpuBtn = document.getElementById('sortCpuBtn');
  const sortRamBtn = document.getElementById('sortRamBtn');

  if (sortCpuBtn && sortRamBtn) {
    sortCpuBtn.addEventListener('click', () => {
      currentSort = 'cpu';
      sortCpuBtn.classList.add('active');
      sortRamBtn.classList.remove('active');
      fetchProcesses();
    });

    sortRamBtn.addEventListener('click', () => {
      currentSort = 'ram';
      sortRamBtn.classList.add('active');
      sortCpuBtn.classList.remove('active');
      fetchProcesses();
    });
  }

  // =========================================================================
  // 12. ТРИВОГИ ТА СПОВІЩЕННЯ (Alerts & Toasts)
  // =========================================================================
  async function fetchAlerts() {
    try {
      const res = await fetch('/api/alerts?limit=50');
      if (!res.ok) return;
      const data = await res.json();
      const alerts = data.alerts || [];

      // Оновлення лічильника в навігації та бейджика в шапці
      const activeAlerts = alerts.filter((a) => a.is_active);
      const activeCount = activeAlerts.length;

      const navAlertsCount = document.getElementById('navAlertsCount');
      if (navAlertsCount) {
        navAlertsCount.textContent = activeCount;
        navAlertsCount.style.display = activeCount > 0 ? 'inline-block' : 'none';
      }

      if (activeAlertsBadge) {
        if (activeCount > 0) {
          activeAlertsBadge.style.display = 'inline-flex';
          const textEl = document.getElementById('activeAlertsBadgeText');
          if (textEl) textEl.textContent = formatAlertsPlural(activeCount);
        } else {
          activeAlertsBadge.style.display = 'none';
        }
      }

      const activePill = document.getElementById('activeAlertsPill');
      if (activePill) {
        activePill.textContent = formatAlertsPlural(activeCount);
      }

      // Спливаючі сповіщення (Toasts) для нових активних тривог
      activeAlerts.forEach((a) => {
        if (!seenActiveAlertIds.has(a.id)) {
          seenActiveAlertIds.add(a.id);
          if (!isFirstAlertsPoll) {
            showToastAlert(a);
          }
        }
      });
      isFirstAlertsPoll = false;

      // Рендеринг списків тривог на вкладці "Сповіщення"
      if (currentView === 'alerts') {
        renderAlertsLists(alerts);
      }
    } catch (e) {
      console.warn('Помилка завантаження /api/alerts:', e);
    }
  }

  function showToastAlert(alert) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast-card';
    toast.innerHTML = `
      <div class="toast-header">
        <div class="toast-title">
          <span>⚠️</span>
          <span>${alert.rule_condition}</span>
        </div>
        <span class="toast-time">зараз</span>
      </div>
      <div class="toast-body">
        Зафіксовано значення: <strong>${alert.trigger_value}</strong> (поріг: ${alert.threshold_value})
      </div>
      ${alert.culprits_summary ? `<div class="toast-culprits">Винуватці: ${alert.culprits_summary}</div>` : ''}
    `;

    container.appendChild(toast);

    // Автоматичне зникнення через 6 секунд
    setTimeout(() => {
      toast.style.transition = 'all 0.3s ease';
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(50px)';
      setTimeout(() => toast.remove(), 300);
    }, 6000);
  }

  function showToastNotification(title, message, isSuccess = true) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast-card ${isSuccess ? 'toast-success' : ''}`;
    toast.innerHTML = `
      <div class="toast-header">
        <div class="toast-title">
          <span>${isSuccess ? '✓' : '⚠️'}</span>
          <span>${title}</span>
        </div>
        <span class="toast-time">зараз</span>
      </div>
      <div class="toast-body" style="word-break: break-all;">
        ${message}
      </div>
    `;

    container.appendChild(toast);

    setTimeout(() => {
      toast.style.transition = 'all 0.3s ease';
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(50px)';
      setTimeout(() => toast.remove(), 300);
    }, 6000);
  }

  function renderAlertsLists(alerts) {
    const activeList = document.getElementById('activeAlertsList');
    const resolvedList = document.getElementById('resolvedAlertsList');
    if (!activeList || !resolvedList) return;

    const active = alerts.filter((a) => a.is_active);
    const resolved = alerts.filter((a) => !a.is_active);

    if (active.length === 0) {
      activeList.innerHTML = '<div class="empty-state-success">Всі показники системи в нормі. Активних тривог немає.</div>';
    } else {
      activeList.innerHTML = '';
      active.forEach((a) => activeList.appendChild(createAlertCard(a, true)));
    }

    if (resolved.length === 0) {
      resolvedList.innerHTML = '<div class="empty-state">Історія тривог порожня</div>';
    } else {
      resolvedList.innerHTML = '';
      resolved.forEach((a) => resolvedList.appendChild(createAlertCard(a, false)));
    }
  }

  function createAlertCard(alert, isActive) {
    const card = document.createElement('div');
    card.className = `alert-item ${isActive ? 'active' : ''}`;

    let culpritsHtml = '';
    if (alert.culprits && alert.culprits.length > 0) {
      culpritsHtml = alert.culprits.map((c) => `
        <span class="culprit-tag">${c.name} (${c.cpu_percent}% CPU, ${c.memory_percent}% RAM)</span>
      `).join('');
    } else if (alert.culprits_summary) {
      culpritsHtml = `<span class="culprit-tag">${alert.culprits_summary}</span>`;
    }

    const timeStr = new Date(alert.timestamp * 1000).toLocaleTimeString();
    const resolvedStr = alert.resolved_at ? `Вирішено о ${new Date(alert.resolved_at * 1000).toLocaleTimeString()}` : 'АКТИВНА';

    card.innerHTML = `
      <div class="alert-top-row">
        <div class="alert-rule-title">
          ${isActive ? '<span class="alert-dot"></span>' : ''}
          <span>${alert.rule_condition}</span>
        </div>
        <div class="alert-time">${timeStr} • ${resolvedStr}</div>
      </div>
      <div style="font-size: 13px; color: var(--text-secondary);">
        Метрика: <strong>${alert.metric_name}</strong> • Значення: <strong>${alert.trigger_value}</strong> (поріг: ${alert.threshold_value})
      </div>
      ${culpritsHtml ? `
        <div class="alert-culprits-row">
          <span>Винуватці:</span>
          ${culpritsHtml}
        </div>
      ` : ''}
    `;
    return card;
  }

  // =========================================================================
  // 13. АНАЛІТИКА ТА ІНДЕКС ЗДОРОВ'Я СИСТЕМИ (Analytics)
  // =========================================================================
  const periodButtons = document.querySelectorAll('.period-btn');
  periodButtons.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      periodButtons.forEach((b) => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      currentAnalyticsPeriod = e.currentTarget.getAttribute('data-period') || '24h';
      fetchAnalytics(currentAnalyticsPeriod);
    });
  });

  async function fetchAnalytics(period) {
    try {
      const res = await fetch(`/api/analytics?period=${period}`);
      if (!res.ok) return;
      const data = await res.json();

      // 13.1 Індекс стану системи
      const scoreVal = document.getElementById('healthScoreValue');
      const scoreBadge = document.getElementById('healthStatusBadge');
      const scoreDesc = document.getElementById('healthScoreDesc');
      const factorsList = document.getElementById('healthFactorsList');

      if (scoreVal) scoreVal.textContent = data.health_index;
      if (scoreBadge) {
        scoreBadge.textContent = data.health_status;
        scoreBadge.className = 'health-badge';
        if (data.health_index >= 85) scoreBadge.classList.add('badge-green');
        else if (data.health_index >= 70) scoreBadge.classList.add('badge-blue');
        else if (data.health_index >= 50) scoreBadge.classList.add('badge-yellow');
        else scoreBadge.classList.add('badge-red');
      }

      if (factorsList && data.health_breakdown) {
        const bd = data.health_breakdown;
        factorsList.innerHTML = `
          <div class="factor-row"><span>Базовий бал:</span><span>100</span></div>
          ${bd.penalty_cpu_load > 0 ? `<div class="factor-row"><span>Навантаження CPU:</span><span class="factor-penalty">-${bd.penalty_cpu_load}</span></div>` : ''}
          ${bd.penalty_ram_load > 0 ? `<div class="factor-row"><span>Навантаження RAM:</span><span class="factor-penalty">-${bd.penalty_ram_load}</span></div>` : ''}
          ${bd.penalty_cpu_redzone > 0 ? `<div class="factor-row"><span>Червона зона CPU:</span><span class="factor-penalty">-${bd.penalty_cpu_redzone}</span></div>` : ''}
          ${bd.penalty_ram_redzone > 0 ? `<div class="factor-row"><span>Червона зона RAM:</span><span class="factor-penalty">-${bd.penalty_ram_redzone}</span></div>` : ''}
          ${bd.penalty_alerts > 0 ? `<div class="factor-row"><span>Штраф за тривоги:</span><span class="factor-penalty">-${bd.penalty_alerts}</span></div>` : ''}
          ${bd.penalty_disk_space > 0 ? `<div class="factor-row"><span>Нестача місця на диску:</span><span class="factor-penalty">-${bd.penalty_disk_space}</span></div>` : ''}
          ${bd.penalty_gpu_temp > 0 ? `<div class="factor-row"><span>Температура GPU:</span><span class="factor-penalty">-${bd.penalty_gpu_temp}</span></div>` : ''}
        `;
      }

      // 13.2 Середні та пікові значення
      const s = data.summary || {};
      const elCpuAvg = document.getElementById('anaCpuAvg');
      const elCpuPeak = document.getElementById('anaCpuPeak');
      const elRamAvg = document.getElementById('anaRamAvg');
      const elRamPeak = document.getElementById('anaRamPeak');
      const elGpuAvg = document.getElementById('anaGpuAvg');
      const elGpuPeak = document.getElementById('anaGpuPeak');
      const elGpuTempAvg = document.getElementById('anaGpuTempAvg');
      const elGpuTempPeak = document.getElementById('anaGpuTempPeak');

      if (elCpuAvg) elCpuAvg.textContent = `${s.cpu_avg || 0}%`;
      if (elCpuPeak) elCpuPeak.textContent = `Пік: ${s.cpu_peak || 0}%`;
      if (elRamAvg) elRamAvg.textContent = `${s.ram_avg || 0}%`;
      if (elRamPeak) elRamPeak.textContent = `Пік: ${s.ram_peak || 0}%`;

      if (elGpuAvg) elGpuAvg.textContent = s.gpu_avg !== null ? `${s.gpu_avg}%` : '--%';
      if (elGpuPeak) elGpuPeak.textContent = s.gpu_peak !== null ? `Пік: ${s.gpu_peak}%` : '--%';
      if (elGpuTempAvg) elGpuTempAvg.textContent = s.gpu_temp_avg !== null ? `${s.gpu_temp_avg}°C` : '--°C';
      if (elGpuTempPeak) elGpuTempPeak.textContent = s.gpu_temp_peak !== null ? `Пік: ${s.gpu_temp_peak}°C` : '--°C';

      // 13.3 Червона зона
      const elCpuRz = document.getElementById('anaCpuRz');
      const elRamRz = document.getElementById('anaRamRz');
      if (elCpuRz) elCpuRz.textContent = `${s.cpu_redzone_sec || 0} с (${s.cpu_redzone_percent || 0}%)`;
      if (elRamRz) elRamRz.textContent = `${s.ram_redzone_sec || 0} с (${s.ram_redzone_percent || 0}%)`;

      // 13.4 Прогноз заповнення дисків
      renderDiskProjections(data.disk_projections || []);

      // 13.5 Топ програм за навантаженням
      renderAnalyticsTopApps(data.top_apps || []);

      // 13.6 Рейтинг винуватців
      renderAnalyticsCulprits(data.culprits_ranking || []);

    } catch (e) {
      console.warn('Помилка при завантаженні /api/analytics:', e);
    }
  }

  function renderDiskProjections(projections) {
    const container = document.getElementById('diskProjectionsContainer');
    if (!container) return;

    if (!projections || projections.length === 0) {
      container.innerHTML = '<div class="empty-state">Дані використання дисків відсутні</div>';
      return;
    }

    container.innerHTML = '';
    projections.forEach((p) => {
      const freePct = p.free_percent !== undefined ? Number(p.free_percent) : 0;
      const usedPct = p.used_percent !== undefined ? Number(p.used_percent) : (100 - freePct);
      const safePct = Math.min(Math.max(usedPct, 0), 100);
      const stateClass = getValueStateClass(safePct);
      const isWarn = p.status === 'critical' || p.status === 'warning' || (p.days_to_full !== null && p.days_to_full <= 7.0);

      const fstypeStr = p.fstype ? ` (${p.fstype})` : '';
      const totalGB = p.total_gb !== undefined ? `${p.total_gb} ГБ` : '--';
      const freeGB = p.free_gb !== undefined ? `${p.free_gb} ГБ` : '--';
      const usedGB = p.used_gb !== undefined ? `${p.used_gb} ГБ` : (p.total_gb && p.free_gb ? `${(p.total_gb - p.free_gb).toFixed(1)} ГБ` : '--');

      const card = document.createElement('div');
      card.className = 'dp-card';
      card.innerHTML = `
        <div class="dp-header">
          <div style="display: flex; align-items: baseline; gap: 10px;">
            <span class="dp-mount">${p.mountpoint}${fstypeStr}</span>
            <span class="disk-item-stats">Використано ${usedGB} / Вільно ${freeGB} / Всього ${totalGB}</span>
          </div>
          <span class="disk-item-pct ${stateClass}">${safePct.toFixed(0)}%</span>
        </div>
        <div class="disk-bar-track">
          <div class="disk-bar-fill ${stateClass}" style="width: ${safePct}%;"></div>
        </div>
        <div class="dp-projection-text ${isWarn ? 'warning' : ''}">
          ${p.projection_text}
        </div>
      `;
      container.appendChild(card);
    });
  }

  function renderAnalyticsTopApps(apps) {
    const tbody = document.getElementById('analyticsTopAppsBody');
    if (!tbody) return;

    if (!apps || apps.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state">Дані по програмах ще накопичуються</div></td></tr>';
      return;
    }

    tbody.innerHTML = '';
    apps.forEach((a) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${a.process_name}</strong></td>
        <td>${a.total_appearances}</td>
        <td><span class="${getValueStateClass(a.avg_cpu)}">${a.avg_cpu}%</span></td>
        <td>${a.max_cpu}%</td>
        <td>${a.max_memory}%</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function renderAnalyticsCulprits(culprits) {
    const tbody = document.getElementById('analyticsCulpritsBody');
    if (!tbody) return;

    if (!culprits || culprits.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state">Немає зафіксованих тривог за вказаний період</div></td></tr>';
      return;
    }

    tbody.innerHTML = '';
    culprits.forEach((c) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong style="color: var(--status-red);">${c.name}</strong></td>
        <td><strong>${c.alert_triggers_count}</strong></td>
        <td>${c.avg_cpu}%</td>
        <td>${c.max_cpu}%</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // =========================================================================
  // 14. ХАРАКТЕРИСТИКИ КОМП'ЮТЕРА (System Specs)
  // =========================================================================
  const btnRefreshSpecs = document.getElementById('btnRefreshSpecs');
  if (btnRefreshSpecs) {
    btnRefreshSpecs.addEventListener('click', () => fetchSpecs(true));
  }

  async function fetchSpecs(force = false) {
    try {
      const res = await fetch(`/api/system/specs${force ? '?refresh=true' : ''}`);
      if (!res.ok) return;
      const data = await res.json();

      // CPU
      const cpuRows = document.getElementById('specCpuRows');
      if (cpuRows && data.cpu) {
        cpuRows.innerHTML = `
          <div class="spec-row"><span>Модель:</span><strong>${data.cpu.model}</strong></div>
          <div class="spec-row"><span>Фізичні ядра:</span><strong>${data.cpu.physical_cores}</strong></div>
          <div class="spec-row"><span>Логічні ядра:</span><strong>${data.cpu.logical_cores}</strong></div>
          <div class="spec-row"><span>Базова частота:</span><strong>${data.cpu.base_freq_mhz || '--'} МГц</strong></div>
          <div class="spec-row"><span>Поточна частота:</span><strong>${data.cpu.current_freq_mhz || '--'} МГц</strong></div>
        `;
      }

      // RAM
      const ramRows = document.getElementById('specRamRows');
      if (ramRows && data.ram) {
        ramRows.innerHTML = `
          <div class="spec-row"><span>Загальний обсяг:</span><strong>${data.ram.total_gb} ГБ</strong></div>
          <div class="spec-row"><span>Байтів:</span><strong>${data.ram.total_bytes.toLocaleString()}</strong></div>
        `;
      }

      // GPU
      const gpuRows = document.getElementById('specGpuRows');
      if (gpuRows && data.gpus) {
        if (data.gpus.length === 0) {
          gpuRows.innerHTML = '<div class="empty-state">Відеокарту не виявлено</div>';
        } else {
          gpuRows.innerHTML = data.gpus.map((g, idx) => `
            <div class="spec-row">
              <span>Відеоадаптер #${idx + 1}:</span>
              <strong>${g.name}</strong>
            </div>
            ${g.memory_total_gb ? `<div class="spec-row"><span>Відеопам'ять (VRAM):</span><strong>${g.memory_total_gb} ГБ</strong></div>` : ''}
            ${g.driver_version ? `<div class="spec-row"><span>Драйвер:</span><strong>${g.driver_version}</strong></div>` : ''}
          `).join('');
        }
      }

      // OS & Boot
      const osRows = document.getElementById('specOsRows');
      if (osRows && data.os && data.system) {
        osRows.innerHTML = `
          <div class="spec-row"><span>ОС:</span><strong>${data.os.version_string}</strong></div>
          <div class="spec-row"><span>Архітектура:</span><strong>${data.os.architecture}</strong></div>
          <div class="spec-row"><span>Час запуску:</span><strong>${data.system.boot_time_local}</strong></div>
        `;
      }

      // Disks table
      const disksBody = document.getElementById('specsDisksBody');
      if (disksBody && data.disks) {
        disksBody.innerHTML = data.disks.map((d) => `
          <tr>
            <td><strong>${d.mountpoint}</strong></td>
            <td>${d.device}</td>
            <td>${d.fstype || 'NTFS'}</td>
            <td>${d.total_gb} ГБ</td>
            <td>${d.free_gb} ГБ</td>
            <td><span class="${getValueStateClass(d.used_percent)}">${d.used_percent}%</span></td>
          </tr>
        `).join('');
      }
    } catch (e) {
      console.warn('Помилка при завантаженні /api/system/specs:', e);
    }
  }

  // =========================================================================
  // 15. ЕКСПОРТ В EXCEL (.xlsx)
  // =========================================================================
  const exportModal = document.getElementById('exportModal');
  const btnOpenExport = document.getElementById('btnOpenExportModal');
  const btnCloseExport = document.getElementById('btnCloseExportModal');
  const btnCancelExport = document.getElementById('btnCancelExport');
  const btnStartExport = document.getElementById('btnStartExport');
  const exportProgressWrap = document.getElementById('exportProgressWrap');

  function openExportModal() {
    if (exportModal) {
      exportModal.style.display = 'flex';
      if (exportProgressWrap) exportProgressWrap.style.display = 'none';
      if (btnStartExport) btnStartExport.disabled = false;
    }
  }

  function closeExportModal() {
    if (exportModal) exportModal.style.display = 'none';
  }

  if (btnOpenExport) btnOpenExport.addEventListener('click', openExportModal);
  if (btnCloseExport) btnCloseExport.addEventListener('click', closeExportModal);
  if (btnCancelExport) btnCancelExport.addEventListener('click', closeExportModal);

  if (btnStartExport) {
    btnStartExport.addEventListener('click', async () => {
      const periodRadio = document.querySelector('input[name="exportPeriod"]:checked');
      const period = periodRadio ? periodRadio.value : '24h';

      if (exportProgressWrap) exportProgressWrap.style.display = 'block';
      btnStartExport.disabled = true;

      try {
        // Перевіряємо наявність нативного API pywebview
        const hasPywebview = window.pywebview && window.pywebview.api && typeof window.pywebview.api.save_excel === 'function';

        if (hasPywebview) {
          // 1. Десктопний режим pywebview: отримуємо base64 і викликаємо системний діалог Windows
          const res = await fetch(`/api/export/excel?period=${period}&base64=true`);
          if (!res.ok) throw new Error(`HTTP помилка ${res.status}`);
          const json = await res.json();

          const saveRes = await window.pywebview.api.save_excel(json.data, json.filename);
          if (saveRes && saveRes.success) {
            if (exportProgressWrap) exportProgressWrap.style.display = 'none';
            btnStartExport.disabled = false;
            closeExportModal();
            const filePath = saveRes.path || '';
            showToastNotification('Звіт Excel збережено', filePath ? `Файл: ${filePath}` : 'Файл успішно збережено', true);
            alert(`Звіт Excel успішно збережено:\n${filePath}`);
          } else if (saveRes && saveRes.cancelled) {
            // Користувач скасував діалог без збереження — закриваємо модалку без помилки
            if (exportProgressWrap) exportProgressWrap.style.display = 'none';
            btnStartExport.disabled = false;
            closeExportModal();
          } else {
            if (exportProgressWrap) exportProgressWrap.style.display = 'none';
            btnStartExport.disabled = false;
            alert(`Помилка збереження файлу: ${saveRes ? saveRes.error : 'Невідома помилка'}`);
          }
        } else {
          // 2. Режим звичайного браузера: пряме завантаження файлу через <a>
          const a = document.createElement('a');
          a.href = `/api/export/excel?period=${period}`;
          a.download = '';
          document.body.appendChild(a);
          a.click();
          a.remove();
          closeExportModal();
          showToastNotification('Звіт сформовано', 'Файл Excel завантажується через браузер', true);
        }
      } catch (err) {
        console.error('Помилка експорту Excel:', err);
        alert(`Не вдалося сформувати звіт Excel: ${err.message}`);
        if (exportProgressWrap) exportProgressWrap.style.display = 'none';
        btnStartExport.disabled = false;
      }
    });
  }

  // =========================================================================
  // 16. ГОЛОВНІ ТАЙМЕРИ ОПИТУВАННЯ
  // =========================================================================
  // Первинний виклик усіх служб
  fetchStatus();
  fetchCurrentMetrics();
  fetchProcesses();
  fetchChartHistory('live');
  fetchAlerts();
  fetchSpecs();

  // Посекундне опитування поточних показників
  setInterval(fetchCurrentMetrics, 1000);

  // Опитування статусу та тривог кожні 2 секунди
  setInterval(() => {
    fetchStatus();
    fetchAlerts();
  }, 2000);

  // Опитування списку процесів кожні 5 секунд
  setInterval(fetchProcesses, 5000);

  // Оновлення графіка "Наживо" кожну 1 секунду, або "Година"/"День" кожні 60 секунд
  setInterval(() => {
    const tab = tabsController.getCurrentTab();
    if (tab === 'live') {
      fetchChartHistory('live');
    }
  }, 1000);

  setInterval(() => {
    const tab = tabsController.getCurrentTab();
    if (tab !== 'live') {
      fetchChartHistory(tab);
    }
  }, 60000);
});
