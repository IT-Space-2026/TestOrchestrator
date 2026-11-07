// Plotly init loader — CSP-safe (no inline JS).
function initPlots() {
  if (typeof Plotly === 'undefined') { return; }
  document.querySelectorAll('[data-plotly-fig]').forEach(function (el) {
    try {
      var fig = JSON.parse(el.getAttribute('data-plotly-fig'));
      if (!fig.layout.autosize && !fig.layout.width && el.clientWidth) {
        fig.layout.width = el.clientWidth;
      }
      el.style.minHeight = '300px';
      Plotly.newPlot(el, fig.data, fig.layout,
        {responsive: true, displaylogo: false});
    } catch (e) {
      el.textContent = 'Chart unavailable: ' + e.message;
    }
  });
}
window.addEventListener('load', initPlots);   // after CSS/fonts settle
window.addEventListener('resize', function () {
  document.querySelectorAll('[data-plotly-fig]').forEach(function (el) {
    if (el.classList.contains('js-plotly-plot')) { Plotly.Plots.resize(el); }
  });
});
