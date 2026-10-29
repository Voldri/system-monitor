/**
 * Модуль керування вкладками періодів графіка: «Наживо», «Година», «День».
 */

class TabsController {
  constructor(onTabChange) {
    this.currentTab = 'live';
    this.onTabChange = onTabChange;

    this.tabButtons = document.querySelectorAll('.tab-btn');
    this.peakLegend = document.getElementById('peakCpuLegend');

    this._bindEvents();
  }

  _bindEvents() {
    this.tabButtons.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const range = e.currentTarget.getAttribute('data-range');
        if (range && range !== this.currentTab) {
          this.switchTab(range);
        }
      });
    });
  }

  switchTab(range) {
    this.currentTab = range;

    this.tabButtons.forEach((btn) => {
      if (btn.getAttribute('data-range') === range) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    // Відображення легенди піків тільки для Години та Дня
    if (this.peakLegend) {
      this.peakLegend.style.display = (range === 'live') ? 'none' : 'flex';
    }

    if (typeof this.onTabChange === 'function') {
      this.onTabChange(range);
    }
  }

  getCurrentTab() {
    return this.currentTab;
  }
}
