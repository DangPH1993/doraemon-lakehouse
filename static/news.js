(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const state = {period: 'day', selectedTopic: null, timelinePage: 1, articlePage: 1, articleQuery: '', articlePageSize: 20, timelinePageSize: 20};
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
  async function getJSON(url) {
    const response = await fetch(url, {headers:{'Accept':'application/json'}});
    let data;
    try { data = await response.json(); } catch { throw new Error('Máy chủ trả về phản hồi không hợp lệ.'); }
    if (!response.ok) throw new Error(data.detail || `Yêu cầu thất bại (${response.status})`);
    return data;
  }
  function setMessage(target, title, description, isError = false) {
    $(target).innerHTML = `<div class="message-card"><strong>${esc(title)}</strong>${esc(description || '')}${isError ? '<br><small>Vui lòng tải lại trang sau ít phút.</small>' : ''}</div>`;
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
      const selected = state.selectedTopic && Number(state.selectedTopic.topic_id) === Number(topic.topic_id) ? ' selected' : '';
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
  function renderTimeline(data) {
    const topic = data.topic;
    state.selectedTopic = topic;
    $('#timelineSelectionLabel').textContent = topic.name;
    if (!(data.items || []).length) {
      setMessage('#timelineContainer', 'Chưa có diễn biến trong topic này', 'Khi có bài báo được liên kết với sự kiện, chúng sẽ xuất hiện tại đây.');
      $('#timelinePagination').innerHTML = '';
      return;
    }
    $('#timelineContainer').innerHTML = data.items.map((item, index) => `<article class="timeline-entry">
      <div class="timeline-time">${esc(localDateText(item.timeline_at || item.published_at || item.collected_at, true))}</div>
      <div class="timeline-rail"><span class="timeline-dot"></span><span class="timeline-line"></span></div>
      <div class="timeline-card"><div class="timeline-meta"><span>${esc(item.source_name)}</span>${item.author ? `<span>· ${esc(item.author)}</span>` : ''}</div><h3><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}${item.entities ? `<div class="entity-list">Liên quan: ${esc(item.entities)}</div>` : ''}</div>
    </article>`).join('');
    renderPagination('#timelinePagination', data.pagination, 'timeline');
  }
  async function loadTimeline(topicId, page = 1) {
    state.timelinePage = page;
    $('#timelineContainer').innerHTML = '<div class="loading-card">Đang tải timeline…</div>';
    $('#timelinePagination').innerHTML = '';
    try {
      const data = await getJSON(`/api/public/news/topics/${encodeURIComponent(topicId)}/timeline?page=${page}&page_size=${state.timelinePageSize}`);
      renderTimeline(data);
      $('#timeline').scrollIntoView({behavior:'smooth', block:'start'});
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
  document.addEventListener('click', async event => {
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
      const id = Number(topic.dataset.topicId);
      await loadTimeline(id, 1); return;
    }
    const pager = event.target.closest('[data-pager]');
    if (pager && !pager.disabled) {
      const dir = pager.dataset.dir === 'next' ? 1 : -1;
      if (pager.dataset.pager === 'articles') await loadArticles(Math.max(1, state.articlePage + dir));
      else if (pager.dataset.pager === 'timeline' && state.selectedTopic) await loadTimeline(state.selectedTopic.topic_id, Math.max(1, state.timelinePage + dir));
    }
  });
  $('#articleSearchForm').addEventListener('submit', event => {
    event.preventDefault(); state.articleQuery = $('#articleSearch').value.trim(); loadArticles(1);
  });
  const today = new Intl.DateTimeFormat('vi-VN', {timeZone:'Asia/Ho_Chi_Minh', weekday:'long', day:'numeric', month:'long', year:'numeric'}).format(new Date());
  $('#currentDate').textContent = today.charAt(0).toUpperCase() + today.slice(1);
  Promise.all([loadOverview(), loadHotTopics(), loadArticles(1)]);
})();
