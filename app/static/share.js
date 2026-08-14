const token=document.body.dataset.token;
const passwordRequired=document.body.dataset.password==='true';
const unlock=document.querySelector('#unlock');
const content=document.querySelector('#content');
const widgets=window.DashboardWidgets;
const csrf=()=>document.cookie.split('; ').find(x=>x.startsWith('csrf_token='))?.split('=').slice(1).join('=')||'';

widgets.initTheme();
function renderRecords(data){
  document.querySelector('#record-count').textContent=`最近 ${data.records.length} 条·共 ${data.pagination.total.toLocaleString('zh-CN')} 条`;
  document.querySelector('#recent-records').replaceChildren(...data.records.map(item=>{const row=document.createElement('tr');[widgets.formatTime(item.charged_at,data.timezone),`${item.name} (${item.uid})`,widgets.money(item.amount),widgets.money(item.brokerage)].forEach((value,index)=>{const cell=document.createElement('td');cell.textContent=value;if(index>1)cell.className=`money-cell ${index===2?'charge':''}`;row.append(cell)});return row}));
}
function renderDashboard(data){
  const first=data.trend[0]?.date,last=data.trend.at(-1)?.date;
  document.querySelector('#state').textContent=first?`${first} 至 ${last} · 共 ${data.pagination.total.toLocaleString('zh-CN')} 条充电记录`:'暂无充电记录';
  document.querySelector('#generated-at').textContent=`页面生成于 ${widgets.formatTime(new Date().toISOString(),data.timezone)}`;
  widgets.renderSummary(document.querySelector('#cards'),data);
  widgets.drawTrend(document.querySelector('#trend-chart'),data.trend);
  widgets.renderRanking(document.querySelector('#supporter-ranking'),data.top_supporters);
  widgets.renderMonthly(document.querySelector('#monthly-bars'),data.trend);
  renderRecords(data);
}
async function show(){
  const response=await fetch(`/api/share/${token}`);
  if(response.status===401){unlock.classList.remove('hidden');document.querySelector('#state').textContent='请先解锁这份数据';return}
  if(!response.ok){document.querySelector('#state').textContent='分享已失效或不存在';return}
  const data=await response.json();unlock.classList.add('hidden');content.classList.remove('hidden');renderDashboard(data);
}
document.querySelector('#unlock-form').addEventListener('submit',async event=>{event.preventDefault();const password=new FormData(event.target).get('password');const headers={'Content-Type':'application/json'};const csrfToken=csrf();if(csrfToken)headers['X-CSRF-Token']=decodeURIComponent(csrfToken);const response=await fetch(`/api/share/${token}/unlock`,{method:'POST',headers,body:JSON.stringify({password})});if(!response.ok){const data=await response.json().catch(()=>({}));const detail=data.detail&&typeof data.detail==='object'?data.detail:data;document.querySelector('#state').textContent=detail.message||'解锁失败';return}show()});
if(!passwordRequired)document.querySelector('#unlock-form input').value='unused-password';
show();
