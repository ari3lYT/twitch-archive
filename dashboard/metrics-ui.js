let transferSelected='',transferData=null,transferLoading=false,transferFetchedAt=0,transferRange='all';
const elapsedTime=n=>{if(n==null||!Number.isFinite(+n))return '—';n=Math.max(0,Math.round(n));const h=Math.floor(n/3600),m=Math.floor(n%3600/60),s=n%60;return (h?h+' ч ':'')+String(m).padStart(2,'0')+' мин '+String(s).padStart(2,'0')+' с';};
const rateText=n=>n==null?'—':bytes(n)+'/с';
function renderTransfer(s){
 const select=$('#transfer-select'),selected=transferSelected||s.worker.active_vod_id||s.telemetry?.summary?.vod_id||s.vods[0]?.id;
 const signature=s.vods.map(v=>v.id).join(',');if(select.dataset.signature!==signature){select.innerHTML=s.vods.map(v=>`<option value="${esc(v.id)}">${esc(v.title)} · #${esc(v.id)}</option>`).join('');select.dataset.signature=signature;}select.value=selected||'';
 if(s.telemetry?.summary?.vod_id===selected){transferData=s.telemetry;transferFetchedAt=Date.now();}
 else if(!transferLoading&&(transferData?.summary?.vod_id!==selected||Date.now()-transferFetchedAt>4500))loadTransfer(selected);
 if(transferData?.summary?.vod_id===selected)drawTransfer(transferData,s);else drawTransfer(null,s);
}
async function loadTransfer(id){if(!id||transferLoading)return;transferLoading=true;try{transferData=await api('vods/'+id+'/transfer');transferFetchedAt=Date.now();if(state)drawTransfer(transferData,state);}catch(e){toast(e.message,true);}finally{transferLoading=false;}}
function drawTransfer(data,s){
 const m=data?.summary,empty=$('#transfer-empty');empty.hidden=!!m;$('#transfer-body').hidden=!m;
 if(!m){empty.textContent=transferLoading?'Получаем статистику…':'Для этой записи измерений ещё нет. Новые загрузки записывают показатели автоматически.';return;}
 const estimated=m.source==='log_estimate',approx=estimated?'≈ ':'';
 const stageNames={preparing:'Подготовка',downloading:'Скачивается',verifying:'Проверка файла',paused:'Приостановлено',retry:'Ожидает повтора',done:'Завершено'};
 $('#transfer-stage').textContent=(s.worker.paused&&m.stage!=='done'?'Ручная пауза':!s.schedule.allowed&&m.stage!=='done'?'Ночная пауза':m.stale?'Нет свежих измерений':stageNames[m.stage]||m.stage)+(estimated?' · восстановлено из журнала':'');
 $('#transfer-current').textContent=estimated?'—':rateText(m.network_bps);
 $('#transfer-average').textContent=approx+rateText(m.average_bps);$('#transfer-peak').textContent=approx+rateText(m.peak_bps);
 $('#transfer-active').textContent=approx+elapsedTime(m.active_seconds);$('#transfer-wall').textContent=elapsedTime(m.wall_seconds);
 $('#transfer-wait').textContent=approx+elapsedTime(m.waiting_seconds);$('#transfer-verify').textContent=elapsedTime(m.verification_seconds);
 $('#transfer-downloaded').textContent=bytes(m.offset);$('#transfer-estimated-size').textContent=m.estimated_total_bytes?'≈ '+bytes(m.estimated_total_bytes):'—';
 $('#transfer-eta').textContent=m.stage==='done'?'Завершено':m.stage==='verifying'?'Проверяется':m.eta_active_seconds!=null?'≈ '+elapsedTime(m.eta_active_seconds):m.stalled?'Ожидаем фрагмент':'Собираем замеры';
 $('#transfer-finish').textContent=m.eta_at?'Окончание загрузки ≈ '+time(m.eta_at)+' МСК':m.stage==='verifying'?'Проверяем видео, звук и SHA-256':s.worker.paused&&m.stage!=='done'?'После снятия ручной паузы':'ETA учитывает ночное окно; проверка файла отдельно';
 $('#transfer-retries').textContent=m.fragment_retries||0;$('#transfer-resumes').textContent=m.resumptions||0;
 $('#transfer-note').textContent=estimated?'Старые данные восстановлены из реальных журналов. Время и скорость оценочные, сетевой трафик тогда не измерялся. Новые загрузки пишут точные счётчики каждые 2 секунды.':'Фиолетовый — получено по сети, зелёный — сохранено на NAS с контрольной точкой. Средняя скорость и ETA считаются по сохранённым байтам. Паузы и перезапуски исключены из активного времени. Размер и ETA предварительные.';
 $('#transfer-json').href='./api/vods/'+encodeURIComponent(m.vod_id)+'/transfer';$('#transfer-json').setAttribute('download','twitch_'+m.vod_id+'_transfer.json');
 const names={started:'Начало',resumed:'Возобновление',paused:'Пауза',preparing:'Подготовка',downloading:'Скачивание',verifying:'Проверка',done:'Завершено',retry:'Ожидание повтора',fragment_retry:'Повтор фрагмента',source_reset:'Изменение источника'};
 $('#transfer-events').innerHTML=(data.events||[]).slice(-12).map(e=>`<li><time>${esc(time(e.at))}</time><span>${esc(names[e.type]||e.type)}${e.type==='fragment_retry'?' #'+esc(e.data?.fragment):''}</span></li>`).join('');
 let points=data.history||[];if(transferRange!=='all'&&points.length){const edge=points.at(-1).at-Number(transferRange);points=points.filter(p=>p.at>=edge);}
 speedChart(points,estimated);
}
function speedChart(points,estimated){
 const svg=$('#transfer-chart'),labels=$('#transfer-chart-labels'),tip=$('#transfer-tooltip');tip.hidden=true;
 if(!points.length){svg.innerHTML='<text x="500" y="120" text-anchor="middle" fill="#aaa">Собираем историю скорости</text>';labels.textContent='';return;}
 const start=points[0].at,end=Math.max(start+1,points.at(-1).at),max=Math.max(1048576,...points.flatMap(p=>[Number(p.network_bps)||0,Number(p.committed_bps)||0]))*1.12;
 const x=p=>54+(p.at-start)/(end-start)*930,y=v=>206-(Number(v)||0)/max*180;
 let html='';for(let i=0;i<=4;i++){const value=max*i/4,yy=y(value);html+=`<line x1="54" y1="${yy}" x2="984" y2="${yy}" stroke="#34313f" stroke-dasharray="3 6"/><text x="45" y="${yy+4}" text-anchor="end" fill="#8c829c" font-size="11">${(value/1048576).toFixed(value>=10485760?0:1)}</text>`;}
 for(const [field,color] of [['network_bps','#ba9afa'],['committed_bps','#b4ed7a']]){if(estimated&&field==='network_bps')continue;let path='',prev=null;for(const p of points){const gap=!prev||p.at-prev.at>20||p.stage==='paused'||prev.stage==='paused';path+=(gap?'M':'L')+x(p).toFixed(1)+','+y(p[field]).toFixed(1)+' ';prev=p;}html+=`<path d="${path}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;}
 html+='<line id="transfer-crosshair" x1="0" y1="18" x2="0" y2="206" stroke="#d2c0e5" stroke-dasharray="4 4" visibility="hidden"/>';
 svg.innerHTML=html;labels.innerHTML=`<span>МиБ/с</span><span>${esc(time(start))} → ${esc(time(end))} МСК · ${points.length} точек${estimated?' · оценка':''}</span>`;
 svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),target=start+Math.max(0,Math.min(1,((event.clientX-rect.left)/rect.width*1000-54)/930))*(end-start);const point=points.reduce((a,b)=>Math.abs(b.at-target)<Math.abs(a.at-target)?b:a);const cross=$('#transfer-crosshair');cross.setAttribute('x1',x(point));cross.setAttribute('x2',x(point));cross.setAttribute('visibility','visible');tip.hidden=false;tip.textContent=time(point.at)+' МСК · '+(estimated?'Оценка: '+rateText(point.committed_bps):'Сеть '+rateText(point.network_bps)+' · NAS '+rateText(point.committed_bps));};
 svg.onpointerleave=()=>{tip.hidden=true;$('#transfer-crosshair')?.setAttribute('visibility','hidden');};
}
$('#transfer-select').onchange=event=>{transferSelected=event.target.value;transferData=null;renderTransfer(state);};
$('#transfer-range').onchange=event=>{transferRange=event.target.value;if(transferData)drawTransfer(transferData,state);};
function insightsSection(d){const m=d.insights;if(!m?.files)return '';return `<section class="metadata-section"><h3>Метрики, события и действия</h3><div class="insight-numbers"><span><b>${m.messages}</b> сообщений</span><span><b>${m.unique_users}</b> авторов</span><span><b>${m.events}</b> событий</span><span><b>${m.actions}</b> действий в чате</span></div><div id="chat-activity-chart"></div><div class="detail-actions"><a class="button secondary" href="./api/vods/${d.id}/metrics.json">Метрики JSON</a><a class="button secondary" href="./api/vods/${d.id}/events.jsonl">События JSONL</a><a class="button secondary" href="./api/vods/${d.id}/actions.jsonl">Действия JSONL</a>${d.transfer_available?`<a class="button secondary" href="./api/vods/${d.id}/download.metrics.json">Статистика загрузки</a>`:''}</div><p class="muted small">Действия: команды, упоминания, ссылки и смайлы из сообщений. Выполнение команд не подтверждается. Полная история сабов, рейдов, банов и зрителей в replay-источнике отсутствует.</p></section>`;}
async function loadInsightPlot(d){if(!d.insights?.files)return;try{const m=await api('vods/'+d.id+'/metrics.json'),root=$('#chat-activity-chart');if(!root)return;const points=m.chat?.timeline||[],last=Math.max(1,...points.map(x=>x.minute)),max=Math.max(1,...points.map(x=>x.messages));root.innerHTML='<p class="muted small">Активность чата по минутам · пик '+max+' сообщений/мин</p><svg role="img" aria-label="Активность чата по минутам" viewBox="0 0 600 90" width="100%" height="90">'+points.map(p=>`<rect x="${p.minute/(last+1)*600}" y="${85-p.messages/max*75}" width="${Math.max(1,600/(last+1)-.5)}" height="${p.messages/max*75}" fill="#ba9afa"><title>${p.minute} мин: ${p.messages} сообщений</title></rect>`).join('')+'</svg>';}catch{}}
