/* ============ StudyBuddy 展示页 · 知识图谱浏览器 ============ */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);

/* 与主应用调色板保持一致 */
const TYPE_COLORS = { concept: '#4f7fd6', method: '#8e63cf', property: '#e0a63c',
                      formula: '#d9534f', theorem: '#c2559b', rule: '#6b7a99' };
const TYPE_CN = { concept: '概念', method: '方法', property: '性质',
                  formula: '公式', theorem: '定理', rule: '法则' };
const REL_STYLE = {
  PREREQUISITE_OF: { color: '#aab3c2', type: 'solid' },    // 前置依赖
  DERIVED_FROM:    { color: '#4f8fd6', type: 'dashed' },   // 派生
  RELATED_TO:      { color: '#c2559b', type: 'dotted' },   // 横向关联
};

const state = { data: null, kpById: null, chart: null, module: 'SEQ', selected: null };

function renderMath(root) {
  if (!window.renderMathInElement || !root) return;
  try {
    window.renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
      ],
      throwOnError: false,
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'code'],
    });
  } catch (e) { /* 离线时忽略 */ }
}

async function main() {
  const res = await fetch('data/kg.json');
  const data = await res.json();
  state.data = data;
  state.kpById = new Map(data.kps.map(kp => [kp.id, kp]));

  buildChips();
  renderGraph();
  bindSearch();
  bindCopy();

  // 供调试 / 外部调用
  window.__demo = {
    focusKp: id => selectKp(id),
    setModule: m => { state.module = m; buildChips(); renderGraph(); },
    stats: () => ({ kps: state.kps ? state.kps.length : 0, edges: state.edges ? state.edges.length : 0 }),
  };
}

/* ---------- 模块快捷选择 ---------- */
function buildChips() {
  const box = $('#moduleChips');
  const mods = state.data.modules;
  const items = [{ code: '', name: '全部', count: state.data.kps.length }].concat(
    mods.map(m => ({ code: m.code, name: m.name, count: m.kp_count })));
  box.innerHTML = items.map(m => `
    <button class="chip ${state.module === m.code ? 'active' : ''}" data-m="${m.code}">
      ${m.name}<span class="n">${m.count}</span>
    </button>`).join('');
  box.querySelectorAll('.chip').forEach(btn => btn.addEventListener('click', () => {
    state.module = btn.dataset.m;
    buildChips();
    renderGraph();
  }));
}

/* ---------- 图谱渲染 ---------- */
function currentGraph() {
  const mod = state.module;
  const kps = mod ? state.data.kps.filter(k => k.module === mod) : state.data.kps;
  const ids = new Set(kps.map(k => k.id));
  const edges = state.data.edges.filter(e => ids.has(e.from) && ids.has(e.to));
  return { kps, edges };
}

