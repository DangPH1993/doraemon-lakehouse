(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  function todayInVietnam() {
    const parts = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Ho_Chi_Minh', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(new Date());
    const values = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }
  function formatDateInput(value) {
    const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? `${match[3]}/${match[2]}/${match[1]}` : String(value || '');
  }
  const MAX_SAVED_TIMELINES = 10;
  const initialToday = todayInVietnam();
  const state = {
    period: 'day', selectedTimeline: null, timelinePage: 1, articlePage: 1, searchPage: 1,
    articlePageSize: 8, searchPageSize: 20, timelinePageSize: 20, searchQuery: '', fieldCategory: '',
    articleHasMore: false, articleLoading: false, articleVisibleCount: 0, articleRequestSeq: 0, articleLoadError: '',
    dateFrom: initialToday, dateTo: initialToday,
    user: null, savedTimelines: [], picked: new Set(), followMeta: new Map(),
    pendingFollow: null, followSaving: false, authMode: 'login', telegram: null, planInfo: null, openPlanAfterAuth: false, passwordResetToken: null, paymentProofSubmitting: false, proPurchaseSelected: false
  };
  $('#articleDateFrom').value = initialToday;
  $('#articleDateTo').value = initialToday;
  const periodLabels = {day: 'Hôm nay', week: 'Tuần này', month: 'Tháng này'};
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const num = (value) => new Intl.NumberFormat('vi-VN').format(Number(value || 0));
  const safeHref = (value) => {
    try { const url = new URL(String(value || ''), window.location.origin); return ['http:', 'https:'].includes(url.protocol) ? esc(url.href) : '#'; }
    catch { return '#'; }
  };
  function articleSlug(value) {
    return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^\x00-\x7F]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 90) || 'topic';
  }
  function articlePageHref(item) {
    const id = Number(item?.article_id || 0);
    return id > 0 ? `/tin/${id}/${articleSlug(item?.title)}` : safeHref(item?.url);
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
  function articlePermalinkLinkHTML(item) {
    const id = Number(item?.article_id || 0);
    return id > 0 ? `<a class="article-source-link article-permalink-link" href="${articlePageHref(item)}" target="_blank" rel="noopener noreferrer">Trang tin 3T ↗</a>` : '';
  }
  const safeImageHref = (value) => {
    try {
      const url = new URL(String(value || ''));
      return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? esc(url.href) : '';
    } catch { return ''; }
  };
  function articleThumbnailHTML(item, className = 'article-thumbnail') {
    const src = safeImageHref(item.image_url);
    if (!src) return '';
    return `<a class="article-image-link ${className}" href="${articlePrimaryHref(item)}"${articlePrimaryTargetAttrs(item)} tabindex="-1" aria-hidden="true"><img class="article-thumbnail-img" src="${src}" alt="" loading="lazy" decoding="async" referrerpolicy="strict-origin-when-cross-origin"></a>`;
  }
  document.addEventListener('error', (event) => {
    if (event.target && event.target.matches && event.target.matches('img.article-thumbnail-img')) {
      const link = event.target.closest('.article-image-link');
      if (link) { link.classList.add('image-failed'); const layout = link.closest('.article-media-layout, .timeline-card'); if (layout) layout.classList.remove('has-image'); }
    }
  }, true);
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
    node.classList.remove('error');
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

  let lastEntityCloudItems = [];
  let entityCloudResizeTimer = null;

  function entityBubbleBaseSize(entity) {
    const count = Math.max(1, Number(entity.article_count || 1));
    return Math.min(142, Math.max(58, Math.round(40 + Math.sqrt(count) * 18)));
  }

  function layoutEntityCloud(container, items) {
    if (!container || !items.length) return;
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) {
      window.requestAnimationFrame(() => layoutEntityCloud(container, items));
      return;
    }

    const cx = width / 2;
    const cy = height / 2;
    const gap = width < 420 ? 4 : 7;
    const responsiveScale = Math.min(1, Math.max(0.54, width / 600));
    const indexed = items.map((item, index) => ({ item, index, base: entityBubbleBaseSize(item) }))
      .sort((a, b) => b.base - a.base);
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    const phase = Math.random() * Math.PI * 2;
    let packed = null;

    // Place larger bubbles first and fill from the center outward. Retry at
    // slightly smaller scales on narrow screens so every bubble stays inside
    // the circular cloud without overlaps.
    for (const compression of [1, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76, 0.72, 0.68, 0.64]) {
      const scale = responsiveScale * compression;
      const placed = [];
      let failed = false;
      for (const entry of indexed) {
        const size = Math.max(width < 420 ? 34 : 40, Math.round(entry.base * scale));
        const allowedRadius = Math.max(0, Math.min(width, height) / 2 - size / 2 - 7);
        let found = null;
        const candidates = 5200;
        for (let trial = 0; trial < candidates; trial++) {
          const rho = Math.sqrt(trial / candidates);
          const angle = trial * goldenAngle + entry.index * 1.61803398875 + phase;
          const x = cx + Math.cos(angle) * rho * allowedRadius;
          const y = cy + Math.sin(angle) * rho * allowedRadius;
          let overlaps = false;
          for (const other of placed) {
            const dx = x - other.x;
            const dy = y - other.y;
            const minDistance = (size + other.size) / 2 + gap;
            if (dx * dx + dy * dy < minDistance * minDistance) {
              overlaps = true;
              break;
            }
          }
          if (!overlaps) {
            found = { index: entry.index, x, y, size };
            break;
          }
        }
        if (!found) {
          failed = true;
          break;
        }
        placed.push(found);
      }
      if (!failed) {
        packed = placed;
        break;
      }
    }

    // Defensive fallback: a smaller, still circular arrangement if unusual
    // viewport dimensions prevent the normal packing pass.
    if (!packed) {
      packed = indexed.map((entry, i) => {
        const angle = phase + i * goldenAngle;
        const radius = Math.min(width, height) * 0.31 * Math.sqrt((i + 1) / indexed.length);
        const size = Math.max(30, Math.round(entry.base * responsiveScale * 0.58));
        return {
          index: entry.index,
          x: Math.max(size / 2 + 3, Math.min(width - size / 2 - 3, cx + Math.cos(angle) * radius)),
          y: Math.max(size / 2 + 3, Math.min(height - size / 2 - 3, cy + Math.sin(angle) * radius)),
          size,
        };
      });
    }

    for (const position of packed) {
      const bubble = container.querySelector(`[data-cloud-index="${position.index}"]`);
      if (!bubble) continue;
      bubble.style.left = `${position.x}px`;
      bubble.style.top = `${position.y}px`;
      bubble.style.width = `${position.size}px`;
      bubble.style.height = `${position.size}px`;
      bubble.style.setProperty('--bubble-size', `${position.size}px`);
    }
  }

  function renderEntityCloud(items) {
    const container = $('#entityCloud');
    if (!items.length) {
      lastEntityCloudItems = [];
      container.innerHTML = '<div class="entity-cloud-empty">Chưa có entity nổi bật trong 24 giờ gần nhất. Các bubble sẽ xuất hiện khi bài báo được liên kết với entity chuẩn.</div>';
      return;
    }
    const tones = ['blue','teal','violet','orange','green','slate'];
    // Shuffle the ranked top 20 for a varied cloud while packing bubbles into a circle.
    const cloudItems = items.slice(0, 20).map((item) => ({...item, _cloudKey: Math.random()}))
      .sort((a, b) => a._cloudKey - b._cloudKey);
    lastEntityCloudItems = cloudItems;
    container.innerHTML = cloudItems.map((entity, index) => {
      const count = Math.max(1, Number(entity.article_count || 1));
      const longName = String(entity.name || '').length > 19 ? ' is-long' : '';
      const title = `${entity.name || 'Entity'} · ${num(count)} bài báo trong 24 giờ · ${num(entity.source_count)} nguồn`;
      return `<button type="button" class="entity-bubble tone-${tones[index % tones.length]}${longName}" data-cloud-index="${index}" data-open-timeline="entity" data-timeline-id="${Number(entity.entity_id)}" data-timeline-name="${esc(entity.name)}" title="${esc(title)}" aria-label="Mở timeline ${esc(entity.name)}; ${num(count)} bài báo trong 24 giờ">
        <span class="entity-bubble-name">${esc(entity.name)}</span><span class="entity-bubble-count">${num(count)} bài</span>
      </button>`;
    }).join('');
    layoutEntityCloud(container, cloudItems);
  }
  window.addEventListener('resize', () => {
    if (entityCloudResizeTimer) window.clearTimeout(entityCloudResizeTimer);
    entityCloudResizeTimer = window.setTimeout(() => {
      const container = $('#entityCloud');
      if (container && lastEntityCloudItems.length) layoutEntityCloud(container, lastEntityCloudItems);
    }, 120);
  });
  async function loadEntityCloud() {
    const container = $('#entityCloud');
    if (!container) return;
    container.innerHTML = '<div class="entity-cloud-loading">Đang tải entity nổi bật…</div>';
    try {
      const data = await getJSON('/api/public/news/entity-cloud?limit=20');
      renderEntityCloud(data.items || []);
    } catch (error) {
      container.innerHTML = `<div class="entity-cloud-empty">Không tải được entity nổi bật: ${esc(error.message)}. Vui lòng thử tải lại trang.</div>`;
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
      const thumbnail = articleThumbnailHTML(item, 'timeline-thumbnail');
      return `<article class="timeline-entry">
        <div class="timeline-time">${esc(localDateText(item.timeline_at || item.published_at || item.collected_at, true))}</div>
        <div class="timeline-rail"><span class="timeline-dot"></span><span class="timeline-line"></span></div>
        <div class="timeline-card${thumbnail ? ' has-image' : ''}">${thumbnail}<div class="timeline-card-body"><div class="timeline-meta"><span>${esc(item.source_name)}</span>${item.author ? `<span>· ${esc(item.author)}</span>` : ''}</div><h3><a href="${articlePrimaryHref(item)}"${articlePrimaryTargetAttrs(item)}>${esc(item.title)}</a></h3>${item.snippet ? `<p>${esc(item.snippet)}</p>` : ''}${related ? `<div class="entity-list">${esc(related)}</div>` : ''}<div class="timeline-source-footer">${articlePermalinkLinkHTML(item)}${articleSourceLinkHTML(item)}</div></div></div>
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
    state.timelinePage = Number(data.pagination?.page || state.timelinePage || 1);
    const modeText = data.mode === 'topic_id' ? 'Topic ID' : data.mode === 'entity_id' ? 'Entity ID' : 'Bài báo';
    $('#timelineSelectionLabel').textContent = `${modeText} · ${name}`;
    $('#timelineContainer').innerHTML = timelineItemsHTML(data.items || [], data.mode || type);
    renderPagination('#timelinePagination', data.pagination, 'timeline');
    $('#shareTimelineButton').disabled = !(Number.isInteger(id) && id > 0 && ['topic', 'entity', 'article'].includes(type));
    setFeedback('#timelineShareFeedback', '');
  }
  async function fetchTimelineData(type, id, page = 1, pageSize = 20) {
    if (type === 'article') return getJSON(`/api/public/news/articles/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
    if (type === 'entity') return getJSON(`/api/public/news/entities/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
    return getJSON(`/api/public/news/topics/${encodeURIComponent(id)}/timeline?page=${page}&page_size=${pageSize}`);
  }
  async function loadTimeline(type, id, page = 1, shouldScroll = true) {
    if (!['topic', 'entity', 'article'].includes(type) || !Number.isInteger(Number(id)) || Number(id) < 1) {
      setMessage('#timelineContainer', 'Liên kết timeline không hợp lệ', 'Hãy kiểm tra lại loại timeline và mã timeline trong liên kết.', true);
      return;
    }
    state.timelinePage = Math.max(1, Number(page) || 1);
    $('#shareTimelineButton').disabled = true;
    $('#timelineShareFeedback').textContent = '';
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

  function sharedTimelineFromURL() {
    const params = new URLSearchParams(window.location.search);
    const type = params.get('timeline_type');
    const id = Number(params.get('timeline_id'));
    const page = Number(params.get('timeline_page') || 1);
    if (!['topic', 'entity', 'article'].includes(type) || !Number.isSafeInteger(id) || id < 1) return null;
    return {type, id, page: Number.isSafeInteger(page) && page > 0 ? Math.min(page, 10000) : 1};
  }
  function buildTimelineShareURL() {
    if (!state.selectedTimeline || !['topic', 'entity', 'article'].includes(state.selectedTimeline.type) || !Number.isSafeInteger(Number(state.selectedTimeline.id)) || Number(state.selectedTimeline.id) < 1) return null;
    // Share a dedicated view so the homepage's article feed cannot push the timeline away.
    const url = new URL('/timeline', window.location.origin);
    url.searchParams.set('type', state.selectedTimeline.type);
    url.searchParams.set('id', String(state.selectedTimeline.id));
    url.searchParams.set('page', String(Math.max(1, Number(state.timelinePage) || 1)));
    return url.href;
  }
  function openTimelineShareDialog() {
    const shareURL = buildTimelineShareURL();
    if (!shareURL) {
      setFeedback('#timelineShareFeedback', 'Hãy mở một timeline trước khi chia sẻ.');
      return;
    }
    $('#timelineShareURL').value = shareURL;
    $('#timelineShareModalFeedback').textContent = '';
    $('#timelineShareModal').hidden = false;
    $('#timelineShareURL').focus();
    $('#timelineShareURL').select();
  }
  function closeTimelineShareDialog() {
    $('#timelineShareModal').hidden = true;
    const trigger = $('#shareTimelineButton');
    if (trigger && !trigger.disabled) trigger.focus();
  }
  async function copyTimelineShareURL() {
    const field = $('#timelineShareURL');
    const shareURL = field?.value || buildTimelineShareURL();
    if (!shareURL) {
      setFeedback('#timelineShareModalFeedback', 'Hãy mở một timeline trước khi chia sẻ.');
      return;
    }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(shareURL);
      } else {
        field.value = shareURL; field.focus(); field.select();
        const copied = document.execCommand('copy');
        if (!copied) throw new Error('Trình duyệt không cho phép sao chép tự động.');
      }
      setFeedback('#timelineShareModalFeedback', 'Đã copy link thành công. Bạn có thể gửi link này cho người khác.', true);
    } catch (error) {
      // Leave the URL selected so users can copy it manually with Ctrl+C / Cmd+C.
      field.value = shareURL; field.focus(); field.select();
      setFeedback('#timelineShareModalFeedback', 'Chưa thể tự động copy. Link đã được chọn — hãy nhấn Ctrl+C (hoặc Cmd+C trên Mac).');
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
    const thumbnail = articleThumbnailHTML(item);
    return `<article class="radar-row article-row${feedMode ? ' feed-row' : ''}">
      <div class="article-media-layout${thumbnail ? ' has-image' : ''}">${thumbnail}<div class="radar-row-main"><div class="radar-row-meta"><span class="source-chip">${esc(item.source_name)}</span><span>${esc(time)}</span>${fieldTag}${feedMode ? '<span class="feed-match-label">LIÊN QUAN ĐẾN BẠN</span>' : ''}</div>
        <h3><a href="${articlePrimaryHref(item)}"${articlePrimaryTargetAttrs(item)}>${esc(item.title)}</a></h3>
        ${item.snippet ? `<p class="radar-row-snippet">${esc(item.snippet)}</p>` : ''}
        ${tags ? `<div class="article-tags">${tags}</div>` : ''}
        ${articleActionsHTML(item, feedMode)}
        <div class="article-card-footer"><span></span>${articlePermalinkLinkHTML(item)}${articleSourceLinkHTML(item)}</div>
      </div></div>
    </article>`;
  }

  function renderArticleList(target, items, feedMode = false, emptyTitle = 'Chưa có bài báo phù hợp', emptyDescription = 'Hãy thử tìm bằng từ khóa khác.') {
    if (!items.length) { setMessage(target, emptyTitle, emptyDescription); return; }
    $(target).innerHTML = items.map(item => articleRowHTML(item, feedMode)).join('');
  }
  function articleSkeletonHTML(count = 5) {
    return Array.from({length: count}, () => `<article class="article-skeleton" aria-hidden="true"><span class="article-skeleton-meta"></span><span class="article-skeleton-title"></span><span class="article-skeleton-title short"></span><span class="article-skeleton-summary"></span></article>`).join('');
  }
  let articleLoadObserver = null;
  function observeArticleLoadSentinel() {
    const sentinel = $('#articleLoadSentinel');
    if (articleLoadObserver) articleLoadObserver.disconnect();
    if (!sentinel || !state.articleHasMore || !('IntersectionObserver' in window)) return;
    if (!articleLoadObserver) {
      articleLoadObserver = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting)) loadArticles(state.articlePage + 1, true);
      }, {rootMargin: '420px 0px'});
    }
    articleLoadObserver.observe(sentinel);
  }
  function updateArticleLoadMoreUI() {
    const container = $('#articlePagination');
    if (!container) return;
    if (state.articleHasMore) {
      const buttonLabel = state.articleLoading ? 'Đang tải thêm…' : (state.articleLoadError ? 'Thử tải lại ↻' : 'Tải thêm tin ↓');
      const status = state.articleLoading ? 'Đang lấy lô tin tiếp theo…' : (state.articleLoadError ? `Chưa tải được lô tiếp theo: ${esc(state.articleLoadError)}` : `Đã hiển thị ${num(state.articleVisibleCount)} bài`);
      container.innerHTML = `<div class="article-load-more-controls"><span class="${state.articleLoadError ? 'article-load-error' : ''}" role="status">${status}</span><button type="button" id="loadMoreArticlesButton" class="article-load-more-button" ${state.articleLoading ? 'disabled' : ''}>${buttonLabel}</button></div><div id="articleLoadSentinel" aria-hidden="true"></div>`;
      if (!state.articleLoading && !state.articleLoadError) observeArticleLoadSentinel();
      else if (articleLoadObserver) articleLoadObserver.disconnect();
      return;
    }
    if (articleLoadObserver) articleLoadObserver.disconnect();
    container.innerHTML = state.articleVisibleCount ? `<div class="article-load-more-controls article-load-complete"><span role="status">Đã tải hết ${num(state.articleVisibleCount)} bài phù hợp.</span></div>` : '';
  }
  async function loadArticles(page = 1, append = false) {
    if (append && (state.articleLoading || !state.articleHasMore)) return;
    const requestId = ++state.articleRequestSeq;
    state.articleLoading = true;
    state.articleLoadError = '';
    if (!append) {
      state.articlePage = 0;
      state.articleHasMore = false;
      state.articleVisibleCount = 0;
      if (articleLoadObserver) articleLoadObserver.disconnect();
      $('#dailyArticles').setAttribute('aria-busy', 'true');
      $('#dailyArticles').innerHTML = articleSkeletonHTML(5);
      $('#articlePagination').innerHTML = '';
    } else {
      const button = $('#loadMoreArticlesButton');
      if (button) { button.disabled = true; button.textContent = 'Đang tải thêm…'; }
    }
    const params = new URLSearchParams({page:String(page), page_size:String(state.articlePageSize), progressive:'true'});
    if (state.fieldCategory) params.set('field_category', state.fieldCategory);
    if (state.dateFrom) params.set('date_from', state.dateFrom);
    if (state.dateTo) params.set('date_to', state.dateTo);
    try {
      const data = await getJSON(`/api/public/news/articles?${params.toString()}`);
      if (requestId !== state.articleRequestSeq) return;
      state.dateFrom = data.date_from || state.dateFrom;
      state.dateTo = data.date_to || state.dateTo;
      $('#articleDateFrom').value = state.dateFrom;
      $('#articleDateTo').value = state.dateTo;
      const fromLabel = formatDateInput(state.dateFrom);
      const toLabel = formatDateInput(state.dateTo);
      const rangeDescription = fromLabel === toLabel ? fromLabel : `${fromLabel} đến ${toLabel}`;
      const items = data.items || [];
      state.articlePage = Number(data.pagination?.page || page);
      state.articleHasMore = Boolean(data.has_more) && items.length > 0;
      state.articleVisibleCount += items.length;
      if (append) {
        if (items.length) $('#dailyArticles').insertAdjacentHTML('beforeend', items.map(item => articleRowHTML(item, false)).join(''));
        else if (!state.articleVisibleCount) renderArticleList('#dailyArticles', items, false, 'Chưa có bài báo trong khoảng ngày đã chọn', `Không có bài nào từ ${fromLabel} đến ${toLabel}, theo giờ Việt Nam.`);
      } else {
        renderArticleList('#dailyArticles', items, false, 'Chưa có bài báo trong khoảng ngày đã chọn', `Không có bài nào từ ${fromLabel} đến ${toLabel}, theo giờ Việt Nam.`);
      }
      $('#dailyArticles').setAttribute('aria-busy', 'false');
      $('#articleDateLabel').textContent = `${num(state.articleVisibleCount)} bài đã tải từ ${rangeDescription}${state.articleHasMore ? ' · Cuộn xuống để tải tiếp' : ' · Đã tải hết tin phù hợp'}, theo giờ Việt Nam.`;
      setFeedback('#articleDateFilterFeedback', '');
      syncFollowButtons();
    } catch (error) {
      if (requestId !== state.articleRequestSeq) return;
      if (append && state.articleVisibleCount) {
        state.articleLoadError = error.message;
        state.articleHasMore = true;
      } else {
        setMessage('#dailyArticles', 'Không tải được bài báo', error.message, true);
      }
    } finally {
      if (requestId === state.articleRequestSeq) {
        state.articleLoading = false;
        $('#dailyArticles').setAttribute('aria-busy', 'false');
        updateArticleLoadMoreUI();
      }
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
    $('#authArea').innerHTML = `<div class="account-menu" id="accountMenu"><button class="auth-user-name account-menu-toggle" type="button" id="accountMenuToggle" aria-haspopup="true" aria-expanded="false"><span class="account-greeting">Xin chào, ${esc(state.user.username)}</span><svg class="account-menu-chevron" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="m4 6 4 4 4-4"/></svg></button><div class="account-menu-dropdown" id="accountMenuDropdown" hidden><button type="button" id="openAccountButton">Thông tin tài khoản</button><button type="button" id="logoutButton">Đăng xuất</button></div></div>`;
    $('#accountIdentityLine').textContent = `${state.user.username}${state.user.email ? ` · ${state.user.email}` : ''} · Quản lý gói dịch vụ và thông báo Telegram`;
  }
  // Reset tokens remain in the URL fragment, not in the request URL or referrer.
  function readPasswordResetToken() {
    const queryToken = new URLSearchParams(window.location.search).get('reset_token');
    const fragment = String(window.location.hash || '').replace(/^#/, '');
    return queryToken || new URLSearchParams(fragment).get('reset_token');
  }
  function setAuthMode(mode) {
    state.authMode = ['register', 'forgot', 'reset'].includes(mode) ? mode : 'login';
    const registering = state.authMode === 'register';
    const forgot = state.authMode === 'forgot';
    const resetting = state.authMode === 'reset';
    $('#authTitle').textContent = registering ? 'Tạo tài khoản' : forgot ? 'Quên mật khẩu' : resetting ? 'Đặt lại mật khẩu' : 'Đăng nhập';
    $('#authDescription').textContent = registering
      ? 'Tạo tài khoản để xem Radar. Nâng cấp Pro để follow và nhận tin mới qua Telegram.'
      : forgot ? 'Nhập email đã đăng ký. Nếu tài khoản tồn tại, bạn sẽ nhận được liên kết đặt lại mật khẩu.'
      : resetting ? 'Liên kết đặt lại mật khẩu chỉ dùng một lần và hết hạn sau 30 phút.'
      : 'Đăng nhập để xem feed cá nhân và quản lý gói dịch vụ.';
    $('#authIdentifierLabel').textContent = registering ? 'Tên đăng nhập' : forgot ? 'Email đăng ký' : 'Tên đăng nhập hoặc email';
    $('#authIdentifier').type = forgot ? 'email' : 'text';
    $('#authIdentifier').placeholder = registering ? 'Tên đăng nhập' : forgot ? 'email@example.com' : 'Tên đăng nhập hoặc email';
    $('#authIdentifier').autocomplete = registering || resetting ? 'new-password' : forgot ? 'email' : 'username';
    $('#authIdentifier').maxLength = registering ? 32 : 254;
    $('#authIdentifier').required = !resetting;
    $('#authIdentifier').closest('.auth-field').hidden = resetting;
    $('#authEmailField').hidden = !registering;
    $('#authEmail').required = registering;
    $('#authPassword').closest('.auth-field').hidden = forgot;
    $('#authPassword').required = !forgot;
    $('#authPassword').minLength = registering || resetting ? 8 : 1;
    $('#authPassword').autocomplete = registering || resetting ? 'new-password' : 'current-password';
    $('#authPassword').closest('.auth-field').querySelector('span').textContent = resetting ? 'Mật khẩu mới' : 'Mật khẩu';
    $('#authPasswordConfirmField').hidden = !(registering || resetting);
    $('#authPasswordConfirm').required = registering || resetting;
    $('#authPasswordConfirmField').querySelector('span').textContent = resetting ? 'Nhập lại mật khẩu mới' : 'Nhập lại mật khẩu';
    $('#authSubmit').textContent = registering ? 'Tạo tài khoản' : forgot ? 'Gửi liên kết đặt lại' : resetting ? 'Lưu mật khẩu mới' : 'Đăng nhập';
    $('#authSwitchLine').hidden = forgot || resetting;
    $('#forgotPasswordLine').hidden = state.authMode !== 'login';
    $('#authSwitchPrompt').textContent = registering ? 'Đã có tài khoản?' : 'Chưa có tài khoản?';
    $('#authSwitchMode').textContent = registering ? 'Đăng nhập' : 'Đăng ký';
    $('#authSwitchMode').dataset.switchAuth = registering ? 'login' : 'register';
    setFeedback('#authFeedback', '');
  }
  function openAuthModal(mode = 'login') {
    $('#authForm').reset(); setAuthMode(mode);
    $('#authModal').hidden = false;
    const focusTarget = mode === 'reset' ? $('#authPassword') : $('#authIdentifier');
    window.setTimeout(() => focusTarget.focus(), 0);
  }
  function closeAuthModal() { $('#authModal').hidden = true; }
  function openProUpgradePopup() { $('#proUpgradeModal').hidden = false; }
  function closeProUpgradePopup() { $('#proUpgradeModal').hidden = true; }
  function setAccountTab(tab = 'plan') {
    const selected = tab === 'telegram' ? 'telegram' : 'plan';
    $('#accountPlanTabButton').classList.toggle('active', selected === 'plan');
    $('#accountTelegramTabButton').classList.toggle('active', selected === 'telegram');
    $('#accountPlanTabButton').setAttribute('aria-selected', String(selected === 'plan'));
    $('#accountTelegramTabButton').setAttribute('aria-selected', String(selected === 'telegram'));
    $('#accountPlanPanel').hidden = selected !== 'plan';
    $('#accountTelegramPanel').hidden = selected !== 'telegram';
    if (selected === 'telegram') {
      if (!(state.user && state.user.plan === 'Pro')) {
        $('#telegramStatus').textContent = 'Kết nối và nhận thông báo Telegram là quyền lợi của gói Pro. Hãy nâng cấp tại tab Gói dịch vụ.';
        $('#telegramEnabled').checked = false;
        $('#telegramEnabled').disabled = true;
        $('#createTelegramLink').disabled = true;
        $('#saveTelegramSettings').disabled = true;
      }
    }
  }
  async function openAccountModal(tab = 'plan') {
    if (!state.user) {
      state.openPlanAfterAuth = true;
      openAuthModal('login');
      setFeedback('#authFeedback', 'Đăng nhập hoặc đăng ký để xem thông tin tài khoản.');
      return;
    }
    $('#accountMenuDropdown').hidden = true;
    $('#accountMenuToggle').setAttribute('aria-expanded', 'false');
    $('#accountModal').hidden = false;
    setAccountTab(tab);
    try { await loadPlanInfo(); } catch (error) { setFeedback('#paymentRequestStatus', error.message); }
    if (tab === 'telegram' && state.user.plan === 'Pro') await loadTelegramSettings();
    $('#accountModal .account-dialog').scrollTop = 0;
  }
  function closeAccountModal() { $('#accountModal').hidden = true; }
  async function openPlanPanel() { return openAccountModal('plan'); }
  function renderPlanInfo(data) {
    state.planInfo = data;
    if (state.user) state.user.plan = data.plan || 'Free';
    const isPro = Boolean(state.user && state.user.plan === 'Pro');
    $('#userPlanBadge').textContent = isPro ? 'PRO' : 'FREE';
    $('#userPlanBadge').classList.toggle('pro', isPro);
    $('#userPlanBadge').classList.toggle('free', !isPro);
    $('#userPlanSummary').textContent = isPro
      ? 'Gói Pro đang hoạt động. Bạn có thể follow topic/entity, sử dụng feed cá nhân và nhận thông báo tin mới qua Telegram khi đã kết nối bot.'
      : 'Gói Free đang hoạt động. Bạn có thể xem Radar công khai; quyền follow và thông báo tin mới qua Telegram dành cho gói Pro.';
    $('#userPlanExpiry').textContent = isPro
      ? `Ngày hết hạn gói Pro: ${data.plan_expires_at ? localDateText(data.plan_expires_at) : 'chưa được thiết lập'}.`
      : 'Gói Free không có thời hạn. Nâng cấp Pro sẽ có hiệu lực 6 tháng kể từ ngày được kích hoạt.';
    $('#proPlanPriceLabel').textContent = data.pro_price_label || 'Xem hướng dẫn thanh toán để biết mức phí Pro.';
    const qrUrl = safeImageHref(data.qr_image_url);
    const request = data.payment_request;
    const waiting = Boolean(request && request.status === 'pending');
    $('#selectProPlanButton').hidden = isPro || !qrUrl;
    $('#selectProPlanButton').textContent = state.proPurchaseSelected ? 'Đã chọn gói Pro' : 'Chọn gói Pro';
    $('#paymentQrWrap').hidden = isPro || !qrUrl || !state.proPurchaseSelected;
    $('#paymentQrImage').src = qrUrl || '';
    $('#paymentQrImage').hidden = !qrUrl;
    $('#paymentInstructions').textContent = data.payment_instructions || '';
    $('#paymentProofForm').hidden = isPro || !qrUrl || !state.proPurchaseSelected;
    const status = $('#paymentRequestStatus');
    status.className = 'payment-request-status';
    if (isPro) {
      status.textContent = 'Gói Pro đã được kích hoạt cho tài khoản của bạn.'; status.classList.add('success');
    } else if (waiting) {
      status.textContent = `Yêu cầu nâng cấp Pro gần nhất đang chờ admin xác nhận (gửi lúc ${localDateText(request.created_at, true)}). Bạn vẫn có thể gửi thông báo/bằng chứng bổ sung nếu cần.`;
    } else if (request && request.status === 'rejected') {
      status.textContent = `Yêu cầu thanh toán trước đó chưa được duyệt.${request.admin_note ? ` Ghi chú của admin: ${request.admin_note}` : ''} Bạn có thể gửi bằng chứng mới sau khi kiểm tra lại.`;
      status.classList.add('error');
    } else if (!qrUrl && !isPro) {
      status.textContent = 'Mã QR thanh toán chưa được cấu hình. Vui lòng quay lại sau hoặc liên hệ admin.';
    } else if (request && request.status === 'approved') {
      status.textContent = 'Yêu cầu gần nhất đã duyệt. Nếu gói hiển thị Free, hãy tải lại trang hoặc liên hệ admin. Bạn vẫn có thể gửi thêm thông báo/bằng chứng nếu cần.'; status.classList.add('success');
    } else if (!isPro && !state.proPurchaseSelected) { status.textContent = 'Chọn gói Pro để xem QR và hướng dẫn thanh toán.';
    } else status.textContent = '';
    $('#proOnlyFeed').hidden = !isPro;
    if (!isPro) {
      $('#feedSummary').textContent = 'Feed cá nhân và follow dành cho gói Pro.';
      $('#userFeedContainer').innerHTML = '<div class="message-card"><strong>Tính năng dành cho gói Pro</strong>Nâng cấp Pro để follow topic/entity, cá nhân hóa feed và nhận bản tin mới qua Telegram.</div>';
      $('#telegramStatus').textContent = 'Kết nối và nhận thông báo Telegram là quyền lợi của gói Pro. Hãy nâng cấp tại tab Gói dịch vụ.';
      $('#telegramEnabled').checked = false; $('#telegramEnabled').disabled = true; $('#createTelegramLink').disabled = true; $('#saveTelegramSettings').disabled = true;
    } else { $('#saveTelegramSettings').disabled = false; }
    syncFollowButtons();
  }
  async function loadPlanInfo() {
    if (!state.user) return null;
    const data = await getJSON('/api/user/plan');
    renderPlanInfo(data);
    return data;
  }
  async function loadPaymentPlanAfterAuth() {
    if (state.openPlanAfterAuth) { state.openPlanAfterAuth = false; await openPlanPanel(); }
  }
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
    if (!state.user) { renderAuthArea(); $('#myRadar').hidden = true; return; }
    renderAuthArea();
    try {
      await loadPlanInfo();
      if (!state.user || state.user.plan !== 'Pro') {
        state.savedTimelines = []; state.picked.clear();
        renderFollowTags(); syncFollowButtons();
        return;
      }
      $('#proOnlyFeed').hidden = false;
      $('#userFeedContainer').innerHTML = '<div class="loading-card">Đang tải feed của bạn…</div>';
      const savedData = await getJSON('/api/user/saved-timelines');
      state.savedTimelines = savedData.items || [];
      state.picked = new Set(state.savedTimelines.map(item => {
        rememberFollow(item.type, item.id, item.name);
        return keyFor(item.type, item.id);
      }));
      renderFollowTags(); syncFollowButtons();
      await renderUserFeed();
    } catch (error) {
      setMessage('#userFeedContainer', 'Không tải được thông tin gói hoặc feed cá nhân', error.message, true);
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
    if (state.user && state.user.plan !== 'Pro' && !state.picked.has(key)) {
      openProUpgradePopup();
      return;
    }
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
    state.telegram = null; state.planInfo = null; state.openPlanAfterAuth = false; state.proPurchaseSelected = false; $('#accountModal').hidden = true; $('#proOnlyFeed').hidden = true; $('#userPlanBadge').textContent = 'FREE'; $('#userPlanBadge').classList.add('free'); $('#userPlanBadge').classList.remove('pro'); $('#openTelegramLink').hidden = true; $('#telegramEnabled').checked = false; $('#telegramEnabled').disabled = true;
    renderAuthArea(); $('#userFeedContainer').innerHTML = ''; $('#savedTimelineTags').innerHTML = ''; $('#telegramStatus').textContent = 'Đăng nhập để cấu hình Telegram.'; syncFollowButtons(); updateSelectionCount();
  }

  document.addEventListener('click', async event => {
    const authOpen = event.target.closest('[data-auth-open]'); if (authOpen) { openAuthModal(authOpen.dataset.authOpen); return; }
    const authClose = event.target.closest('[data-auth-close]'); if (authClose) { closeAuthModal(); return; }
    const authSwitch = event.target.closest('[data-switch-auth]'); if (authSwitch) { openAuthModal(authSwitch.dataset.switchAuth); return; }
    if (event.target.closest('#forgotPasswordLink')) { openAuthModal('forgot'); return; }
    const accountMenuToggle = event.target.closest('#accountMenuToggle');
    if (accountMenuToggle) {
      const menu = $('#accountMenuDropdown'); menu.hidden = !menu.hidden;
      accountMenuToggle.setAttribute('aria-expanded', String(!menu.hidden)); return;
    }
    if (event.target.closest('#openAccountButton')) { await openAccountModal('plan'); return; }
    const accountTab = event.target.closest('[data-account-tab]');
    if (accountTab) { setAccountTab(accountTab.dataset.accountTab); if (accountTab.dataset.accountTab === 'telegram' && state.user && state.user.plan === 'Pro') await loadTelegramSettings(); return; }
    if (event.target.closest('[data-account-close]')) { closeAccountModal(); return; }
    if (!event.target.closest('#accountMenu')) { const menu = $('#accountMenuDropdown'); if (menu) { menu.hidden = true; $('#accountMenuToggle')?.setAttribute('aria-expanded', 'false'); } }
    if (event.target.closest('#proUpgradeOpenPlan')) { closeProUpgradePopup(); await openAccountModal('plan'); return; }
    if (event.target.closest('[data-pro-close]')) { closeProUpgradePopup(); return; }
    if (event.target.closest('#logoutButton')) { closeAccountModal(); await logout(); return; }
    if (event.target.closest('#openTimelinePicker')) {
      $('#timelinePickerDetails').open = true;
      $('#timelinePickerDetails').scrollIntoView({behavior:'smooth', block:'center'}); return;
    }
    if (event.target.closest('#saveTimelinesButton')) { await saveTimelines(); return; }
    if (event.target.closest('#selectProPlanButton')) { state.proPurchaseSelected = true; await loadPlanInfo(); $('#paymentQrWrap').scrollIntoView({behavior:'smooth',block:'center'}); return; }
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
    if (event.target.closest('#loadMoreArticlesButton')) { await loadArticles(state.articlePage + 1, true); return; }
    if (event.target.closest('[data-timeline-share-close]')) { closeTimelineShareDialog(); return; }
    if (event.target.closest('#shareTimelineButton')) { openTimelineShareDialog(); return; }
    if (event.target.closest('#copyTimelineShareButton')) { await copyTimelineShareURL(); return; }
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
  async function selectFieldCategory(category) {
    state.fieldCategory = String(category || '');
    $('#fieldCategoryFilter').querySelectorAll('[data-field-category]').forEach(tab => {
      const active = tab.dataset.fieldCategory === state.fieldCategory;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active) $('#dailyArticles').setAttribute('aria-labelledby', tab.id);
    });
    await loadArticles(1);
    if (state.searchQuery) await loadSearchResults(1);
  }
  $('#fieldCategoryFilter').addEventListener('click', async event => {
    const tab = event.target.closest('[data-field-category]');
    if (!tab) return;
    await selectFieldCategory(tab.dataset.fieldCategory || '');
  });
  $('#fieldCategoryFilter').addEventListener('keydown', event => {
    const tabs = Array.from($('#fieldCategoryFilter').querySelectorAll('[data-field-category]'));
    const current = tabs.indexOf(event.target.closest('[data-field-category]'));
    if (current < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    let next = current;
    if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    else if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    tabs[next].focus();
    tabs[next].click();
  });
  // Keep the arrow at the right edge of the tab bar so every field can be reached
  // without relying on a horizontal scrollbar. The arrow changes direction at either end.
  const fieldCategoryStrip = $('#fieldCategoryFilter');
  const fieldCategoryScrollButton = $('#fieldCategoryScrollButton');
  let fieldCategoryScrollDirection = 'next';
  function updateFieldCategoryScrollButton() {
    if (!fieldCategoryStrip || !fieldCategoryScrollButton) return;
    const maxScroll = Math.max(0, fieldCategoryStrip.scrollWidth - fieldCategoryStrip.clientWidth);
    const overflows = maxScroll > 2;
    fieldCategoryScrollButton.hidden = !overflows;
    if (!overflows) {
      fieldCategoryScrollDirection = 'next';
      fieldCategoryScrollButton.querySelector('span').textContent = '›';
      fieldCategoryScrollButton.setAttribute('aria-label', 'Xem các lĩnh vực tiếp theo');
      fieldCategoryScrollButton.title = 'Xem các lĩnh vực tiếp theo';
      return;
    }
    if (fieldCategoryStrip.scrollLeft <= 2) fieldCategoryScrollDirection = 'next';
    else if (fieldCategoryStrip.scrollLeft >= maxScroll - 2) fieldCategoryScrollDirection = 'previous';
    const goingBack = fieldCategoryScrollDirection === 'previous';
    fieldCategoryScrollButton.querySelector('span').textContent = goingBack ? '‹' : '›';
    const label = goingBack ? 'Xem các lĩnh vực trước đó' : 'Xem các lĩnh vực tiếp theo';
    fieldCategoryScrollButton.setAttribute('aria-label', label);
    fieldCategoryScrollButton.title = label;
  }
  fieldCategoryScrollButton.addEventListener('click', () => {
    const maxScroll = Math.max(0, fieldCategoryStrip.scrollWidth - fieldCategoryStrip.clientWidth);
    if (maxScroll <= 2) return;
    const delta = Math.max(150, fieldCategoryStrip.clientWidth * 0.72);
    const current = fieldCategoryStrip.scrollLeft;
    const target = fieldCategoryScrollDirection === 'previous'
      ? Math.max(0, current - delta)
      : Math.min(maxScroll, current + delta);
    fieldCategoryScrollStripTo(target);
  });
  function fieldCategoryScrollStripTo(left) {
    fieldCategoryStrip.scrollTo({left, behavior: 'smooth'});
    // Keep direction while moving through the middle, and switch only at an edge.
    if (left <= 2) fieldCategoryScrollDirection = 'next';
    else if (left >= fieldCategoryStrip.scrollWidth - fieldCategoryStrip.clientWidth - 2) fieldCategoryScrollDirection = 'previous';
    updateFieldCategoryScrollButton();
  }
  fieldCategoryStrip.addEventListener('scroll', updateFieldCategoryScrollButton, {passive: true});
  window.addEventListener('resize', updateFieldCategoryScrollButton);
  updateFieldCategoryScrollButton();
  $('#articleDateFilterForm').addEventListener('submit', async event => {
    event.preventDefault();
    const from = String($('#articleDateFrom').value || '').trim();
    const to = String($('#articleDateTo').value || '').trim();
    if (!from || !to) { setFeedback('#articleDateFilterFeedback', 'Vui lòng chọn cả ngày bắt đầu và ngày kết thúc.'); return; }
    if (from > to) { setFeedback('#articleDateFilterFeedback', 'Ngày bắt đầu không được sau ngày kết thúc.'); return; }
    state.dateFrom = from; state.dateTo = to;
    await loadArticles(1);
  });
  $('#radarSearchForm').addEventListener('submit', async event => {
    event.preventDefault(); state.searchQuery = $('#radarSearch').value.trim();
    if (!state.searchQuery) { $('#searchResultsSection').hidden = true; return; }
    await loadSearchResults(1);
  });
  $('#authForm').addEventListener('submit', async event => {
    event.preventDefault();
    const mode = state.authMode;
    const identifier = $('#authIdentifier').value.trim();
    const password = $('#authPassword').value;
    if ((mode === 'register' || mode === 'reset') && password !== $('#authPasswordConfirm').value) {
      setFeedback('#authFeedback', 'Hai lần nhập mật khẩu chưa khớp.'); return;
    }
    $('#authSubmit').disabled = true;
    try {
      if (mode === 'forgot') {
        setFeedback('#authFeedback', 'Đang gửi email nếu tài khoản tồn tại…');
        const result = await sendJSON('/api/user-auth/forgot-password', 'POST', {email: identifier});
        setFeedback('#authFeedback', result.message || 'Nếu email tồn tại, hướng dẫn đã được gửi.', true);
        return;
      }
      if (mode === 'reset') {
        if (!state.passwordResetToken && !readPasswordResetToken()) throw new Error('Thiếu mã đặt lại mật khẩu. Hãy mở liên kết trong email hoặc yêu cầu liên kết mới.');
        setFeedback('#authFeedback', 'Đang cập nhật mật khẩu…');
        const result = await sendJSON('/api/user-auth/reset-password', 'POST', {token: state.passwordResetToken || readPasswordResetToken(), new_password: password});
        history.replaceState({}, '', location.pathname + location.search);
        state.passwordResetToken = null; $('#authForm').reset(); setAuthMode('login');
        setFeedback('#authFeedback', result.message || 'Đã đổi mật khẩu. Hãy đăng nhập.', true);
        return;
      }
      const registering = mode === 'register';
      const body = registering ? {username: identifier, email: $('#authEmail').value.trim(), password} : {identifier, password};
      setFeedback('#authFeedback', registering ? 'Đang tạo tài khoản…' : 'Đang đăng nhập…');
      const data = await sendJSON(registering ? '/api/user-auth/register' : '/api/user-auth/login', 'POST', body);
      state.user = data.user; closeAuthModal(); renderAuthArea(); await loadUserRadar();
      if (state.pendingFollow) { const pending = state.pendingFollow; state.pendingFollow = null; if (!state.picked.has(keyFor(pending.type, pending.id))) await toggleFollow(pending.type, pending.id, pending.name); }
      await loadPaymentPlanAfterAuth();
    } catch (error) { setFeedback('#authFeedback', error.message); }
    finally { $('#authSubmit').disabled = false; }
  });
  $('#paymentProofForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (!state.user || state.user.plan === 'Pro' || state.paymentProofSubmitting) return;
    const file = $('#paymentProofFile').files[0];
    if (!file) { setFeedback('#paymentRequestStatus', 'Hãy chọn ảnh bằng chứng thanh toán.'); return; }
    if (file.size > 3 * 1024 * 1024) { setFeedback('#paymentRequestStatus', 'Ảnh quá lớn. Vui lòng chọn ảnh nhỏ hơn 3 MB.'); return; }
    state.paymentProofSubmitting = true; $('#submitPaymentProof').disabled = true;
    setFeedback('#paymentRequestStatus', 'Đang gửi bằng chứng thanh toán…');
    try {
      const formData = new FormData(); formData.append('proof', file); formData.append('note', $('#paymentProofNote').value.trim());
      const response = await fetch('/api/user/payment-requests', {method:'POST', body:formData, credentials:'same-origin', headers:{'Accept':'application/json'}});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Không gửi được yêu cầu (${response.status})`);
      $('#paymentProofForm').reset();
      try { await loadPlanInfo(); }
      catch (refreshError) { console.warn('Payment request was accepted, but plan status could not be refreshed:', refreshError.message); }
      setFeedback('#paymentRequestStatus', 'Bạn đã gửi thông báo cho admin nâng cấp gói Pro thành công!', true);
    } catch (error) { setFeedback('#paymentRequestStatus', error.message); $('#paymentRequestStatus').classList.add('error'); }
    finally { state.paymentProofSubmitting = false; $('#submitPaymentProof').disabled = false; }
  });
  $('#authModal').addEventListener('click', event => { if (event.target.id === 'authModal') closeAuthModal(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#authModal').hidden) closeAuthModal(); if (event.key === 'Escape' && !$('#accountModal').hidden) closeAccountModal(); if (event.key === 'Escape' && !$('#proUpgradeModal').hidden) closeProUpgradePopup(); if (event.key === 'Escape' && !$('#timelineShareModal').hidden) closeTimelineShareDialog(); });

  async function initialize() {
    // Older shared links point at /news?timeline_type=...; forward them before
    // loading the homepage feed so article loading cannot push the timeline away.
    const sharedTimeline = sharedTimelineFromURL();
    if (sharedTimeline) {
      const shareURL = new URL('/timeline', window.location.origin);
      shareURL.searchParams.set('type', sharedTimeline.type);
      shareURL.searchParams.set('id', String(sharedTimeline.id));
      shareURL.searchParams.set('page', String(sharedTimeline.page));
      window.location.replace(shareURL.href);
      return;
    }
    await restoreUser();
    const jobs = [loadHotTopics(), loadEntityCloud(), loadArticles(1)];
    if (state.user) jobs.push(loadUserRadar());
    await Promise.all(jobs);
    const resetToken = readPasswordResetToken();
    if (resetToken) {
      state.passwordResetToken = resetToken;
      openAuthModal('reset');
      // Remove the fragment after capturing it so it is not left in browser history/address bar.
      history.replaceState({}, '', location.pathname + location.search);
    }
  }
  initialize();
})();
