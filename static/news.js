(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const MAX_SAVED_TIMELINES = 10;
  const state = {
    period: 'day', selectedTimeline: null, timelinePage: 1, articlePage: 1, searchPage: 1,
    articlePageSize: 20, searchPageSize: 20, timelinePageSize: 20, searchQuery: '', fieldCategory: '',
    user: null, savedTimelines: [], picked: new Set(), followMeta: new Map(),
    pendingFollow: null, followSaving: false, authMode: 'login', telegram: null
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

  function renderTopics(items) {
    if (!items.length) {
      setMessage('#hotTopics', 'Chưa có tin nổi bật', 'Chưa có sự kiện được xếp hạng trong kỳ này. Tin nổi bật sẽ xuất hiện khi bài báo được phân loại theo topic.');
      return;
    }
    $('#hotTopics').innerHTML = items.map((topic, index) => {
      const growth = topic.growth_pct === null || topic.growth_pct === undefined ? '' : `${Number(topic.growth_pct) > 0 ? '+' : ''}${Number(topic.growth_pct).toLocaleString('vi-VN')}%`;
      const growthClass = Number(topic.growth_pct) < 0 ? 'down' : '';
      return `<article class="radar-row featured-row">
        <span class="row-rank">${index + 1}</span>
        <div class="radar-row-main"><div class="radar-row-meta"><span>SỰ KIỆN</span><span>${num(topic.article_count)} bài</span><span>${num(topic.source_count)} nguồn</span>${growth ? `<span class="growth-badge ${growthClass}">${esc(growth)}</span>` : ''}</div>
          <h3>${esc(topic.topic_name)}</h3>${topic.description ? `<p class="radar-row-snippet">${esc(topic.description)}</p>` : ''}
        </div>
        <div class="radar-row-actions"><button class="timeline-action primary" type="button" data-open-timeline="topic" data-timeline-id="${Number(topic.topic_id || 0)}" ${Number(topic.topic_id || 0) < 1 ? 'disabled' : ''}>Xem timeline <span aria-hidden="true">↗</span></button>${followButtonHTML('topic', {id: topic.topic_id, name: topic.topic_name})}</div>
      </article>`;
    }).join('');
    $('#hotTopicsFootnote').textContent = `Xếp hạng theo dữ liệu ${periodLabels[state.period].toLocaleLowerCase('vi-VN')} đã được thu thập và phân loại.`;
  }
  async function loadHotTopics() {
    $('#hotTopics').innerHTML = '<div class="loading-card">Đang tải tin nổi bật…</div>';
    try {
      const data = await getJSON(`/api/public/news/hot-topics?period=${encodeURIComponent(state.period)}&limit=10`);
      renderTopics(data.items || []);
    } catch (error) {
      setMessage('#hotTopics', 'Không tải được tin nổi bật', error.message, true);
    }
  }

  function renderPagination(target, pagination, type) {
    if (!pagination || !pagination.total) { $(target).innerHTML = ''; return; }
    const start = pagination.start ?? ((pagination.page - 1) * pagination.page_size + 1);
    const end = pagination.end ?? Math.min(pagination.total, pagination.page * pagination.page_size);
    $(target).innerHTML = `<span>Hiển thị ${num(start)}–${num(end)} trong ${num(pagination.total)} bài</span><div class="pagination-actions"><button type="button" data-pager="${type}" data-dir="prev" ${pagination.page <= 1 ? 'disabled' : ''}>← Trước</button><button type="button" data-pager="${type}" data-dir="next" ${pagination.page >= pagination.total_pages ? 'disabled' : ''}>Sau →</button></div>`;
  }
  function timelineItemsHTML(items, mode = 'entity_48h') {
    if (!items.length) return '<div class="message-card"><strong>Chưa có bài báo được liên kết</strong>Những bài mới sẽ xuất hiện khi có entity chuẩn trùng khớp.</div>';
    return items.map(item => {
      const related = [item.entities ? `Entity: ${item.entities}` : '', item.topics ? `Nhãn: ${item.topics}` : ''].filter(Boolean).join(' · ');
      return `<article class="timeline-entry">
        <div class="timeline-time">${esc(localDateText(item.timeline_at || item.published_at || item.collected_at, true))}</div>
        <div class="timeline-rail"><span class="timeline-dot"></span><span class="timeline-line"></span></div>
        <div class="timeline-card"><div class="timeline-meta"><span>${esc(item.source_name)}</span>${item.author ? `<span>· ${esc(item.author)}</span>` : ''}</div><h3><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}${related ? `<div class="entity-list">${esc(related)}</div>` : ''}</div>
      </article>`;
    }).join('');
  }
  function renderTimeline(data) {
    const anchor = data.anchor || data.topic || data.entity || {};
    const articleMode = Boolean(data.anchor);
    const type = articleMode ? 'article' : (data.topic ? 'topic' : 'entity');
    const id = articleMode ? Number(anchor.article_id) : (data.topic ? Number(data.topic.topic_id) : Number(data.entity.entity_id));
    const name = data.timeline_name || (data.topic ? data.topic.name : data.entity ? data.entity.canonical_name : anchor.title) || 'Timeline';
    state.selectedTimeline = {type, id, name};
    const modeText = data.mode === 'topic_id' ? 'Topic ID' : data.mode === 'entity_id' ? 'Entity ID' : 'Bài báo';
    $('#timelineSelectionLabel').textContent = `${modeText} · ${name}`;
    $('#timelineContainer').innerHTML = timelineItemsHTML(data.items || [], data.mode || type);
    renderPagination('#timelinePagination', data.pagination, 'timeline');
  }
  async function fetchTimelineData(type, id, page = 1, pageSize = 20) {
    if (type === 'article') return getJSON(`/api/public/news/articles/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
    if (type === 'entity') return getJSON(`/api/public/news/entities/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
    return getJSON(`/api/public/news/topics/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
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

  function followButtonHTML(type, item, compact = false) {
    const id = Number(item.id);
    if (!Number.isInteger(id) || id < 1) return '';
    const key = keyFor(type, id);
    const followed = state.picked.has(key);
    const label = compact ? (followed ? '✓ following' : 'follow') : (followed ? '✓ Đang follow' : '+ Theo dõi');
    const itemName = String(item.name || (type === 'topic' ? 'topic' : 'entity'));
    const accessibleLabel = followed ? `Bỏ follow ${type} ${itemName}` : `Follow ${type} ${itemName}`;
    return `<button class="follow-toggle${followed ? ' is-following' : ''}${compact ? ' follow-toggle-compact' : ''}" type="button" data-toggle-follow="${type}" data-follow-id="${id}" data-follow-name="${esc(item.name)}" data-follow-compact="${compact ? 'true' : 'false'}" aria-label="${esc(accessibleLabel)}" title="${esc(accessibleLabel)}" aria-pressed="${followed}">${label}</button>`;
  }
  function articleActionsHTML(item, feedMode = false) {
    const topics = feedMode ? (item.matched_topics || []) : (item.topic_refs || []);
    const entities = feedMode ? (item.matched_entities || []) : (item.entity_refs || []);
    const buttons = [];
    const articleId = Number(item.article_id || 0);
    topics.filter(x => Number(x.id) > 0).forEach(topic => {
      buttons.push(`<span class="article-action-pair article-action-pair-compact"><button class="timeline-action article-topic-label" type="button" data-open-timeline="topic" data-timeline-id="${Number(topic.id)}" data-timeline-name="${esc(topic.name)}">Topic: ${esc(topic.name)}</button>${followButtonHTML('topic', topic, true)}</span>`);
    });
    entities.filter(x => Number(x.id) > 0).forEach(entity => {
      const isEvent = String(entity.type || '').toUpperCase() === 'EVENT';
      const label = isEvent ? 'Sự kiện' : 'Entity';
      buttons.push(`<span class="article-action-pair article-action-pair-compact"><button class="timeline-action ${isEvent ? 'event-action' : 'entity-action'} article-topic-label" type="button" data-open-timeline="entity" data-timeline-id="${Number(entity.id)}" data-timeline-name="${esc(entity.name)}">${label}: ${esc(entity.name)}</button>${followButtonHTML('entity', entity, true)}</span>`);
    });
    return buttons.length ? `<div class="article-follow-tags">${buttons.join('')}</div>` : '<div class="article-no-timeline">Chưa có tag topic/entity được phân loại.</div>';
  }
  function articleRowHTML(item, feedMode = false) {
    const time = localDateText(item.published_at || item.collected_at, true);
    const followableTopicNames = new Set((feedMode ? (item.matched_topics || []) : (item.topic_refs || [])).map(tag => String(tag.name || '').toLocaleLowerCase('vi')));
    const tags = (feedMode ? [] : (item.topics || []).filter(name => !followableTopicNames.has(String(name || '').toLocaleLowerCase('vi'))).map(name => ({name})))
      .slice(0, 6).map(tag => `<span class="article-tag">${esc(tag.name)}</span>`).join('');
    const fieldTag = item.field_category ? `<span class="field-category-badge">${esc(item.field_category)}</span>` : '';
    return `<article class="radar-row article-row${feedMode ? ' feed-row' : ''}">
      <div class="radar-row-main"><div class="radar-row-meta"><span class="source-chip">${esc(item.source_name)}</span><span>${esc(time)}</span>${fieldTag}${feedMode ? '<span class="feed-match-label">LIÊN QUAN ĐẾN BẠN</span>' : ''}</div>
        <h3><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>
        ${item.snippet ? `<p class="radar-row-snippet">${esc(item.snippet)}</p>` : ''}
        ${tags ? `<div class="article-tags">${tags}</div>` : ''}
        ${articleActionsHTML(item, feedMode)}
        <div class="article-card-footer"><span></span><a class="article-source-link" href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">Đọc bài gốc ↗</a></div>
      </div>
    </article>`;
  }

  function renderArticleList(target, items, feedMode = false, emptyTitle = 'Chưa có bài báo phù hợp', emptyDescription = 'Hãy thử tìm bằng từ khóa khác.') {
    if (!items.length) { setMessage(target, emptyTitle, emptyDescription); return; }
    $(target).innerHTML = items.map(item => articleRowHTML(item, feedMode)).join('');
  }
  async function loadArticles(page = 1) {
    state.articlePage = page;
    $('#dailyArticles').innerHTML = '<div class="loading-card">Đang tải bài báo…</div>';
    $('#articlePagination').innerHTML = '';
    const params = new URLSearchParams({page:String(page), page_size:String(state.articlePageSize)});
    if (state.fieldCategory) params.set('field_category', state.fieldCategory);
    try {
      const data = await getJSON(`/api/public/news/articles?${params.toString()}`);
      renderArticleList('#dailyArticles', data.items || [], false, 'Chưa có bài báo hôm nay', 'Chưa có bài nào được xuất bản hoặc thu thập trong ngày hôm nay.');
      renderPagination('#articlePagination', data.pagination, 'articles');
      $('#articleDateLabel').textContent = `${num(data.pagination.total)} bài được xuất bản hôm nay, theo giờ Việt Nam.`;
      syncFollowButtons();
    } catch (error) {
      setMessage('#dailyArticles', 'Không tải được bài báo', error.message, true);
    }
  }
  async function loadSearchResults(page = 1) {
    state.searchPage = page;
    const query = state.searchQuery.trim();
    if (!query) { $('#searchResultsSection').hidden = true; return; }
    $('#searchResultsSection').hidden = false;
    $('#searchResults').innerHTML = '<div class="loading-card">Đang tìm trên kho tin…</div>';
    $('#searchPagination').innerHTML = '';
    const params = new URLSearchParams({q:query, page:String(page), page_size:String(state.searchPageSize)});
    if (state.fieldCategory) params.set('field_category', state.fieldCategory);
    try {
      const data = await getJSON(`/api/public/news/articles?${params.toString()}`);
      renderArticleList('#searchResults', data.items || [], false, 'Không tìm thấy tin phù hợp', 'Thử từ khóa khác hoặc kiểm tra chính tả.');
      renderPagination('#searchPagination', data.pagination, 'search');
      $('#searchResultLabel').textContent = `${num(data.pagination.total)} kết quả cho “${query}”.`;
      syncFollowButtons();
      $('#searchResultsSection').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (error) {
      setMessage('#searchResults', 'Không tìm kiếm được', error.message, true);
    }
  }

  // Account and personalized news feed.
  function renderAuthArea() {
    const signedIn = Boolean(state.user);
    $('#myRadar').hidden = !signedIn;
    $('#myRadarNav').hidden = !signedIn;
    if (!signedIn) {
      $('#authArea').innerHTML = '<button class="auth-button auth-button-secondary" type="button" data-auth-open="login">Đăng nhập</button><button class="auth-button" type="button" data-auth-open="register">Đăng ký</button>';
      return;
    }
    $('#authArea').innerHTML = `<div class="auth-user"><span class="auth-user-name" title="${esc(state.user.username)}">Xin chào, ${esc(state.user.username)}</span><button class="auth-logout" type="button" id="logoutButton">Đăng xuất</button></div>`;
  }
  function setAuthMode(mode) {
    state.authMode = mode === 'register' ? 'register' : 'login';
    const registering = state.authMode === 'register';
    $('#authTitle').textContent = registering ? 'Tạo tài khoản' : 'Đăng nhập';
    $('#authDescription').textContent = registering
      ? 'Tạo tài khoản để lưu chủ đề và đối tượng muốn theo dõi.'
      : 'Đăng nhập để xem feed cá nhân và các mục đang theo dõi.';
    $('#authIdentifierLabel').textContent = registering ? 'Tên đăng nhập' : 'Tên đăng nhập hoặc email';
    $('#authIdentifier').autocomplete = 'username';
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
    setAuthMode(mode); $('#authForm').reset(); setAuthMode(mode);
    $('#authModal').hidden = false; window.setTimeout(() => $('#authIdentifier').focus(), 0);
  }
  function closeAuthModal() { $('#authModal').hidden = true; }
  async function restoreUser() {
    try { const data = await getJSON('/api/user-auth/me'); state.user = data.user; }
    catch (error) { state.user = null; if (error.status !== 401) console.warn('Could not restore News Radar session:', error.message); }
    renderAuthArea();
  }
  function keyFor(type, id) { return `${type}:${Number(id)}`; }
  function rememberFollow(type, id, name) {
    const key = keyFor(type, id);
    if (name) state.followMeta.set(key, {type, id:Number(id), name:String(name)});
  }
  function hasUnsavedFollowChanges() {
    const saved = new Set(state.savedTimelines.map(item => keyFor(item.type, item.id)));
    if (saved.size !== state.picked.size) return true;
    for (const key of saved) if (!state.picked.has(key)) return true;
    return false;
  }
  function updateSelectionCount() {
    $('#selectionCount').textContent = `Đang theo dõi ${state.picked.size}/${MAX_SAVED_TIMELINES}`;
    $('#saveTimelinesButton').disabled = state.picked.size > MAX_SAVED_TIMELINES || !hasUnsavedFollowChanges();
  }
  function renderFollowTags() {
    const selected = [...state.picked].map(key => {
      const meta = state.followMeta.get(key) || state.savedTimelines.find(item => keyFor(item.type, item.id) === key);
      if (!meta) return null;
      return {...meta, id:Number(meta.id)};
    }).filter(Boolean).sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name, 'vi'));
    if (!selected.length) {
      $('#savedTimelineTags').innerHTML = '<div class="picker-empty">Bạn chưa follow topic/entity nào. Hãy bấm “+ Theo dõi” ngay trên một bài tin.</div>';
    } else {
      $('#savedTimelineTags').innerHTML = selected.map(item => `<span class="followed-tag"><span class="followed-tag-type">${item.type === 'topic' ? 'TOPIC' : 'ENTITY'}</span><span class="followed-tag-name">${esc(item.name)}</span><button type="button" data-remove-follow="${item.type}" data-follow-id="${item.id}" aria-label="Xóa follow ${esc(item.name)}" title="Xóa tag">×</button></span>`).join('');
    }
    updateSelectionCount();
  }
  function syncFollowButtons() {
    document.querySelectorAll('[data-toggle-follow][data-follow-id]').forEach(button => {
      const type = button.dataset.toggleFollow;
      const id = Number(button.dataset.followId);
      const followed = state.picked.has(keyFor(type, id));
      button.classList.toggle('is-following', followed);
      button.setAttribute('aria-pressed', String(followed));
      const compact = button.dataset.followCompact === 'true';
      button.textContent = compact ? (followed ? '✓ following' : 'follow') : (followed ? '✓ Đang follow' : '+ Theo dõi');
      const itemName = button.dataset.followName || (type === 'topic' ? 'topic' : 'entity');
      const accessibleLabel = followed ? `Bỏ follow ${type} ${itemName}` : `Follow ${type} ${itemName}`;
      button.setAttribute('aria-label', accessibleLabel);
      button.title = accessibleLabel;
      button.disabled = state.followSaving;
    });
    document.querySelectorAll('[data-remove-follow]').forEach(button => { button.disabled = state.followSaving; });
  }
  async function renderUserFeed() {
    if (!state.user) return;
    $('#userFeedContainer').innerHTML = '<div class="loading-card">Đang tải feed cá nhân…</div>';
    try {
      const data = await getJSON('/api/user/feed');
      const groups = data.groups || [];
      const followedCount = state.savedTimelines.length;
      $('#feedSummary').textContent = followedCount
        ? `Đang follow ${num(followedCount)}/${MAX_SAVED_TIMELINES} topic/entity · mỗi mục hiển thị tối đa 5 bài mới nhất.`
        : 'Bạn chưa follow topic/entity nào. Bấm “+ Theo dõi” ngay trên các bài tin để cá nhân hóa feed.';
      if (!followedCount) {
        setMessage('#userFeedContainer', 'Bắt đầu tạo feed của bạn', 'Hãy mở một bài tin và bấm “+ Theo dõi” bên cạnh topic hoặc entity liên quan.');
        return;
      }
      if (!groups.length) {
        setMessage('#userFeedContainer', 'Chưa có bài trong feed', 'Chưa tìm thấy bài báo khớp với các topic/entity đã theo dõi. Feed sẽ xuất hiện khi có bài được phân loại.');
        return;
      }
      $('#userFeedContainer').innerHTML = groups.map(group => {
        const typeLabel = group.type === 'topic' ? 'TOPIC' : 'ENTITY';
        const articleHTML = (group.items || []).length
          ? group.items.map(item => articleRowHTML(item, true)).join('')
          : '<div class="message-card"><strong>Chưa có bài</strong>Chưa có bài báo liên quan đến mục này.</div>';
        const more = Number(group.count) > 5
          ? `<a class="feed-more-link" href="/news/following?type=${encodeURIComponent(group.type)}&id=${Number(group.id)}">Xem thêm (${num(group.count)}) <span aria-hidden="true">→</span></a>`
          : '';
        return `<section class="feed-group"><div class="feed-group-heading"><div><span class="followed-tag-type">${typeLabel}</span><h3>${esc(group.name)}</h3><p>${num(group.count)} bài phù hợp</p></div>${more}</div><div class="feed-group-articles">${articleHTML}</div></section>`;
      }).join('');
      syncFollowButtons();
    } catch (error) {
      setMessage('#userFeedContainer', 'Không tải được feed cá nhân', error.message, true);
    }
  }
  function renderTelegramSettings(data) {
    state.telegram = data;
    $('#telegramEnabled').checked = Boolean(data.enabled);
    $('#telegramEnabled').disabled = !data.connected;
    $('#createTelegramLink').disabled = !data.bot_available;
    $('#createTelegramLink').textContent = data.connected ? 'Tạo liên kết mới' : 'Tạo liên kết Telegram';
    $('#unlinkTelegramButton').hidden = !data.connected;
    if (data.connected) $('#openTelegramLink').hidden = true;
    if (!data.bot_available) {
      $('#telegramStatus').textContent = 'Máy chủ chưa cấu hình đầy đủ Telegram. Quản trị viên cần đặt TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME và TELEGRAM_WEBHOOK_SECRET.';
    } else if (data.connected) {
      $('#telegramStatus').textContent = `Telegram đã kết nối${data.enabled ? ' · thông báo đang bật' : ' · thông báo đang tắt'}.`;
    } else {
      $('#telegramStatus').textContent = `Chưa kết nối Telegram. Mở bot @${data.bot_username} để liên kết tài khoản.`;
    }
  }
  async function loadTelegramSettings() {
    if (!state.user) return;
    try {
      const data = await getJSON('/api/user/telegram/settings');
      renderTelegramSettings(data);
    } catch (error) {
      $('#telegramStatus').textContent = error.message || 'Không tải được cấu hình Telegram.';
      $('#telegramEnabled').disabled = true;
      $('#createTelegramLink').disabled = true;
    }
  }
  async function createTelegramLink() {
    $('#createTelegramLink').disabled = true;
    setFeedback('#telegramSettingsFeedback', 'Đang tạo liên kết an toàn…');
    try {
      const data = await sendJSON('/api/user/telegram/link', 'POST', {});
      const link = $('#openTelegramLink');
      if (!String(data.link_url || '').startsWith('https://t.me/')) throw new Error('Liên kết Telegram máy chủ trả về không hợp lệ.');
      link.href = data.link_url; link.hidden = false;
      setFeedback('#telegramSettingsFeedback', 'Liên kết có hiệu lực trong 15 phút. Mở Telegram và bấm Start, sau đó quay lại kiểm tra kết nối.', true);
    } catch (error) { setFeedback('#telegramSettingsFeedback', error.message); }
    finally { if (state.telegram) $('#createTelegramLink').disabled = !state.telegram.bot_available; }
  }
  async function saveTelegramSettings() {
    const button = $('#saveTelegramSettings'); button.disabled = true;
    setFeedback('#telegramSettingsFeedback', 'Đang lưu cấu hình…');
    try {
      const result = await sendJSON('/api/user/telegram/settings', 'PUT', {enabled: $('#telegramEnabled').checked});
      setFeedback('#telegramSettingsFeedback', result.enabled ? 'Đã bật thông báo Telegram. Bản tin sẽ được gửi sau khi pipeline xử lý bài mới.' : 'Đã lưu cấu hình. Thông báo Telegram đang tắt.', true);
      await loadTelegramSettings();
    } catch (error) { setFeedback('#telegramSettingsFeedback', error.message); await loadTelegramSettings(); }
    finally { button.disabled = false; }
  }
  async function unlinkTelegram() {
    if (!window.confirm('Ngắt kết nối Telegram? Bạn sẽ không còn nhận thông báo qua tài khoản Telegram hiện tại.')) return;
    $('#unlinkTelegramButton').disabled = true;
    try {
      await getJSON('/api/user/telegram/link', {method:'DELETE'});
      $('#openTelegramLink').hidden = true; $('#openTelegramLink').removeAttribute('href');
      setFeedback('#telegramSettingsFeedback', 'Đã ngắt kết nối Telegram.', true);
      await loadTelegramSettings();
    } catch (error) { setFeedback('#telegramSettingsFeedback', error.message); }
    finally { $('#unlinkTelegramButton').disabled = false; }
  }

  async function loadUserRadar() {
    if (!state.user) { renderAuthArea(); return; }
    renderAuthArea();
    $('#userFeedContainer').innerHTML = '<div class="loading-card">Đang tải feed của bạn…</div>';
    try {
      const savedData = await getJSON('/api/user/saved-timelines');
      state.savedTimelines = savedData.items || [];
      state.picked = new Set(state.savedTimelines.map(item => {
        rememberFollow(item.type, item.id, item.name);
        return keyFor(item.type, item.id);
      }));
      renderFollowTags(); syncFollowButtons();
      await Promise.all([renderUserFeed(), loadTelegramSettings()]);
    } catch (error) {
      setMessage('#userFeedContainer', 'Không tải được feed cá nhân', error.message, true);
    }
  }
  async function saveTimelines({silent = false} = {}) {
    if (!state.user) { openAuthModal('login'); return false; }
    if (state.picked.size > MAX_SAVED_TIMELINES) {
      setFeedback('#saveTimelineFeedback', `Chỉ được lưu tối đa ${MAX_SAVED_TIMELINES} mục.`); return false;
    }
    const items = [...state.picked].map(key => { const [type, id] = key.split(':'); return {type, id:Number(id)}; });
    const button = $('#saveTimelinesButton');
    state.followSaving = true;
    button.disabled = true; syncFollowButtons();
    if (!silent) setFeedback('#saveTimelineFeedback', 'Đang lưu các mục follow…');
    try {
      const result = await sendJSON('/api/user/saved-timelines', 'PUT', {items});
      const saved = await getJSON('/api/user/saved-timelines');
      state.savedTimelines = saved.items || [];
      state.picked = new Set(state.savedTimelines.map(item => {
        rememberFollow(item.type, item.id, item.name);
        return keyFor(item.type, item.id);
      }));
      renderFollowTags(); syncFollowButtons(); await renderUserFeed();
      setFeedback('#saveTimelineFeedback', silent ? 'Đã cập nhật follow từ bài tin.' : `Đã lưu ${num(result.saved_count)} mục follow.`, true);
      return true;
    } catch (error) {
      setFeedback('#saveTimelineFeedback', error.message);
      renderFollowTags(); syncFollowButtons();
      return false;
    } finally {
      state.followSaving = false; syncFollowButtons(); updateSelectionCount();
    }
  }
  async function toggleFollow(type, id, name) {
    id = Number(id);
    if (!Number.isInteger(id) || id < 1) return;
    rememberFollow(type, id, name);
    if (!state.user) {
      state.pendingFollow = {type, id, name};
      openAuthModal('login');
      setFeedback('#authFeedback', 'Đăng nhập để lưu mục follow này. Sau khi đăng nhập, lựa chọn sẽ được áp dụng tự động.');
      return;
    }
    const key = keyFor(type, id);
    if (state.picked.has(key)) {
      state.picked.delete(key);
    } else {
      if (state.picked.size >= MAX_SAVED_TIMELINES) {
        setFeedback('#saveTimelineFeedback', `Bạn đã follow đủ ${MAX_SAVED_TIMELINES} mục. Xóa một tag trong cấu hình trước khi thêm mục mới.`);
        $('#timelinePickerDetails').open = true;
        $('#timelinePickerDetails').scrollIntoView({behavior:'smooth', block:'center'});
        return;
      }
      state.picked.add(key);
    }
    renderFollowTags(); syncFollowButtons();
    await saveTimelines({silent:true});
  }
  async function logout() {
    try { await sendJSON('/api/user-auth/logout', 'POST', {}); } catch (error) { console.warn('Logout request failed:', error.message); }
    state.user = null; state.savedTimelines = []; state.picked.clear(); state.followMeta.clear(); state.pendingFollow = null;
    state.telegram = null; $('#openTelegramLink').hidden = true; $('#telegramEnabled').checked = false; $('#telegramEnabled').disabled = true;
    renderAuthArea(); $('#userFeedContainer').innerHTML = ''; $('#savedTimelineTags').innerHTML = ''; $('#telegramStatus').textContent = 'Đăng nhập để cấu hình Telegram.'; syncFollowButtons(); updateSelectionCount();
  }

  document.addEventListener('click', async event => {
    const authOpen = event.target.closest('[data-auth-open]'); if (authOpen) { openAuthModal(authOpen.dataset.authOpen); return; }
    const authClose = event.target.closest('[data-auth-close]'); if (authClose) { closeAuthModal(); return; }
    const authSwitch = event.target.closest('[data-switch-auth]'); if (authSwitch) { openAuthModal(authSwitch.dataset.switchAuth); return; }
    if (event.target.closest('#logoutButton')) { await logout(); return; }
    if (event.target.closest('#openTimelinePicker')) {
      $('#timelinePickerDetails').open = true;
      $('#timelinePickerDetails').scrollIntoView({behavior:'smooth', block:'center'}); return;
    }
    if (event.target.closest('#saveTimelinesButton')) { await saveTimelines(); return; }
    const toggle = event.target.closest('[data-toggle-follow]');
    if (toggle) { await toggleFollow(toggle.dataset.toggleFollow, toggle.dataset.followId, toggle.dataset.followName); return; }
    const remove = event.target.closest('[data-remove-follow]');
    if (remove) {
      const key = keyFor(remove.dataset.removeFollow, remove.dataset.followId);
      state.picked.delete(key); renderFollowTags(); syncFollowButtons();
      setFeedback('#saveTimelineFeedback', 'Tag đã được gỡ khỏi danh sách tạm. Bấm “Lưu thay đổi” để áp dụng.'); return;
    }
    if (event.target.closest('#createTelegramLink')) { await createTelegramLink(); return; }
    if (event.target.closest('#saveTelegramSettings')) { await saveTelegramSettings(); return; }
    if (event.target.closest('#refreshTelegramStatus')) { await loadTelegramSettings(); setFeedback('#telegramSettingsFeedback', 'Đã kiểm tra trạng thái kết nối.', true); return; }
    if (event.target.closest('#unlinkTelegramButton')) { await unlinkTelegram(); return; }
    if (event.target.closest('#refreshUserFeed')) { await renderUserFeed(); return; }
    if (event.target.closest('#clearSearch')) {
      state.searchQuery = ''; $('#radarSearch').value = ''; $('#searchResultsSection').hidden = true; $('#radarSearch').focus(); return;
    }
    const period = event.target.closest('[data-period]');
    if (period) {
      state.period = period.dataset.period;
      document.querySelectorAll('.period-btn').forEach(btn => { const active = btn.dataset.period === state.period; btn.classList.toggle('active', active); btn.setAttribute('aria-selected', String(active)); });
      await loadHotTopics(); return;
    }
    const articleTimelineAction = event.target.closest('[data-open-article-timeline]');
    if (articleTimelineAction) {
      const articleId = Number(articleTimelineAction.dataset.openArticleTimeline);
      if (articleId > 0) await loadTimeline('article', articleId, 1);
      return;
    }
    const timelineAction = event.target.closest('[data-open-timeline]');
    if (timelineAction) {
      await loadTimeline(timelineAction.dataset.openTimeline, Number(timelineAction.dataset.timelineId), 1); return;
    }
    const pager = event.target.closest('[data-pager]');
    if (pager && !pager.disabled) {
      const dir = pager.dataset.dir === 'next' ? 1 : -1;
      if (pager.dataset.pager === 'articles') await loadArticles(Math.max(1, state.articlePage + dir));
      else if (pager.dataset.pager === 'search') await loadSearchResults(Math.max(1, state.searchPage + dir));
      else if (pager.dataset.pager === 'timeline' && state.selectedTimeline) await loadTimeline(state.selectedTimeline.type, state.selectedTimeline.id, Math.max(1, state.timelinePage + dir));
    }
  });
  $('#fieldCategoryFilter').addEventListener('change', async event => {
    state.fieldCategory = String(event.target.value || '');
    await loadArticles(1);
    if (state.searchQuery) await loadSearchResults(1);
  });
  $('#radarSearchForm').addEventListener('submit', async event => {
    event.preventDefault(); state.searchQuery = $('#radarSearch').value.trim();
    if (!state.searchQuery) { $('#searchResultsSection').hidden = true; return; }
    await loadSearchResults(1);
  });
  $('#authForm').addEventListener('submit', async event => {
    event.preventDefault();
    const registering = state.authMode === 'register';
    const usernameOrIdentifier = $('#authIdentifier').value.trim(); const password = $('#authPassword').value;
    if (registering && password !== $('#authPasswordConfirm').value) { setFeedback('#authFeedback', 'Hai lần nhập mật khẩu chưa khớp.'); return; }
    const body = registering ? {username: usernameOrIdentifier, email: $('#authEmail').value.trim(), password} : {identifier: usernameOrIdentifier, password};
    $('#authSubmit').disabled = true; setFeedback('#authFeedback', registering ? 'Đang tạo tài khoản…' : 'Đang đăng nhập…');
    try {
      const data = await sendJSON(registering ? '/api/user-auth/register' : '/api/user-auth/login', 'POST', body);
      state.user = data.user; closeAuthModal(); renderAuthArea(); await loadUserRadar();
      if (state.pendingFollow) { const pending = state.pendingFollow; state.pendingFollow = null; if (!state.picked.has(keyFor(pending.type, pending.id))) await toggleFollow(pending.type, pending.id, pending.name); }
    } catch (error) { setFeedback('#authFeedback', error.message); }
    finally { $('#authSubmit').disabled = false; }
  });
  $('#authModal').addEventListener('click', event => { if (event.target.id === 'authModal') closeAuthModal(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#authModal').hidden) closeAuthModal(); });

  async function initialize() {
    await restoreUser();
    const jobs = [loadHotTopics(), loadArticles(1)];
    if (state.user) jobs.push(loadUserRadar());
    await Promise.all(jobs);
  }
  initialize();
})();