function renderGraph() {
  const box = $('#graph');
  const { kps, edges } = currentGraph();
  state.kps = kps;
  state.edges = edges;
  if (!kps.length) return;

  if (state.chart) { state.chart.dispose(); state.chart = null; }
  const chart = echarts.init(box);
  state.chart = chart;

  const dense = kps.length > 150;
  const nodes = kps.map(kp => ({
    id: kp.id, name: kp.name,
    symbolSize: 9 + Math.min(26, (kp.in_degree || 0) * 2.2),
    itemStyle: {
      color: TYPE_COLORS[kp.kp_type] || '#9aa4b2',
      borderColor: '#ffffff', borderWidth: 1.2,
      shadowBlur: (kp.in_degree || 0) >= 3 ? 8 : 0,
      shadowColor: 'rgba(79, 70, 229, .28)',
    },
    label: {
      show: kps.length <= 80 ? true : (kp.in_degree || 0) >= 4,
      fontSize: 10.5,
      color: '#4b5563',
    },
    _kp: kp,
  }));
  const links = edges.map(e => {
    const s = REL_STYLE[e.rel] || { color: '#c3cad6', type: 'solid' };
    return {
      source: e.from, target: e.to,
      lineStyle: { color: s.color, type: s.type, width: 1.3, curveness: 0.08,
                   opacity: e.strength === 'recommended' ? 0.35 : 0.55 },
      _edge: e,
    };
  });

  chart.setOption({
    animation: !dense,
    tooltip: {
      confine: true,
      formatter: params => {
        if (params.dataType === 'edge') {
          const e = params.data._edge;
          const relCn = { PREREQUISITE_OF: '前置依赖', RELATED_TO: '横向关联', DERIVED_FROM: '派生' }[e.rel] || e.rel;
          return `<b>${relCn}</b><br>${e.reason || ''}`;
        }
        const kp = params.data._kp;
        return `<b>${kp.name}</b><br>
          <span style="opacity:.7">${kp.id}</span><br>
          ${kp.module_name || ''} · ${kp.section_no || ''} ${kp.section_name || ''}<br>
          ${kp.kp_type_cn || TYPE_CN[kp.kp_type] || ''} · 难度 ${kp.difficulty_cn || ''}<br>
          被 ${kp.in_degree || 0} 个知识点依赖`;
      },
    },
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      data: nodes, links,
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 5,
      force: {
        repulsion: dense ? 80 : 240,
        edgeLength: dense ? [22, 80] : [70, 150],
        gravity: 0.12, friction: 0.22,
      },
      emphasis: { focus: 'adjacency', scale: 1.18,
                  label: { show: true, fontSize: 12, fontWeight: 'bold' } },
      label: { position: 'right' },
      labelLayout: { hideOverlap: true },
      lineStyle: { color: '#c3cad6' },
    }],
  });

  chart.on('click', params => {
    if (params.dataType === 'node') selectKp(params.data.id);
  });
  // 力导向收敛需要时间，分几次自动适配视图
  [900, 2600].forEach(ms => setTimeout(() => fitGraph(chart, box), ms));

  // 当前选中的知识点如果还在图里，保持高亮状态
  if (state.selected && kps.some(k => k.id === state.selected)) {
    setTimeout(() => highlight(state.selected), 300);
  }
}

function fitGraph(chart, box) {
  try {
    const series = chart.getModel().getSeriesByIndex(0);
    if (!series) return;
    const points = series.getData();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < points.count(); i += 1) {
      const pos = points.getItemLayout(i);
      if (!pos || !isFinite(pos[0]) || !isFinite(pos[1])) continue;
      minX = Math.min(minX, pos[0]); maxX = Math.max(maxX, pos[0]);
      minY = Math.min(minY, pos[1]); maxY = Math.max(maxY, pos[1]);
    }
    if (!isFinite(minX)) return;
    const w = box.clientWidth || 800, h = box.clientHeight || 500, pad = 60;
    const zoom = Math.min((w - pad) / Math.max(maxX - minX, 1),
                          (h - pad) / Math.max(maxY - minY, 1));
    chart.setOption({
      series: [{ center: [(minX + maxX) / 2, (minY + maxY) / 2],
                 zoom: Math.max(0.05, Math.min(2, zoom)) }],
    }, false);
  } catch (e) { /* 布局未就绪时忽略 */ }
}

function highlight(kpId) {
  const chart = state.chart;
  if (!chart || !state.kps) return;
  const idx = state.kps.findIndex(k => k.id === kpId);
  chart.dispatchAction({ type: 'downplay', seriesIndex: 0 });
  if (idx >= 0) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx });
}

