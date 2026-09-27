/* StudyBuddy · 前端逻辑（原生 JS，无构建步骤） */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { files: [], student: 'default', lastResult: null, lastJobId: null,
                modules: [], heat: [] };

/* ------------------------------------------------------------------ 工具 */
function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* 模型生成的解答与解析带 Markdown 记号（**加粗**、- 列表），而这些块是纯文本渲染，
   记号会原样露出来。这里做一次最小改写：先 esc 再只产出 strong 一种标签，不引入
   任何 Markdown 依赖（本项目前端无构建步骤）。入参传原文，转义在里面做。 */
function mdLite(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^[\t ]*[-*][\t ]+/gm, '· ');
}

function toast(message, ms = 2600) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, ms);
}

function renderMath(root) {
  if (!window.renderMathInElement || !root) return;
  try {
    window.renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
      ],
      throwOnError: false, ignoredTags: ['script', 'noscript', 'style', 'textarea', 'code'],
    });
  } catch (e) { console.warn('KaTeX 渲染失败', e); }
}

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
  return data;
}

function assetUrl(relPath) {
  if (!relPath) return '';
  const idx = relPath.indexOf('assets/');
  return idx >= 0 ? '/' + relPath.slice(idx) : '/' + relPath;
}

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;

/* 原题附件多数是图片，但早期记录留下的是整个 PDF。PDF 放进 <img> 只会显示破图，
   所以这里先判扩展名，非图片就退化为可点击的文件链接。 */
function assetBlock(relPath, alt = '原题') {
  if (!relPath) return '';
  const url = assetUrl(relPath);
  if (IMAGE_EXT.test(relPath)) return `<img class="q-asset" src="${url}" alt="${alt}">`;
  const ext = (relPath.match(/\.([A-Za-z0-9]+)$/) || [null, '附件'])[1].toUpperCase();
  return `<a class="q-asset-file" href="${url}" target="_blank" rel="noopener">
    原题附件是 ${ext} 文件，点击在新窗口打开</a>`;
}

function heatDot(heat) {
  return `<span class="heat-dot" style="background:${heat.color}"></span>`;
}

const ROLE_CN = { primary: '主要考查', secondary: '涉及', root_cause: '根因候选' };

/* ------------------------------------------------------------------ 标签页（左侧导航） */
const TAB_TITLES = { upload: '录入错题', book: '错题本', heat: '知识点热度',
                     graph: '图谱查询', view: '图谱视图' };
$$('.tab').forEach(tab => tab.addEventListener('click', () => {
  $$('.tab').forEach(t => t.classList.toggle('active', t === tab));
  $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab.dataset.tab));
  $('#pageTitle').textContent = TAB_TITLES[tab.dataset.tab] || '';
  if (tab.dataset.tab === 'book') loadBook();
  if (tab.dataset.tab === 'heat') loadHeat();
  if (tab.dataset.tab === 'view') loadView();
}));

function refreshAvatar() {
  const name = ($('#studentId').value || '').trim() || 'default';
  $('#avatarBadge').textContent = name.slice(0, 1).toUpperCase();
}
$('#studentId').addEventListener('change', e => {
  state.student = e.target.value.trim() || 'default';
  refreshAvatar();
  loadBook(); loadHeat();
  if (state.chart) loadView();
});

/* ------------------------------------------------------------------ 启动自检 */
function setEnvPill(env) {
  const pill = $('#envPill');
  pill.textContent = env.vision_ready
    ? `识别：${env.vision_provider} / ${env.vision_model}`
    : '识别未配置（点这里设置）';
  pill.className = 'pill ' + (env.vision_ready ? 'pill-ok' : 'pill-warn');
  pill.title = env.vision_note || '';
}

async function refreshEnvPill() {
  try {
    setEnvPill((await api('/api/health')).environment || {});
  } catch (e) {
    const pill = $('#envPill');
    pill.textContent = '服务未就绪：' + e.message;
    pill.className = 'pill pill-warn';
  }
}

async function boot() {
  try {
    const health = await api('/api/health');
    state.modules = health.kg.modules || [];
    const env = health.environment || {};
    setEnvPill(env);
    if (!env.vision_ready) {
      toast(env.vision_note + '（也可点右上角「设置」填写密钥）', 6000);
    }
    const heatSel = $('#heatModule');
    state.modules.forEach(m => {
      const html = `<option value="${m.code}">${esc(m.name)}（${m.kp_count}）</option>`;
      heatSel.insertAdjacentHTML('beforeend', html);
    });
    // 图谱视图的视图范围：按册 / 按模块 / 全部
    const viewSel = $('#viewScope');
    const books = await api('/api/books').catch(() => ({ books: [] }));
    (books.books || []).forEach(b => viewSel.insertAdjacentHTML('beforeend',
      `<option value="book:${b.code}">${esc(b.name)}（${b.kp_count} 个）</option>`));
    state.modules.forEach(m => viewSel.insertAdjacentHTML('beforeend',
      `<option value="module:${m.code}">模块 · ${esc(m.name)}（${m.kp_count} 个）</option>`));
    // 默认打开必修一（和原始知识图谱可视化一致：节点少、布局快）
    if ((books.books || []).some(b => b.code === 'BX1')) viewSel.value = 'book:BX1';
  } catch (e) {
    $('#envPill').textContent = '服务未就绪：' + e.message;
    $('#envPill').className = 'pill pill-warn';
  }
}

/* ------------------------------------------------------------------ 设置 */
const settingsModal = $('#settingsModal');
const modalBody = $('#settingsModal .modal-body');

function showSetResult(ok, message) {
  const box = $('#setResult');
  box.hidden = false;
  box.className = 'set-result ' + (ok ? 'ok' : 'err');
  box.textContent = message;
}

async function openSettings() {
  settingsModal.hidden = false;
  $('#setResult').hidden = true;
  try {
    const s = await api('/api/settings');
    $('#setProvider').value = s.vision.provider || 'deepseek';
    $('#setBase').value = s.vision.base || '';
    $('#setModel').value = s.vision.model || '';
    $('#setTextModel').value = s.text_model || '';
    $('#setMaxTokens').value = s.max_tokens || '';
    $('#setMaxPages').value = s.max_pages || '';
    $('#setConcurrency').value = s.concurrency || '';
    $('#setKey').value = '';
    $('#setKeyState').textContent = s.vision.has_key
      ? `（已保存：${s.vision.key_masked}，留空则不修改）`
      : '（尚未设置）';
    $('#setEnvPath').textContent = '配置文件：' + s.env_path;
  } catch (e) {
    showSetResult(false, '读取设置失败：' + e.message);
  }
}

function closeSettings() {
  settingsModal.hidden = true;
  $$('.pop-list').forEach(p => { p.hidden = true; });   // 别把下拉留在屏幕上
}

function settingsPayload() {
  return {
    provider: $('#setProvider').value,
    base: $('#setBase').value.trim(),
    model: $('#setModel').value.trim(),
    text_model: $('#setTextModel').value.trim(),
    max_tokens: $('#setMaxTokens').value.trim(),
    max_pages: $('#setMaxPages').value.trim(),
    concurrency: $('#setConcurrency').value.trim(),
    api_key: $('#setKey').value.trim(),
  };
}

async function postJson(path, body) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                     body: JSON.stringify(body) });
}

$('#settingsBtn').addEventListener('click', openSettings);
$('#envPill').addEventListener('click', openSettings);
$('#settingsClose').addEventListener('click', closeSettings);
$('#settingsCancel').addEventListener('click', closeSettings);
settingsModal.addEventListener('click', e => { if (e.target === settingsModal) closeSettings(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !settingsModal.hidden) closeSettings();
});

$('#settingsTest').addEventListener('click', async () => {
  const btn = $('#settingsTest');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>测试中';
  showSetResult(true, '正在用填写的配置请求模型，稍等…');
  try {
    const r = await postJson('/api/settings/test', settingsPayload());
    showSetResult(r.ok, (r.ok ? '✅ ' : '❌ ') + r.message);
  } catch (e) {
    showSetResult(false, '测试失败：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '测试连接';
  }
});

/* ---------- 模型名候选下拉 ---------- */
function modelPicker(inputSel, popSel) {
  const input = $(inputSel), pop = $(popSel);
  let items = [], idx = -1;

  function place() {
    const r = input.getBoundingClientRect();
    const h = Math.min(pop.scrollHeight || 0, 210);
    const below = window.innerHeight - r.bottom - 10;
    pop.style.left = r.left + 'px';
    pop.style.width = r.width + 'px';
    if (below < Math.min(h, 120) && r.top > h + 10) {
      pop.style.top = (r.top - h - 6) + 'px';   // 下面装不下就翻到上方
    } else {
      pop.style.top = (r.bottom + 5) + 'px';
    }
  }

  function render(useFilter = true) {
    const kw = useFilter ? input.value.trim().toLowerCase() : '';
    const shown = items.filter(m => !kw || m.toLowerCase().includes(kw));
    if (!shown.length) return hide();
    $$('.pop-list').forEach(p => { if (p !== pop) p.hidden = true; });  // 同时只开一个
    pop.innerHTML = shown.map(m => `<div class="pop-item" data-v="${esc(m)}">${esc(m)}</div>`).join('');
    pop.hidden = false;
    idx = -1;
    place();
  }

  function hide() { pop.hidden = true; idx = -1; }

  input.addEventListener('focus', () => render(false));   // 点开先看全量
  input.addEventListener('click', () => render(false));
  input.addEventListener('input', () => render(true));    // 敲字才过滤
  input.addEventListener('keydown', e => {
    if (pop.hidden) return;
    const nodes = [...pop.querySelectorAll('.pop-item')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      idx = e.key === 'ArrowDown' ? Math.min(idx + 1, nodes.length - 1) : Math.max(idx - 1, 0);
      nodes.forEach((n, i) => n.classList.toggle('on', i === idx));
      nodes[idx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && idx >= 0) {
      e.preventDefault();                       // 否则会误触弹窗的保存
      input.value = nodes[idx].dataset.v;
      hide();
    } else if (e.key === 'Escape') {
      e.stopPropagation();                      // 只收起列表，别顺手关掉弹窗
      hide();
    }
  });
  pop.addEventListener('mousedown', e => {
    const it = e.target.closest('.pop-item');
    if (!it) return;
    e.preventDefault();                         // 保住输入框焦点
    input.value = it.dataset.v;
    hide();
  });
  input.addEventListener('blur', () => setTimeout(hide, 120));
  modalBody.addEventListener('scroll', hide);
  window.addEventListener('resize', hide);

  return {
    set(list) { items = list || []; },
    open() { if (items.length) { input.focus(); render(); } },
    hide,
  };
}

const visionPicker = modelPicker('#setModel', '#setModelPop');
const textPicker = modelPicker('#setTextModel', '#setTextModelPop');

$('#settingsFetchModels').addEventListener('click', async () => {
  const btn = $('#settingsFetchModels');
  const hint = $('#setModelsHint');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>拉取中';
  hint.textContent = '正在向服务商请求模型列表…';
  try {
    const r = await postJson('/api/settings/models', settingsPayload());
    if (!r.ok) { hint.textContent = '❌ ' + r.message; return; }
    visionPicker.set(r.models);
    textPicker.set(r.models);
    hint.textContent = `✅ ${r.message}点输入框就能选，也可以直接手填。`;
    visionPicker.open();
  } catch (e) {
    hint.textContent = '❌ 拉取失败：' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '⬇ 拉取模型列表';
  }
});

$('#settingsSave').addEventListener('click', async () => {
  const btn = $('#settingsSave');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>保存中';
  try {
    const r = await postJson('/api/settings', settingsPayload());
    showSetResult(true, `✅ 已保存到 ${r.saved_to}，立即生效。当前识别环境：${r.vision_note}`);
    $('#setKey').value = '';               // 不回显明文
    $('#setKeyState').textContent = r.settings.vision.has_key
      ? `（已保存：${r.settings.vision.key_masked}，留空则不修改）` : '（尚未设置）';
    await refreshEnvPill();
    toast('设置已保存并生效');
  } catch (e) {
    showSetResult(false, '保存失败：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '保存';
  }
});

/* ------------------------------------------------------------------ 录入 */
const dropzone = $('#dropzone');
const fileInput = $('#fileInput');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('over'));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); dropzone.classList.remove('over');
  addFiles([...e.dataTransfer.files]);
});
fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value = ''; });

