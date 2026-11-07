// Plotly init loader — runs Plotly.newPlot for any element whose
// data-plotly-fig attribute holds figure JSON (CSP-safe: no inline JS).
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-plotly-fig]').forEach(function (el) {
    try {
      var fig = JSON.parse(el.getAttribute('data-plotly-fig'));
      el.style.minHeight = '300px';
      Plotly.newPlot(el, fig.data, fig.layout,
        {responsive: true, displaylogo: false}).then(function () {
        Plotly.Plots.resize(el);   // re-measure after CSS/fonts settle
      });
    } catch (e) {
      el.textContent = 'Chart unavailable: ' + e.message;
    }
  });
});
