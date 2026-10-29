/* 内容区原生 SVG：按字段绘图，不把缓存再次叠加到输入中。 */
import {compact, fmt, valueOf, costLabel} from './usage_viewmodel.js';
const NS='http://www.w3.org/2000/svg';
const svg=(tag,attrs={})=>{const e=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e;};
export function mountTrend(host, rows, metric, bucket) {
  let observer;
  const draw=()=>{
    const focused=host.querySelector(':focus')?.dataset.day;
    host.replaceChildren();
    const fields=metric==='io'?['input_total','output']:[metric];
    const values=rows.flatMap(r=>fields.map(f=>valueOf(r,f,bucket))).filter(v=>v!=null);
    if (!rows.length || !values.length) { const p=document.createElement('p');p.textContent=rows.length?'所选指标未报告，无法绘图。':'当前筛选无记录。';host.append(p);return; }
    const width=Math.max(260,host.clientWidth), height=Math.max(240,Math.round(host.clientHeight)||240), left=62, right=12, top=16, bottom=32;
    const raw=Math.max(0,...values.map(Number)), exp=10**Math.floor(Math.log10((raw||4)/4));
    const peak=[1,2,2.5,5,10].map(m=>m*exp*4).find(v=>v>=raw)||4;
    const plotW=width-left-right, plotH=height-top-bottom, slot=plotW/rows.length;
    const root=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'group','aria-label':'UTC 日级用量趋势；聚焦日期查看计数'});
    for(let i=0;i<=4;i++){
      const y=top+plotH*i/4;
      root.append(svg('line',{x1:left,x2:width-right,y1:y,y2:y,class:'usage-grid'}));
      const text=svg('text',{x:left-8,y:y+4,'text-anchor':'end',class:'usage-axis'});const value=peak*(1-i/4);text.textContent=metric==='cost'&&value>0&&value<1?value.toPrecision(2):compact(value);root.append(text);
    }
    const tip=document.createElement('p');tip.className='usage-tip';tip.setAttribute('role','status');tip.textContent='聚焦、轻点或悬停日期查看完整数值。';
    const every=Math.max(1,Math.ceil(rows.length/Math.max(2,plotW/78)));
    rows.forEach((row,i)=>{
      const x=left+i*slot;
      fields.forEach((field,j)=>{
        const value=valueOf(row,field,bucket), w=Math.min(24,slot*.65/fields.length), cx=x+slot/2+(j-(fields.length-1)/2)*w;
        if(value!=null && Number(value)>0)root.append(svg('rect',{x:cx-w/2+1,y:top+plotH-Number(value)/peak*plotH,width:Math.max(1,w-2),height:Number(value)/peak*plotH,class:j?'usage-output':'usage-input'}));
        else root.append(svg('circle',{cx,cy:top+plotH,r:2,class:value==null?'usage-unknown':'usage-input'}));
      });
      if(i%every===0 || rows.length===1){const text=svg('text',{x:x+slot/2,y:height-8,'text-anchor':'middle',class:'usage-axis'});text.textContent=row.day.slice(5);root.append(text);}
      const label=row.noRecords?`${row.day} UTC：本次查询无记录`:`${row.day} UTC：`+fields.map(f=>`${f==='input_total'?'输入':f==='output'?'输出':f==='calls'?'调用':costLabel(bucket)} ${(f==='cost'?String(valueOf(row,f,bucket)??'无金额记录'):fmt(valueOf(row,f,bucket)))}${['input_total','output'].includes(f)?`（${row.field_coverage?.[f]||0}/${row.calls} 次）`:''}`).join('；');
      const hit=svg('rect',{x,y:top,width:slot,height:plotH+5,fill:'transparent',tabindex:'0',role:'button','aria-label':label,'data-day':row.day,class:'usage-hit'});
      const show=()=>{tip.textContent=label;};
      hit.addEventListener('pointerenter',show);hit.addEventListener('focus',show);hit.addEventListener('click',show);
      hit.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();show();}if(e.key==='Escape')tip.textContent='聚焦、轻点或悬停日期查看完整数值。';});
      root.append(hit);
    });
    host.append(root,tip);
    if(focused)Array.from(host.querySelectorAll('[data-day]')).find(e=>e.dataset.day===focused)?.focus({preventScroll:true});
  };
  draw();let oldWidth=host.clientWidth,oldHeight=host.clientHeight;
  observer=new ResizeObserver(()=>{if(host.clientWidth!==oldWidth||host.clientHeight!==oldHeight){oldWidth=host.clientWidth;oldHeight=host.clientHeight;draw();}});observer.observe(host);
  return ()=>observer.disconnect();
}