/* ---------- 知识点详情 ---------- */
function selectKp(kpId) {
  const kp = state.kpById.get(kpId);
  if (!kp) return;
  state.selected = kpId;
  highlight(kpId);

  const prereqs = state.data.edges.filter(e => e.to === kpId && e.rel === 'PREREQUISITE_OF');
  const succs = state.data.edges.filter(e => e.from === kpId && e.rel === 'PREREQUISITE_OF');
  const links = (list, idField) => list.slice(0, 9).map(e => {
    const other = state.kpById.get(e[idField]);
    if (!other) return '';
    return `<button class="kp-link ${idField === 'from' ? 'pre' : ''}" data-kp="${other.id}"
      title="${(e.reason || '').replace(/"/g, '&quot;')}">${other.name}</button>`;
  }).join('') + (list.length > 9 ? `<span class="hint">…等 ${list.length} 个</span>` : '');

  const box = $('#detail');
  box.innerHTML = `
    <h3>${kp.name}</h3>
    <div class="meta-line">${kp.id} · ${kp.book_name || ''} ${kp.chapter_name || ''} ${kp.section_no || ''} ${kp.section_name || ''}</div>
    <div class="badges">
      <span class="badge type">${kp.kp_type_cn || TYPE_CN[kp.kp_type] || ''}</span>
      ${kp.difficulty_cn ? `<span class="badge">难度 ${kp.difficulty_cn}</span>` : ''}
      ${kp.importance_cn ? `<span class="badge">课标「${kp.importance_cn}」</span>` : ''}
      <span class="badge">被 ${kp.in_degree || 0} 个知识点依赖</span>
    </div>
    ${kp.statement ? `<div class="sec-title">规范陈述</div><div class="statement">${escapeHtml(kp.statement)}</div>` : ''}
    ${kp.latex ? `<div class="sec-title">核心公式</div><div>$$${escapeHtml(kp.latex)}$$</div>` : ''}
    <div class="sec-title">前置知识（${prereqs.length}）</div>
    ${prereqs.length ? links(prereqs, 'from') : '<span class="hint">无（是起点知识）</span>'}
    <div class="sec-title">后续知识点（${succs.length}）</div>
    ${succs.length ? links(succs, 'to') : '<span class="hint">无</span>'}
  `;
  renderMath(box);
  box.querySelectorAll('.kp-link').forEach(btn =>
    btn.addEventListener('click', () => selectKp(btn.dataset.kp)));
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------- 搜索 ---------- */
function bindSearch() {
  const input = $('#searchBox');
  const box = $('#searchResults');

  const search = () => {
    const kw = input.value.trim().toLowerCase();
    if (!kw) { box.hidden = true; return; }
    const hits = state.data.kps.filter(kp => {
      if (kp.name.toLowerCase().includes(kw)) return true;
      return (kp.aliases || []).some(a => String(a).toLowerCase().includes(kw));
    }).slice(0, 30);
    box.innerHTML = hits.length ? hits.map(kp => `
      <button class="sr-item" data-kp="${kp.id}">
        ${kp.name}
        <span class="meta">${kp.module_name || ''} · ${kp.section_no || ''} ${kp.section_name || ''} · ${kp.kp_type_cn || ''}</span>
      </button>`).join('') : '<div class="sr-empty">没有匹配的知识点，换个关键词试试</div>';
    box.hidden = false;
    box.querySelectorAll('.sr-item').forEach(item => item.addEventListener('click', () => {
      const id = item.dataset.kp;
      const kp = state.kpById.get(id);
      // 若目标不在当前视图，自动切到「全部」
      if (state.module && kp.module !== state.module) {
        state.module = '';
        buildChips();
        renderGraph();
      }
      selectKp(id);
      box.hidden = true;
      input.blur();
    }));
  };

  input.addEventListener('input', search);
  input.addEventListener('focus', search);
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') box.hidden = true;
    if (e.key === 'Enter') {
      const first = box.querySelector('.sr-item');
      if (first) first.click();
    }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.search-wrap')) box.hidden = true;
  });
}

/* ---------- 复制命令 ---------- */
function bindCopy() {
  const btn = $('#copyBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const text = $('#cmd').textContent;
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = '已复制 ✓';
    } catch (e) {
      const range = document.createRange();
      range.selectNodeContents($('#cmd'));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      btn.textContent = '已选中，按 Ctrl+C';
    }
    setTimeout(() => { btn.textContent = '复制'; }, 2200);
  });
}

window.addEventListener('resize', () => { if (state.chart) state.chart.resize(); });

main().catch(err => {
  const box = $('#graph');
  if (box) box.innerHTML = `<div class="graph-loading">图谱数据加载失败：${err.message}</div>`;
});
