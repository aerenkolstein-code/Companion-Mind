import {readVisible,stable} from './raw-auto-dom.mjs';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const posCode="(()=>{const e=document.querySelector('.thread-scroll-container');if(!e)return null;const input=document.querySelector('[contenteditable=\"true\"]');return {top:e.scrollTop,max:Math.max(0,e.scrollHeight-e.clientHeight),height:e.scrollHeight,view:e.clientHeight,editing:document.hasFocus()&&Boolean(input?.textContent?.trim())}})()";
const setCode=top=>"(()=>{const e=document.querySelector('.thread-scroll-container');if(!e)return null;e.scrollTop="+Number(top)+";return {top:e.scrollTop,max:Math.max(0,e.scrollHeight-e.clientHeight)}})()";

export async function collectScopedHistory({cdp,deadlineMs=70000,maxSteps=175,stepPx=360,settleMs=210}){
 const failed=(status,extra={})=>({ok:false,status,...extra});
 const started=Date.now();
 const evaluate=async code=>{
  const r=await cdp.send('Runtime.evaluate',{expression:code,returnByValue:true},9000);
  if(r.exceptionDetails)throw Error('HANDOFF_BROWSER_EXCEPTION');
  return r.result?.value||null;
 };
 let original=null;
 async function snap(){
  for(let i=0;i<4;i++){
   const a=readVisible((await cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
   await sleep(settleMs);
   const b=readVisible((await cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
   if(stable(a,b))return b;
   await sleep(180);
  }
  return null;
 }
 try{
  original=await evaluate(posCode);
  if(!original)return failed('HANDOFF_SCROLLER_NOT_FOUND_HOLD');
  if(original.editing)return failed('HANDOFF_COMPOSER_BUSY_DEFERRED');
  await evaluate(setCode(0));
  let topReady=false;
  for(let i=0;i<7;i++){
   const before=await evaluate(posCode);
   await sleep(650);
   await evaluate(setCode(0));
   const after=await evaluate(posCode);
   if(Math.abs(after.top)<2 && Math.abs(after.max-before.max)<1){
    topReady=true;break;
   }
  }
  if(!topReady)return failed('HANDOFF_HISTORY_TOP_UNSTABLE_HOLD');
  const collected=[],seen=new Map(),windows=[];
  let last=null,bottomReady=false;
  for(let step=0;step<maxSteps;step++){
   if(Date.now()-started>deadlineMs)return failed('HANDOFF_HISTORY_TIMEOUT_HOLD',{windows:windows.length});
   const at=await evaluate(posCode);
   if(at.editing)return failed('HANDOFF_COMPOSER_BECAME_BUSY_DEFERRED');
   const sample=await snap();
   if(!sample?.length)return failed('HANDOFF_HISTORY_DOM_UNSTABLE_HOLD',{step});
   if(last){
    const prevKeys=new Set(last.map(m=>m.key));
    const common=sample.filter(m=>prevKeys.has(m.key)).map(m=>m.key);
    if(!common.length)return failed('HANDOFF_HISTORY_VIEWPORT_GAP_HOLD',{step});
    const order=last.filter(m=>common.includes(m.key)).map(m=>m.key);
    if(order.join('|')!==common.join('|'))return failed('HANDOFF_HISTORY_OVERLAP_ORDER_HOLD',{step});
   }
   for(const m of sample){
    const previous=seen.get(m.key);
    if(previous){
     if(previous.role!==m.role||previous.text!==m.text||previous.final!==m.final)
      return failed('HANDOFF_HISTORY_CONTENT_REVISION_HOLD',{step});
    }else{
     if(!m.key||!m.text)return failed('HANDOFF_HISTORY_EMPTY_NODE_HOLD',{step});
     seen.set(m.key,m);
     collected.push(m);
    }
   }
   const after=await evaluate(posCode);
   if(!last||sample.map(m=>m.key).join('|')!==last.map(m=>m.key).join('|'))
    windows.push({step,top:Math.round(after.top),messages:sample.length,first:sample[0].key,last:sample.at(-1).key});
   last=sample;
   if(after.max-after.top<2){
    await sleep(880);
    const latest=await evaluate(posCode);
    if(latest.max-latest.top<2){
     const again=await snap();
     if(again&&again.map(m=>m.key).join('|')===sample.map(m=>m.key).join('|')){
      bottomReady=true;break;
     }
    }
   }
   await evaluate(setCode(Math.min(after.max,after.top+stepPx)));
   await sleep(200);
  }
  if(!bottomReady)return failed('HANDOFF_HISTORY_BOTTOM_NOT_REACHED_HOLD',{windows:windows.length});
  return {ok:true,status:'HANDOFF_SCOPED_HISTORY_COLLECTED',topReached:true,bottomReached:true,
   observedAt:new Date().toISOString(),messages:collected,windowCount:windows.length,
   cloudCompleteness:'UNKNOWN',elapsedMs:Date.now()-started};
 }catch(e){return failed('HANDOFF_HISTORY_BROWSER_ERROR_HOLD',{detail:String(e.message).slice(0,110)})}
 finally{
  if(original)try{await evaluate(setCode(original.top))}catch{}
 }
}