function addFiles(list) {
  list.forEach(f => {
    if (!state.files.some(x => x.name === f.name && x.size === f.size)) state.files.push(f);
  });
  renderFileList();
}

function renderFileList() {
  $('#fileList').innerHTML = state.files.map((f, i) =>
    `<li>${esc(f.name)} ${(f.size / 1024).toFixed(0)}KB
      <button data-i="${i}" title="移除">✕</button></li>`).join('');
  $$('#fileList button').forEach(btn => btn.addEventListener('click', () => {
    state.files.splice(+btn.dataset.i, 1); renderFileList();
  }));
}

function buildIngestForm(picks) {
  const form = new FormData();
  form.append('student_id', state.student);
  form.append('stem_text', $('#stemText').value.trim());
  form.append('hint', $('#hint').value.trim());
  form.append('student_answer', $('#studentAnswer').value.trim());
  form.append('error_type', $('#errorType').value);
  form.append('auto_analyze', $('#autoAnalyze').checked ? 'true' : 'false');
  if (picks !== undefined) form.append('picks', picks);
  state.files.forEach(f => form.append('files', f, f.name));
  return form;
}

const STATE_LABEL = { running: '处理中', waiting: '等待勾选', done: '完成', error: '失败' };
/** 更新进度。两处同时更新：左侧控制区 + 结果卡片里的进度面板。
 *
 * 为什么要有结果卡片那份：点「记录选中的题」之后，用户的视线在结果卡片上，
 * 只更新左侧的进度条等于没显示（实测用户就是这么反馈的）。
 */
function setProgress(percent, message, stateName) {
  const value = Math.max(0, Math.min(100, percent));
  const cls = stateName && stateName !== 'running' ? ' ' + stateName : '';

  const box = $('#progressBox');
  if (box) {
    box.hidden = false;
    box.className = 'progress-box' + cls;
    $('#progressBar').style.width = value + '%';
    $('#progressMsg').innerHTML =
      `<span>${esc(message)}</span><span>${Math.round(value)}%</span>`;
  }

  const panel = $('#panelProgress');
  if (panel) {
    panel.className = 'progress-box' + cls;
    $('#panelBar').style.width = value + '%';
    $('#panelMsg').textContent = message;
    const badge = $('#panelState');
    if (badge) {
      badge.textContent = `${Math.round(value)}% · ${STATE_LABEL[stateName] || '处理中'}`;
      badge.className = 'badge' + (stateName === 'done' ? ' badge-ok'
                                 : stateName === 'error' ? '' : ' badge-warn');
    }
  }
}

/** 在结果卡片里渲染进度面板（用户点完勾选后视线就在这里）。 */
function renderProgressPanel(percent, message, stateName) {
  const box = $('#resultBody');
  $('#resultEmpty').hidden = true;
  box.hidden = false;
  box.innerHTML = `
    <div class="q-head">
      <span class="q-id">正在入库</span>
      <span class="badge badge-warn" id="panelState">处理中</span>
    </div>
    <div class="q-body">
      <div class="progress-box" id="panelProgress">
        <div class="progress-track"><i id="panelBar"></i></div>
        <p class="hint" id="panelMsg" style="margin-top:9px"></p>
      </div>
      <p class="hint mt">流程：按页识别 → 逐题抽取知识点 → 图谱对齐 → 生成解析。
        整页试卷通常 10 秒到几分钟，期间可以留在本页等待。</p>
    </div>`;
  setProgress(percent, message, stateName);
}

/** 轮询后台任务直到结束并按结果渲染。上传与入库两条路径共用。 */
function watchJob(jobId, pickBackup) {
  const btn = $('#ingestBtn');
  const msg = $('#uploadMsg');

  const finish = () => {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    btn.disabled = false;
    btn.textContent = '识别并入库';
    // 任务失败时勾选列表会被恢复，得把它的按钮也恢复可用，否则没法重试
    const pickBtn = $('#pickSave');
    if (pickBtn) { pickBtn.disabled = false; pickBtn.textContent = '记录选中的题'; }
  };

  const tick = async () => {
    let job;
    try {
      job = await api('/api/jobs/' + jobId);
    } catch (e) {
      return;   // 单次查询失败不致命，等下一轮再试
    }
    const elapsed = `已用 ${Math.round(job.elapsed || 0)} 秒`;
    if (job.state === 'running') {
      setProgress(job.percent, `${job.message} · ${elapsed}`, 'running');
      return;
    }
    finish();
    if (job.state === 'error') {
      setProgress(100, (job.error || '处理失败'), 'error');
      msg.className = 'msg err';
      msg.textContent = '处理失败：' + job.error;
      // 勾选阶段失败时把清单还回去，否则用户只能重新上传一遍
      if (pickBackup) {
        toast('入库失败，可重新勾选后再试', 5000);
        setTimeout(() => renderPick(pickBackup), 2500);
      }
      return;
    }
    const data = job.result || {};
    if (data.needs_pick) {
      state.lastResult = data;
      state.lastJobId = jobId;         // 供「记录选中的题」复用识别结果
      renderPick(data);
      setProgress(100, `识别到 ${data.problem_count} 道题，等待勾选 · ${elapsed}`, 'waiting');
      msg.className = 'msg ok';
      msg.textContent = `识别到 ${data.problem_count} 道题，请勾选要记录的那些。`;
      return;
    }
    state.lastResult = data;
    renderResult(data);
    setProgress(100, `完成 · ${elapsed}`, 'done');
    msg.className = 'msg ok';
    msg.textContent = data.created_count > 1
      ? `已入库 ${data.created_count} 道题` : `已入库：${data.question_id}`;
    state.files = []; renderFileList();
    $('#stemText').value = ''; $('#studentAnswer').value = ''; $('#hint').value = '';
  };

  state.pollTimer = setInterval(tick, 900);
  tick();
}

async function runIngest(picks) {
  const btn = $('#ingestBtn');
  const msg = $('#uploadMsg');
  if (!state.files.length && !$('#stemText').value.trim()) {
    msg.className = 'msg err';
    msg.textContent = '请至少上传一张图片，或填写题干文字。';
    return;
  }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.lastJobId = null;          // 新的一轮上传，旧结果作废
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>处理中…';
  msg.className = 'msg';
  msg.textContent = '';
  renderProgressPanel(0, '正在上传文件…', 'running');

  // 录入要走「按页识别 → 逐题抽取 → 图谱对齐 → AI 解析」，整页试卷动辄几分钟。
  // 用一个阻塞请求时界面只能干等，所以改成后台任务 + 轮询进度。
  try {
    const started = await api('/api/ingest_async',
                              { method: 'POST', body: buildIngestForm(picks) });
    watchJob(started.job_id, null);
  } catch (e) {
    setProgress(100, '上传失败：' + e.message, 'error');
    msg.className = 'msg err';
    msg.textContent = '识别失败：' + e.message;
    btn.disabled = false; btn.textContent = '识别并入库';
  }
}

/** 勾选后入库：复用服务端缓存的识别结果，不重新上传、不重新识别。
 *
 * 为什么不能重传：模型每次输出可能不同，重跑会出现
 * 「勾选的是第 5 题，入库的却是另一道题」这种对不上的情况。
 */
async function runCommit(picks, edits) {
  const btn = $('#ingestBtn');
  const msg = $('#uploadMsg');
  if (!state.lastJobId) {
    toast('识别结果已失效，请重新上传', 5000);
    return;
  }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  const pickBackup = (state.lastResult && state.lastResult.needs_pick)
    ? state.lastResult : null;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>处理中…';
  msg.className = 'msg';
  msg.textContent = '';
  renderProgressPanel(0, '正在入库选中的题目…', 'running');
  try {
    const started = await api('/api/ingest_commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: state.lastJobId,
        picks,
        edits,                    // 手改过的题干/作答：只含改动项，其余用识别结果
        student_id: state.student,
        error_type: $('#errorType').value,
        auto_analyze: $('#autoAnalyze').checked,
      }),
    });
    watchJob(started.job_id, pickBackup);
  } catch (e) {
    setProgress(100, '入库失败：' + e.message, 'error');
    msg.className = 'msg err';
    msg.textContent = '入库失败：' + e.message;
    btn.disabled = false; btn.textContent = '识别并入库';
  }
}

