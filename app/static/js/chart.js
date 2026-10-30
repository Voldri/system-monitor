/**
 * Високопродуктивний легкий Canvas-графік для System Monitor.
 * - Перемальовується ВИКЛЮЧНО при надходженні нових даних або зміні розміру вікна.
 * - Вісь X пропорційна часу (позиція точки = timestamp).
 * - Рівномірні підписи часу по всій осі X.
 * - Три лінії навантаження: CPU (синя #4c8dff), RAM (фіолетова #a78bfa), GPU (помаранчева #f0883e).
 * - Напівпрозора пунктирна лінія піків CPU (cpu_max) для періодів "Година" та "День".
 * - Розриває лінію, якщо розрив між точками перевищує 2 хвилини (120 с).
 * - Інтерактивний tooltip при наведенні курсору.
 */

class TelemetryChart {
  constructor(canvasId, containerId, tooltipId) {
    this.canvas = document.getElementById(canvasId);
    this.container = document.getElementById(containerId);
    this.tooltip = document.getElementById(tooltipId);
    this.ctx = this.canvas.getContext('2d');

    this.dataPoints = [];
    this.currentRange = 'live'; // 'live', 'hour', 'day'
    this.hoverIndex = -1;

    this.padding = { top: 16, right: 16, bottom: 26, left: 42 };

    this.renderedMinTs = 0;
    this.renderedMaxTs = 0;

    this._initEvents();
  }

  _initEvents() {
    window.addEventListener('resize', () => {
      this.resize();
      this.render();
    });

    this.canvas.addEventListener('mousemove', (e) => this._onMouseMove(e));
    this.canvas.addEventListener('mouseleave', () => this._onMouseLeave());
  }

  resize() {
    const rect = this.container.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.width = rect.width;
    this.height = rect.height;

    this.canvas.width = Math.floor(rect.width * dpr);
    this.canvas.height = Math.floor(rect.height * dpr);

    this.ctx.resetTransform();
    this.ctx.scale(dpr, dpr);
  }

  setData(dataPoints, range = 'live') {
    this.dataPoints = dataPoints || [];
    this.currentRange = range;
    this.render();
  }

  render() {
    if (!this.width || !this.height) {
      this.resize();
    }

    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;
    const pad = this.padding;

    ctx.clearRect(0, 0, w, h);

    const chartW = w - pad.left - pad.right;
    const chartH = h - pad.top - pad.bottom;

    if (chartW <= 0 || chartH <= 0) return;

    // 1. Малювання горизонтальної сітки та шкали % Y
    this._drawGrid(ctx, pad, chartW, chartH);

    if (this.dataPoints.length === 0) {
      ctx.fillStyle = '#586069';
      ctx.font = '13px "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Очікування телеметричних даних...', pad.left + chartW / 2, pad.top + chartH / 2);
      return;
    }

    // 2. Розрахунок координат точок пропорційно часу
    const points = this._calculateCoordinates(chartW, chartH, pad);

    // 3. Малювання піків (cpu_max) для періодів 'hour' та 'day'
    if (this.currentRange !== 'live') {
      this._drawPeakLine(ctx, points);
    }

    // 4. Малювання ліній RAM, GPU та CPU з перевіркою розриву часу (> 120 с)
    this._drawLine(ctx, points, 'ramY', '#a78bfa', 2);
    this._drawLine(ctx, points, 'gpuY', '#f0883e', 2);
    this._drawLine(ctx, points, 'cpuY', '#4c8dff', 2);

    // 5. Рівномірні підписи часу по осі X
    this._drawTimeLabels(ctx, pad, chartW, chartH);

