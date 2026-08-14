const token=document.body.dataset.token;
const passwordRequired=document.body.dataset.password==='true';
const unlock=document.querySelector('#unlock');
const content=document.querySelector('#content');
const csrf=()=>document.cookie.split('; ').find(x=>x.startsWith('csrf_token='))?.split('=').slice(1).join('=')||'';
const number=value=>Number(value)||0;
const money=value=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(number(value));
const svg=(tag,attributes={})=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,value));return node};

function formatTime(value,timeZone){const date=new Date(value);if(Number.isNaN(date.getTime()))return value;return new Intl.DateTimeFormat('zh-CN',{timeZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date)}
function metric(label,value,note,tone='blue'){
  const card=document.createElement('article');card.className=`summary-card ${tone}`;
  const heading=document.createElement('span');heading.textContent=label;
  const metricValue=document.createElement('strong');metricValue.textContent=value;
  const detail=document.createElement('small');detail.textContent=note;
  card.append(heading,metricValue,detail);return card;
}
function renderSummary(data){
  const summary=data.summary,total=number(summary.total_amount),brokerage=number(summary.brokerage),count=data.pagination.total;
  const rate=total?`${(brokerage/total*100).toFixed(2)}%`:'0%';
  document.querySelector('#cards').replaceChildren(
    metric('总充电金额',money(total),`${count.toLocaleString('zh-CN')} 条记录`),
    metric('实际到账',money(brokerage),`到账率 ${rate}`),
    metric('平台分成差额',money(summary.platform_difference),'充电总额 − 实际到账','pink'),
    metric('充电用户',number(summary.supporters).toLocaleString('zh-CN'),'独立贡献者','ink'),
    metric('今日充电',money(summary.today_amount),'按驾驶舱时区计算','ink'),
    metric('本月充电',money(summary.month_amount),'当月累计','ink'),
    metric('充电笔数',count.toLocaleString('zh-CN'),'全部历史记录','ink'),
    metric('单笔均值',money(count?total/count:0),'历史平均充电金额','ink')
  );
}
function drawTrend(items){
  const chart=document.querySelector('#trend-chart');chart.replaceChildren();
  if(!items.length){const text=svg('text',{x:500,y:145,'text-anchor':'middle',class:'chart-empty'});text.textContent='暂无趋势数据';chart.append(text);return}
  const values=items.map(item=>number(item.amount)),max=Math.max(...values,1),left=55,right=975,top=24,bottom=230;
  [0,.5,1].forEach(ratio=>{const y=bottom-(bottom-top)*ratio;chart.append(svg('line',{x1:left,y1:y,x2:right,y2:y,class:'chart-grid'}));const label=svg('text',{x:left-12,y:y+5,'text-anchor':'end',class:'chart-label'});label.textContent=money(max*ratio);chart.append(label)});
  const points=values.map((value,index)=>`${left+index*(right-left)/Math.max(1,values.length-1)},${bottom-value/max*(bottom-top)}`);
  const area=svg('polygon',{points:`${left},${bottom} ${points.join(' ')} ${right},${bottom}`,class:'trend-area'});
  const line=svg('polyline',{points:points.join(' '),class:'trend-line'});chart.append(area,line);
  [0,Math.floor((items.length-1)/2),items.length-1].filter((value,index,array)=>array.indexOf(value)===index).forEach(index=>{const label=svg('text',{x:left+index*(right-left)/Math.max(1,items.length-1),y:262,'text-anchor':index===0?'start':index===items.length-1?'end':'middle',class:'chart-label'});label.textContent=items[index].date;chart.append(label)});
}
function renderRanking(items){
  const root=document.querySelector('#supporter-ranking');
  if(!items.length){root.textContent='暂无贡献者数据';return}
  const max=Math.max(...items.map(item=>number(item.amount)),1);
  root.replaceChildren(...items.map((item,index)=>{const row=document.createElement('div');row.className='rank-row';const rank=document.createElement('b');rank.textContent=index+1;const info=document.createElement('div');const line=document.createElement('div');line.className='rank-line';const name=document.createElement('span');name.textContent=item.name;const amount=document.createElement('strong');amount.textContent=money(item.amount);line.append(name,amount);const track=document.createElement('i');track.style.setProperty('--rank-width',`${number(item.amount)/max*100}%`);info.append(line,track);row.append(rank,info);return row}));
}
function renderMonthly(items){
  const totals=new Map();items.forEach(item=>{const month=item.date.slice(0,7);totals.set(month,(totals.get(month)||0)+number(item.amount))});
  const months=[...totals.entries()].slice(-10),max=Math.max(...months.map(([,value])=>value),1),root=document.querySelector('#monthly-bars');
  if(!months.length){root.textContent='暂无月度数据';return}
  root.replaceChildren(...months.map(([month,value])=>{const column=document.createElement('div');column.className='month-column';const amount=document.createElement('span');amount.textContent=money(value);const bar=document.createElement('i');bar.style.height=`${Math.max(8,value/max*150)}px`;const label=document.createElement('small');label.textContent=month;column.append(amount,bar,label);return column}));
}
function renderRecords(data){
  document.querySelector('#record-count').textContent=`最近 ${data.records.length} 条·共 ${data.pagination.total.toLocaleString('zh-CN')} 条`;
  document.querySelector('#recent-records').replaceChildren(...data.records.map(item=>{const row=document.createElement('tr');[formatTime(item.charged_at,data.timezone),`${item.name} (${item.uid})`,money(item.amount),money(item.brokerage)].forEach((value,index)=>{const cell=document.createElement('td');cell.textContent=value;if(index>1)cell.className='money-cell';row.append(cell)});return row}));
}
function renderDashboard(data){
  const first=data.trend[0]?.date,last=data.trend.at(-1)?.date;
  document.querySelector('#state').textContent=first?`${first} 至 ${last} · 共 ${data.pagination.total.toLocaleString('zh-CN')} 条充电记录`:'暂无充电记录';
  document.querySelector('#generated-at').textContent=`页面生成于 ${formatTime(new Date().toISOString(),data.timezone)}`;
  renderSummary(data);drawTrend(data.trend);renderRanking(data.top_supporters);renderMonthly(data.trend);renderRecords(data);
}
async function show(){
  const response=await fetch(`/api/share/${token}`);
  if(response.status===401){unlock.classList.remove('hidden');document.querySelector('#state').textContent='请先解锁这份数据';return}
  if(!response.ok){document.querySelector('#state').textContent='分享已失效或不存在';return}
  const data=await response.json();unlock.classList.add('hidden');content.classList.remove('hidden');renderDashboard(data);
}
document.querySelector('#theme-toggle').addEventListener('click',()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'light';document.querySelector('#theme-toggle').textContent=dark?'◑ 浅色模式':'◐ 暗色模式'});
document.querySelector('#unlock-form').addEventListener('submit',async event=>{event.preventDefault();const password=new FormData(event.target).get('password');const headers={'Content-Type':'application/json'};const csrfToken=csrf();if(csrfToken)headers['X-CSRF-Token']=decodeURIComponent(csrfToken);const response=await fetch(`/api/share/${token}/unlock`,{method:'POST',headers,body:JSON.stringify({password})});if(!response.ok){const data=await response.json().catch(()=>({}));const detail=data.detail&&typeof data.detail==='object'?data.detail:data;document.querySelector('#state').textContent=detail.message||'解锁失败';return}show()});
if(!passwordRequired)document.querySelector('#unlock-form input').value='unused-password';
show();