$('#ingestBtn').addEventListener('click', () => runIngest(undefined));

const QTYPE_CN = { choice: '选择题', blank: '填空题', solution: '解答题' };

/* ---------- 题干编辑：一题两栏（渲染 / 原文） ----------
 * 识别总会有错：下标丢了、分式排成一行、多认或少认几个字。
 * 学生得能在入库前把题干改对，所以给同一份文本两种看法 ——
 *   渲染 = KaTeX 排好版的，用来核对公式；
 *   原文 = 可编辑的 Markdown / LaTeX，用来改。
 * 两栏共用一份 state，切换时不需要互相同步。
 */
function stemEditorHTML() {
  return `
    <div class="seg" role="group" aria-label="题干视图切换">
      <button type="button" class="seg-btn active" data-pane="render">渲染</button>
      <button type="button" class="seg-btn" data-pane="raw">原文</button>
    </div>
    <div class="stem-pane" data-pane="render">
      <div class="stem-preview" data-math></div>
      <div class="ans-preview" data-math></div>
      <p class="hint ans-empty" hidden title="切到「原文」可以自己补上">未识别</p>
    </div>
    <div class="stem-pane" data-pane="raw" hidden>
      <label class="mini-label">题干 Markdown / LaTeX（行内公式用 $...$，独立公式用 $$...$$）</label>
      <textarea class="pick-edit stem-edit" rows="3" spellcheck="false"></textarea>
      <label class="mini-label">我的作答</label>
      <textarea class="pick-edit ans-edit" rows="2" spellcheck="false"></textarea>
      <div class="row-inline" style="margin:8px 0 0">
        <button type="button" class="btn btn-sm stem-reset">还原识别结果</button>
        <span class="hint">改完切到「渲染」核对公式排得对不对。</span>
      </div>
    </div>`;
}

/** 绑定一题的两栏编辑器。card 里要有 [data-editor]，可选 .badge-edit。 */
function bindStemEditor(card, initial) {
  const editor = $('[data-editor]', card);
  const stemBox = $('.stem-edit', editor);
  const ansBox = $('.ans-edit', editor);
  const stemView = $('.stem-preview', editor);
  const ansView = $('.ans-preview', editor);
  const ansEmpty = $('.ans-empty', editor);
  const badge = $('.badge-edit', card);
  const base = { stem: initial.stem_md || '', answer: initial.student_answer || '' };
  let text = { ...base };
  let timer = null;

  stemBox.value = text.stem;
  ansBox.value = text.answer;

  const changed = () => text.stem.trim() !== base.stem.trim()
                     || text.answer.trim() !== base.answer.trim();

  const paint = () => {
    stemView.textContent = text.stem;
    ansView.textContent = text.answer;
    stemView.hidden = !text.stem.trim();
    ansView.hidden = !text.answer.trim();
    if (ansEmpty) ansEmpty.hidden = !!text.answer.trim();
    renderMath($('.stem-pane[data-pane="render"]', editor));
  };

  const showPane = pane => {
    $$('.seg-btn', editor).forEach(b => b.classList.toggle('active', b.dataset.pane === pane));
    $$('.stem-pane', editor).forEach(p => { p.hidden = p.dataset.pane !== pane; });
    if (pane === 'render') paint();
  };

  $$('.seg-btn', editor).forEach(btn =>
    btn.addEventListener('click', () => showPane(btn.dataset.pane)));

  const onInput = () => {
    text = { stem: stemBox.value, answer: ansBox.value };
    const dirty = changed();
    if (badge) badge.hidden = !dirty;
    card.classList.toggle('edited', dirty);
    // 边打边重排会打断输入，停手一会儿再渲染
    if (timer) clearTimeout(timer);
    timer = setTimeout(paint, 300);
  };
  stemBox.addEventListener('input', onInput);
  ansBox.addEventListener('input', onInput);

  $('.stem-reset', editor).addEventListener('click', () => {
    text = { ...base };
    stemBox.value = text.stem;
    ansBox.value = text.answer;
    if (badge) badge.hidden = true;
    card.classList.remove('edited');
    showPane('render');
  });

  paint();
  return {
    values: () => ({ stem_md: text.stem.trim(), student_answer: text.answer.trim() }),
    changed,
    showPane,
  };
}

/** 多题勾选界面：一页试卷里往往有多道题，不能一股脑全录进错题本。 */
function renderPick(data) {
  const box = $('#resultBody');
  $('#resultEmpty').hidden = true;
  box.hidden = false;
  // 作答只显示模型真正识别到的结果：
  // 左侧表单里那个「我的作答」是给单题用的，不能套到每道题头上
  // （一张卷子 13 道题全都写成同一句话，看起来就像坏了）。
  // 没识别到就写「未识别」，学生可以在「原文」栏自己补。
  const originals = data.problems.map(p => ({
    stem_md: p.stem_md || '',
    student_answer: (p.student_answer || '').trim(),
  }));
  box.innerHTML = `
    <div class="q-head">
      <span class="q-id">识别到 ${data.problem_count} 道题 · ${esc(data.ocr_engine || '')}</span>
      <span class="badge badge-warn">待选择</span>
    </div>
    <div class="q-body">
      <p class="hint">这张图里有多道题。<strong>只勾选你这次要记录的</strong>，其余的不会保存。
        全选会把整页都录进错题本，通常不是你想要的。</p>
      <p class="hint">识别不准的可以先改：每题右上有 <strong>渲染 / 原文</strong> 两栏，
        到「原文」里改文字，切回「渲染」核对公式排版。</p>
      ${(data.warnings || []).map(w => `<p class="hint">⚠️ ${esc(w)}</p>`).join('')}
      <div class="row mt">
        <button class="btn btn-sm" id="pickAll">全选</button>
        <button class="btn btn-sm" id="pickNone">全不选</button>
      </div>
      <div class="stack mt">
        ${data.problems.map(p => `
          <div class="pick-card" data-i="${p.index}">
            <div class="pick-head">
              <label class="pick-check">
                <input type="checkbox" class="pickBox" value="${p.index}">
                <strong>${esc(p.label || ('第' + (p.index + 1) + '道'))}</strong>
              </label>
              ${p.page ? `<span class="badge">第 ${p.page} 页</span>` : ''}
              <span class="badge">${esc(QTYPE_CN[p.question_type] || p.question_type)}</span>
              <span class="badge">置信度 ${Math.round((p.confidence || 0) * 100)}%</span>
              <span class="badge badge-edit" hidden>已修改</span>
            </div>
            <div data-editor>${stemEditorHTML()}</div>
          </div>`).join('')}
      </div>
      <button id="pickSave" class="btn btn-primary btn-block mt">记录选中的题</button>
    </div>`;

  const editors = new Map();
  $$('.pick-card', box).forEach(card => {
    const index = Number(card.dataset.i);
    editors.set(index, bindStemEditor(card, originals[index] || {}));
  });
  $('#pickAll').addEventListener('click', () => $$('.pickBox', box).forEach(c => { c.checked = true; }));
  $('#pickNone').addEventListener('click', () => $$('.pickBox', box).forEach(c => { c.checked = false; }));
  $('#pickSave').addEventListener('click', async () => {
    const ids = $$('.pickBox', box).filter(c => c.checked).map(c => Number(c.value));
    if (!ids.length) { toast('请至少勾选一道题'); return; }
    // 只把真改过的题发上去；没动的沿用服务端缓存的识别结果
    const edits = {};
    ids.forEach(i => {
      const editor = editors.get(i);
      if (editor && editor.changed()) edits[i] = editor.values();
    });
    const saveBtn = $('#pickSave');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner"></span>入库中…';
    // 走 runCommit：复用服务端已缓存的识别结果，不再重新上传加识别
    await runCommit(ids, edits);
  });
}

function kpChips(kps, { clickable = true } = {}) {
  if (!kps || !kps.length) return '<p class="hint">未匹配到知识点。</p>';
  return '<div class="kp-chips">' + kps.map(k => {
    const role = k.role || 'primary';
    const cls = role === 'primary' ? 'role-primary' : (role === 'root_cause' ? 'role-root' : 'role-secondary');
    const score = typeof k.score === 'number' ? `<span class="score">${(k.score * 100).toFixed(0)}%</span>` : '';
    return `<span class="kp-chip ${cls}" ${clickable ? `data-kp="${esc(k.kp_id)}"` : ''}
      title="${esc(k.evidence || '')}">${esc(k.name || k.kp_id)}
      <span class="id">${esc(k.kp_id)}</span>${score}</span>`;
  }).join('') + '</div>';
}

function bindKpChips(root) {
  $$('[data-kp]', root).forEach(chip => chip.addEventListener('click', () => {
    const kpId = chip.dataset.kp;
    $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'graph'));
    $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-graph'));
    loadKpDetail(kpId);
  }));
}

function renderResult(data) {
  const box = $('#resultBody');
  $('#resultEmpty').hidden = true;
  box.hidden = false;
  const warns = (data.warnings || []).map(w => `<p class="hint">⚠️ ${esc(w)}</p>`).join('');
  const analysis = data.analysis;

  box.innerHTML = `
    <div class="q-head">
      <span class="q-id">${esc(data.question_id)}</span>
      <span class="badge badge-warn">待确认</span>
    </div>
    <div class="q-body">
      ${warns}
      ${assetBlock(data.asset_path, '原题图片')}
      <div class="q-section-title">原题干
        <button type="button" class="btn btn-sm" id="editStem">✎ 修改</button>
      </div>
      <div class="q-stem" data-math id="stemView">${esc(data.stem_md)}</div>
      <div id="ansWrap" ${data.student_answer ? '' : 'hidden'}>
        <div class="q-section-title">你的作答</div>
        <div class="q-analysis" data-math id="ansView">${esc(data.student_answer || '')}</div>
      </div>
      <div id="stemEditor" hidden></div>

      <div class="q-section-title">涉及知识点（${(data.linked || []).length}）</div>
      ${kpChips(data.linked)}
      ${(data.out_of_graph || []).length ? `<p class="hint">图谱外候选：
        ${data.out_of_graph.map(o => esc(o.candidate)).join('、')}（已记录原文，未落 ID）</p>` : ''}
      <div id="confirmArea"></div>

      ${analysis ? `
        <div class="q-section-title">AI 解答</div>
        <div class="q-answer" data-math>${mdLite(analysis.ai_answer || '（未生成）')}</div>
        <div class="q-section-title">AI 分步解析</div>
        <div class="q-analysis" data-math>${mdLite(analysis.ai_analysis)}</div>
        ${analysis.error_detail ? `<div class="q-section-title">错因分析</div>
          <div class="q-error" data-math><strong>${esc(errorTypeCn(analysis.error_type))}</strong>：
          ${mdLite(analysis.error_detail)}</div>` : ''}
        ${analysis.hint ? `<p class="hint mt">💡 ${mdLite(analysis.hint)}</p>` : ''}
        ${renderRootCauses(analysis.root_causes)}
      ` : '<p class="hint mt">未生成 AI 解析（可在错题本中点击「重新分析」）。</p>'}
    </div>`;
  renderConfirm(data);
  renderMath(box);
  bindKpChips(box);
  bindStemEdit(box, data);
  $$('.q-asset', box).forEach(img => img.addEventListener('click', () => window.open(img.src, '_blank')));
}

