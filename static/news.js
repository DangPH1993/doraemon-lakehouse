(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const MAX_SAVED_TIMELINES = 10;
  const state = {
    period: 'day', selectedTimeline: null, timelinePage: 1, articlePage: 1,
    articleQuery: '', articlePageSize: 20, timelinePageSize: 20,
    user: null, options: {topics: [], entities: []}, savedTimelines: [],
    picked: new Set(), optionQuery: '', authMode: 'login'
  };
  const periodLabels = {day: 'Hôm nay', week: 'Tuần này', month: 'Tháng này'};
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const num = (value) => new Intl.NumberFormat('vi-VN').format(Number(value || 0));
  const safeHref = (value) => {
    try { const url = new URL(String(value || ''), window.location.origin); return ['http:', 'https:'].includes(url.protocol) ? esc(url.href) : '#'; }
    catch { return '#'; }
  };
  function localDateText(value, withTime = false) {
    if (!value) return 'Chưa rõ thời gian';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return new Intl.DateTimeFormat('vi-VN', withTime
      ? {timeZone:'Asia/Ho_Chi_Minh', day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'}
      : {timeZone:'Asia/Ho_Chi_Minh', day:'2-digit', month:'long', year:'numeric'}).format(date);
  }
  async function getJSON(url, options = {}) {
    const headers = {'Accept':'application/json', ...(options.body ? {'Content-Type':'application/json'} : {}), ...(options.headers || {})};
    const response = await fetch(url, {...options, headers, credentials:'same-origin'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Máy chủ trả về phản hồi không hợp lệ.'); }
    if (!response.ok) { const error = new Error(data.detail || `Yêu cầu thất bại (${response.status})`); error.status = response.status; throw error; }
    return data;
  }
  const sendJSON = (url, method, body) => getJSON(url, {method, body:JSON.stringify(body)});
  function setMessage(target, title, description, isError = false) {
    $(target).innerHTML = `<div class="message-card"><strong>${esc(title)}</strong>${esc(description || '')}${isError ? '<br><small>Vui lòng tải lại trang sau ít phút.</small>' : ''}</div>`;
  }
  function setFeedback(target, message, success = false) {
    const node = $(target);
    if (!node) return;
    node.textContent = message || '';
    node.classList.toggle('success', Boolean(success));
  }
  async function loadOverview() {
    try {
      const data = await getJSON('/api/public/news/overview');
      $('#statArticles').textContent = num(data.articles_today);
      $('#statSources').textContent = num(data.enabled_sources);
      $('#statEvents').textContent = num(data.active_event_topics);
    } catch (error) {
      ['#statArticles','#statSources','#statEvents'].forEach(id => $(id).textContent = '—');
      console.warn('Overview could not be loaded:', error);
    }
  }
  function renderTopics(items) {
    if (!items.length) {
      setMessage('#hotTopics', 'Chưa có topic sự kiện nổi bật', 'Pipeline chưa tạo FACT cho kỳ này hoặc chưa có đủ bài báo được gắn topic sự kiện.');
      return;
    }
    $('#hotTopics').innerHTML = items.map((topic, index) => {
      const growth = topic.growth_pct === null || topic.growth_pct === undefined ? '' : `${Number(topic.growth_pct) > 0 ? '+' : ''}${Number(topic.growth_pct).toLocaleString('vi-VN')}%`;
      const growthClass = Number(topic.growth_pct) < 0 ? 'down' : '';
      const selected = state.selectedTimeline && state.selectedTimeline.type === 'topic' && Number(state.selectedTimeline.id) === Number(topic.topic_id) ? ' selected' : '';
      return `<button class="topic-card${selected}" type="button" data-topic-id="${Number(topic.topic_id)}" data-topic-name="${esc(topic.topic_name)}" aria-label="Xem timeline sự kiện ${esc(topic.topic_name)}">
        <div class="topic-card-top"><span class="rank-chip">${index + 1}</span><span class="topic-category">SỰ KIỆN NỔI BẬT</span></div>
        <h3>${esc(topic.topic_name)}</h3>
        <div class="topic-card-bottom"><span class="topic-metric">▤ ${num(topic.article_count)} bài · ${num(topic.source_count)} nguồn</span><span class="topic-open">Timeline ↗</span></div>
        ${growth ? `<div class="topic-metric"><span class="growth-badge ${growthClass}">${esc(growth)}</span><span>so với kỳ trước</span></div>` : ''}
      </button>`;
    }).join('');
    $('#hotTopicsFootnote').textContent = `Kỳ thống kê: ${periodLabels[state.period]}. Chọn một topic để xem dòng thời gian và các nguồn bài báo.`;
  }
  async function loadHotTopics() {
    $('#hotTopics').innerHTML = '<div class="loading-card">Đang tải sự kiện nổi bật…</div>';
    try {
      const data = await getJSON(`/api/public/news/hot-topics?period=${encodeURIComponent(state.period)}&limit=9`);
      renderTopics(data.items || []);
    } catch (error) {
      setMessage('#hotTopics', 'Không tải được danh sách sự kiện', error.message, true);
    }
  }
  function renderPagination(target, pagination, type) {
    if (!pagination || !pagination.total) { $(target).innerHTML = ''; return; }
    const start = pagination.start ?? ((pagination.page - 1) * pagination.page_size + 1);
    const end = pagination.end ?? Math.min(pagination.total, pagination.page * pagination.page_size);
    $(target).innerHTML = `<span>Hiển thị ${num(start)}–${num(end)} trong ${num(pagination.total)} bài</span><div class="pagination-actions"><button type="button" data-pager="${type}" data-dir="prev" ${pagination.page <= 1 ? 'disabled' : ''}>← Trước</button><button type="button" data-pager="${type}" data-dir="next" ${pagination.page >= pagination.total_pages ? 'disabled' : ''}>Sau →</button></div>`;
  }
  function timelineItemsHTML(items, entityMode = false) {
    if (!items.length) return '<div class="message-card"><strong>Chưa có bài báo được liên kết</strong>Những bài mới sẽ xuất hiện khi được phân loại vào timeline này.</div>';
    return items.map(item => {
      const related = entityMode ? item.topics : item.entities;
      const relatedLabel = entityMode ? 'Topic liên quan: ' : 'Thực thể liên quan: ';
      return `<article class="timeline-entry">
        <div class="timeline-time">${esc(localDateText(item.timeline_at || item.published_at || item.collected_at, true))}</div>
        <div class="timeline-rail"><span class="timeline-dot"></span><span class="timeline-line"></span></div>
        <div class="timeline-card"><div class="timeline-meta"><span>${esc(item.source_name)}</span>${item.author ? `<span>· ${esc(item.author)}</span>` : ''}</div><h3><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}${related ? `<div class="entity-list">${relatedLabel}${esc(related)}</div>` : ''}</div>
      </article>`;
    }).join('');
  }
  function renderTimeline(data) {
    const meta = data.topic || data.entity;
    const type = data.topic ? 'topic' : 'entity';
    const id = data.topic ? data.topic.topic_id : data.entity.entity_id;
    const name = data.topic ? data.topic.name : data.entity.canonical_name;
    state.selectedTimeline = {type, id: Number(id), name};
    $('#timelineSelectionLabel').textContent = `${type === 'topic' ? 'Topic' : 'Entity'} · ${name}`;
    $('#timelineContainer').innerHTML = timelineItemsHTML(data.items || [], type === 'entity');
    renderPagination('#timelinePagination', data.pagination, 'timeline');
  }
  async function fetchTimelineData(type, id, page = 1, pageSize = 8) {
    if (type === 'entity') {
      return getJSON(`/api/public/news/entities/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
    }
    const topicId = id;
    return getJSON(`/api/public/news/topics/${encodeURIComponent(topicId)}/timeline?page=${page}&page_size=${pageSize}`);
  }
  async function loadTimeline(type, id, page = 1, shouldScroll = true) {
    state.timelinePage = page;
    $('#timelineContainer').innerHTML = '<div class="loading-card">Đang tải timeline…</div>';
    $('#timelinePagination').innerHTML = '';
    try {
      const data = await fetchTimelineData(type, id, page, state.timelinePageSize);
      renderTimeline(data);
      if (shouldScroll) $('#timeline').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (error) {
      setMessage('#timelineContainer', 'Không tải được timeline', error.message, true);
    }
  }
  function renderArticles(data) {
    const items = data.items || [];
    if (!items.length) {
      setMessage('#dailyArticles', 'Chưa có bài báo phù hợp', state.articleQuery ? 'Thử từ khóa khác hoặc xóa bộ lọc tìm kiếm.' : 'Chưa có bài nào được thu thập cho ngày hôm nay.');
      $('#articlePagination').innerHTML = '';
      return;
    }
    $('#dailyArticles').innerHTML = items.map(item => {
      const tags = (item.topics || []).slice(0, 3).map(topic => `<span class="article-tag">${esc(topic)}</span>`).join('');
      const time = localDateText(item.published_at || item.collected_at, true);
      return `<article class="article-card"><div class="article-meta"><span class="source-chip">${esc(item.source_name)}</span><span>${esc(time)}</span></div><h3 class="article-title"><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>${item.snippet ? `<p class="article-snippet">${esc(item.snippet)}</p>` : ''}<div class="article-tags">${tags}</div><div class="article-card-footer"><span class="article-author">${item.author ? `Tác giả: ${esc(item.author)}` : 'Đọc tại nguồn gốc'}</span><a class="article-source-link" href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">Đọc bài gốc ↗</a></div></article>`;
    }).join('');
    renderPagination('#articlePagination', data.pagination, 'articles');
    $('#articleDateLabel').textContent = `${num(data.pagination.total)} bài được xuất bản hôm nay, theo giờ Việt Nam.`;
  }
  async function loadArticles(page = 1) {
    state.articlePage = page;
    $('#dailyArticles').innerHTML = '<div class="loading-card">Đang tải bài báo…</div>';
    $('#articlePagination').innerHTML = '';
    const params = new URLSearchParams({page:String(page), page_size:String(state.articlePageSize)});
    if (state.articleQuery) params.set('q', state.articleQuery);
    try {
      const data = await getJSON(`/api/public/news/articles?${params.toString()}`);
      renderArticles(data);
    } catch (error) {
      setMessage('#dailyArticles', 'Không tải được bài báo', error.message, true);
    }
  }

  // User account and preference controls.
  function renderAuthArea() {
    if (!state.user) {
      $('#authArea').innerHTML = '<button class="auth-button auth-button-secondary" type="button" data-auth-open="login">Đăng nhập</button><button class="auth-button" type="button" data-auth-open="register">Đăng ký</button>';
      $('#myRadar').hidden = true;
      return;
    }
    $('#authArea').innerHTML = `<div class="auth-user"><span class="auth-user-name" title="${esc(state.user.username)}">Xin chào, ${esc(state.user.username)}</span><button class="auth-logout" type="button" id="logoutButton">Đăng xuất</button></div>`;
    $('#myRadar').hidden = false;
  }
  function setAuthMode(mode) {
    state.authMode = mode === 'register' ? 'register' : 'login';
    const registering = state.authMode === 'register';
    $('#authTitle').textContent = registering ? 'Tạo tài khoản' : 'Đăng nhập';
    $('#authDescription').textContent = registering
      ? 'Tạo tài khoản để lưu timeline topic và entity theo dõi riêng của bạn.'
      : 'Đăng nhập để tiếp tục xem những timeline bạn đã lưu.';
    $('#authIdentifierLabel').textContent = registering ? 'Tên đăng nhập' : 'Tên đăng nhập hoặc email';
    $('#authIdentifier').autocomplete = registering ? 'username' : 'username';
    $('#authIdentifier').maxLength = registering ? 32 : 254;
    $('#authEmailField').hidden = !registering;
    $('#authEmail').required = registering;
    $('#authPasswordConfirmField').hidden = !registering;
    $('#authPasswordConfirm').required = registering;
    $('#authPassword').autocomplete = registering ? 'new-password' : 'current-password';
    $('#authPassword').minLength = registering ? 8 : 1;
    $('#authSubmit').textContent = registering ? 'Tạo tài khoản' : 'Đăng nhập';
    $('#authSwitchPrompt').textContent = registering ? 'Đã có tài khoản?' : 'Chưa có tài khoản?';
    $('#authSwitchMode').textContent = registering ? 'Đăng nhập' : 'Đăng ký';
    $('#authSwitchMode').dataset.switchAuth = registering ? 'login' : 'register';
    setFeedback('#authFeedback', '');
  }
  function openAuthModal(mode = 'login') {
    setAuthMode(mode);
    $('#authForm').reset();
    setAuthMode(mode);
    $('#authModal').hidden = false;
    window.setTimeout(() => $('#authIdentifier').focus(), 0);
  }
  function closeAuthModal() { $('#authModal').hidden = true; }
  async function restoreUser() {
    try {
      const data = await getJSON('/api/user-auth/me');
      state.user = data.user;
    } catch (error) {
      state.user = null;
      if (error.status !== 401) console.warn('Could not restore News Radar session:', error.message);
    }
    renderAuthArea();
  }
  function keyFor(type, id) { return `${type}:${Number(id)}`; }
  function updateSelectionCount() {
    $('#selectionCount').textContent = `Đã chọn ${state.picked.size}/${MAX_SAVED_TIMELINES}`;
    $('#saveTimelinesButton').disabled = state.picked.size > MAX_SAVED_TIMELINES;
  }
  function renderOptionGroup(target, countTarget, items, type) {
    const query = state.optionQuery.toLocaleLowerCase('vi-VN');
    const visible = items.filter(item => !query || `${item.name} ${item.description || ''} ${item.entity_type || ''}`.toLocaleLowerCase('vi-VN').includes(query));
    $(countTarget).textContent = `${num(visible.length)}/${num(items.length)}`;
    if (!visible.length) {
      $(target).innerHTML = `<div class="picker-empty">${items.length ? 'Không có kết quả phù hợp.' : 'Chưa có dữ liệu để chọn.'}</div>`;
      return;
    }
    $(target).innerHTML = visible.map(item => {
      const key = keyFor(type, item.id);
      const checked = state.picked.has(key) ? ' checked' : '';
      const subtitle = `${num(item.article_count)} bài${item.source_count !== undefined ? ` · ${num(item.source_count)} nguồn` : ''}${item.entity_type ? ` · ${esc(item.entity_type)}` : ''}`;
      return `<label class="picker-option"><input type="checkbox" data-pick-type="${type}" data-pick-id="${Number(item.id)}"${checked}><span class="picker-option-copy"><strong>${esc(item.name)}</strong><small>${subtitle}</small></span></label>`;
    }).join('');
  }
  function renderPicker() {
    renderOptionGroup('#topicOptions', '#topicOptionCount', state.options.topics, 'topic');
    renderOptionGroup('#entityOptions', '#entityOptionCount', state.options.entities, 'entity');
    updateSelectionCount();
  }
  function renderSavedTimelineCard(ref, data) {
    const meta = data.topic || data.entity;
    const entityMode = ref.type === 'entity';
    const title = entityMode ? meta.canonical_name : meta.name;
    const allCount = data.pagination ? data.pagination.total : Number(ref.article_count || 0);
    const items = data.items || [];
    const articleHTML = items.length ? items.map(item => `<article class="saved-article"><div class="saved-article-meta"><span>${esc(item.source_name)}</span><span>${esc(localDateText(item.timeline_at || item.published_at || item.collected_at, true))}</span></div><h4><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h4>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}</article>`).join('') : '<div class="picker-empty">Chưa có bài báo gắn với mục này.</div>';
    return `<section class="saved-timeline-card"><div class="saved-timeline-head"><div class="saved-timeline-title"><span class="timeline-type-chip${entityMode ? ' entity' : ''}">${entityMode ? 'ENTITY' : 'TOPIC'}</span><h3>${esc(title)}</h3><span class="saved-timeline-count">${num(allCount)} bài</span></div><button class="remove-follow" type="button" data-remove-saved="${esc(keyFor(ref.type, ref.id))}">Bỏ theo dõi</button></div><div class="saved-timeline-list">${articleHTML}</div><div class="saved-timeline-foot"><span>Hiển thị tối đa 8 bài mới nhất</span><button type="button" data-open-full="${esc(keyFor(ref.type, ref.id))}">Mở timeline đầy đủ ↗</button></div></section>`;
  }
  async function renderSavedTimelines() {
    $('#savedSummary').textContent = state.savedTimelines.length
      ? `Bạn đang theo dõi ${num(state.savedTimelines.length)}/${MAX_SAVED_TIMELINES} timeline. Lựa chọn này đã được lưu vào tài khoản.`
      : 'Bạn chưa lưu timeline nào. Mở mục “Quản lý topic và entity” để bắt đầu theo dõi.';
    if (!state.savedTimelines.length) {
      $('#savedTimelinesContainer').innerHTML = '<div class="empty-state"><span class="empty-icon">◎</span><strong>Radar cá nhân chưa có timeline</strong><p>Chọn các topic sự kiện hoặc entity bạn quan tâm; những timeline đã lưu sẽ xuất hiện ở đây khi đăng nhập lần sau.</p></div>';
      return;
    }
    $('#savedTimelinesContainer').innerHTML = state.savedTimelines.map(ref => `<div class="loading-card" data-saved-placeholder="${esc(keyFor(ref.type, ref.id))}">Đang tải timeline: ${esc(ref.name)}…</div>`).join('');
    await Promise.all(state.savedTimelines.map(async ref => {
      try {
        const data = await fetchTimelineData(ref.type, ref.id, 1, 8);
        const placeholder = document.querySelector(`[data-saved-placeholder="${CSS.escape(keyFor(ref.type, ref.id))}"]`);
        if (placeholder) placeholder.outerHTML = renderSavedTimelineCard(ref, data);
      } catch (error) {
        const placeholder = document.querySelector(`[data-saved-placeholder="${CSS.escape(keyFor(ref.type, ref.id))}"]`);
        if (placeholder) placeholder.outerHTML = `<div class="message-card"><strong>${esc(ref.name)}</strong>Không tải được timeline: ${esc(error.message)}</div>`;
      }
    }));
  }
  async function loadUserRadar() {
    if (!state.user) { renderAuthArea(); return; }
    renderAuthArea();
    $('#savedTimelinesContainer').innerHTML = '<div class="loading-card">Đang tải timeline cá nhân…</div>';
    try {
      const [optionData, savedData] = await Promise.all([
        getJSON('/api/user/timeline-options'),
        getJSON('/api/user/saved-timelines')
      ]);
      state.options = {topics: optionData.topics || [], entities: optionData.entities || []};
      state.savedTimelines = savedData.items || [];
      state.picked = new Set(state.savedTimelines.map(item => keyFor(item.type, item.id)));
      renderPicker();
      await renderSavedTimelines();
    } catch (error) {
      setMessage('#savedTimelinesContainer', 'Không tải được radar cá nhân', error.message, true);
      setFeedback('#saveTimelineFeedback', error.message);
    }
  }
  async function saveTimelines() {
    if (!state.user) { openAuthModal('login'); return; }
    if (state.picked.size > MAX_SAVED_TIMELINES) {
      setFeedback('#saveTimelineFeedback', `Chỉ được lưu tối đa ${MAX_SAVED_TIMELINES} timeline.`);
      return;
    }
    const items = [...state.picked].map(key => {
      const [type, id] = key.split(':');
      return {type, id:Number(id)};
    });
    $('#saveTimelinesButton').disabled = true;
    setFeedback('#saveTimelineFeedback', 'Đang lưu lựa chọn…');
    try {
      const result = await sendJSON('/api/user/saved-timelines', 'PUT', {items});
      const saved = await getJSON('/api/user/saved-timelines');
      state.savedTimelines = saved.items || [];
      state.picked = new Set(state.savedTimelines.map(item => keyFor(item.type, item.id)));
      renderPicker();
      $('#saveTimelinesButton').disabled = true;
      await renderSavedTimelines();
      setFeedback('#saveTimelineFeedback', `Đã lưu ${num(result.saved_count)} timeline. Lần sau đăng nhập, radar cá nhân sẽ tự tải các mục này.`, true);
    } catch (error) {
      setFeedback('#saveTimelineFeedback', error.message);
    } finally {
      $('#saveTimelinesButton').disabled = state.picked.size > MAX_SAVED_TIMELINES;
    }
  }
  async function logout() {
    try { await sendJSON('/api/user-auth/logout', 'POST', {}); } catch (error) { console.warn('Logout request failed:', error.message); }
    state.user = null;
    state.savedTimelines = [];
    state.picked.clear();
    state.options = {topics: [], entities: []};
    renderAuthArea();
    $('#savedTimelinesContainer').innerHTML = '';
  }

  document.addEventListener('click', async event => {
    const authOpen = event.target.closest('[data-auth-open]');
    if (authOpen) { openAuthModal(authOpen.dataset.authOpen); return; }
    const authClose = event.target.closest('[data-auth-close]');
    if (authClose) { closeAuthModal(); return; }
    const authSwitch = event.target.closest('[data-switch-auth]');
    if (authSwitch) { openAuthModal(authSwitch.dataset.switchAuth); return; }
    if (event.target.closest('#logoutButton')) { await logout(); return; }
    if (event.target.closest('#openTimelinePicker')) {
      $('#timelinePickerDetails').open = true;
      $('#timelinePickerDetails').scrollIntoView({behavior:'smooth', block:'center'});
      $('#timelineOptionSearch').focus({preventScroll:true});
      return;
    }
    if (event.target.closest('#saveTimelinesButton')) { await saveTimelines(); return; }
    const period = event.target.closest('[data-period]');
    if (period) {
      state.period = period.dataset.period;
      document.querySelectorAll('.period-btn').forEach(btn => {
        const active = btn.dataset.period === state.period;
        btn.classList.toggle('active', active); btn.setAttribute('aria-selected', String(active));
      });
      await loadHotTopics(); return;
    }
    const topic = event.target.closest('[data-topic-id]');
    if (topic) {
      document.querySelectorAll('.topic-card').forEach(card => card.classList.toggle('selected', card === topic));
      await loadTimeline('topic', Number(topic.dataset.topicId), 1); return;
    }
    const remove = event.target.closest('[data-remove-saved]');
    if (remove) {
      state.picked.delete(remove.dataset.removeSaved);
      renderPicker();
      await saveTimelines(); return;
    }
    const full = event.target.closest('[data-open-full]');
    if (full) {
      const [type, id] = full.dataset.openFull.split(':');
      await loadTimeline(type, Number(id), 1); return;
    }
    const pager = event.target.closest('[data-pager]');
    if (pager && !pager.disabled) {
      const dir = pager.dataset.dir === 'next' ? 1 : -1;
      if (pager.dataset.pager === 'articles') await loadArticles(Math.max(1, state.articlePage + dir));
      else if (pager.dataset.pager === 'timeline' && state.selectedTimeline) await loadTimeline(state.selectedTimeline.type, state.selectedTimeline.id, Math.max(1, state.timelinePage + dir));
    }
  });
  document.addEventListener('change', event => {
    const input = event.target.closest('[data-pick-type]');
    if (!input) return;
    const key = keyFor(input.dataset.pickType, input.dataset.pickId);
    if (input.checked) {
      if (state.picked.size >= MAX_SAVED_TIMELINES) {
        input.checked = false;
        setFeedback('#saveTimelineFeedback', `Bạn đã chọn đủ ${MAX_SAVED_TIMELINES} mục. Bỏ chọn một mục trước khi thêm mục mới.`);
        return;
      }
      state.picked.add(key);
    } else {
      state.picked.delete(key);
    }
    updateSelectionCount();
    setFeedback('#saveTimelineFeedback', '');
  });
  $('#timelineOptionSearch').addEventListener('input', event => {
    state.optionQuery = event.target.value.trim();
    renderPicker();
  });
  $('#articleSearchForm').addEventListener('submit', event => {
    event.preventDefault(); state.articleQuery = $('#articleSearch').value.trim(); loadArticles(1);
  });
  $('#authForm').addEventListener('submit', async event => {
    event.preventDefault();
    const registering = state.authMode === 'register';
    const usernameOrIdentifier = $('#authIdentifier').value.trim();
    const password = $('#authPassword').value;
    if (registering && password !== $('#authPasswordConfirm').value) {
      setFeedback('#authFeedback', 'Hai lần nhập mật khẩu chưa khớp.'); return;
    }
    const body = registering
      ? {username: usernameOrIdentifier, email: $('#authEmail').value.trim(), password}
      : {identifier: usernameOrIdentifier, password};
    $('#authSubmit').disabled = true;
    setFeedback('#authFeedback', registering ? 'Đang tạo tài khoản…' : 'Đang đăng nhập…');
    try {
      const data = await sendJSON(registering ? '/api/user-auth/register' : '/api/user-auth/login', 'POST', body);
      state.user = data.user;
      closeAuthModal();
      renderAuthArea();
      await loadUserRadar();
    } catch (error) {
      setFeedback('#authFeedback', error.message);
    } finally {
      $('#authSubmit').disabled = false;
    }
  });
  $('#authModal').addEventListener('click', event => { if (event.target.id === 'authModal') closeAuthModal(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#authModal').hidden) closeAuthModal(); });
  const today = new Intl.DateTimeFormat('vi-VN', {timeZone:'Asia/Ho_Chi_Minh', weekday:'long', day:'numeric', month:'long', year:'numeric'}).format(new Date());
  $('#currentDate').textContent = today.charAt(0).toUpperCase() + today.slice(1);
  async function initialize() {
    await restoreUser();
    if (state.user) await loadUserRadar();
    await Promise.all([loadOverview(), loadHotTopics(), loadArticles(1)]);
  }
  initialize();
})();