    // 6. Індикатор hover (курсор)
    if (this.hoverIndex >= 0 && this.hoverIndex < points.length) {
      this._drawHoverIndicator(ctx, points[this.hoverIndex], pad, chartH);
    }
  }

  _drawGrid(ctx, pad, chartW, chartH) {
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
    ctx.fillStyle = '#8b949e';
    ctx.font = '11px "Segoe UI Variable", "Segoe UI", sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';

    const levels = [0, 25, 50, 75, 100];
    for (const lvl of levels) {
      const y = Math.round(pad.top + chartH - (lvl / 100) * chartH);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + chartW, y);
      ctx.stroke();

      ctx.fillText(`${lvl}%`, pad.left - 8, y);
    }
  }

  _calculateCoordinates(chartW, chartH, pad) {
    const len = this.dataPoints.length;
    if (len === 0) return [];

    let minTs = this.dataPoints[0].timestamp;
    let maxTs = this.dataPoints[len - 1].timestamp;

    // Масштабування вікна часу під діапазон
    if (this.currentRange === 'live') {
      minTs = Math.min(minTs, maxTs - 60);
    } else if (this.currentRange === 'hour') {
      minTs = Math.min(minTs, maxTs - 3600);
    } else if (this.currentRange === 'day') {
      minTs = Math.min(minTs, maxTs - 86400);
    }

    const timeSpan = Math.max(1, maxTs - minTs);
    this.renderedMinTs = minTs;
    this.renderedMaxTs = maxTs;

    const points = [];

    for (let i = 0; i < len; i++) {
      const d = this.dataPoints[i];
      const ts = d.timestamp;

      // Позиція X пропорційна timestamp
      const relTime = Math.max(0, Math.min(1, (ts - minTs) / timeSpan));
      const x = pad.left + relTime * chartW;

      const cpuVal = Math.max(0, Math.min(100, d.cpu_total || 0));
      const cpuMaxVal = Math.max(0, Math.min(100, d.cpu_max !== undefined ? d.cpu_max : cpuVal));
      const ramVal = Math.max(0, Math.min(100, d.ram_percent || 0));

      const cpuY = pad.top + chartH - (cpuVal / 100) * chartH;
      const cpuMaxY = pad.top + chartH - (cpuMaxVal / 100) * chartH;
      const ramY = pad.top + chartH - (ramVal / 100) * chartH;

      let gpuY = null;
      if (d.gpu_percent !== null && d.gpu_percent !== undefined) {
        const gpuVal = Math.max(0, Math.min(100, Number(d.gpu_percent)));
        gpuY = pad.top + chartH - (gpuVal / 100) * chartH;
      }

      points.push({
        x,
        cpuY,
        cpuMaxY,
        ramY,
        gpuY,
        data: d,
      });
    }

    return points;
  }

  _drawLine(ctx, points, yKey, color, lineWidth) {
    // Відбираємо точки, де є валідне значення Y
    const validPoints = points.filter((p) => p[yKey] !== null);
    if (validPoints.length === 0) return;

    ctx.lineWidth = lineWidth;
    ctx.strokeStyle = color;
    ctx.beginPath();

    let isDrawing = false;

    for (let i = 0; i < validPoints.length; i++) {
      const p = validPoints[i];
      const prev = i > 0 ? validPoints[i - 1] : null;

      // Розрив лінії, якщо розрив у часі більше 2 хвилин (120 с)
      if (prev && p.data.timestamp - prev.data.timestamp > 120) {
        ctx.stroke();
        ctx.beginPath();
        isDrawing = false;
      }

      if (!isDrawing) {
        ctx.moveTo(p.x, p[yKey]);
        isDrawing = true;
      } else {
        ctx.lineTo(p.x, p[yKey]);
      }
    }

    ctx.stroke();
  }

  _drawPeakLine(ctx, points) {
    ctx.save();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(76, 141, 255, 0.45)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();

    let isDrawing = false;
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const prev = i > 0 ? points[i - 1] : null;

      if (prev && p.data.timestamp - prev.data.timestamp > 120) {
        ctx.stroke();
        ctx.beginPath();
        isDrawing = false;
      }

      if (!isDrawing) {
        ctx.moveTo(p.x, p.cpuMaxY);
        isDrawing = true;
      } else {
        ctx.lineTo(p.x, p.cpuMaxY);
      }
    }
    ctx.stroke();
    ctx.restore();
  }

  _drawTimeLabels(ctx, pad, chartW, chartH) {
    if (!this.renderedMinTs || !this.renderedMaxTs) return;

    ctx.fillStyle = '#8b949e';
    ctx.font = '11px "Segoe UI Variable", "Segoe UI", sans-serif';
    ctx.textBaseline = 'top';

    const minTs = this.renderedMinTs;
    const maxTs = this.renderedMaxTs;
    const timeSpan = maxTs - minTs;
    if (timeSpan <= 0) return;

    // Рівномірні засічки (6 міток по всій довжині осі X)
    const tickCount = 6;
    for (let i = 0; i < tickCount; i++) {
      const ratio = i / (tickCount - 1);
      const x = pad.left + ratio * chartW;
      const t = minTs + ratio * timeSpan;

      const date = new Date(t * 1000);
      let timeStr;
      if (this.currentRange === 'day') {
        timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } else {
        timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      }

      ctx.textAlign = i === 0 ? 'left' : (i === tickCount - 1 ? 'right' : 'center');
      ctx.fillText(timeStr, x, pad.top + chartH + 8);
    }
  }

  _drawHoverIndicator(ctx, point, pad, chartH) {
    // Вертикальна напрямна пунктирна лінія
    ctx.save();
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(point.x, pad.top);
    ctx.lineTo(point.x, pad.top + chartH);
    ctx.stroke();
    ctx.restore();

    // Точка CPU
    ctx.fillStyle = '#4c8dff';
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(point.x, point.cpuY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Точка RAM
    ctx.fillStyle = '#a78bfa';
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(point.x, point.ramY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Точка GPU (якщо є)
    if (point.gpuY !== null) {
      ctx.fillStyle = '#f0883e';
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(point.x, point.gpuY, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }

  _onMouseMove(e) {
    if (this.dataPoints.length === 0) return;

    const rect = this.canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;

    const pad = this.padding;
    const chartW = this.width - pad.left - pad.right;
    const minTs = this.renderedMinTs;
    const maxTs = this.renderedMaxTs;
    const timeSpan = maxTs - minTs;

    // Знаходимо найближчу точку за відстанню до координати X
    let closestIdx = 0;
    let minDiff = Infinity;

    for (let i = 0; i < this.dataPoints.length; i++) {
      const d = this.dataPoints[i];
      const relTime = timeSpan > 0 ? (d.timestamp - minTs) / timeSpan : 0.5;
      const x = pad.left + Math.max(0, Math.min(1, relTime)) * chartW;
      const diff = Math.abs(mouseX - x);
      if (diff < minDiff) {
        minDiff = diff;
        closestIdx = i;
      }
    }

    this.hoverIndex = closestIdx;
    this.render();
    this._updateTooltip(this.dataPoints[closestIdx], mouseX, e.clientY - rect.top);
  }

  _onMouseLeave() {
    this.hoverIndex = -1;
    this.render();
    if (this.tooltip) {
      this.tooltip.style.display = 'none';
    }
  }

  _updateTooltip(d, x, y) {
    if (!this.tooltip || !d) return;

    const ttTime = document.getElementById('ttTime');
    const ttCpu = document.getElementById('ttCpu');
    const ttCpuPeakRow = document.getElementById('ttCpuPeakRow');
    const ttCpuPeak = document.getElementById('ttCpuPeak');
    const ttRam = document.getElementById('ttRam');
    const ttGpuRow = document.getElementById('ttGpuRow');
    const ttGpu = document.getElementById('ttGpu');

    if (ttTime) ttTime.textContent = d.local_time || '';
    if (ttCpu) ttCpu.textContent = `${Number(d.cpu_total || 0).toFixed(1)}%`;
    if (ttRam) ttRam.textContent = `${Number(d.ram_percent || 0).toFixed(1)}%`;

    // Пік CPU (тільки для Година/День)
    if (this.currentRange !== 'live' && d.cpu_max !== undefined) {
      if (ttCpuPeakRow) ttCpuPeakRow.style.display = 'flex';
      if (ttCpuPeak) ttCpuPeak.textContent = `${Number(d.cpu_max).toFixed(1)}%`;
    } else {
      if (ttCpuPeakRow) ttCpuPeakRow.style.display = 'none';
    }

    // GPU
    if (d.gpu_percent !== null && d.gpu_percent !== undefined) {
      if (ttGpuRow) ttGpuRow.style.display = 'flex';
      if (ttGpu) ttGpu.textContent = `${Number(d.gpu_percent).toFixed(1)}%`;
    } else {
      if (ttGpuRow) ttGpuRow.style.display = 'none';
    }

    this.tooltip.style.display = 'block';
    const boundedX = Math.max(75, Math.min(this.width - 75, x));
    this.tooltip.style.left = `${boundedX}px`;
    this.tooltip.style.top = `${Math.max(40, y)}px`;
  }
}

/**
 * Рендерер міні-спарклайнів для плиток (останні 60 с).
 */
function renderSparkline(canvasId, values, strokeColor, maxVal = 100) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();

  if (rect.width === 0 || rect.height === 0) return;

  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);

  ctx.resetTransform();
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  if (!values || values.length < 2) return;

  const len = values.length;
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = strokeColor;
  ctx.beginPath();

  for (let i = 0; i < len; i++) {
    const val = Math.max(0, Math.min(maxVal, values[i] || 0));
    const x = (i / (len - 1)) * w;
    const y = h - (val / maxVal) * (h - 4) - 2;

    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}