/** 已入库题目的题干订正。
 *
 * 单题上传是直接入库的（不走勾选页），所以要留一个入口让学生改错。
 * 只改文本：知识点与已生成的解析不动 —— 解析换不换由学生到错题本点「重新分析」。
 */
function bindStemEdit(box, data) {
  const btn = $('#editStem', box);
  const holder = $('#stemEditor', box);
  if (!btn || !holder) return;
  let editor = null;

  const close = () => {
    holder.hidden = true;
    holder.innerHTML = '';          // 未保存的改动直接丢掉
    editor = null;
    btn.textContent = '✎ 修改';
  };

  btn.addEventListener('click', () => {
    if (!holder.hidden) { close(); return; }
    holder.hidden = false;
    btn.textContent = '收起';
    holder.innerHTML = `
      <div class="pick-card">
        <div data-editor>${stemEditorHTML()}</div>
        <div class="row-inline" style="margin:10px 0 0">
          <button type="button" class="btn btn-primary btn-sm" id="stemSave">保存修改</button>
          <button type="button" class="btn btn-sm" id="stemCancel">取消</button>
          <span class="hint">已有解析仍是按原题干生成的，需要时到错题本点「重新分析」。</span>
        </div>
      </div>`;
    const card = holder.firstElementChild;
    editor = bindStemEditor(card, { stem_md: data.stem_md || '',
                                    student_answer: data.student_answer || '' });
    $('#stemCancel', holder).addEventListener('click', close);
    $('#stemSave', holder).addEventListener('click', async () => {
      const saveBtn = $('#stemSave', holder);
      const values = editor.values();
      if (!values.stem_md) { toast('题干不能改成空的'); return; }
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span class="spinner"></span>保存中…';
      try {
        await api(`/api/wrongbook/${encodeURIComponent(data.question_id)}/stem`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(values),
        });
        Object.assign(data, values);                  // 后续重渲染用新文本
        $('#stemView', box).textContent = values.stem_md;
        renderMath($('#stemView', box));
        $('#ansView', box).textContent = values.student_answer;
        $('#ansWrap', box).hidden = !values.student_answer;
        renderMath($('#ansView', box));
        close();
        toast('题干已保存');
      } catch (e) {
        toast('保存失败：' + e.message, 5000);
        saveBtn.disabled = false;
        saveBtn.textContent = '保存修改';
      }
    });
  });
}

function renderConfirm(data) {
  const needs = data.needs_confirm || [];
  if (!needs.length) return;
  const area = $('#confirmArea');
  area.innerHTML = `
    <div class="q-section-title">需要你确认的知识点（${needs.length}）</div>
    <p class="hint">LLM 给的是候选名称，下面是从图谱里按相似度找出的真实知识点，请选择正确的。</p>
    ${needs.map((item, idx) => `
      <div style="margin-bottom:12px">
        <p class="hint">候选「<strong>${esc(item.candidate)}</strong>」最接近：</p>
        ${(item.options || []).map((opt, oi) => `
          <label class="confirm-opt">
            <input type="radio" name="cf-${idx}" value="${esc(opt.kp_id)}"
              data-role="${esc(item.role)}" data-candidate="${esc(item.candidate)}"
              ${oi === 0 ? 'checked' : ''}>
            <span>${esc(opt.name)} <span class="q-id">${esc(opt.kp_id)}</span>
              · ${esc(opt.module_name || '')} ${esc(opt.section_name || '')}
              <span class="score">${(opt.score * 100).toFixed(0)}%</span></span>
          </label>`).join('')}
        <label class="confirm-opt">
          <input type="radio" name="cf-${idx}" value="__skip__" data-role="${esc(item.role)}"
            data-candidate="${esc(item.candidate)}">
          <span>都不是（跳过，不记录该知识点）</span>
        </label>
      </div>`).join('')}
    <button id="confirmBtn" class="btn btn-primary mt">保存确认结果</button>`;

  $('#confirmBtn').addEventListener('click', async () => {
    const chosen = [], drop = [];
    needs.forEach((item, idx) => {
      const picked = $(`input[name="cf-${idx}"]:checked`);
      if (!picked) return;
      if (picked.value === '__skip__') drop.push(null);
      else chosen.push({ kp_id: picked.value, role: picked.dataset.role,
                         candidate_name: picked.dataset.candidate, score: 1.0 });
    });
    try {
      await api(`/api/wrongbook/${data.question_id}/confirm`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chosen, drop: [] }) });
      toast('已保存确认结果');
      refreshQuestion(data.question_id);
    } catch (e) { toast('保存失败：' + e.message); }
  });
}

function renderRootCauses(roots) {
  if (!roots || !roots.length) return '';
  return `<div class="q-section-title">根因候选（沿前置链上溯）</div>
    <p class="hint">这些是更基础的知识点，如果你也忘了，建议先补它们。</p>
    <div class="table-wrap"><table>
      <thead><tr><th>知识点</th><th>深度</th><th>被依赖</th><th>难度</th><th>依据</th></tr></thead>
      <tbody>${roots.map(r => `<tr>
        <td><span class="kp-chip role-root" data-kp="${esc(r.kp_id)}">${esc(r.name)}
          <span class="id">${esc(r.kp_id)}</span></span></td>
        <td class="num">${r.depth}</td><td class="num">${r.depended_by_count}</td>
        <td>${esc(r.difficulty_cn || '')}</td>
        <td style="white-space:normal">${esc(r.reason || '')}</td></tr>`).join('')}
      </tbody></table></div>`;
}

function errorTypeCn(type) {
  return { concept: '概念不清', method: '方法不会', calc: '计算失误',
           read: '审题失误', unknown: '未归类' }[type] || '未归类';
}

async function refreshQuestion(qid) {
  const q = await api('/api/wrongbook/' + qid);
  renderResult({
    question_id: q.id, asset_path: q.asset_path, stem_md: q.stem_md,
    student_answer: q.student_answer, linked: q.kps, out_of_graph: [],
    needs_confirm: [], warnings: [], analysis: {
      ai_answer: q.ai_answer, ai_analysis: q.ai_analysis,
      error_type: (q.kps[0] || {}).error_type || 'unknown',
      error_detail: '', root_causes: q.root_causes,
    },
  });
}

/* ------------------------------------------------------------------ 错题本 */
const ERR_TYPE_CN = { concept: '概念不清', method: '方法不会', calc: '计算失误',
                      read: '审题失误', unknown: '未归类' };

/** 侧边栏筛选状态。日期用 YYYY-MM-DD（与后端 date() 比较的格式一致）。 */
function bookFilters() {
  return {
    error_types: $$('.errType').filter(c => c.checked).map(c => c.value),
    date_from: $('#bookFrom').value || '',
    date_to: $('#bookTo').value || '',
    sort: $('#bookSort').value,
  };
}

function setBookDates(from, to) {
  $('#bookFrom').value = from;
  $('#bookTo').value = to;
}

