(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const state = { type: null, id: null, page: 1, pageSize: 20, loading: false };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const num = (value) => new Intl.NumberFormat('vi-VN').format(Number(value || 0));
  function safeHref(value) {
    try { const url = new URL(String(value || ''), window.location.origin); return ['http:', 'https:'].includes(url.protocol) ? esc(url.href) : '#'; }
    catch { return '#'; }
  }
  function articleSlug(value) {
    return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^\x00-\x7F]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 90) || 'topic';
  }
  function articlePageHref(item) {
    const id = Number(item?.article_id || 0);
    return id > 0 ? `/tin/${id}/${articleSlug(item?.title)}` : safeHref(item?.url);
  }
  function articleTimestampHTML(item, value) {
    const label = localDateText(value);
    const id = Number(item?.article_id || 0);
    if (id < 1) return label;
    return `<a class="article-timestamp-link" href="${articlePageHref(item)}" title="Mở trang tin trên 3T" aria-label="Mở trang tin 3T: ${esc(item?.title || 'Bài viết')}">${label}</a>`;
  }
  function originalArticleURL(item) {
    try {
      const url = new URL(String(item?.url || ''));
      return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : '';
    } catch { return ''; }
  }
  function articlePrimaryHref(item) {
    const sourceURL = originalArticleURL(item);
    return sourceURL ? esc(sourceURL) : articlePageHref(item);
  }
  function articlePrimaryTargetAttrs(item) {
    return originalArticleURL(item) ? ' target="_blank" rel="noopener noreferrer"' : '';
  }
  function articleSourceLinkHTML(item) {
    const sourceURL = originalArticleURL(item);
    return sourceURL ? `<a class="article-source-link" href="${esc(sourceURL)}" target="_blank" rel="noopener noreferrer">Đọc bài gốc ↗</a>` : '';
  }
  function safeImageHref(value) {
    try { const url = new URL(String(value || '')); return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? esc(url.href) : ''; }
    catch { return ''; }
  }
  // Hide unavailable publisher thumbnails and restore the full-width text layout.
  // Image error events do not bubble, so use capture mode on document.
  document.addEventListener('error', event => {
    const image = event.target;
    if (!image || !image.matches || !image.matches('img.article-thumbnail-img')) return;
    const link = image.closest('.article-image-link');
    if (!link) return;
    link.classList.add('image-failed');
    link.style.display = 'none';
    const layout = link.closest('.article-media-layout, .timeline-card');
    if (layout) layout.classList.remove('has-image');
  }, true);
  function localDateText(value) {
    if (!value) return 'Chưa rõ thời gian';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return new Intl.DateTimeFormat('vi-VN', {timeZone:'Asia/Ho_Chi_Minh', day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'}).format(date);
  }
  async function getJSON(url) {
    const response = await fetch(url, {headers:{Accept:'application/json'}, credentials:'same-origin'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Máy chủ trả về phản hồi không hợp lệ.'); }
    if (!response.ok) throw new Error(data.detail || `Không tải được timeline (${response.status}).`);
    return data;
  }
  function parseTimelineURL() {
    const params = new URLSearchParams(window.location.search);
    const type = params.get('type') || params.get('timeline_type');
    const id = Number(params.get('id') || params.get('timeline_id'));
    const page = Number(params.get('page') || params.get('timeline_page') || 1);
    if (!['topic', 'entity', 'article'].includes(type) || !Number.isSafeInteger(id) || id < 1) return null;
    return {type, id, page:Number.isSafeInteger(page) && page > 0 ? Math.min(page, 10000) : 1};
  }
  function buildURL(page = state.page) {
    const url = new URL('/timeline', window.location.origin);
    url.searchParams.set('type', state.type);
    url.searchParams.set('id', String(state.id));
    url.searchParams.set('page', String(Math.max(1, Number(page) || 1)));
    return url.href;
  }
  function setStatus(title, detail, isError = false) {
    $('#standaloneTimelineContainer').innerHTML = `<div class="standalone-status"><strong>${esc(title)}</strong>${esc(detail)}${isError ? '<p><a href="/news">Về trang tin và thử mở timeline khác</a></p>' : ''}</div>`;
    $('#standaloneTimelinePagination').innerHTML = '';
  }
  function timelineItemsHTML(items) {
    if (!items.length) return '<div class="standalone-status"><strong>Chưa có bài báo được liên kết</strong>Chưa tìm thấy bài báo cho timeline này.</div>';
    return items.map(item => {
      const related = [item.entities ? `Thực thể: ${item.entities}` : '', item.topics ? `Chủ đề: ${item.topics}` : ''].filter(Boolean).join(' · ');
      const src = safeImageHref(item.image_url);
      const thumbnail = src ? `<a class="article-image-link timeline-thumbnail" href="${articlePrimaryHref(item)}"${articlePrimaryTargetAttrs(item)}><img class="article-thumbnail-img" src="${src}" alt="" loading="lazy" decoding="async" referrerpolicy="strict-origin-when-cross-origin"></a>` : '';
      return `<article class="timeline-entry">
        <div class="timeline-time">${articleTimestampHTML(item, item.timeline_at || item.published_at || item.collected_at)}</div>
        <div class="timeline-rail"><span class="timeline-dot"></span><span class="timeline-line"></span></div>
        <div class="timeline-card${thumbnail ? ' has-image' : ''}">${thumbnail}<div class="timeline-card-body"><div class="timeline-meta"><span>${esc(item.source_name || 'Nguồn tin')}</span>${item.author ? `<span>· ${esc(item.author)}</span>` : ''}</div><h3><a href="${articlePrimaryHref(item)}"${articlePrimaryTargetAttrs(item)}>${esc(item.title || 'Bài viết')}</a></h3>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}${related ? `<div class="entity-list">${esc(related)}</div>` : ''}${articleSourceLinkHTML(item) ? `<div class="timeline-source-footer">${articleSourceLinkHTML(item)}</div>` : ''}</div></div>
      </article>`;
    }).join('');
  }
  function renderPagination(pagination) {
    if (!pagination || !pagination.total) { $('#standaloneTimelinePagination').innerHTML = ''; return; }
    const start = pagination.start ?? ((pagination.page - 1) * pagination.page_size + 1);
    const end = pagination.end ?? Math.min(pagination.total, pagination.page * pagination.page_size);
    $('#standaloneTimelinePagination').innerHTML = `<span>Hiển thị ${num(start)}–${num(end)} trong ${num(pagination.total)} bài</span><div class="pagination-actions"><button type="button" data-page-direction="prev" ${pagination.page <= 1 ? 'disabled' : ''}>← Trước</button><button type="button" data-page-direction="next" ${pagination.page >= pagination.total_pages ? 'disabled' : ''}>Sau →</button></div>`;
  }
  function timelineEndpoint() {
    const suffix = `?page=${state.page}&page_size=${state.pageSize}`;
    if (state.type === 'article') return `/api/public/news/articles/${encodeURIComponent(state.id)}/timeline${suffix}`;
    if (state.type === 'entity') return `/api/public/news/entities/${encodeURIComponent(state.id)}/timeline${suffix}`;
    return `/api/public/news/topics/${encodeURIComponent(state.id)}/timeline${suffix}`;
  }
  async function loadTimeline(page = state.page, updateHistory = false) {
    if (state.loading) return;
    state.page = Math.max(1, Number(page) || 1);
    state.loading = true;
    if (updateHistory) history.pushState({timelinePage:state.page}, '', buildURL(state.page));
    $('#standaloneShareURL').value = buildURL(state.page);
    $('#standaloneTimelineContainer').innerHTML = '<div class="loading-card">Đang tải timeline…</div>';
    $('#standaloneTimelinePagination').innerHTML = '';
    try {
      const data = await getJSON(timelineEndpoint());
      const anchor = data.anchor || data.topic || data.entity || {};
      const name = data.timeline_name || data.topic?.name || data.entity?.canonical_name || anchor.title || 'Timeline tin tức';
      const label = state.type === 'topic' ? 'Chủ đề' : state.type === 'entity' ? 'Thực thể' : 'Bài báo';
      document.title = `${name} — Timeline | Tìm Tin Tức`;
      $('#standaloneTimelineTitle').textContent = name;
      $('#standaloneTimelineDescription').textContent = `Timeline ${label.toLocaleLowerCase('vi-VN')} gồm các bài viết được liên kết chính xác theo ${state.type === 'topic' ? 'chủ đề' : state.type === 'entity' ? 'thực thể' : 'bài báo gốc'}, sắp xếp mới nhất trước.`;
      $('#standaloneTimelineMeta').textContent = `${label} · ${num(data.pagination?.total || 0)} bài báo · Trang ${num(data.pagination?.page || state.page)}`;
      $('#standaloneTimelineLabel').textContent = `${label} · ${name}`;
      $('#standaloneTimelineLabel').hidden = false;
      state.page = Number(data.pagination?.page || state.page);
      $('#standaloneShareURL').value = buildURL(state.page);
      $('#standaloneTimelineContainer').innerHTML = timelineItemsHTML(data.items || []);
      renderPagination(data.pagination);
      if (updateHistory) $('#standaloneTimelineTitle').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (error) {
      $('#standaloneTimelineTitle').textContent = 'Không tải được timeline';
      $('#standaloneTimelineDescription').textContent = 'Có thể timeline không còn tồn tại hoặc đường dẫn không hợp lệ.';
      setStatus('Không tải được timeline', error.message, true);
    } finally { state.loading = false; }
  }
  async function copyLink() {
    const field = $('#standaloneShareURL');
    const link = buildURL(state.page);
    field.value = link;
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(link);
      else { field.focus(); field.select(); if (!document.execCommand('copy')) throw new Error('Clipboard unavailable'); }
      $('#standaloneShareFeedback').textContent = 'Đã sao chép link timeline. Bạn có thể gửi đường dẫn này cho người khác.';
    } catch (_) {
      field.focus(); field.select();
      $('#standaloneShareFeedback').textContent = 'Chưa thể tự sao chép. Link đã được chọn — hãy nhấn Ctrl+C (hoặc Cmd+C trên Mac).';
    }
  }
  document.addEventListener('click', async event => {
    const copy = event.target.closest('#standaloneCopyLinkButton, #standaloneCopyLinkButton2');
    if (copy) { await copyLink(); return; }
    const pager = event.target.closest('[data-page-direction]');
    if (pager && !pager.disabled) await loadTimeline(Math.max(1, state.page + (pager.dataset.pageDirection === 'next' ? 1 : -1)), true);
  });
  window.addEventListener('popstate', () => {
    const params = parseTimelineURL();
    if (!params) return;
    state.page = params.page;
    loadTimeline(params.page, false);
  });
  async function initialize() {
    const params = parseTimelineURL();
    if (!params) {
      $('#standaloneTimelineTitle').textContent = 'Mở timeline được chia sẻ';
      $('#standaloneTimelineDescription').textContent = 'Đường dẫn này chưa chỉ định timeline. Hãy mở một timeline từ trang Tìm Tin Tức rồi chọn “Chia sẻ timeline”.';
      $('#standaloneShareURL').value = window.location.href;
      setStatus('Chưa có timeline để hiển thị', 'Hãy quay lại trang tin, chọn một chủ đề hoặc thực thể rồi tạo link chia sẻ.', true);
      return;
    }
    state.type = params.type; state.id = params.id; state.page = params.page;
    $('#standaloneCopyLinkButton').disabled = false;
    $('#standaloneCopyLinkButton2').disabled = false;
    $('#standaloneShareURL').value = buildURL(state.page);
    await loadTimeline(state.page, false);
  }
  initialize();
})();
