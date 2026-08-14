(function(){
  const number=value=>Number(value)||0;
  const money=value=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(number(value));
  const svg=(tag,attributes={})=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,value));return node};
  function formatTime(value,timeZone){const date=new Date(value);if(Number.isNaN(date.getTime()))return value;return new Intl.DateTimeFormat('zh-CN',{timeZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date)}
  function initTheme(toggleSelector='#theme-toggle'){
    const toggle=document.querySelector(toggleSelector);
    const stored=localStorage.getItem('charge-hub-theme');
    const theme=stored||((window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light');
    const apply=value=>{document.documentElement.dataset.theme=value;if(toggle)toggle.textContent=value==='dark'?'◑ 浅色模式':'◐ 暗色模式'};
    apply(theme);
    if(toggle)toggle.addEventListener('click',()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';localStorage.setItem('charge-hub-theme',next);apply(next)});
  }
  function metric(label,value,note,tone='blue'){
    const card=document.createElement('article');card.className=`summary-card ${tone}`;
    const heading=document.createElement('span');heading.textContent=label;
    const metricValue=document.createElement('strong');metricValue.textContent=value;
    const detail=document.createElement('small');detail.textContent=note;
    card.append(heading,metricValue,detail);return card;
  }
  function renderSummary(root,data){
    const summary=data.summary,total=number(summary.total_amount),brokerage=number(summary.brokerage),count=data.pagination.total;
    const rate=total?`${(brokerage/total*100).toFixed(2)}%`:'0%';
    root.replaceChildren(
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
  function drawTrend(chart,items){
    chart.replaceChildren();
    if(!items.length){const text=svg('text',{x:500,y:145,'text-anchor':'middle',class:'chart-empty'});text.textContent='暂无趋势数据';chart.append(text);return}
    const values=items.map(item=>number(item.amount)),max=Math.max(...values,1),left=55,right=975,top=24,bottom=230;
    [0,.5,1].forEach(ratio=>{const y=bottom-(bottom-top)*ratio;chart.append(svg('line',{x1:left,y1:y,x2:right,y2:y,class:'chart-grid'}));const label=svg('text',{x:left-12,y:y+5,'text-anchor':'end',class:'chart-label'});label.textContent=money(max*ratio);chart.append(label)});
    const points=values.map((value,index)=>`${left+index*(right-left)/Math.max(1,values.length-1)},${bottom-value/max*(bottom-top)}`);
    chart.append(svg('polygon',{points:`${left},${bottom} ${points.join(' ')} ${right},${bottom}`,class:'trend-area'}),svg('polyline',{points:points.join(' '),class:'trend-line'}));
    [0,Math.floor((items.length-1)/2),items.length-1].filter((value,index,array)=>array.indexOf(value)===index).forEach(index=>{const label=svg('text',{x:left+index*(right-left)/Math.max(1,items.length-1),y:262,'text-anchor':index===0?'start':index===items.length-1?'end':'middle',class:'chart-label'});label.textContent=items[index].date;chart.append(label)});
  }
  function renderRanking(root,items){
    if(!items.length){root.textContent='暂无贡献者数据';return}
    const max=Math.max(...items.map(item=>number(item.amount)),1);
    root.replaceChildren(...items.map((item,index)=>{const row=document.createElement('div');row.className='rank-row';const rank=document.createElement('b');rank.textContent=index+1;const info=document.createElement('div');const line=document.createElement('div');line.className='rank-line';const name=document.createElement('span');name.textContent=item.name;const amount=document.createElement('strong');amount.textContent=money(item.amount);line.append(name,amount);const track=document.createElement('i');track.style.setProperty('--rank-width',`${number(item.amount)/max*100}%`);info.append(line,track);row.append(rank,info);return row}));
  }
  function renderMonthly(root,items){
    const totals=new Map();items.forEach(item=>{const month=item.date.slice(0,7);totals.set(month,(totals.get(month)||0)+number(item.amount))});
    const months=[...totals.entries()].slice(-10),max=Math.max(...months.map(([,value])=>value),1);
    if(!months.length){root.textContent='暂无月度数据';return}
    root.replaceChildren(...months.map(([month,value])=>{const column=document.createElement('div');column.className='month-column';const amount=document.createElement('span');amount.textContent=money(value);const bar=document.createElement('i');bar.style.height=`${Math.max(8,value/max*150)}px`;const label=document.createElement('small');label.textContent=month;column.append(amount,bar,label);return column}));
  }
  window.DashboardWidgets={number,money,formatTime,initTheme,renderSummary,drawTrend,renderRanking,renderMonthly};
})();