function localDate(d) {
  // toISOString() 是 UTC，在中国会差一天，所以自己拼
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function loadBook() {
  const list = $('#bookList');
  const q = $('#bookSearch').value.trim();
  const f = bookFilters();
  list.innerHTML = '<div class="card empty">加载中…</div>';
  try {
    let url = `/api/wrongbook?student_id=${encodeURIComponent(state.student)}`;
    if (f.error_types.length) url += `&error_types=${encodeURIComponent(f.error_types.join(','))}`;
    if (f.date_from) url += `&date_from=${f.date_from}`;
    if (f.date_to) url += `&date_to=${f.date_to}`;
    if (f.sort) url += `&sort=${f.sort}`;
    let data = await api(url);
    let items = data.items || [];
    if (q) {
      // 按知识点搜索走图谱检索：先找出相关知识点，再筛题目（服务端不做这种语义筛选）
      const hit = await api('/api/link?q=' + encodeURIComponent(q));
      const ids = new Set((hit.results || []).map(r => r.kp.id));
      items = items.filter(it => (it.kps || []).some(k => ids.has(k.kp_id)));
    }
    const ov = data.overview || {};
    const filtered = q ? items.length : (data.matched || items.length);
    $('#bookStats').innerHTML = `
      <div class="stat"><div class="num">${ov.question_count || 0}</div><div class="lbl">已录入题目</div></div>
      <div class="stat"><div class="num">${ov.wrong_times || 0}</div><div class="lbl">错误次数</div></div>
      <div class="stat"><div class="num">${ov.resolved_count || 0}</div><div class="lbl">已订正</div></div>
      <div class="stat"><div class="num">${filtered}</div><div class="lbl">当前筛选</div></div>`;
    // 侧边栏每个错因后面的计数（全量统计，不受当前筛选影响）
    const byType = ov.error_by_type || {};
    $$('[data-cnt]').forEach(el => {
      const n = byType[el.dataset.cnt] || 0;
      el.textContent = n ? `(${n})` : '';
    });
    list.innerHTML = items.length ? items.map(questionCard).join('')
      : `<div class="card empty">${filtered || ov.question_count
          ? '当前筛选下没有错题，换个条件或点「重置筛选」。'
          : '还没有错题。到「① 录入错题」上传第一道吧。'}</div>`;
    renderMath(list);
    bindKpChips(list);
    bindQuestionActions(list);
  } catch (e) {
    list.innerHTML = `<div class="card empty">加载失败：${esc(e.message)}</div>`;
  }
}

function questionCard(q) {
  const kps = (q.kps || []).map(k => ({ kp_id: k.kp_id, name: k.name || k.kp_id,
    role: k.role, score: k.confidence, evidence: k.evidence }));
  // 错因与出错次数由 /api/wrongbook 一并返回（wrong_records 的聚合）
  const errTags = (q.error_types || []).filter(Boolean)
    .map(t => `<span class="badge">${esc(ERR_TYPE_CN[t] || t)}</span>`).join('');
  const meta = [
    errTags,
    q.wrong_times > 1 ? `<span class="badge badge-warn">错 ${q.wrong_times} 次</span>` : '',
    q.last_wrong ? `<span class="badge">最近出错 ${esc(String(q.last_wrong).slice(0, 10))}</span>` : '',
  ].join('');
  return `<div class="q-card">
    <div class="q-head">
      <span class="q-id">${esc(q.id)} · ${esc(String(q.created_at || '').slice(0, 16))} · ${esc(q.source_name || '')}</span>
      <span>
        <button class="btn btn-sm" data-ask="${esc(q.id)}">问 AI</button>
        <button class="btn btn-sm btn-ghost" data-analyze="${esc(q.id)}">重新分析</button>
        <button class="btn btn-sm btn-ghost" data-resolve="${esc(q.id)}">标记已订正</button>
        <button class="btn btn-sm btn-ghost" data-delete="${esc(q.id)}">删除</button>
      </span>
    </div>
    <div class="q-body">
      ${meta ? `<div class="q-meta">${meta}</div>` : ''}
      ${assetBlock(q.asset_path)}
      <div class="q-stem" data-math>${esc(q.stem_md || '')}</div>
      <div class="q-section-title">知识点</div>
      ${kpChips(kps)}
      ${q.student_answer ? `<div class="q-section-title">学生的作答</div>
        <div class="q-analysis" data-math>${esc(q.student_answer)}</div>` : ''}
      ${q.ai_answer ? `<div class="q-section-title">AI 解答</div>
        <div class="q-answer" data-math>${mdLite(q.ai_answer)}</div>` : ''}
      ${q.ai_analysis ? `<div class="q-section-title">AI 解析</div>
        <div class="q-analysis" data-math>${mdLite(q.ai_analysis)}</div>` : ''}
    </div>
  </div>`;
}

function bindQuestionActions(root) {
  $$('[data-ask]', root).forEach(b => b.addEventListener('click', async () => {
    // 打开右侧 AI 侧边栏并把这道题带上（题目数据现取，保证是最新的题干）
    openChatSidebar();
    await loadChatTab();
    await pickChatQuestion(b.dataset.ask);
    $('#chatText').focus();
  }));
  $$('[data-analyze]', root).forEach(b => b.addEventListener('click', async () => {
    b.disabled = true; b.innerHTML = '<span class="spinner"></span>分析中';
    try { await api(`/api/wrongbook/${b.dataset.analyze}/analyze`, { method: 'POST' }); toast('解析已更新'); loadBook(); }
    catch (e) { toast('失败：' + e.message); b.disabled = false; b.textContent = '重新分析'; }
  }));
  $$('[data-resolve]', root).forEach(b => b.addEventListener('click', async () => {
    try { await api(`/api/wrongbook/${b.dataset.resolve}/resolve`, { method: 'POST' }); toast('已标记订正'); loadBook(); }
    catch (e) { toast('失败：' + e.message); }
  }));
  $$('[data-delete]', root).forEach(b => b.addEventListener('click', async () => {
    if (!confirm('确定删除这道错题吗？')) return;
    try { await api('/api/wrongbook/' + b.dataset.delete, { method: 'DELETE' }); toast('已删除'); loadBook(); }
    catch (e) { toast('失败：' + e.message); }
  }));
  $$('.q-asset', root).forEach(img => img.addEventListener('click', () => window.open(img.src, '_blank')));
}

$('#bookRefresh').addEventListener('click', loadBook);
$('#bookSearch').addEventListener('keydown', e => { if (e.key === 'Enter') loadBook(); });
$$('.errType').forEach(c => c.addEventListener('change', loadBook));
$('#bookSort').addEventListener('change', loadBook);
$('#bookFrom').addEventListener('change', () => {
  $$('.chip').forEach(c => c.classList.remove('active'));
  loadBook();
});
$('#bookTo').addEventListener('change', () => {
  $$('.chip').forEach(c => c.classList.remove('active'));
  loadBook();
});

// 快捷日期：算好起止再填进两个 date 框，学生也能手动改
$$('.chip[data-range]').forEach(btn => btn.addEventListener('click', () => {
  const kind = btn.dataset.range;
  $$('.chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  if (kind === 'all') {
    setBookDates('', '');
  } else if (kind === 'month') {
    const now = new Date();
    setBookDates(localDate(new Date(now.getFullYear(), now.getMonth(), 1)), localDate(now));
  } else {
    const days = Number(kind);
    const now = new Date();
    const from = new Date(now.getTime() - (days - 1) * 86400000);
    setBookDates(localDate(from), localDate(now));
  }
  loadBook();
}));

$('#bookResetFilters').addEventListener('click', () => {
  $$('.errType').forEach(c => { c.checked = false; });
  $('#bookSort').value = 'newest';
  $('#bookSearch').value = '';
  setBookDates('', '');
  loadBook();
});

/* ------------------------------------------------------------------ AI 对话 */
/* 两道流程：/api/chat/draft 先答一遍，/api/chat/verify 再复核一遍。
   界面上把两遍都摆出来（初稿可展开对比），学生能看见「老师改了什么」。

   界面形态：不再是独立的「⑥ AI 对话」标签页，而是仿 VS Code Copilot 的
   右侧可折叠侧边栏 —— 左侧导航底部的「💬 AI 对话」按钮（或 Ctrl+I）开合，
   打开时大屏会把主内容推开，收起后完全不占地方；任意错题卡片上的「问 AI」
   都能直接唤起它并把题目带进来。 */
const chatState = { question: null, history: [], busy: false, questions: [] };

const chatSidebar = $('#chatSidebar');

function setChatOpen(open) {
  document.body.classList.toggle('chat-open', open);
  chatSidebar.classList.toggle('open', open);
  chatSidebar.inert = !open;          // 收起时不让 Tab 键跑进看不见的面板
  if (open) {
    loadChatTab();
    requestAnimationFrame(() => $('#chatText').focus());
  }
  // 主内容被推开/放回后，图谱画布的像素尺寸得跟着重算
  setTimeout(() => { if (state.chart) state.chart.resize(); }, 340);
}

function openChatSidebar() { setChatOpen(true); }
function closeChatSidebar() { setChatOpen(false); }

chatSidebar.addEventListener('transitionend', e => {
  if (e.propertyName === 'transform' && state.chart) state.chart.resize();
});

function chatAttachHTML(q) {
  if (!q) return '';
  return `<div class="chat-attached">
      <span class="q-id">${esc(q.id)}</span>
      <span class="chat-attached-stem">${esc((q.stem_md || '').slice(0, 80))}</span>
      <button type="button" class="btn btn-sm btn-ghost" id="chatDetach">移除</button>
    </div>`;
}

function renderChatAttach() {
  const box = $('#chatAttach');
  box.hidden = !chatState.question;
  box.innerHTML = chatAttachHTML(chatState.question);
  const btn = $('#chatDetach');
  if (btn) btn.addEventListener('click', () => setChatQuestion(null));
}

function setChatQuestion(q) {
  chatState.question = q || null;
  renderChatAttach();
  const btn = $('#chatPickBtn');
  btn.classList.toggle('has-question', !!q);
  btn.title = q ? `当前引用错题：${q.id}（点击更换）` : '带一道错题来问';
}

/** 一行摘要：优先用入库时存的 stem_plain（本来就去掉了 LaTeX）；
 * 老记录没有就用正则粗略扒一遍 —— 下拉里满屏 $\frac{}{} 实在没法认。 */
function stemPreview(text, n = 64) {
  let t = String(text || '')
    .replace(/\$+([^$]*)\$+/g, '$1')
    .replace(/\\[a-zA-Z]+/g, ' ')
    .replace(/[{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return t.length > n ? t.slice(0, n) + '…' : t;
}

function chatPickRow(q) {
  const errs = (q.error_types || []).filter(Boolean)
    .map(t => `<span class="badge">${esc(ERR_TYPE_CN[t] || t)}</span>`).join('');
  const kps = (q.kps || []).slice(0, 2).map(k => esc(k.name || k.kp_id)).join('、');
  return `<button type="button" class="pick-row" data-id="${esc(q.id)}">
      <div class="pick-row-stem">${esc(q.stem_plain || stemPreview(q.stem_md))}</div>
      <div class="pick-row-meta">
        <span class="q-id">${esc(q.id)}</span>
        <span>${esc(String(q.created_at || '').slice(0, 10))}</span>
        ${errs}${q.wrong_times > 1 ? `<span class="badge badge-warn">错 ${q.wrong_times} 次</span>` : ''}
        ${kps ? `<span class="pick-row-kp">${kps}</span>` : ''}
      </div>
    </button>`;
}

function renderChatPicker() {
  const kw = ($('#chatPickSearch').value || '').trim().toLowerCase();
  const all = chatState.questions || [];
  // 按题干、题号、知识点名都能搜 —— 学生记得的往往是「三角」而不是哪一条 ID
  const hit = all.filter(q => {
    if (!kw) return true;
    const hay = [q.id, q.stem_plain, q.stem_md,
                 (q.kps || []).map(k => [k.name, k.kp_id].join(' ')).join(' ')]
      .join(' ').toLowerCase();
    return hay.includes(kw);
  });
  const list = $('#chatPickList');
  if (!all.length) { list.innerHTML = '<p class="hint">还没有错题。</p>'; return; }
  list.innerHTML = hit.length
    ? hit.slice(0, 40).map(chatPickRow).join('')
      + (hit.length > 40 ? `<p class="hint">只显示了前 40 条，输入关键词再缩小范围。</p>` : '')
    : '<p class="hint">没找到匹配的错题，换个关键词试试。</p>';
  $$('.pick-row', list).forEach(row => row.addEventListener('click', () => {
    const q = chatState.questions.find(x => x.id === row.dataset.id);
    if (q) { setChatQuestion(q); $('#chatPicker').hidden = true; $('#chatText').focus(); }
  }));
}

function openChat(questionId) {
  openChatSidebar();
  loadChatTab().then(() => { if (questionId) pickChatQuestion(questionId); });
}

async function loadChatTab() {
  renderChatAttach();
  if (chatState.questions.length) { renderChatPicker(); return; }
  try {
    const data = await api(`/api/wrongbook?student_id=${encodeURIComponent(state.student)}&limit=200`);
    chatState.questions = data.items || [];
    renderChatPicker();
  } catch (e) {
    $('#chatMsg').textContent = '错题列表加载失败：' + e.message;
  }
}

function pickChatQuestion(questionId) {
  const q = chatState.questions.find(x => x.id === questionId);
  if (q) return setChatQuestion(q);
  // 列表里没有（比如刚入库的题）就单独取一次
  return api('/api/wrongbook/' + encodeURIComponent(questionId))
    .then(item => setChatQuestion(item))
    .catch(e => toast('取题失败：' + e.message));
}

function chatBubble(html, cls = '') {
  const log = $('#chatLog');
  const el = document.createElement('div');
  el.className = 'chat-msg ' + cls;
  el.innerHTML = html;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function chatStatus(el, text) {
  let box = el.querySelector('.chat-status');
  if (!box) {
    el.insertAdjacentHTML('beforeend', '<div class="chat-status"></div>');
    box = el.querySelector('.chat-status');
  }
  box.innerHTML = `<span class="spinner"></span>${esc(text)}`;
}

function kpChipsBrief(kps) {
  if (!kps || !kps.length) return '<span class="hint">没有引用图谱知识点（这题还没对齐，或是纯概念提问）。</span>';
  return '<div class="kp-chips">' + kps.map(k => `<span class="kp-chip" data-kp="${esc(k.kp_id)}"
      title="${esc(k.statement || '')}">${esc(k.name || k.kp_id)}<span class="id">${esc(k.kp_id)}</span>${k.role === 'candidate' ? '<span class="score">现查</span>' : ''}</span>`).join('') + '</div>';
}

/** 侧栏展示「这次真正引用到的」知识点：候选清单可能有 3~8 个，全列会看不出重点。 */
function renderChatKps(kps, used) {
  const usedSet = new Set(used || []);
  const hit = (kps || []).filter(k => usedSet.has(k.kp_id));
  const list = hit.length ? hit : (kps || []).slice(0, 3);
  $('#chatKps').innerHTML = kpChipsBrief(list) + ((kps || []).length > list.length
    ? `<p class="hint" style="margin-top:6px">本题对齐到 ${kps.length} 个知识点，本次讲解引用了 ${list.length} 个。</p>`
    : '');
  bindKpChips($('#chatKps'));
}

/** 复核结论 → 徽标 + 意见列表。 */
function reviewHTML(review, scopeHits) {
  const map = {
    ok: ['badge-ok', '✓ 复核通过'],
    fixed: ['badge-warn', '已修订'],
    reject: ['badge-warn', '未能在高中范围内给出答案'],
  };
  const [cls, label] = map[review.verdict] || map.fixed;
  const conf = review.confidence ? `<span class="badge">置信度 ${review.confidence.toFixed(2)}</span>` : '';
  const issues = (review.issues || []).map(i => `<li><strong>${esc(i.type)}</strong>：${esc(i.detail)}
      ${i.fix ? `<br><span class="hint">改法：${esc(i.fix)}</span>` : ''}</li>`).join('');
  const warn = (scopeHits && scopeHits.length)
    ? `<p class="hint chat-warn">⚠️ 解答里用到了「${esc(scopeHits.join('、'))}」，这属于高中范围外的方法，
        建议对照课内解法再确认一遍。</p>`
    : '';
  return `${warn}<div class="chat-review">
      <span class="badge ${cls}">${label}</span>${conf}
      ${issues ? `<details><summary>复核意见（${review.issues.length} 条）</summary><ul>${issues}</ul></details>`
               : '<span class="hint">没有发现问题。</span>'}
    </div>`;
}

async function sendChat() {
  if (chatState.busy) return;
  const text = $('#chatText').value.trim();
  const question = chatState.question;
  if (!text && !question) { toast('先输入问题，或点 📎 带一道错题来问'); return; }
  const message = text || '这道题我不会，请讲一下怎么想。';

  chatState.busy = true;
  $('#chatSend').disabled = true;
  $('#chatMsg').textContent = '';
  $('#chatText').value = '';
  chatBubble(`${question ? `<div class="chat-q">${esc((question.stem_md || '').slice(0, 120))}</div>` : ''}
    <div class="bubble">${esc(message)}</div>`, 'user');
  const el = chatBubble('', 'ai');
  chatStatus(el, '正在解答（第一遍）…');

  const payload = { message, question_id: question ? question.id : '', history: chatState.history };
  let draft = null;
  try {
    const res = await postJson('/api/chat/draft', payload);
    draft = res.draft;
    renderChatKps(res.kps, draft.kp_used);
    el.innerHTML = `<div class="chat-answer" data-math>${mdLite(draft.answer_md)}</div>`;
    renderMath(el);
    chatStatus(el, '正在复核（第二遍）…');
    chatState.history.push({ role: 'user', content: message });
  } catch (e) {
    el.innerHTML = `<div class="chat-error">解答失败：${esc(e.message)}</div>`;
    chatState.busy = false;
    $('#chatSend').disabled = false;
    return;
  }

  try {
    const res = await postJson('/api/chat/verify', { ...payload, draft });
    const review = res.review;
    const final = review.answer_md || draft.answer_md;
    el.innerHTML = `
      <div class="chat-answer" data-math>${mdLite(final)}</div>
      ${reviewHTML(review, res.scope_hits)}
      ${(review.verdict === 'fixed' && final !== draft.answer_md)
        ? `<details class="chat-draft"><summary>看看第一遍写了什么（复核改动前）</summary>
             <div data-math>${mdLite(draft.answer_md)}</div></details>` : ''}
      ${review.follow_up ? `<p class="hint">💬 ${mdLite(review.follow_up)}</p>` : ''}
      ${draft.follow_up ? '' : ''}`;
    renderMath(el);
    chatState.history.push({ role: 'assistant', content: final.slice(0, 1500) });
    if (review.scope_note || draft.scope_note) {
      $('#chatMsg').textContent = '🔎 ' + (review.scope_note || draft.scope_note);
    }
  } catch (e) {
    // 复核失败不该把第一遍的解答也弄丢：留着初稿，标明未复核
    el.insertAdjacentHTML('beforeend',
      `<div class="chat-error">复核失败：${esc(e.message)}（上面是第一遍解答，未经复核）</div>`);
    chatState.history.push({ role: 'assistant', content: (draft.answer_md || '').slice(0, 1500) });
  }
  chatState.busy = false;
  $('#chatSend').disabled = false;
  const log = $('#chatLog');
  log.scrollTop = log.scrollHeight;
}

$('#chatSend').addEventListener('click', sendChat);
$('#chatText').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$('#chatPickBtn').addEventListener('click', () => {
  const box = $('#chatPicker');
  box.hidden = !box.hidden;
  if (!box.hidden) { renderChatPicker(); $('#chatPickSearch').focus(); }
});
$('#chatPickSearch').addEventListener('input', renderChatPicker);
$('#chatPickSearch').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const first = $('.pick-row', $('#chatPickList'));   // 回车＝选中第一条，省得再点一下
  if (first) first.click();
});
$('#chatClear').addEventListener('click', () => {
  chatState.history = [];
  $('#chatLog').innerHTML = `<div class="chat-hint card"><h2>问 AI</h2>
    <p class="hint">对话已清空。可以只问概念，也可以先带一道错题过来（点 📎 选错题）。</p></div>`;
  $('#chatKps').innerHTML = '还没提问。';
  $('#chatMsg').textContent = '';
});

$('#chatOpenBtn').addEventListener('click', openChatSidebar);
$('#chatCollapse').addEventListener('click', closeChatSidebar);
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey
      && e.key.toLowerCase() === 'i') {
    e.preventDefault();            // 不让浏览器书签/其它默认行为抢走
    setChatOpen(!chatSidebar.classList.contains('open'));
  }
});

