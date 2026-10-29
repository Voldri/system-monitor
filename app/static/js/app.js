/**
 * Головний контролер веб-дашборду System Monitor.
 * - Опитування поточних метрик: раз на 1 с
 * - Опитування процесів: раз на 5 с
 * - Опитування графіка: 1 с для "Наживо", 60 с для "Година / День"
 * - Локальний час та україномовні одиниці вимірювання
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Ініціалізація компонентів
  const chart = new TelemetryChart('mainTelemetryChart', 'chartContainer', 'chartTooltip');
  let currentSort = 'cpu';

  // Буфери спарклайнів (до 60 останніх точок)
  const sparkBuffers = {
    cpu: [],
    ram: [],
    disk: [],
    net: [],
  };

  const tabsController = new TabsController((newRange) => {
    // При перемиканні вкладок негайно завантажуємо історію за обраний період
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

  // 3. Оновлення статусу сервера
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

  // 4. Оновлення поточних метрик (раз на 1 с)
  async function fetchCurrentMetrics() {
    try {
      const res = await fetch('/api/metrics/current');
      if (!res.ok) return;
      const data = await res.json();

      // Оновлення часу
      const timeElem = document.getElementById('localTimeDisplay');
      if (timeElem && data.local_time) {
        timeElem.textContent = data.local_time;
      }

      // Плитка CPU
      const cpuVal = Number(data.cpu_total || 0);
      const cpuValElem = document.getElementById('cpuTotalVal');
      if (cpuValElem) {
        cpuValElem.textContent = `${cpuVal.toFixed(1)}%`;
        applyValueState(cpuValElem, cpuVal);
      }
      pushSparkline('cpu', cpuVal, 100, '#4c8dff');

      // Плитка RAM
      const ramVal = Number(data.ram_percent || 0);
      const ramValElem = document.getElementById('ramPercentVal');
      const ramCaption = document.getElementById('ramUsedCaption');
      const ramTotalInfo = document.getElementById('ramTotalInfo');

      if (ramValElem) {
        ramValElem.textContent = `${ramVal.toFixed(1)}%`;
        applyValueState(ramValElem, ramVal);
      }
      if (ramCaption) {
        ramCaption.textContent = `${formatBytesGB(data.ram_used_bytes)} / ${formatBytesGB(data.ram_total_bytes)}`;
      }
      if (ramTotalInfo) {
        ramTotalInfo.textContent = formatBytesGB(data.ram_total_bytes);
      }
      pushSparkline('ram', ramVal, 100, '#a78bfa');

      // Плитка Диск C:
      let cDrive = null;
      if (Array.isArray(data.disks_info)) {
        cDrive = data.disks_info.find((d) => d.mount && d.mount.toUpperCase().startsWith('C:')) || data.disks_info[0];
      }
      if (cDrive) {
        const diskPct = Number(cDrive.percent || 0);
        const diskValElem = document.getElementById('diskPercentVal');
        const diskCaption = document.getElementById('diskFreeCaption');
        const diskTotalInfo = document.getElementById('diskTotalInfo');

        if (diskValElem) {
          diskValElem.textContent = `${diskPct.toFixed(1)}%`;
          applyValueState(diskValElem, diskPct);
        }
        if (diskCaption) {
          diskCaption.textContent = `Вільно: ${formatBytesGB(cDrive.free_bytes)}`;
        }
        if (diskTotalInfo) {
          diskTotalInfo.textContent = formatBytesGB(cDrive.total_bytes);
        }
        pushSparkline('disk', diskPct, 100, '#3fb950');
      }

      // Плитка Мережа
      const netRecv = Number(data.net_recv_bytes_sec || 0);
      const netSent = Number(data.net_sent_bytes_sec || 0);
      const netRecvElem = document.getElementById('netRecvVal');
      const netSentElem = document.getElementById('netSentVal');

      if (netRecvElem) netRecvElem.textContent = `↓ ${formatSpeed(netRecv)}`;
      if (netSentElem) netSentElem.textContent = `↑ ${formatSpeed(netSent)}`;

      const totalNetSpeed = (netRecv + netSent) / 1024; // в КБ/с
      pushSparkline('net', totalNetSpeed, Math.max(500, totalNetSpeed * 1.5), '#4c8dff');

      // Оновлення смужок ядер процесора
      updateCoresBars(data.cpu_cores);

    } catch (e) {
      console.warn('Помилка оновлення поточних метрик:', e);
    }
  }

  function pushSparkline(key, value, maxVal, color) {
    const buf = sparkBuffers[key];
    buf.push(value);
    if (buf.length > 60) buf.shift();

    const canvasId = `sparkline${key.charAt(0).toUpperCase() + key.slice(1)}`;
    renderSparkline(canvasId, buf, color, maxVal);
  }

  // 5. Оновлення вертикальних смужок ядер процесора
  function updateCoresBars(coresArray) {
    const container = document.getElementById('coresGrid');
    if (!container || !Array.isArray(coresArray)) return;

    let existingWrappers = container.querySelectorAll('.core-bar-wrapper');

    // Якщо кількість ядер змінилась або блок ще не створено
    if (existingWrappers.length !== coresArray.length) {
      container.innerHTML = '';
      coresArray.forEach((_, idx) => {
        const wrap = document.createElement('div');
        wrap.className = 'core-bar-wrapper';
        wrap.innerHTML = `
          <div class="core-pct-label" id="corePct_${idx}">0%</div>
          <div class="core-bar-track">
            <div class="core-bar-fill" id="coreFill_${idx}" style="height: 0%;"></div>
          </div>
          <div class="core-label">C${idx + 1}</div>
        `;
        container.appendChild(wrap);
      });
      existingWrappers = container.querySelectorAll('.core-bar-wrapper');
    }

    coresArray.forEach((pct, idx) => {
      const pctVal = Math.max(0, Math.min(100, Number(pct) || 0));
      const fill = document.getElementById(`coreFill_${idx}`);
      const label = document.getElementById(`corePct_${idx}`);

      if (fill) {
        fill.style.height = `${pctVal}%`;
        if (pctVal > 90) {
          fill.style.backgroundColor = 'var(--status-red)';
        } else if (pctVal >= 70) {
          fill.style.backgroundColor = 'var(--status-yellow)';
        } else {
          fill.style.backgroundColor = 'var(--accent-blue)';
        }
      }
      if (label) {
        label.textContent = `${Math.round(pctVal)}%`;
      }
    });
  }

  // 6. Оновлення даних графіка
  async function fetchChartHistory(range) {
    try {
      const res = await fetch(`/api/metrics/history?range=${range}`);
      if (!res.ok) return;
      const json = await res.json();
      chart.setData(json.data || [], range);
    } catch (e) {
      console.warn(`Помилка отримання історії для ${range}:`, e);
    }
  }

  // 7. Оновлення списку процесів (раз на 5 с)
  async function fetchProcesses() {
    try {
      const res = await fetch(`/api/processes?sort=${currentSort}&limit=12`);
      if (!res.ok) return;
      const data = await res.json();
      renderProcessesTable(data.processes || []);
    } catch (e) {
      console.warn('Помилка оновлення процесів:', e);
    }
  }

  function renderProcessesTable(processes) {
    const tbody = document.getElementById('processesTableBody');
    if (!tbody) return;

    if (processes.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="5">
            <div class="empty-state">Немає активних процесів для відображення</div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = processes.map((p) => {
      const cpu = Number(p.cpu_percent || 0).toFixed(1);
      const ram = Number(p.memory_percent || 0).toFixed(1);
      const memMb = formatBytesMB(p.memory_rss_bytes);
      const cpuBarWidth = Math.min(100, Math.max(0, cpu));

      return `
        <tr>
          <td>
            <div class="proc-name-cell">
              <span>${escapeHtml(p.name)}</span>
            </div>
          </td>
          <td><span class="proc-pid">${p.pid}</span></td>
          <td class="proc-bar-cell">
            <div style="display: flex; justify-content: space-between; font-size: 11px;">
              <span>${cpu}%</span>
            </div>
            <div class="proc-cpu-bar-track">
              <div class="proc-cpu-bar-fill" style="width: ${cpuBarWidth}%;"></div>
            </div>
          </td>
          <td>${ram}%</td>
          <td>${memMb}</td>
        </tr>
      `;
    }).join('');
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Керування сортуванням процесів
  const sortCpuBtn = document.getElementById('sortCpuBtn');
  const sortRamBtn = document.getElementById('sortRamBtn');

  if (sortCpuBtn && sortRamBtn) {
    sortCpuBtn.addEventListener('click', () => {
      if (currentSort !== 'cpu') {
        currentSort = 'cpu';
        sortCpuBtn.classList.add('active');
        sortRamBtn.classList.remove('active');
        fetchProcesses();
      }
    });

    sortRamBtn.addEventListener('click', () => {
      if (currentSort !== 'ram') {
        currentSort = 'ram';
        sortRamBtn.classList.add('active');
        sortCpuBtn.classList.remove('active');
        fetchProcesses();
      }
    });
  }

  // 8. Таймери та періодичні інтервали
  // Первинне завантаження
  fetchStatus();
  fetchCurrentMetrics();
  fetchChartHistory('live');
  fetchProcesses();

  // Опитування раз на 1 секунду: плитки поточних показників + графік "Наживо"
  setInterval(() => {
    fetchCurrentMetrics();
    if (tabsController.getCurrentTab() === 'live') {
      fetchChartHistory('live');
    }
  }, 1000);

  // Опитування раз на 5 секунд: топ процесів
  setInterval(() => {
    fetchProcesses();
  }, 5000);

  // Опитування раз на 60 секунд: історія для вкладок "Година" або "День" + статус
  setInterval(() => {
    fetchStatus();
    const currentTab = tabsController.getCurrentTab();
    if (currentTab === 'hour' || currentTab === 'day') {
      fetchChartHistory(currentTab);
    }
  }, 60000);
});
