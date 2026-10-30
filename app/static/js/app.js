/**
 * Головний контролер веб-дашборду System Monitor.
 * - Опитування поточних метрик: раз на 1 с (плитки, смужки ядер, диски)
 * - Графік "Наживо": раз на 1 с (зчитується з in-memory буфера)
 * - Опитування процесів: раз на 5 с
 * - Графіки "Година" / "День": при кліку та раз на 60 с
 * - Підтримка GPU (NVIDIA RTX), розділів NVMe SSD зі спільною швидкістю I/O.
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Ініціалізація компонентів
  const chart = new TelemetryChart('mainTelemetryChart', 'chartContainer', 'chartTooltip');
  let currentSort = 'cpu';

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

      // Плитка GPU (Відеокарта)
      const gpuValElem = document.getElementById('gpuPercentVal');
      const gpuCaption = document.getElementById('gpuCaption');
      const gpuNameInfo = document.getElementById('gpuNameInfo');

      if (data.gpu_percent !== null && data.gpu_percent !== undefined) {
        const gpuPct = Number(data.gpu_percent);
        if (gpuValElem) {
          gpuValElem.textContent = `${gpuPct.toFixed(1)}%`;
          applyValueState(gpuValElem, gpuPct);
        }
        if (gpuNameInfo && data.gpu_name) {
          gpuNameInfo.textContent = data.gpu_name;
        }
        const vramUsed = formatBytesGB(data.gpu_mem_used_bytes);
        const vramTotal = formatBytesGB(data.gpu_mem_total_bytes);
        const tempStr = data.gpu_temp_c !== null ? `${Math.round(data.gpu_temp_c)}°C` : '--°C';
        if (gpuCaption) {
          gpuCaption.textContent = `${vramUsed} / ${vramTotal} • ${tempStr}`;
        }
        pushSparkline('gpu', gpuPct, 100, '#f0883e');
      } else {
        if (gpuValElem) {
          gpuValElem.textContent = 'Недоступно';
          gpuValElem.className = 'tile-value';
        }
        if (gpuCaption) {
          gpuCaption.textContent = 'GPU не виявлено або вимкнено';
        }
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

      // Оновлення смужок ядер процесора (сітка 2x8)
      updateCoresBars(data.cpu_cores);

      // Оновлення картки дисків
      updateDisksCard(data);

    } catch (e) {
      console.warn('Помилка оновлення поточних метрик:', e);
    }
  }

  function pushSparkline(key, value, maxVal, color) {
    const buf = sparkBuffers[key];
    if (!buf) return;
    buf.push(value);
    if (buf.length > 60) buf.shift();

    const canvasId = `sparkline${key.charAt(0).toUpperCase() + key.slice(1)}`;
    renderSparkline(canvasId, buf, color, maxVal);
  }

  // 5. Оновлення смужок ядер процесора (рівна сітка 2 ряди по 8 колонок)
  function updateCoresBars(coresArray) {
    const container = document.getElementById('coresGrid');
    if (!container || !Array.isArray(coresArray)) return;

    const numCores = coresArray.length;
    const cols = Math.ceil(numCores / 2) || 8;
    container.style.setProperty('--core-cols', cols);

    let existingWrappers = container.querySelectorAll('.core-bar-wrapper');

    if (existingWrappers.length !== numCores) {
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

  // 6. Оновлення картки дисків (спільна швидкість NVMe та всі локальні розділи)
  function updateDisksCard(data) {
    // Спільна швидкість
    const readElem = document.getElementById('diskSharedRead');
    const writeElem = document.getElementById('diskSharedWrite');
    if (readElem) readElem.textContent = `↓ ${formatSpeed(data.disk_read_bytes_sec || 0)}`;
    if (writeElem) writeElem.textContent = `↑ ${formatSpeed(data.disk_write_bytes_sec || 0)}`;

    const listContainer = document.getElementById('disksList');
    if (!listContainer || !Array.isArray(data.disks_info)) return;

    if (data.disks_info.length === 0) {
      listContainer.innerHTML = '<div class="empty-state">Розділів не знайдено</div>';
      return;
    }

    listContainer.innerHTML = data.disks_info.map((d) => {
      const pct = Math.max(0, Math.min(100, Number(d.percent || 0)));
      const mount = (d.mount || 'Диск').replace(/\\+$/, '');
      const usedGb = formatBytesGB(d.used_bytes);
      const freeGb = formatBytesGB(d.free_bytes);
      const totalGb = formatBytesGB(d.total_bytes);

      let colorVar = 'var(--status-green)';
      let colorClass = 'val-green';
      if (pct > 90) {
        colorVar = 'var(--status-red)';
        colorClass = 'val-red';
      } else if (pct >= 70) {
        colorVar = 'var(--status-yellow)';
        colorClass = 'val-yellow';
      }

      return `
        <div class="disk-item">
          <div class="disk-item-header">
            <span class="disk-item-name">
              <span>Локальний диск (${escapeHtml(mount)})</span>
            </span>
            <span class="disk-item-stats">
              Використано: ${usedGb} • Вільно: ${freeGb} • Всього: ${totalGb}
            </span>
            <span class="disk-item-pct ${colorClass}">${pct.toFixed(1)}%</span>
          </div>
          <div class="disk-bar-track">
            <div class="disk-bar-fill" style="width: ${pct}%; background-color: ${colorVar};"></div>
          </div>
        </div>
      `;
    }).join('');
  }

  // 7. Оновлення даних графіка
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

  // 8. Оновлення списку процесів (раз на 5 с)
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

  // 9. Таймери та періодичні інтервали
  // Первинне завантаження
  fetchStatus();
  fetchCurrentMetrics();
  fetchChartHistory('live');
  fetchProcesses();

  // Опитування раз на 1 секунду: плитки + графік "Наживо"
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

  // Опитування раз на 60 секунд: статус + історія "Година/День"
  setInterval(() => {
    fetchStatus();
    const currentTab = tabsController.getCurrentTab();
    if (currentTab === 'hour' || currentTab === 'day') {
      fetchChartHistory(currentTab);
    }
  }, 60000);
});
