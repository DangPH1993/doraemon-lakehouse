(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  let token = sessionStorage.getItem('dlh_token') || '';
  let currentPage = 'overview';
  const listPages = {articles:1, topics:1, entities:1};
  const listPageSize = 25;
  let paymentProofBlobUrl = null;
  let paymentBadgeTimer = null;
  const pageNames = {overview:'Tổng quan',sources:'Nguồn dữ liệu',articles:'Bài báo & làm sạch',topics:'Quản lý Topic',timeline:'Topic Timeline',entities:'Quản lý Entity',facts:'Bảng FACT',pipeline:'Pipeline & chất lượng',users:'Người dùng & gói',analytics:'Google Analytics',branding:'Logo & giao diện'};
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtDate = (v) => { if (!v) return '—'; try { return new Date(v).toLocaleString('vi-VN',{dateStyle:'short',timeStyle:'short'}); } catch { return esc(v); } };
  const fmtNumber = (v) => Number(v || 0).toLocaleString('vi-VN');
  const statusPill = (status) => { const s=String(status||'unknown').toLowerCase(); const klass=['success','partial'].includes(s)?'success':['failed','error'].includes(s)?'error':['running','warning'].includes(s)?'warning':s==='pending'?'pending':'neutral'; return `<span class="status ${klass}">${esc(status||'unknown')}</span>`; };
  const toast = (msg,kind='') => { const el=document.createElement('div');el.className=`toast ${kind}`;el.textContent=msg;$('#toastStack').appendChild(el);setTimeout(()=>el.remove(),4800); };
  const notice = (msg,kind='') => {const el=$('#globalNotice');el.className=`notice ${kind}`;el.textContent=msg;el.classList.remove('hidden');};
  const clearNotice = () => $('#globalNotice').classList.add('hidden');
  const busy = (on,text='Đang xử lý...') => {$('#busyText').textContent=text;$('#busyOverlay').classList.toggle('hidden',!on);};
  async function api(path, opts={}) {
    const headers = {'Content-Type':'application/json', ...(opts.headers||{})};
    if(token) headers.Authorization=`Bearer ${token}`;
    let response;
    try { response = await fetch(path,{...opts,headers}); }
    catch(e){ throw new Error('Không kết nối được API. Kiểm tra server và DATABASE_URL.'); }
    const data = await response.json().catch(()=>({}));
    if(response.status===401){ token='';sessionStorage.removeItem('dlh_token');showLogin();throw new Error(data.detail||'Phiên đăng nhập hết hạn.'); }
    if(!response.ok) throw new Error(data.detail||data.error||`HTTP ${response.status}`);
    return data;
  }
  function showLogin(){ $('#loginView').classList.remove('hidden');$('#appView').classList.add('hidden'); }
  function showApp(){ $('#loginView').classList.add('hidden');$('#appView').classList.remove('hidden'); }
  function showPage(name, options={}){ currentPage=name; $$('.page').forEach(p=>p.classList.toggle('active',p.id===`page-${name}`));$$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.page===name));$('#pageTitle').textContent=pageNames[name]||name;$('#crumbCurrent').textContent=pageNames[name]||name;if(!options.skipLoad)loadPage(name).catch(e=>toast(e.message,'error')); }
  async function login(username,password){ const r=await api('/api/auth/login',{method:'POST',body:JSON.stringify({username,password}),headers:{}});token=r.token;sessionStorage.setItem('dlh_token',token);showApp();await boot(); }
  async function checkReady(){try{await fetch('/api/health/ready').then(r=>{if(!r.ok) throw new Error();return r.json()});$('#dbStatus').textContent='Database kết nối được';$('#heroSystemState').textContent='Database sẵn sàng';$('.status-dot').style.background='#20a878';}catch{$('#dbStatus').textContent='Chưa kết nối DB';$('#heroSystemState').textContent='Cần kiểm tra cấu hình';$('.status-dot').style.background='#d14343';}}
  async function boot(){await checkReady();try{await api('/api/auth/me');await Promise.all([loadPage(currentPage),loadPaymentSummary()]);$('#lastRefreshed').textContent='Vừa cập nhật';if(paymentBadgeTimer)clearInterval(paymentBadgeTimer);paymentBadgeTimer=setInterval(()=>loadPaymentSummary(),60000);}catch(e){notice(`Chưa tải được dữ liệu: ${e.message}. Hãy kiểm tra DATABASE_URL và đã khởi tạo schema trong Neon chưa.`,'error');}}
  async function loadPage(name){clearNotice();if(name==='overview')return loadDashboard();if(name==='sources')return Promise.all([loadSources(),populateSourceFilter()]);if(name==='articles')return Promise.all([loadArticles(),loadRules(),loadRawItems(),populateSourceFilter()]);if(name==='topics')return loadTopics();if(name==='timeline')return loadTopicTimelineOptions();if(name==='entities')return loadEntities();if(name==='facts')return Promise.all([loadFacts(),loadModels()]);if(name==='pipeline')return Promise.all([loadRuns(),loadIssues()]);if(name==='users')return loadUsersPage();if(name==='analytics')return loadGoogleAnalyticsConfig();if(name==='branding')return loadSiteBrandingConfig();}
  function renderEmpty(message='Chưa có dữ liệu.'){return `<div class="empty">${esc(message)}</div>`;}
  function renderPagination(kind, meta={}){
    const p=Number(meta.page||1), totalPages=Number(meta.total_pages||1), total=Number(meta.total||0);
    const start=Number(meta.start||0), end=Number(meta.end||0);
    const prevDisabled=p<=1?'disabled':''; const nextDisabled=p>=totalPages?'disabled':'';
    return `<div class="pagination-controls"><span class="pagination-summary">${total?`Đang hiển thị ${fmtNumber(start)}–${fmtNumber(end)} / ${fmtNumber(total)}`:'Chưa có bản ghi'} · Trang ${fmtNumber(p)} / ${fmtNumber(totalPages)}</span><div class="pagination-buttons"><button class="btn secondary tiny" data-page-control="${kind}" data-page-dir="prev" ${prevDisabled}>← Trước</button><button class="btn secondary tiny" data-page-control="${kind}" data-page-dir="next" ${nextDisabled}>Sau →</button></div></div>`;
  }
  async function loadDashboard(){const d=await api('/api/dashboard');const c=d.counts||{};const cards=[['Nguồn dữ liệu',c.sources_total,'đang hoạt động '+(c.sources_enabled||0),'◉'],['Bài báo đã nạp',c.articles_total,'+'+fmtNumber(c.articles_24h)+' trong 24 giờ','▤'],['Topic hoạt động',c.topics_active,'sự kiện: '+fmtNumber(c.event_topics_active),'⌗'],['Entity catalog',c.entities_active,'AI chưa xử lý: '+fmtNumber(c.ai_pending_articles),'◎'],['Vấn đề chất lượng',c.open_issues,'AI lỗi: '+fmtNumber(c.ai_classification_errors),'△']];$('#kpiGrid').innerHTML=cards.map(x=>`<div class="kpi"><div class="kpi-top"><span>${esc(x[0])}</span><span class="kpi-icon">${x[3]}</span></div><div class="kpi-value">${fmtNumber(x[1])}</div><div class="kpi-foot">${esc(x[2])}</div></div>`).join('');
    const trends=d.collection_trend||[];const max=Math.max(1,...trends.map(x=>Number(x.count)));$('#collectionChart').innerHTML=trends.length?trends.map(x=>`<div class="chart-bar-item"><div class="chart-bar" style="height:${Math.max(3,Number(x.count)/max*82)}%"><span class="chart-bar-value">${fmtNumber(x.count)}</span></div><span class="chart-bar-label">${esc(String(x.day).slice(5))}</span></div>`).join(''):renderEmpty('Chưa có số liệu theo ngày.');
    const tt=d.top_topics||[], tm=Math.max(1,...tt.map(x=>Number(x.article_count)));$('#topTopics').innerHTML=tt.length?tt.map(x=>`<div class="rank-item"><div><div class="rank-name">${esc(x.name)}</div><div class="rank-track"><div class="rank-fill" style="width:${Math.max(2,Number(x.article_count)/tm*100)}%"></div></div></div><div class="rank-count">${fmtNumber(x.article_count)}</div></div>`).join(''):renderEmpty('Chưa phát hiện sự kiện cụ thể; hãy chạy pipeline để phân tích tiêu đề.');
    const ss=d.top_sources||[], sm=Math.max(1,...ss.map(x=>Number(x.article_count)));$('#topSources').innerHTML=ss.length?ss.map(x=>`<div class="rank-item"><div><div class="rank-name">${esc(x.name)}</div><div class="rank-track"><div class="rank-fill" style="width:${Math.max(2,Number(x.article_count)/sm*100)}%"></div></div></div><div class="rank-count">${fmtNumber(x.article_count)}</div></div>`).join(''):renderEmpty('Chưa có nguồn được cấu hình.');
    const hot=d.hot_topics||[]; for (const [period,elementId] of [['day','#hotTopicsDay'],['week','#hotTopicsWeek'],['month','#hotTopicsMonth']]) { const rows=hot.filter(x=>x.period_type===period).sort((a,b)=>Number(a.rank_position)-Number(b.rank_position)).slice(0,8); const peak=Math.max(1,...rows.map(x=>Number(x.article_count||0))); $(elementId).innerHTML=rows.length?rows.map((x,i)=>{const growth=x.growth_pct===null||x.growth_pct===undefined?'—':`${Number(x.growth_pct)>0?'+':''}${Number(x.growth_pct).toLocaleString('vi-VN',{maximumFractionDigits:1})}%`;return `<div class="hot-topic-row"><div class="hot-topic-top"><span class="hot-topic-rank">${i+1}</span><div class="hot-topic-info"><div class="rank-name">${esc(x.topic_name)}</div><div class="rank-track"><div class="rank-fill" style="width:${Math.max(2,Number(x.article_count||0)/peak*100)}%"></div></div><div class="hot-topic-meta">${fmtNumber(x.article_count)} bài · ${fmtNumber(x.source_count)} nguồn</div></div><span class="hot-topic-growth ${Number(x.growth_pct)>0?'up':Number(x.growth_pct)<0?'down':''}">${esc(growth)}</span></div></div>`}).join(''):renderEmpty('Chưa có sự kiện đủ điều kiện. Hãy chạy pipeline để phân tích và chuẩn hóa tiêu đề.'); }
    $('#recentRuns').innerHTML=renderRunsTable(d.recent_runs||[]);$('#lastRefreshed').textContent=`Cập nhật ${new Date().toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'})}`;}
  const renderRunsTable=(rows)=>rows.length?`<table><thead><tr><th>Run</th><th>Thời điểm</th><th>Trạng thái</th><th>Đã thấy</th><th>Mới / cập nhật</th><th>Bỏ qua đã có</th><th>Feed không đổi</th><th>Thông tin</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>#${esc(r.run_id)}</strong><div class="subline">${esc(r.trigger_type)}</div></td><td>${fmtDate(r.started_at)}</td><td>${statusPill(r.status)}</td><td>${fmtNumber(r.entries_seen)}</td><td>${fmtNumber(r.articles_inserted)} / ${fmtNumber(r.articles_updated)}</td><td>${fmtNumber(r.entries_skipped_existing)}</td><td>${fmtNumber(r.feeds_not_modified)}</td><td>${esc(r.message||'—')}</td></tr>`).join('')}</tbody></table>`:renderEmpty('Chưa có lần chạy pipeline.');
  async function loadSources(){const rows=await api('/api/sources');$('#sourcesTable').innerHTML=rows.length?`<table><thead><tr><th>Nguồn</th><th>Feed</th><th>Giấy phép</th><th>Bài đã lưu</th><th>Trạng thái</th><th>Lần chạy gần nhất</th><th>Thao tác</th></tr></thead><tbody>${rows.map(s=>`<tr><td><strong>${esc(s.name)}</strong><div class="subline">${esc(s.language)} · ${esc(s.category)}</div></td><td class="content-title"><a href="${esc(s.feed_url)}" target="_blank" rel="noopener">${esc(s.feed_url)}</a></td><td>${esc(s.license_name)}${s.license_url?`<div class="subline"><a href="${esc(s.license_url)}" target="_blank" rel="noopener">Giấy phép ↗</a></div>`:''}</td><td>${fmtNumber(s.article_count)}</td><td>${s.enabled?statusPill(s.last_status||'enabled'):statusPill('disabled')} ${s.last_error?`<div class="subline">${esc(s.last_error)}</div>`:''}</td><td>${fmtDate(s.last_run_at)}</td><td><div class="tiny-actions"><button class="btn secondary tiny" data-action="test-source" data-id="${s.source_id}">Test RSS</button><button class="btn secondary tiny" data-action="edit-source" data-id="${s.source_id}" data-name="${esc(s.name)}" data-url="${esc(s.feed_url)}" data-language="${esc(s.language)}" data-category="${esc(s.category)}" data-license="${esc(s.license_name)}" data-license-url="${esc(s.license_url||'')}">Sửa</button><button class="btn secondary tiny" data-action="toggle-source" data-id="${s.source_id}" data-enabled="${s.enabled}">${s.enabled?'Tắt':'Bật'}</button><button class="btn danger tiny" data-action="delete-source" data-id="${s.source_id}">Xóa</button></div></td></tr>`).join('')}</tbody></table>`:renderEmpty('Chưa có nguồn RSS. Dùng biểu mẫu bên dưới để thêm nguồn.');}
  async function populateSourceFilter(){try{const rows=await api('/api/sources');const current=$('#articleSourceFilter').value;$('#articleSourceFilter').innerHTML='<option value="">Tất cả nguồn</option>'+rows.map(s=>`<option value="${s.source_id}">${esc(s.name)}</option>`).join('');$('#articleSourceFilter').value=current;}catch{}}
  async function loadArticles(){
    const q=$('#articleSearch').value.trim(), source=$('#articleSourceFilter').value, fieldCategory=$('#articleFieldCategoryFilter').value, excluded=$('#articleExcludedFilter').value;
    const params=new URLSearchParams({page:String(listPages.articles),page_size:String(listPageSize)});
    if(q)params.set('q',q);if(source)params.set('source_id',source);if(fieldCategory)params.set('field_category',fieldCategory);if(excluded!=='')params.set('excluded',excluded);
    const d=await api('/api/articles?'+params), rows=d.items||[]; listPages.articles=Number(d.pagination?.page||listPages.articles);
    $('#articlesTable').innerHTML=rows.length?`<table><thead><tr><th>Bài báo</th><th>Nguồn</th><th>Thời gian xuất bản</th><th>Lĩnh vực</th><th>Topic sự kiện</th><th>Entity</th><th>Quality</th><th>Trạng thái</th><th>Thao tác</th></tr></thead><tbody>${rows.map(a=>`<tr><td class="content-title"><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a><div class="subline">${esc(a.summary||a.content_preview||a.url)}</div></td><td>${esc(a.source_name)}</td><td>${fmtDate(a.published_at)}<div class="subline">Thu thập: ${fmtDate(a.collected_at)}</div></td><td><span class="field-category-badge">${esc(a.field_category||'Chưa phân loại')}</span></td><td>${esc(a.topics||'—')}</td><td>${esc(a.entities||'—')}</td><td>${fmtNumber(a.quality_score)}%</td><td>${a.is_excluded?statusPill('excluded')+'<div class="subline">'+esc(a.exclude_reason||'')+'</div>':statusPill('included')}</td><td><button class="btn secondary tiny" data-action="edit-classification" data-id="${a.article_id}">Sửa topic/entity</button></td></tr>`).join('')}</tbody></table>`:renderEmpty('Không tìm thấy bài báo phù hợp.');
    $('#articlesPagination').innerHTML=renderPagination('articles',d.pagination||{});
  }

  async function loadRawItems(){const rows=await api('/api/raw-items?limit=100');$('#rawItemsTable').innerHTML=rows.length?`<table><thead><tr><th>Raw ID</th><th>Tiêu đề gốc</th><th>Nguồn</th><th>Ngày trong RSS</th><th>Fetched at</th><th>URL</th></tr></thead><tbody>${rows.map(r=>`<tr><td>#${r.raw_id}</td><td><strong>${esc(r.raw_title||'(no title)')}</strong><div class="subline">${esc(String(r.raw_summary||'').replace(/<[^>]*>/g,' ').slice(0,180))}</div></td><td>${esc(r.source_name)}</td><td>${esc(r.raw_published||'—')}</td><td>${fmtDate(r.fetched_at)}</td><td class="content-title"><a href="${esc(r.item_url||'#')}" target="_blank" rel="noopener">Mở nguồn ↗</a></td></tr>`).join('')}</tbody></table>`:renderEmpty('Chưa có raw item. Chạy pipeline hoặc thêm nguồn RSS.');}
  async function loadRules(){const rows=await api('/api/cleaning-rules');$('#rulesTable').innerHTML=rows.length?`<table><thead><tr><th>Quy tắc</th><th>Loại</th><th>Cấu hình</th><th>Trạng thái</th><th>Thao tác</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.name)}</strong></td><td><code>${esc(r.rule_type)}</code></td><td><code>${esc(JSON.stringify(r.config))}</code></td><td>${r.enabled?statusPill('enabled'):statusPill('disabled')}</td><td><div class="tiny-actions"><button class="btn secondary tiny" data-action="toggle-rule" data-id="${r.rule_id}" data-enabled="${r.enabled}">${r.enabled?'Tắt':'Bật'}</button><button class="btn danger tiny" data-action="delete-rule" data-id="${r.rule_id}">Xóa</button></div></td></tr>`).join('')}</tbody></table>`:renderEmpty('Chưa có quy tắc làm sạch.');}
  async function loadTopics(){
    const q=$('#topicSearch').value.trim(), params=new URLSearchParams({page:String(listPages.topics),page_size:String(listPageSize)});if(q)params.set('q',q);
    const d=await api('/api/topics?'+params), rows=d.topics||[];listPages.topics=Number(d.pagination?.page||listPages.topics);
    $('#topicsTable').innerHTML=rows.length?`<table><thead><tr><th>Topic ID</th><th>Topic</th><th>Loại</th><th>Keywords</th><th>Bài liên quan</th><th>Trạng thái</th><th>Thao tác</th></tr></thead><tbody>${rows.map(t=>`<tr><td><code>${esc(t.topic_id)}</code></td><td><strong>${esc(t.name)}</strong><div class="subline">${esc(t.slug)} · ${esc(t.origin)}</div></td><td>Sự kiện</td><td>${(t.keywords||[]).slice(0,8).map(k=>`<span class="keyword">${esc(k)}</span>`).join(' ')}${(t.keywords||[]).length>8?` <span class="muted">+${(t.keywords||[]).length-8}</span>`:''}</td><td>${fmtNumber(t.article_count)}</td><td>${statusPill(t.status)}</td><td><div class="tiny-actions"><button class="btn secondary tiny" data-action="edit-topic" data-id="${t.topic_id}" data-name="${esc(t.name)}" data-keywords="${esc((t.keywords||[]).join(', '))}" data-status="${t.status}">Sửa</button><button class="btn primary tiny" data-action="topic-timeline" data-id="${t.topic_id}">Timeline</button></div></td></tr>`).join('')}</tbody></table>`:renderEmpty('Không tìm thấy topic phù hợp.');
    $('#topicsPagination').innerHTML=renderPagination('topics',d.pagination||{});
  }
  async function loadEntities(){
    const q=$('#entitySearch').value.trim(), params=new URLSearchParams({page:String(listPages.entities),page_size:String(listPageSize)});if(q)params.set('q',q);
    const d=await api('/api/entities?'+params), rows=d.entities||[];listPages.entities=Number(d.pagination?.page||listPages.entities);
    $('#entitiesTable').innerHTML=rows.length?`<table><thead><tr><th>Entity ID</th><th>Entity</th><th>Loại</th><th>Bí danh</th><th>Bài liên quan</th><th>Trạng thái</th><th>Thao tác</th></tr></thead><tbody>${rows.map(e=>`<tr><td><code>${esc(e.entity_id)}</code></td><td><strong>${esc(e.canonical_name)}</strong><div class="subline">${esc(e.external_id||e.origin)}</div></td><td><span class="pill-label">${esc(e.entity_type)}</span></td><td>${(e.aliases||[]).map(a=>`<span class="keyword">${esc(a)}</span>`).join(' ')||'—'}</td><td>${fmtNumber(e.article_count)}</td><td>${statusPill(e.status)}</td><td><button class="btn secondary tiny" data-action="edit-entity" data-id="${e.entity_id}" data-name="${esc(e.canonical_name)}" data-type="${esc(e.entity_type)}" data-status="${esc(e.status)}">Sửa</button></td></tr>`).join('')}</tbody></table>`:renderEmpty('Không tìm thấy entity phù hợp.');
    $('#entitiesPagination').innerHTML=renderPagination('entities',d.pagination||{});
  }

  async function loadTopicTimelineOptions(preselect=null){const current=preselect??$('#topicTimelineSelect').value;const d=await api('/api/topics?all=true');const rows=(d.topics||[]).filter(t=>t.status==='active').sort((a,b)=>a.name.localeCompare(b.name,'vi'));$('#topicTimelineSelect').innerHTML='<option value="">Chọn topic…</option>'+rows.map(t=>`<option value="${t.topic_id}">${esc(t.name)}</option>`).join('');if(current&&rows.some(t=>String(t.topic_id)===String(current)))$('#topicTimelineSelect').value=String(current);else if(rows.some(t=>t.topic_type==='event'))$('#topicTimelineSelect').value=String(rows.find(t=>t.topic_type==='event').topic_id);if($('#topicTimelineSelect').value)await loadTopicTimeline();else $('#topicTimelineContainer').innerHTML=renderEmpty('Chọn topic để xem timeline.');}
  async function loadTopicTimeline(){const topicId=$('#topicTimelineSelect').value;if(!topicId){$('#topicTimelineContainer').innerHTML=renderEmpty('Chọn topic để xem timeline.');return;}const d=await api(`/api/topics/${topicId}/timeline?limit=150`);$('#topicTimelineSummary').innerHTML=`<strong>${esc(d.topic.name)}</strong> · ${fmtNumber(d.count)} bài hiển thị / ${fmtNumber(d.total_count??d.count)} bài tổng · theo thời gian xuất bản`;const rows=d.items||[];$('#topicTimelineContainer').innerHTML=rows.length?rows.map((a,i)=>`<article class="timeline-item"><div class="timeline-rail"><span class="timeline-dot"></span>${i===rows.length-1?'':'<span class="timeline-line"></span>'}</div><div class="timeline-content"><div class="timeline-date">${fmtDate(a.timeline_at)} · ${esc(a.source_name)}</div><h3><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></h3>${a.summary?`<p>${esc(a.summary)}</p>`:''}${a.entities?`<div class="timeline-entities">Entities: ${esc(a.entities)}</div>`:''}<div class="timeline-actions"><button class="btn secondary tiny" data-action="edit-classification" data-id="${a.article_id}">Sửa topic/entity</button></div></div></article>`).join(''):renderEmpty('Chưa có bài báo nào được gắn với topic này.');}
  async function openClassificationEditor(articleId){const d=await api(`/api/articles/${articleId}/classifications`);$('#classificationArticleId').value=articleId;$('#classificationArticleTitle').textContent=d.article.title||'';$('#classificationTopics').value=(d.topics||[]).map(x=>x.name).join('\n');$('#classificationEntities').value=(d.entities||[]).map(x=>x.canonical_name).join('\n');$('#classificationDialog').showModal();}

  async function loadFacts(){const table=$('#factSelect').value,limit=$('#factLimit').value;const rows=await api(`/api/facts/${table}?limit=${limit}`);if(!rows.length){$('#factsTable').innerHTML=renderEmpty('Bảng chưa có dữ liệu. Chạy pipeline để nạp FACT.');return;}const cols=Object.keys(rows[0]);$('#factsTable').innerHTML=`<table><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${esc(r[c]===null?'—':typeof r[c]==='object'?JSON.stringify(r[c]):r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;}
  async function loadModels(){const rows=await api('/api/data-models');$('#modelCatalogTable').innerHTML=rows.length?`<table><thead><tr><th>Bảng</th><th>Loại</th><th>Mô tả</th><th>Grain</th><th>Owner</th><th>Trạng thái</th><th></th></tr></thead><tbody>${rows.map(m=>`<tr><td><code>${esc(m.table_name)}</code></td><td>${esc(m.model_type)}</td><td>${esc(m.description)}</td><td>${esc(m.grain)}</td><td>${esc(m.owner)}</td><td>${m.active?statusPill('active'):statusPill('inactive')}</td><td><div class="tiny-actions"><button class="btn secondary tiny" data-action="edit-model" data-id="${m.model_id}" data-description="${esc(m.description)}" data-grain="${esc(m.grain)}" data-owner="${esc(m.owner)}">Sửa</button><button class="btn secondary tiny" data-action="toggle-model" data-id="${m.model_id}" data-active="${m.active}">${m.active?'Tắt':'Bật'}</button></div></td></tr>`).join('')}</tbody></table>`:renderEmpty('Data model catalog trống.');}
  async function loadRuns(){const rows=await api('/api/pipeline/runs?limit=100');$('#runsTable').innerHTML=renderRunsTable(rows);}
  async function loadIssues(){const rows=await api(`/api/quality/issues?open_only=${$('#openIssuesOnly').checked}&limit=300`);$('#issuesTable').innerHTML=rows.length?`<table><thead><tr><th>Vấn đề</th><th>Mức độ</th><th>Bài báo / nguồn</th><th>Chi tiết</th><th>Thời điểm</th><th></th></tr></thead><tbody>${rows.map(q=>`<tr><td><strong>${esc(q.issue_type)}</strong><div class="subline">#${q.issue_id}</div></td><td>${statusPill(q.severity)}</td><td>${esc(q.title||'—')}<div class="subline">${esc(q.source_name||'Unknown source')}</div></td><td>${esc(q.details)}</td><td>${fmtDate(q.detected_at)}</td><td>${q.resolved_at?'':`<button class="btn secondary tiny" data-action="resolve-issue" data-id="${q.issue_id}">Đánh dấu xử lý</button>`}</td></tr>`).join('')}</tbody></table>`:renderEmpty('Không có vấn đề theo bộ lọc.');}
  async function loadPaymentSummary(){
    try {
      const data=await api('/api/admin/payment-summary');
      const count=Number(data.pending_count||0);
      const badge=$('#pendingPaymentBadge'); badge.textContent=count>99?'99+':String(count); badge.hidden=count<1;
      $('#pendingPaymentCount').textContent=fmtNumber(count);
      return count;
    } catch(err) { console.warn('Could not refresh pending-payment badge:',err.message); return null; }
  }
  async function loadPendingPayments(){
    const rows=await api('/api/admin/payment-requests?status=pending&limit=200');
    $('#pendingPaymentCount').textContent=fmtNumber(rows.length);
    $('#pendingPaymentsTable').innerHTML=rows.length?`<table><thead><tr><th>Yêu cầu</th><th>Người dùng</th><th>Gói yêu cầu</th><th>Thời gian</th><th>Ghi chú</th><th>Thao tác</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>#${r.payment_request_id}</strong><div class="subline">${statusPill(r.status)}</div></td><td><strong>${esc(r.username)}</strong><div class="subline">${esc(r.email)}</div></td><td>${esc(r.requested_plan||'Pro')}</td><td>${fmtDate(r.created_at)}</td><td>${esc(r.note||'—')}</td><td><div class="tiny-actions"><button class="btn secondary tiny" data-action="payment-proof" data-id="${r.payment_request_id}" data-username="${esc(r.username)}">Xem bằng chứng</button><button class="btn primary tiny" data-action="approve-payment" data-id="${r.payment_request_id}">Duyệt Pro</button><button class="btn danger tiny" data-action="reject-payment" data-id="${r.payment_request_id}">Từ chối</button></div></td></tr>`).join('')}</tbody></table>`:renderEmpty('Không có yêu cầu thanh toán chờ duyệt.');
  }
  async function loadUsers(){
    const q=$('#userSearch').value.trim(); const params=new URLSearchParams({limit:'500'}); if(q)params.set('q',q);
    const rows=await api('/api/admin/users?'+params.toString());
    $('#usersTable').innerHTML=rows.length?`<table><thead><tr><th>User</th><th>Ngày tạo / đăng nhập</th><th>Yêu cầu thanh toán gần nhất</th><th>Gói hiện tại</th><th>Ngày hết hạn Pro</th><th>Thao tác</th></tr></thead><tbody>${rows.map(u=>`<tr class="${u.payment_status==='pending'?'payment-pending-row':''}"><td><strong>${esc(u.username)}</strong><div class="subline">${esc(u.email)} · #${u.user_id}</div></td><td>${fmtDate(u.created_at)}<div class="subline">Đăng nhập cuối: ${fmtDate(u.last_login_at)}</div></td><td>${u.payment_request_id?`#${u.payment_request_id} · ${statusPill(u.payment_status)}<div class="subline">${fmtDate(u.payment_created_at)}</div>${u.payment_status==='pending'?`<button class="btn secondary tiny" data-action="payment-proof" data-id="${u.payment_request_id}" data-username="${esc(u.username)}">Xem bằng chứng</button>`:''}`:'—'}</td><td><select class="user-plan-select" data-user-plan="${u.user_id}" aria-label="Gói của ${esc(u.username)}"><option value="Free" ${u.plan==='Free'?'selected':''}>Free</option><option value="Pro" ${u.plan==='Pro'?'selected':''}>Pro</option></select></td><td>${u.plan==='Pro'?(u.plan_expires_at?fmtDate(u.plan_expires_at):'<span class="status warning">Chưa đặt</span>'):'—'}</td><td><button class="btn primary tiny" data-action="save-user-plan" data-id="${u.user_id}">Lưu gói</button></td></tr>`).join('')}</tbody></table>`:renderEmpty('Không tìm thấy tài khoản.');
  }
  async function loadUsersPage(){
    $('#pendingPaymentsTable').innerHTML='<div class="loading-card">Đang tải yêu cầu thanh toán…</div>';
    $('#usersTable').innerHTML='<div class="loading-card">Đang tải danh sách tài khoản…</div>';
    await Promise.all([loadPendingPayments(),loadUsers(),loadPaymentSummary(),loadPlanConfig()]);
  }
  async function loadGoogleAnalyticsConfig(){
    const status=$('#googleAnalyticsConfigStatus');
    try{
      const data=await api('/api/admin/analytics-config');
      $('#googleAnalyticsMeasurementId').value=data.measurement_id||'';
      status.textContent=data.enabled?'Đang bật theo dõi GA4.':'Chưa bật Google Analytics.';
      status.classList.toggle('success',Boolean(data.enabled));
    }catch(err){status.textContent=err.message||'Không tải được cấu hình Google Analytics.';status.classList.remove('success');}
  }
  $('#googleAnalyticsConfigForm').addEventListener('submit',async e=>{
    e.preventDefault();
    const input=$('#googleAnalyticsMeasurementId');
    const measurementId=input.value.trim().toUpperCase();
    if(measurementId&&!/^G-[A-Z0-9]{4,20}$/.test(measurementId)){input.setCustomValidity('Nhập Measurement ID đúng định dạng G-XXXXXXXXXX, hoặc để trống để tắt.');input.reportValidity();return;}
    input.setCustomValidity('');
    const button=$('#saveGoogleAnalyticsBtn'),status=$('#googleAnalyticsConfigStatus');
    button.disabled=true;status.textContent='Đang lưu cấu hình…';status.classList.remove('success');
    try{
      const data=await api('/api/admin/analytics-config',{method:'PUT',body:JSON.stringify({measurement_id:measurementId})});
      status.textContent=data.message||'Đã lưu cấu hình Google Analytics.';status.classList.toggle('success',true);toast(status.textContent,'success');
    }catch(err){status.textContent=err.message||'Không lưu được cấu hình.';toast(status.textContent,'error');}
    finally{button.disabled=false;}
  });
  $('#googleAnalyticsMeasurementId').addEventListener('input',e=>e.target.setCustomValidity(''));

  async function loadSiteBrandingConfig(){
    const status=$('#siteBrandingStatus');
    try{
      const data=await api('/api/admin/site-branding');
      $('#siteLogoPreview').src=data.logo_url||'/static/site-logo.png?v=0.7.56';
      $('#siteFaviconPreview').src=data.favicon_url||'/static/favicon.png?v=0.7.56';
      $('#siteBrandingStatus').textContent=data.is_custom?'Đang dùng logo tùy chỉnh.':'Đang dùng logo 3T mặc định.';
      $('#clearSiteLogo').checked=false;
      $('#siteLogoFile').value='';
    }catch(err){status.textContent=err.message||'Không tải được cấu hình logo.';status.classList.remove('success');}
  }
  $('#siteLogoFile').addEventListener('change',()=>{const file=$('#siteLogoFile').files[0];if(!file)return;$('#clearSiteLogo').checked=false;const url=URL.createObjectURL(file);$('#siteLogoPreview').src=url;$('#siteLogoPreview').onload=()=>URL.revokeObjectURL(url);});
  $('#clearSiteLogo').addEventListener('change',()=>{if($('#clearSiteLogo').checked)$('#siteLogoFile').value='';});
  $('#siteBrandingForm').addEventListener('submit',async e=>{
    e.preventDefault();
    const file=$('#siteLogoFile').files[0];
    if(file&&file.size>3*1024*1024){toast('Logo vượt quá 3 MB','error');return;}
    if(!file&&!$('#clearSiteLogo').checked){toast('Hãy chọn logo mới hoặc đánh dấu bỏ logo tùy chỉnh.','error');return;}
    const form=new FormData();
    form.append('clear_logo',$('#clearSiteLogo').checked?'true':'false');
    if(file)form.append('logo',file);
    const button=$('#saveSiteBrandingBtn'),status=$('#siteBrandingStatus');
    button.disabled=true;status.textContent='Đang tải logo và tạo favicon…';status.classList.remove('success');
    try{
      const response=await fetch('/api/admin/site-branding',{method:'PUT',headers:{Authorization:`Bearer ${token}`,Accept:'application/json'},body:form});
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);
      status.textContent=data.message||'Đã cập nhật logo và favicon.';status.classList.add('success');
      toast(status.textContent,'success');
      await loadSiteBrandingConfig();
      if(window.applySiteBranding)window.applySiteBranding(data);
    }catch(err){status.textContent=err.message||'Không lưu được logo.';status.classList.remove('success');toast(status.textContent,'error');}
    finally{button.disabled=false;}
  });

  async function loadPlanConfig(){
    try {
      const data=await api('/api/admin/plan-config');
      $('#proPlanDescription').value=data.description||'';
      $('#proPlanPriceLabel').value=data.price_label||'';
      const preview=$('#proPlanQrPreview');
      if(data.qr_image_url){preview.src=data.qr_image_url;preview.hidden=false;$('#proPlanQrCurrentLabel').textContent=data.qr_source==='database'?'QR hiện tại đang lưu trong PostgreSQL.': 'QR hiện tại lấy từ biến môi trường PAYMENT_QR_IMAGE_URL.';}
      else {preview.removeAttribute('src');preview.hidden=true;$('#proPlanQrCurrentLabel').textContent='Chưa cấu hình ảnh QR.';}
      $('#clearProPlanQr').checked=false;
      $('#proPlanConfigStatus').textContent='';
    } catch(err) { $('#proPlanConfigStatus').textContent=err.message||'Không tải được cấu hình gói Pro.'; }
  }
  $('#proPlanQrFile').addEventListener('change',()=>{if($('#proPlanQrFile').files.length)$('#clearProPlanQr').checked=false;});
  $('#clearProPlanQr').addEventListener('change',()=>{if($('#clearProPlanQr').checked)$('#proPlanQrFile').value='';});
  $('#proPlanConfigForm').addEventListener('submit',async e=>{
    e.preventDefault();
    const file=$('#proPlanQrFile').files[0];
    if(file&&file.size>3*1024*1024){toast('Ảnh QR vượt quá 3 MB','error');return;}
    const form=new FormData();
    form.append('description',$('#proPlanDescription').value.trim());
    form.append('price_label',$('#proPlanPriceLabel').value.trim());
    form.append('clear_qr',$('#clearProPlanQr').checked?'true':'false');
    if(file)form.append('qr',file);
    $('#saveProPlanConfigBtn').disabled=true;$('#proPlanConfigStatus').textContent='Đang lưu cấu hình…';
    try{
      const response=await fetch('/api/admin/plan-config',{method:'PUT',headers:{Authorization:`Bearer ${token}`,Accept:'application/json'},body:form});
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);
      $('#proPlanQrFile').value='';$('#clearProPlanQr').checked=false;
      toast(data.message||'Đã lưu cấu hình gói Pro','success');await loadPlanConfig();
    }catch(err){$('#proPlanConfigStatus').textContent=err.message;toast(err.message,'error');}
    finally{$('#saveProPlanConfigBtn').disabled=false;}
  });
  async function openPaymentProof(id, username){
    $('#paymentProofError').textContent=''; $('#paymentProofTitle').textContent=`Bằng chứng thanh toán · ${username||`#${id}`}`;
    $('#paymentProofImage').removeAttribute('src');
    const dialog=$('#paymentProofDialog');
    try {
      const response=await fetch(`/api/admin/payment-requests/${id}/proof`,{headers:{Authorization:`Bearer ${token}`}});
      if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(data.detail||`HTTP ${response.status}`);}
      const blob=await response.blob();
      if(paymentProofBlobUrl)URL.revokeObjectURL(paymentProofBlobUrl);
      paymentProofBlobUrl=URL.createObjectURL(blob); $('#paymentProofImage').src=paymentProofBlobUrl;
      if(typeof dialog.showModal==='function')dialog.showModal(); else window.open(paymentProofBlobUrl,'_blank','noopener');
    } catch(err) { toast(`Không mở được bằng chứng: ${err.message}`,'error'); }
  }
  async function runPipeline(){try{const r=await api('/api/pipeline/run',{method:'POST',body:'{}'});toast(r.message,'success');showPage('pipeline');setTimeout(()=>loadRuns().catch(()=>{}),1800);}catch(e){toast(e.message,'error');}}
  $('#loginForm').addEventListener('submit',async e=>{e.preventDefault();$('#loginError').textContent='';busy(true,'Đang đăng nhập...');try{await login($('#loginUsername').value.trim(),$('#loginPassword').value);$('#loginPassword').value='';}catch(err){$('#loginError').textContent=err.message;}finally{busy(false);}});
  $('#logoutBtn').addEventListener('click',()=>{token='';sessionStorage.removeItem('dlh_token');showLogin();});
  $('#mainNav').addEventListener('click',e=>{const b=e.target.closest('[data-page]');if(b)showPage(b.dataset.page);});
  document.body.addEventListener('click',e=>{const goto=e.target.closest('[data-goto]');if(goto)showPage(goto.dataset.goto);});
  $('#refreshBtn').addEventListener('click',()=>loadPage(currentPage).then(()=>toast('Đã làm mới dữ liệu','success')).catch(e=>toast(e.message,'error')));
  $('#refreshUsersBtn').addEventListener('click',()=>loadUsersPage().then(()=>toast('Đã làm mới danh sách người dùng','success')).catch(e=>toast(e.message,'error')));
  $('#userSearchForm').addEventListener('submit',e=>{e.preventDefault();loadUsers().catch(err=>toast(err.message,'error'));});
  $('#closePaymentProofDialog').addEventListener('click',()=>$('#paymentProofDialog').close());
  $('#paymentProofDialog').addEventListener('close',()=>{if(paymentProofBlobUrl){URL.revokeObjectURL(paymentProofBlobUrl);paymentProofBlobUrl=null;}$('#paymentProofImage').removeAttribute('src');});
  $('#runPipelineBtn').addEventListener('click',runPipeline);$('#runPipelineBtn2').addEventListener('click',runPipeline);$('#refreshRunsBtn').addEventListener('click',()=>Promise.all([loadRuns(),loadIssues()]));$('#searchArticlesBtn').addEventListener('click',()=>{listPages.articles=1;loadArticles().catch(e=>toast(e.message,'error'));});$('#loadRawBtn').addEventListener('click',()=>loadRawItems().catch(e=>toast(e.message,'error')));$('#applyRulesBtn').addEventListener('click',async()=>{if(!confirm('Áp dụng các quy tắc đang bật cho toàn bộ bài đã lưu? Đây chỉ cập nhật trạng thái included/excluded, không xóa bản ghi raw.'))return;try{busy(true,'Đang áp dụng quy tắc...');const r=await api('/api/cleaning-rules/apply',{method:'POST',body:'{}'});toast(`Đã quét ${r.scanned} bài, cập nhật ${r.updated}; bị loại sau áp dụng: ${r.excluded_after_apply}`,'success');await Promise.all([loadArticles(),loadDashboard()]);}catch(e){toast(e.message,'error')}finally{busy(false)}});$('#articleSearch').addEventListener('keydown',e=>{if(e.key==='Enter'){listPages.articles=1;loadArticles().catch(err=>toast(err.message,'error'));}});$('#articleSourceFilter').addEventListener('change',()=>{listPages.articles=1;loadArticles().catch(e=>toast(e.message,'error'));});$('#articleExcludedFilter').addEventListener('change',()=>{listPages.articles=1;loadArticles().catch(e=>toast(e.message,'error'));});$('#articleFieldCategoryFilter').addEventListener('change',()=>{listPages.articles=1;loadArticles().catch(e=>toast(e.message,'error'));});$('#searchTopicsBtn').addEventListener('click',()=>{listPages.topics=1;loadTopics().catch(e=>toast(e.message,'error'));});$('#topicSearch').addEventListener('keydown',e=>{if(e.key==='Enter'){listPages.topics=1;loadTopics().catch(err=>toast(err.message,'error'));}});$('#searchEntitiesBtn').addEventListener('click',()=>{listPages.entities=1;loadEntities().catch(e=>toast(e.message,'error'));});$('#entitySearch').addEventListener('keydown',e=>{if(e.key==='Enter'){listPages.entities=1;loadEntities().catch(err=>toast(err.message,'error'));}});$('#openIssuesOnly').addEventListener('change',loadIssues);$('#loadFactBtn').addEventListener('click',loadFacts);$('#loadModelsBtn').addEventListener('click',()=>loadModels().catch(e=>toast(e.message,'error')));
  $('#addSourceBtn').addEventListener('click',()=>{$('#sourceName').focus();$('#sourceForm').scrollIntoView({behavior:'smooth',block:'center'});});
  $('#sourceForm').addEventListener('submit',async e=>{e.preventDefault();const body={name:$('#sourceName').value.trim(),feed_url:$('#sourceUrl').value.trim(),language:$('#sourceLanguage').value,category:$('#sourceCategory').value.trim()||'general',license_name:$('#sourceLicense').value.trim()||'check_required',license_url:$('#sourceLicenseUrl').value.trim()||null,enabled:$('#sourceEnabled').checked};try{busy(true,'Đang lưu nguồn RSS...');await api('/api/sources',{method:'POST',body:JSON.stringify(body)});e.target.reset();$('#sourceEnabled').checked=true;toast('Đã tạo nguồn RSS','success');await Promise.all([loadSources(),populateSourceFilter()]);}catch(err){toast(err.message,'error');}finally{busy(false);}});
  $('#toggleRuleFormBtn').addEventListener('click',()=>$('#ruleFormPanel').classList.toggle('hidden'));
  $('#ruleType').addEventListener('change',()=>{const t=$('#ruleType').value;$('#ruleConfig').value=JSON.stringify(t==='min_title_length'?{min_length:15}:t==='exclude_source'?{source_ids:[]}:{keywords:[]},null,2);});
  $('#ruleForm').addEventListener('submit',async e=>{e.preventDefault();let cfg;try{cfg=JSON.parse($('#ruleConfig').value||'{}');}catch{toast('JSON cấu hình không hợp lệ','error');return;}try{await api('/api/cleaning-rules',{method:'POST',body:JSON.stringify({name:$('#ruleName').value.trim(),rule_type:$('#ruleType').value,config:cfg,enabled:$('#ruleEnabled').checked})});toast('Đã tạo quy tắc; nó sẽ áp dụng ở lần pipeline tiếp theo','success');$('#ruleName').value='';await loadRules();}catch(err){toast(err.message,'error');}});
  $('#topicForm').addEventListener('submit',async e=>{e.preventDefault();const body={name:$('#topicName').value.trim(),topic_type:$('#topicType').value,description:$('#topicDescription').value.trim(),keywords:$('#topicKeywords').value.split(',').map(x=>x.trim()).filter(Boolean)};try{await api('/api/topics',{method:'POST',body:JSON.stringify(body)});e.target.reset();listPages.topics=1;toast('Đã tạo topic','success');await loadTopics();}catch(err){toast(err.message,'error');}});
  $('#entityForm').addEventListener('submit',async e=>{e.preventDefault();const body={canonical_name:$('#entityName').value.trim(),entity_type:$('#entityType').value,external_id:$('#entityExternalId').value.trim()||null,description:$('#entityDescription').value.trim(),aliases:$('#entityAliases').value.split(',').map(x=>x.trim()).filter(Boolean)};try{await api('/api/entities',{method:'POST',body:JSON.stringify(body)});e.target.reset();listPages.entities=1;toast('Đã tạo entity','success');await loadEntities();}catch(err){toast(err.message,'error');}});

  $('#closeClassificationDialog').addEventListener('click',()=>$('#classificationDialog').close());
  $('#cancelClassificationBtn').addEventListener('click',()=>$('#classificationDialog').close());
  $('#classificationForm').addEventListener('submit',async e=>{e.preventDefault();const articleId=Number($('#classificationArticleId').value);const parseNames=id=>$('#'+id).value.split(/\n+/).map(x=>x.trim()).filter(Boolean);try{busy(true,'Đang lưu phân loại bài báo...');await api(`/api/articles/${articleId}/classifications`,{method:'PATCH',body:JSON.stringify({topic_names:parseNames('classificationTopics'),entity_names:parseNames('classificationEntities')})});$('#classificationDialog').close();toast('Đã cập nhật topic/entity cho bài báo','success');await Promise.all([loadArticles(),loadDashboard()]);if(currentPage==='timeline')await loadTopicTimeline();}catch(err){toast(err.message,'error');}finally{busy(false)}});
  $('#loadTopicTimelineBtn').addEventListener('click',()=>loadTopicTimeline().catch(e=>toast(e.message,'error')));
  $('#topicTimelineSelect').addEventListener('change',()=>loadTopicTimeline().catch(e=>toast(e.message,'error')));
  document.body.addEventListener('click',async e=>{const b=e.target.closest('[data-page-control]');if(!b||b.disabled)return;const kind=b.dataset.pageControl,dir=b.dataset.pageDir;if(!Object.prototype.hasOwnProperty.call(listPages,kind))return;listPages[kind]=Math.max(1,listPages[kind]+(dir==='next'?1:-1));try{if(kind==='articles')await loadArticles();else if(kind==='topics')await loadTopics();else if(kind==='entities')await loadEntities();}catch(err){toast(err.message,'error');}});
  document.body.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;const id=b.dataset.id,action=b.dataset.action;try{if(action==='edit-source'){const name=prompt('Tên nguồn',b.dataset.name);if(name===null)return;const feed_url=prompt('RSS URL',b.dataset.url);if(feed_url===null)return;const language=prompt('Ngôn ngữ (en/vi/ja/other)',b.dataset.language);if(language===null)return;const category=prompt('Danh mục',b.dataset.category);if(category===null)return;const license_name=prompt('Tên giấy phép',b.dataset.license);if(license_name===null)return;const license_url=prompt('URL giấy phép',b.dataset.licenseUrl);if(license_url===null)return;await api(`/api/sources/${id}`,{method:'PATCH',body:JSON.stringify({name,feed_url,language,category,license_name,license_url:license_url||null})});toast('Đã cập nhật nguồn','success');await loadSources();}
      else if(action==='test-source'){busy(true,'Đang kiểm tra RSS...');const r=await api(`/api/sources/${id}/test`,{method:'POST',body:'{}'});if(r.ok)toast(`RSS hợp lệ · ${r.entry_count} entries · ${r.feed_title||'Không có feed title'}`,'success');else toast(r.error||r.warning||'RSS test failed','error');}
      else if(action==='toggle-source'){await api(`/api/sources/${id}`,{method:'PATCH',body:JSON.stringify({enabled:b.dataset.enabled!=='true'})});toast('Đã cập nhật nguồn','success');await loadSources();}
      else if(action==='delete-source'){if(!confirm('Xóa nguồn và các bài báo liên quan? Dữ liệu theo nguồn này sẽ bị cascade delete.'))return;await api(`/api/sources/${id}`,{method:'DELETE'});toast('Đã xóa nguồn','success');await Promise.all([loadSources(),populateSourceFilter()]);}
      else if(action==='toggle-rule'){await api(`/api/cleaning-rules/${id}`,{method:'PATCH',body:JSON.stringify({enabled:b.dataset.enabled!=='true'})});toast('Đã cập nhật quy tắc','success');await loadRules();}
      else if(action==='delete-rule'){if(!confirm('Xóa quy tắc này?'))return;await api(`/api/cleaning-rules/${id}`,{method:'DELETE'});await loadRules();toast('Đã xóa quy tắc','success');}
      else if(action==='edit-classification'){await openClassificationEditor(Number(id));}
      else if(action==='topic-timeline'){await loadTopicTimelineOptions(Number(id));showPage('timeline',{skipLoad:true});}
      else if(action==='resolve-issue'){await api(`/api/quality/issues/${id}/resolve`,{method:'POST',body:'{}'});toast('Đã đánh dấu xử lý','success');await loadIssues();}
      else if(action==='edit-entity'){const name=prompt('Tên chính thức',b.dataset.name);if(name===null)return;const type=prompt('Loại entity (ORG/PERSON/LOCATION/PRODUCT/EVENT/GROUP/UNKNOWN)',b.dataset.type);if(type===null)return;const status=confirm('OK = active, Cancel = archived')?'active':'archived';await api(`/api/entities/${id}`,{method:'PATCH',body:JSON.stringify({canonical_name:name,entity_type:type.toUpperCase(),status})});toast('Đã cập nhật entity','success');await loadEntities();}
      else if(action==='toggle-model'){await api(`/api/data-models/${id}`,{method:'PATCH',body:JSON.stringify({active:b.dataset.active!=='true'})});toast('Đã cập nhật trạng thái data model','success');await loadModels();}
      else if(action==='edit-model'){const description=prompt('Mô tả bảng',b.dataset.description);if(description===null)return;const grain=prompt('Grain — một dòng đại diện cho gì?',b.dataset.grain);if(grain===null)return;const owner=prompt('Owner',b.dataset.owner);if(owner===null)return;await api(`/api/data-models/${id}`,{method:'PATCH',body:JSON.stringify({description,grain,owner})});toast('Đã cập nhật data model','success');await loadModels();}
      else if(action==='edit-topic'){const name=prompt('Tên topic',b.dataset.name);if(name===null)return;const keys=prompt('Từ khóa, cách nhau bằng dấu phẩy',b.dataset.keywords);if(keys===null)return;const status=confirm('OK = active, Cancel = archived')?'active':'archived';await api(`/api/topics/${id}`,{method:'PATCH',body:JSON.stringify({name,keywords:keys.split(',').map(x=>x.trim()).filter(Boolean),status})});toast('Đã cập nhật topic','success');await loadTopics();}
      else if(action==='payment-proof'){await openPaymentProof(id,b.dataset.username);}
      else if(action==='approve-payment'){if(!confirm('Xác nhận thanh toán hợp lệ và cấp gói Pro cho user này?'))return;await api(`/api/admin/payment-requests/${id}/review`,{method:'POST',body:JSON.stringify({decision:'approve',note:''})});toast('Đã duyệt thanh toán và kích hoạt Pro','success');await loadUsersPage();}
      else if(action==='reject-payment'){const note=prompt('Lý do từ chối (có thể để trống):','');if(note===null)return;await api(`/api/admin/payment-requests/${id}/review`,{method:'POST',body:JSON.stringify({decision:'reject',note})});toast('Đã từ chối yêu cầu thanh toán','success');await loadUsersPage();}
      else if(action==='save-user-plan'){const select=document.querySelector(`[data-user-plan="${id}"]`);if(!select)return;await api(`/api/admin/users/${id}/plan`,{method:'PATCH',body:JSON.stringify({plan:select.value})});toast(`Đã cập nhật gói user #${id}`,'success');await loadUsersPage();}
    }catch(err){toast(err.message,'error');}finally{busy(false);}});
  async function init(){if(token){showApp();try{await api('/api/auth/me');await boot();return;}catch{}}showLogin();}
  init();
})();