/* ------------------------------------------------------------------ 热度 */
async function loadHeat() {
  const module = $('#heatModule').value;
  const onlyWrong = $('#heatOnlyWrong').checked;
  try {
    const [heat, summary, plan] = await Promise.all([
      api(`/api/heatmap?student_id=${encodeURIComponent(state.student)}&module=${module}&only_wrong=${onlyWrong}`),
      api('/api/module-summary?student_id=' + encodeURIComponent(state.student)),
      api('/api/review-plan?student_id=' + encodeURIComponent(state.student) + '&limit=8'),
    ]);
    state.heat = heat.items;
    $('#heatLegend').innerHTML = ['无', '低', '中', '高', '极高'].map((label, lv) => {
      const color = HEAT_COLORS[lv];
      const count = (heat.summary.by_level || {})[String(lv)] || 0;
      return `<span><i class="heat-dot" style="background:${color}"></i>${label}（${count}）</span>`;
    }).join('') + `<span>共 ${heat.summary.total_kp} 个知识点，其中 ${heat.summary.wrong_kp} 个有过错题</span>`;

    const maxWrong = heat.summary.max_wrong || 1;
    $('#moduleSummary').innerHTML = '<div class="card"><h2>模块薄弱概览</h2><div class="table-wrap"><table>' +
      '<thead><tr><th>模块</th><th>知识点</th><th>有过错题</th><th>错题数</th><th>占比</th><th>最薄弱知识点</th></tr></thead><tbody>' +
      summary.items.map(m => `<tr>
        <td>${heatDot({ color: m.color })}${esc(m.module_name)}</td>
        <td class="num">${m.kp_count}</td><td class="num">${m.wrong_kp}</td>
        <td class="num">${m.wrong_count}</td>
        <td><div class="bar"><i style="width:${Math.round(m.wrong_count / Math.max(1, summary.items[0].wrong_count) * 100)}%;background:${m.color}"></i></div></td>
        <td>${m.top_kp ? esc(m.top_kp.name) + ' (' + m.top_kp.wrong_count + ')' : '—'}</td>
      </tr>`).join('') + '</tbody></table></div></div>';

    $('#heatTable tbody').innerHTML = heat.items.slice(0, 400).map(i => `<tr>
      <td><span class="kp-chip role-secondary" data-kp="${esc(i.kp_id)}">${esc(i.name)}</span></td>
      <td>${esc(i.module_name)}</td><td>${esc(i.section_no)} ${esc(i.section_name)}</td>
      <td>${esc(i.kp_type_cn)}</td><td>${esc(i.difficulty_cn)}</td>
      <td class="num">${i.depended_by_count}</td><td class="num">${i.wrong_count}</td>
      <td>${heatDot(i.heat)}${esc(i.heat.label)}</td>
      <td class="hint">${i.wrong_count ? '优先级 ' + (i.priority * 100).toFixed(0) : '—'}</td>
    </tr>`).join('');
    bindKpChips($('#heatTable'));

    $('#reviewPlan').innerHTML = plan.items.length ? plan.items.map(p => `
      <div class="card" style="box-shadow:none;border-left:3px solid ${p.heat.color}">
        <div class="q-head" style="border:none;background:none;padding:0 0 6px">
          <strong>${p.order}. ${esc(p.name)}</strong>
          <span class="badge">${heatDot(p.heat)} ${p.wrong_count} 道错题 · ${esc(p.heat.label)}</span>
        </div>
        <p class="hint">${esc(p.module_name)} · ${esc(p.section_no)} ${esc(p.section_name)} ·
          被 ${p.depended_by_count} 个知识点依赖 · 主要错因：${esc(p.dominant_error_cn)}</p>
        <p>${esc(p.action)}</p>
      </div>`).join('') : '<div class="card empty">还没有错题记录，先录入几道吧。</div>';
  } catch (e) {
    $('#heatTable tbody').innerHTML = `<tr><td colspan="9">加载失败：${esc(e.message)}</td></tr>`;
  }
}

