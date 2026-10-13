(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const params = new URLSearchParams(window.location.search);
  const type = params.get('type');
  const id = Number(params.get('id'));
  const pageSize = 20;
  let page = Math.max(1, Number(params.get('page')) || 1);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const num = value => new Intl.NumberFormat('vi-VN').format(Number(value || 0));
  const safeHref = value => {
    try { const url = new URL(String(value || ''), window.location.origin); return ['http:','https:'].includes(url.protocol) ? esc(url.href) : '#'; }
    catch { return '#'; }
  };
  function dateText(value) {
    if (!value) return 'Chưa rõ thời gian';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return new Intl.DateTimeFormat('vi-VN', {timeZone:'Asia/Ho_Chi_Minh', day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'}).format(date);
  }
  async function getJSON(url) {
    const response = await fetch(url, {headers:{Accept:'application/json'}, credentials:'same-origin'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Máy chủ trả về phản hồi không hợp lệ.'); }
    if (!response.ok) throw new Error(data.detail || `Yêu cầu thất bại (${response.status})`);
    return data;
  }
  function showMessage(title, message, error = false) {
    $('#followingArticles').innerHTML = `<div class="message-card"><strong>${esc(title)}</strong>${esc(message)}${error ? '<br><small>Vui lòng thử tải lại trang.</small>' : ''}</div>`;
  }
  function renderArticles(items) {
    if (!items.length) { showMessage('Chưa có bài báo', 'Chưa có bài nào được liên kết với topic/entity này.'); return; }
    $('#followingArticles').innerHTML = items.map(item => {
      const related = type === 'topic' ? item.entities : item.topics;
      const relatedLabel = type === 'topic' ? 'Entity liên quan: ' : 'Topic liên quan: ';
      return `<article class="radar-row article-row following-article-row">
        <div class="radar-row-main"><div class="radar-row-meta"><span class="source-chip">${esc(item.source_name)}</span><span>${esc(dateText(item.timeline_at || item.published_at || item.collected_at))}</span></div>
          <h3><a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h3>
          ${item.snippet ? `<p class="radar-row-snippet">${esc(item.snippet)}</p>` : ''}
          ${related ? `<p class="following-related">${relatedLabel}${esc(related)}</p>` : ''}
        </div><div class="radar-row-end"><a class="article-source-link" href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">Đọc bài gốc ↗</a></div>
      </article>`;
    }).join('');
  }
  function renderPagination(pagination) {
    if (!pagination || !pagination.total) { $('#followingPagination').innerHTML = ''; return; }
    const start = pagination.start ?? ((pagination.page - 1) * pagination.page_size + 1);
    const end = pagination.end ?? Math.min(pagination.total, pagination.page * pagination.page_size);
    const prev = pagination.page > 1 ? `<a class="following-page-link" href="${buildPageUrl(pagination.page - 1)}">← Trước</a>` : '<span class="following-page-disabled">← Trước</span>';
    const next = pagination.page < pagination.total_pages ? `<a class="following-page-link" href="${buildPageUrl(pagination.page + 1)}">Sau →</a>` : '<span class="following-page-disabled">Sau →</span>';
    $('#followingPagination').innerHTML = `<span>Hiển thị ${num(start)}–${num(end)} trong ${num(pagination.total)} bài</span><div class="pagination-actions">${prev}${next}</div>`;
  }
  function buildPageUrl(nextPage) { return `/news/following?type=${encodeURIComponent(type)}&id=${id}&page=${nextPage}`; }
  async function load() {
    if (!['topic','entity'].includes(type) || !Number.isInteger(id) || id < 1) {
      $('#followingTitle').textContent = 'Đường dẫn không hợp lệ';
      $('#followingSubtitle').textContent = 'Quay lại Radar và mở “Xem thêm” tại một topic/entity đang theo dõi.';
      showMessage('Không tìm thấy mục theo dõi', 'Thiếu hoặc sai loại topic/entity.');
      return;
    }
    $('#followingArticles').innerHTML = '<div class="loading-card">Đang tải bài báo…</div>';
    try {
      const endpoint = type === 'topic' ? `/api/public/news/topics/${id}/timeline` : `/api/public/news/entities/${id}/timeline`;
      const data = await getJSON(`${endpoint}?page=${page}&page_size=${pageSize}`);
      const record = data.topic || data.entity;
      const name = type === 'topic' ? record.name : record.canonical_name;
      $('#followingTitle').textContent = name;
      document.title = `${name} — News Radar`;
      const pagination = data.pagination || {};
      page = pagination.page || page;
      $('#followingSubtitle').textContent = `${type === 'topic' ? 'Topic sự kiện' : 'Entity / đối tượng'} · ${num(pagination.total)} bài báo · sắp xếp theo thời gian mới nhất.`;
      renderArticles(data.items || []); renderPagination(pagination);
    } catch (error) { showMessage('Không tải được danh sách bài viết', error.message, true); }
  }
  load();
})();
