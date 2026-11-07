// Plotly init loader — runs Plotly.newPlot for any element whose
// data-plotly-fig attribute holds figure JSON (CSP-safe: no inline JS).
function initPlots() {
  if (typeof Plotly === 'undefined') { return; }
  document.querySelectorAll('[data-plotly-fig]').forEach(function (el) {
    try {
      var fig = JSON.parse(el.getAttribute('data-plotly-fig'));
      el.style.minHeight = '300px';
      if (el.clientWidth > 0) { fig.layout.width = el.clientWidth; }
      Plotly.newPlot(el, fig.data, fig.layout,
        {responsive: true, displaylogo: false});
    } catch (e) {
      el.textContent = 'Chart unavailable: ' + e.message;
    }
  });
}
document.addEventListener('DOMContentLoaded', initPlots);