$('#heatRefresh').addEventListener('click', loadHeat);
$('#heatOnlyWrong').addEventListener('change', loadHeat);
$('#heatModule').addEventListener('change', loadHeat);

/* ------------------------------------------------------------------ 图谱查询 */
$('#graphBtn').addEventListener('click', doGraphSearch);
$('#graphSearch').addEventListener('keydown', e => { if (e.key === 'Enter') doGraphSearch(); });

async function doGraphSearch() {
  const q = $('#graphSearch').value.trim();
  if (!q) return;
  const box = $('#graphResults');
  box.innerHTML = '<p class="empty">搜索中…</p>';
  try {
    const data = await api('/api/search?q=' + encodeURIComponent(q) + '&topk=15');
    box.innerHTML = (data.results || []).map(r => `
      <div class="card" style="box-shadow:none;cursor:pointer" data-kp="${esc(r.kp.id)}">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
          <strong>${esc(r.kp.name)}</strong>
          <span class="badge">${(r.score * 100).toFixed(0)}%</span>
        </div>
        <p class="hint">${esc(r.kp.kp_id || r.kp.id)} · ${esc(r.kp.module_name)} ·
          ${esc(r.kp.section_no)} ${esc(r.kp.section_name)} · ${esc(r.kp.kp_type_cn)}</p>
        <p class="hint">${esc((r.evidence || []).join('；'))}</p>
      </div>`).join('') || '<p class="empty">没有匹配的知识点。</p>';
    bindKpChips(box);
    const first = (data.results || [])[0];
    if (first) loadKpDetail(first.kp.id);
  } catch (e) { box.innerHTML = `<p class="empty">搜索失败：${esc(e.message)}</p>`; }
}

async function loadKpDetail(kpId) {
  const box = $('#graphDetail');
  $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-graph'));
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'graph'));
  box.className = '';
  box.innerHTML = '<p class="empty">加载中…</p>';
  try {
    const d = await api('/api/kp/' + encodeURIComponent(kpId) + '?student_id=' + encodeURIComponent(state.student));
    const kp = d.kp;
    box.innerHTML = `
      <div class="q-head" style="border:none;background:none;padding:0 0 8px">
        <strong style="font-size:16px">${esc(kp.name)}</strong>
        <span class="badge">${heatDot(d.heat)} 你的错题 ${d.wrong_count} 道</span>
      </div>
      <p class="hint">${esc(kp.id)} · ${esc(kp.module_name)} · ${esc(kp.book_name)}
        ${esc(kp.section_no)} ${esc(kp.section_name)} · ${esc(kp.kp_type_cn)} ·
        难度 ${esc(kp.difficulty_cn)} · 课标「${esc(kp.importance_cn)}」</p>
      <div class="q-section-title">规范陈述</div>
      <div data-math>${esc(kp.statement || '')}</div>
      ${kp.latex ? `<div class="q-section-title">公式</div><div data-math>$$${esc(kp.latex)}$$</div>` : ''}
      <p class="hint mt">教材来源：${esc(kp.book_name)} 第${kp.source && kp.source.chapter}章 ${esc(kp.source && kp.source.section)} 节</p>

      <div class="q-section-title">前置知识（${d.prerequisites.length}）</div>
      ${d.prerequisites.length ? kpChips(d.prerequisites.map(p => ({
        kp_id: p.kp_id, name: p.name, role: 'secondary',
        evidence: (p.strength === 'required' ? '强前置' : '弱前置') + '：' + p.reason }))) : '<p class="hint">无（是起点知识）</p>'}

      <div class="q-section-title">后续知识点（${d.successors.length}）</div>
      ${d.successors.length ? kpChips(d.successors.map(p => ({
        kp_id: p.kp_id, name: p.name, role: 'secondary', evidence: p.reason }))) : '<p class="hint">无</p>'}

      <div class="mt"><button class="btn btn-primary btn-sm" id="pathBtn">查看学习路径</button></div>
      <div id="pathArea"></div>

      ${d.questions && d.questions.length ? `<div class="q-section-title">相关错题（${d.questions.length}）</div>
        <div class="stack">${d.questions.map(questionCard).join('')}</div>` : ''}`;
    renderMath(box);
    bindKpChips(box);
    bindQuestionActions(box);
    $('#pathBtn').addEventListener('click', () => loadPath(kpId));
  } catch (e) {
    box.innerHTML = `<p class="empty">加载失败：${esc(e.message)}</p>`;
  }
}

async function loadPath(kpId) {
  const area = $('#pathArea');
  area.innerHTML = '<p class="empty">计算中…</p>';
  try {
    const p = await api('/api/path?target=' + encodeURIComponent(kpId));
    if (!p.steps || !p.steps.length) {
      area.innerHTML = '<p class="hint mt">没有未掌握的前置知识点，可以直接学习该知识点。</p>';
      return;
    }
    area.innerHTML = `<div class="q-section-title">学习路径（共 ${p.total} 步）</div>
      <ol style="padding-left:20px">${p.steps.map(s => `
        <li style="margin-bottom:9px">
          <span class="kp-chip ${s.strength === 'required' ? 'role-primary' : 'role-secondary'}" data-kp="${esc(s.kp_id)}">
            ${esc(s.name)}<span class="id">${esc(s.kp_id)}</span></span>
          <span class="badge">${esc(s.module_name)} ${esc(s.section_name)} · ${esc(s.difficulty_cn)}</span>
          ${s.unlocks_names && s.unlocks_names.length ? `<div class="hint">解锁：${esc(s.unlocks_names.join('、'))}</div>` : ''}
          ${s.reason ? `<div class="hint">理由：${esc(s.reason)}</div>` : ''}
        </li>`).join('')}</ol>`;
    bindKpChips(area);
  } catch (e) { area.innerHTML = `<p class="empty">失败：${esc(e.message)}</p>`; }
}

/* ------------------------------------------------------------------ 图谱视图 */
/* 按类型着色：不用绿色（含青绿、青），免得与「已掌插」的语义混淆 */
const TYPE_COLORS = { concept: '#4f7fd6', method: '#8e63cf', property: '#e0a63c',
                      formula: '#d9534f', theorem: '#c2559b', rule: '#6b7a99' };
const TYPE_CN = { concept: '概念', method: '方法', property: '性质',
                  formula: '公式', theorem: '定理', rule: '法则' };
/* 冷→热热力图色阶：无错题（灰）→ 低（蓝）→ 中（琥珀）→ 高（橙红）→ 极高（深红）。
   与后端 store.HEAT_LEVELS 的颜色必须一致（热度页的颜色由后端给）。 */
const HEAT_COLORS = ['#c8cdd6', '#4a90d9', '#f0b429', '#ef6c3a', '#c62828'];
const HEAT_LABELS = ['无错题', '低', '中', '高', '极高'];
const REL_CN = { RELATED_TO: '横向关联', DERIVED_FROM: '派生', PREREQUISITE_OF: '前置依赖' };

function nodeColor(node, mode) {
  if (mode === 'type') return TYPE_COLORS[node.kp_type] || '#9aa4b2';
  return HEAT_COLORS[node.wrong_count ? node.heat.level : 0];
}

/** 节点大小 = 被依赖数（图里的重要性）+ 错题数，两者叠加出「又基础又薄弱」。 */
function nodeSize(node) {
  return 9 + Math.min(24, node.depended_by_count * 2.2)
           + Math.min(14, node.wrong_count * 3);
}

function relStyle(rel) {
  if (rel === 'RELATED_TO') return { color: '#c2559b', type: 'dotted' };
  if (rel === 'DERIVED_FROM') return { color: '#4f8fd6', type: 'dashed' };
  return { color: '#aab3c2', type: 'solid' };
}

