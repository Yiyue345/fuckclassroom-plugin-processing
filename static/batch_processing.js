(() => {
  const lessonList = document.querySelector('.course-media-lesson-list');
  const heading = document.querySelector('.course-lessons-heading');
  const match = window.location.pathname.match(/^\/courses\/([^/]+)\/?$/);
  if (!lessonList || !heading || !match) return;

  const courseId = decodeURIComponent(match[1]);
  const rows = Array.from(lessonList.querySelectorAll('[data-lesson-row]'));
  const eligible = rows.filter(row => row.dataset.live !== 'true' && row.dataset.lessonId);
  if (!eligible.length) return;

  const controller = new AbortController();
  const { signal } = controller;
  let enabled = false;
  let activeTask = '';
  let selected = new Set();
  let pollTimer = null;
  const taskStorageKey = 'fc:processing:batch:' + courseId;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const actions = el('div', 'processing-batch-header');
  const toggle = el('button', 'btn small', '批量选择');
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false');
  actions.append(toggle);
  heading.append(actions);

  const panel = el('section', 'processing-batch-panel hidden');
  panel.setAttribute('aria-label', '批量课次转写');
  const summary = el('div', 'processing-batch-summary');
  const counter = el('strong', '', '已选择 0 个课次');
  const note = el('span', '', '最多 30 个课次；执行过程中可在任务中心取消');
  summary.append(counter, note);
  const controls = el('div', 'processing-batch-controls');
  const selectAll = el('button', 'btn small', '全选');
  selectAll.type = 'button';
  const clear = el('button', 'btn small', '清空');
  clear.type = 'button';
  const mode = el('select', 'processing-batch-mode');
  mode.setAttribute('aria-label', '批量处理方式');
  const automatic = el('option', '', '提取文字（优先转写＋PPT）');
  automatic.value = 'extract';
  const refresh = el('option', '', '重新语音转写（强制重跑）');
  refresh.value = 'retranscribe';
  mode.append(automatic, refresh);
  const submit = el('button', 'btn primary small', '开始批量处理');
  submit.type = 'button';
  submit.disabled = true;
  controls.append(selectAll, clear, mode, submit);
  const progress = el('div', 'processing-batch-progress hidden');
  progress.setAttribute('role', 'status');
  progress.setAttribute('aria-live', 'polite');
  const message = el('div', 'processing-batch-message', '');
  const bar = el('div', 'progress');
  const fill = el('span', '');
  bar.append(fill);
  const taskLink = el('a', 'processing-batch-task-link', '查看任务日志 / 取消任务');
  taskLink.href = '/tasks';
  progress.append(message, bar, taskLink);
  panel.append(summary, controls, progress);
  heading.after(panel);

  const checkboxes = new Map();
  for (const row of eligible) {
    const wrap = el('label', 'processing-batch-checkbox hidden');
    wrap.title = '将此课次加入批量处理';
    const checkbox = el('input');
    checkbox.type = 'checkbox';
    checkbox.setAttribute('aria-label', '选择' + (row.dataset.lessonTitle || row.dataset.lessonId));
    wrap.append(checkbox, el('span', '', '选'));
    // Reuse the existing 1st grid cell; adding another grid item would
    // shift every resource/status/action column and break the host layout.
    const number = row.querySelector('.lesson-number');
    if (!number) continue;
    number.dataset.processingBatchNumber = number.textContent.trim();
    number.textContent = '';
    number.append(wrap);
    for (const event of ['click', 'pointerdown', 'keydown']) {
      wrap.addEventListener(event, e => e.stopPropagation(), { signal });
    }
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selected.add(row.dataset.lessonId);
      else selected.delete(row.dataset.lessonId);
      update();
    }, { signal });
    checkboxes.set(row.dataset.lessonId, { checkbox, wrap, number, row });
  }

  function update() {
    const usableCount = eligible.filter(row => mode.value !== 'retranscribe' || row.dataset.videoUrl).length;
    counter.textContent = '已选择 ' + selected.size + ' / ' + Math.min(usableCount, 30) + ' 个课次';
    submit.disabled = Boolean(activeTask) || !selected.size || selected.size > 30;
    submit.textContent = activeTask ? '批量任务运行中' : '开始批量处理';
    mode.disabled = Boolean(activeTask);
    selectAll.disabled = Boolean(activeTask);
    clear.disabled = Boolean(activeTask);
    for (const { checkbox, wrap, row } of checkboxes.values()) {
      const missingVideo = mode.value === 'retranscribe' && !row.dataset.videoUrl;
      checkbox.disabled = Boolean(activeTask) || missingVideo;
      wrap.title = missingVideo ? '本课次没有可用的课堂录像，无法重新转写' : '将此课次加入批量处理';
    }
  }
  function showTask() {
    panel.classList.remove('hidden');
    enabled = true;
    toggle.setAttribute('aria-expanded', 'true');
    toggle.textContent = '退出批量选择';
    for (const { wrap, number } of checkboxes.values()) {
      wrap.classList.remove('hidden');
      number.classList.add('processing-batch-number-active');
    }
  }
  toggle.addEventListener('click', () => {
    if (activeTask) { showTask(); return; }
    enabled = !enabled;
    panel.classList.toggle('hidden', !enabled);
    toggle.setAttribute('aria-expanded', String(enabled));
    toggle.textContent = enabled ? '退出批量选择' : '批量选择';
    for (const { wrap, number } of checkboxes.values()) {
      wrap.classList.toggle('hidden', !enabled);
      number.classList.toggle('processing-batch-number-active', enabled);
    }
  }, { signal });

  selectAll.addEventListener('click', () => {
    selected = new Set(eligible
      .filter(row => mode.value !== 'retranscribe' || row.dataset.videoUrl)
      .slice(0, 30)
      .map(row => row.dataset.lessonId));
    for (const [id, { checkbox }] of checkboxes) checkbox.checked = selected.has(id);
    update();
  }, { signal });
  mode.addEventListener('change', () => {
    if (mode.value === 'retranscribe') {
      for (const row of eligible) {
        if (!row.dataset.videoUrl) {
          selected.delete(row.dataset.lessonId);
          const entry = checkboxes.get(row.dataset.lessonId);
          if (entry) entry.checkbox.checked = false;
        }
      }
    }
    update();
  }, { signal });

  clear.addEventListener('click', () => {
    selected.clear();
    for (const { checkbox } of checkboxes.values()) checkbox.checked = false;
    update();
  }, { signal });

  async function api(url, options = {}) {
    const response = await fetch(url, {
      cache: 'no-store', credentials: 'same-origin', ...options,
      headers: { Accept: 'application/json', ...(options.headers || {}) },
      signal,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'HTTP ' + response.status);
    return body;
  }
  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }
  async function monitorTask() {
    if (!activeTask || signal.aborted) return;
    try {
      const task = await api('/api/tasks/' + encodeURIComponent(activeTask));
      const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
      fill.style.width = percent + '%';
      const messageText = task.detail_message || task.message || '正在处理课次';
      message.textContent = percent + '% · ' + messageText;
      if (['succeeded', 'failed', 'cancelled'].includes(task.status)) {
        const ended = activeTask;
        stopPolling();
        activeTask = '';
        try { sessionStorage.removeItem(taskStorageKey); } catch (_) {}
        if (task.status === 'succeeded') message.textContent = '批量处理完成。结果已保存，可在课次详情查看。';
        else if (task.status === 'cancelled') message.textContent = '任务已取消，已完成的课次结果保留。';
        else message.textContent = '部分课次处理失败：' + (task.error || task.message || '请查看任务日志');
        taskLink.href = '/tasks/' + encodeURIComponent(ended);
        update();
        return;
      }
    } catch (error) {
      message.textContent = '查询任务进度失败：' + error.message;
    }
    pollTimer = setTimeout(monitorTask, 1400);
  }

  submit.addEventListener('click', async () => {
    if (activeTask || !selected.size || selected.size > 30) return;
    // Send lessons in the same order as the course page, not checkbox order.
    const ids = eligible.map(row => row.dataset.lessonId).filter(id => selected.has(id));
    if (mode.value === 'retranscribe' && !window.confirm(
      '重新转写会强制使用当前模型覆盖这些课次的原转写结果，确定继续吗？'
    )) return;
    submit.disabled = true;
    progress.classList.remove('hidden');
    message.textContent = '正在提交批量任务…';
    try {
      const response = await api('/courses/' + encodeURIComponent(courseId) + '/lessons/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lesson_ids: ids, mode: mode.value }),
      });
      activeTask = response.task_id;
      if (!activeTask) throw new Error('服务器未返回任务编号');
      try { sessionStorage.setItem(taskStorageKey, activeTask); } catch (_) {}
      taskLink.href = '/tasks/' + encodeURIComponent(activeTask);
      showTask();
      update();
      monitorTask();
    } catch (error) {
      message.textContent = '无法创建批量任务：' + error.message;
      update();
    }
  }, { signal });

  try {
    const resumed = sessionStorage.getItem(taskStorageKey);
    if (resumed && /^[a-f0-9]{32}$/.test(resumed)) {
      activeTask = resumed;
      progress.classList.remove('hidden');
      taskLink.href = '/tasks/' + encodeURIComponent(resumed);
      showTask();
      update();
      monitorTask();
    }
  } catch (_) {}

  document.addEventListener('academic:page-before-swap', () => {
    stopPolling();
    controller.abort();
  }, { once: true, signal });
  update();
})();
