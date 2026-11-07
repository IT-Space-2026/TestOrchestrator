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

// Shared client-side figure builder mirroring charts.py so the live
// poll can update the same Plotly chart without an inline script.
window.TOCharts = {
  palette: {passed:'#2e7d32', failed:'#c62828', error:'#ef6c00',
            skipped:'#757575'},
  base: function(){ return {font:{family:'system-ui,sans-serif',
    size:12}, height:300, margin:{l:46,r:16,t:10,b:46},
    paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
    showlegend:true, legend:{orientation:'h', y:-0.28},
    xaxis:{tickangle:0}}; },
  outcomeTrend: function(rows){
    var P=this.palette, days=rows.map(r=>r.day), out=[];
    ['passed','failed','error','skipped'].forEach(function(k){
      if(rows.some(r=>r[k])) out.push({type:'bar',
        name:k.charAt(0).toUpperCase()+k.slice(1),
        x:days, y:rows.map(r=>r[k]||0),
        marker:{color:P[k]}});
    });
    var lay=this.base(); lay.barmode='stack';
    lay.yaxis={title:'Test cases'}; return {data:out, layout:lay};
  }
};