async function loadView() {
  const box = $('#graphCanvas');
  if (!box || !window.echarts) return;
  const scope = $('#viewScope').value;
  const mode = $('#viewColor').value;
  const showLabels = $('#viewLabels').checked;
  box.innerHTML = '<div class="empty">加载中…</div>';
  let url = `/api/graph?student_id=${encodeURIComponent(state.student)}`;
  if (scope.startsWith('book:')) url += '&book=' + scope.slice(5);
  else if (scope.startsWith('module:')) url += '&module=' + scope.slice(7);
  else if (scope.startsWith('focus:')) url += '&focus=' + scope.slice(6) + '&depth=2';
  try {
    const data = await api(url);
    renderGraph(box, data, mode, showLabels);
  } catch (e) {
    box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderGraph(box, data, mode, showLabels) {
  box.innerHTML = '';
  if (state.chart) { state.chart.dispose(); state.chart = null; }
  if (!data.nodes.length) {
    box.innerHTML = '<div class="empty">这个视图里没有知识点。</div>';
    return;
  }
  const chart = echarts.init(box);
  state.chart = chart;

  const dense = data.nodes.length > 260;
  const nodes = data.nodes.map(n => ({
    id: n.id, name: n.name,
    symbolSize: nodeSize(n),
    itemStyle: {
      color: nodeColor(n, mode),
      borderColor: n.wrong_count ? '#ffffff' : '#e4e7ef',
      borderWidth: n.wrong_count >= 3 ? 2.5 : 1,
      shadowBlur: n.wrong_count ? 8 : 0,
      shadowColor: 'rgba(217,43,43,.35)',
    },
    // 标签只给「有错题」和「枢纽（被依赖多）」的节点，否则 100+ 节点挤成一团，
    // 反而看不出重点
    label: {
      show: showLabels && (data.nodes.length <= 60
                           || n.wrong_count > 0 || n.depended_by_count >= 3),
      fontSize: n.wrong_count ? 11.5 : 10,
      fontWeight: n.wrong_count ? 'bold' : 'normal',
      color: n.wrong_count ? '#5a3d00' : '#4b5563',
    },
    _node: n,
  }));
  const links = data.edges.map(e => {
    const style = relStyle(e.rel);
    return {
      source: e.source, target: e.target,
      lineStyle: {
        color: style.color, type: style.type, width: 1.4, curveness: 0.08,
        opacity: e.strength === 'recommended' ? 0.35 : 0.6,
      },
      _edge: e,
    };
  });

  chart.setOption({
    animation: !dense,
    tooltip: {
      confine: true, enterable: false,
      formatter: params => {
        if (params.dataType === 'edge') {
          const e = params.data._edge;
          const label = { PREREQUISITE_OF: '前置', RELATED_TO: '关联', DERIVED_FROM: '派生' }[e.rel] || e.rel;
          return `<b>${esc(label)}</b>${e.strength ? '（' + (e.strength === 'required' ? '强' : '弱') + '）' : ''}<br>${esc(e.reason || '')}`;
        }
        const n = params.data._node;
        return `<b>${esc(n.name)}</b><br>
          <span style="opacity:.7">${esc(n.id)}</span><br>
          ${esc(n.module_name)} · ${esc(n.section_no)} ${esc(n.section_name)}<br>
          ${esc(n.kp_type_cn)} · 难度 ${esc(n.difficulty_cn)} · 课标「${esc(n.importance_cn)}」<br>
          你的错题：<b>${n.wrong_count}</b> 道（${HEAT_LABELS[n.wrong_count ? n.heat.level : 0]}）<br>
          被 ${n.depended_by_count} 个知识点依赖`;
      },
    },
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      data: nodes, links: links,
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 5,
      force: {
        repulsion: dense ? 90 : 260,
        edgeLength: dense ? [25, 90] : [60, 150],
        gravity: 0.12, friction: 0.2,
      },
      emphasis: { focus: 'adjacency', scale: 1.15,
                  label: { show: true, fontSize: 12, fontWeight: 'bold' } },
      label: { position: 'right', fontSize: 10, color: '#4b5563' },
      labelLayout: { hideOverlap: true },
      lineStyle: { color: '#c3cad6' },
    }],
  });

  chart.on('click', params => {
    if (params.dataType === 'node') loadGraphSide(params.data.id);
  });
  // 力导向布局要几秒才铺开，只调一次容易拍到还没展开、被裁掉的画面，
  // 所以分几次自动适配；用户一旦开始拖拽/缩放就立刻停止，不跟人抢控制权。
  state.userRoamed = false;
  (state.fitTimers || []).forEach(clearTimeout);
  state.fitTimers = [1200, 3000, 6000].map(ms =>
    setTimeout(() => { if (!state.userRoamed) fitGraph(chart, box); }, ms));
  chart.on('graphroam', () => { state.userRoamed = true; });
  chart.getZr().on('mousedown', () => { state.userRoamed = true; });
  if (data.nodes.length <= 40) setTimeout(() => fitGraph(chart, box), 300);

  renderViewLegend(data, mode);
}

/** 计算所有节点的包围盒，调整 center/zoom 让整张图刚好铺满画布。 */
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
    const width = box.clientWidth || 800;
    const height = box.clientHeight || 600;
    const padding = 70;
    const zoom = Math.min((width - padding) / Math.max(maxX - minX, 1),
                          (height - padding) / Math.max(maxY - minY, 1));
    chart.setOption({
      series: [{
        center: [(minX + maxX) / 2, (minY + maxY) / 2],
        zoom: Math.max(0.05, Math.min(2, zoom)),
      }],
    }, false);
  } catch (e) { /* 布局尚未就绪时忽略 */ }
}

function renderViewLegend(data, mode) {
  const box = $('#viewLegend');
  const stat = data.stats || {};
  const items = mode === 'type'
    ? Object.keys(TYPE_CN).map(k =>
        `<span><i class="heat-dot" style="background:${TYPE_COLORS[k]}"></i>${TYPE_CN[k]}</span>`)
    : HEAT_COLORS.map((c, i) =>
        `<span><i class="heat-dot" style="background:${c}"></i>${HEAT_LABELS[i]}</span>`);
  // 线色直接取 relStyle，不再另拄一份常量（两份必然会跑偏）
  const relItems = ['PREREQUISITE_OF', 'DERIVED_FROM', 'RELATED_TO'].map(rel =>
    `<span><i class="heat-dot" style="background:${relStyle(rel).color}"></i>${REL_CN[rel]}</span>`);
  box.innerHTML = items.join('') +
    '<span style="color:#c8cdd6">|</span>' + relItems.join('') +
    `<span style="margin-left:auto">${stat.node_count} 个知识点 · ${stat.edge_count} 条关系
      · 其中 ${stat.wrong_kp || 0} 个有你的错题（节点越大＝被依赖越多）</span>`;
}

async function loadGraphSide(kpId) {
  const side = $('#graphSide');
  side.innerHTML = '<p class="empty">加载中…</p>';
  try {
    const d = await api(`/api/kp/${encodeURIComponent(kpId)}?student_id=${encodeURIComponent(state.student)}`);
    const kp = d.kp;
    const prereqs = d.prerequisites.map(p => ({ kp_id: p.kp_id, name: p.name, role: 'secondary',
      evidence: (p.strength === 'required' ? '强前置' : '弱前置') + '：' + p.reason }));
    const succs = d.successors.map(p => ({ kp_id: p.kp_id, name: p.name, role: 'secondary',
      evidence: p.reason }));
    side.innerHTML = `
      <div class="q-head" style="border:none;background:none;padding:0 0 6px">
        <strong style="font-size:15px">${esc(kp.name)}</strong>
        <span class="badge">${heatDot(d.heat)} ${d.wrong_count} 道</span>
      </div>
      <p class="hint">${esc(kp.id)} · ${esc(kp.module_name)} · ${esc(kp.book_name)}
        ${esc(kp.section_no)} ${esc(kp.section_name)}</p>
      <p class="hint">${esc(kp.kp_type_cn)} · 难度 ${esc(kp.difficulty_cn)}
        · 课标「${esc(kp.importance_cn)}」· 被 ${kp.prereq_in_degree} 个知识点依赖</p>
      <div class="q-section-title">规范陈述</div>
      <div data-math style="font-size:13.5px">${esc(kp.statement || '')}</div>
      <div class="q-section-title">前置知识（${d.prerequisites.length}）</div>
      ${d.prerequisites.length ? kpChips(prereqs) : '<p class="hint">无（是起点知识）</p>'}
      <div class="q-section-title">后续知识点（${d.successors.length}）</div>
      ${d.successors.length ? kpChips(succs) : '<p class="hint">无</p>'}
      <div class="mt">
        <button class="btn btn-sm btn-primary" id="focusBtn">以它为中心展开</button>
        <button class="btn btn-sm" id="sideHeatBtn">在热度表中查看</button>
      </div>
      ${d.questions && d.questions.length ? `
        <div class="q-section-title">相关错题（${d.questions.length}）</div>
        <div class="stack">${d.questions.map(q => `
          <div style="border:1px solid var(--border);border-radius:9px;padding:10px">
            <div class="q-id">${esc(q.id)} · ${esc(q.created_at || '')}</div>
            <div data-math style="font-size:13px;margin-top:5px">${esc((q.stem_md || '').slice(0, 160))}</div>
          </div>`).join('')}</div>` : ''}`;
    renderMath(side);
    $$('[data-kp]', side).forEach(chip => chip.addEventListener('click', () => {
      loadGraphSide(chip.dataset.kp);
      focusOn(chip.dataset.kp);
    }));
    $('#focusBtn').addEventListener('click', () => focusOn(kpId));
    $('#sideHeatBtn').addEventListener('click', () => {
      $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'heat'));
      $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-heat'));
      $('#heatOnlyWrong').checked = true;
      loadHeat();
      setTimeout(() => {
        const chip = $(`#heatTable [data-kp="${kpId}"]`);
        if (chip) chip.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }, 900);
    });
  } catch (e) {
    side.innerHTML = `<p class="empty">加载失败：${esc(e.message)}</p>`;
  }
}

/** 以某知识点为中心重新布局（前后各展开 2 层）。 */
function focusOn(kpId) {
  const sel = $('#viewScope');
  const value = 'focus:' + kpId;
  if (![...sel.options].some(o => o.value === value)) {
    sel.add(new Option('焦点 · ' + kpId, value));
  }
  sel.value = value;
  loadView();
}

$('#viewRefresh').addEventListener('click', loadView);
$('#viewScope').addEventListener('change', loadView);
$('#viewColor').addEventListener('change', loadView);
$('#viewLabels').addEventListener('change', loadView);
window.addEventListener('resize', () => { if (state.chart) state.chart.resize(); });

/* ------------------------------------------------------------------ 启动 */
refreshAvatar();
boot();
