import {readVisible,stable} from './raw-auto-dom.mjs';
import {buildScopedBridge} from './raw-auto-bridge.mjs';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const position='(()=>{const s=document.querySelector(".thread-scroll-container");if(!s)return {error:"NO_SCROLL"};return {top:s.scrollTop,max:Math.max(0,s.scrollHeight-s.clientHeight)}})()';
const to=t=>'(()=>{const s=document.querySelector(".thread-scroll-container");if(!s)return {error:"NO_SCROLL"};s.scrollTop='+Number(t)+';return {top:s.scrollTop,max:Math.max(0,s.scrollHeight-s.clientHeight)}})()';

export async function collectForwardBridge({cdp,loaded,stepPx=275,maxSteps=245,deadlineMs=76000,settleMs=150,requireBackground=false}){
 const startAt=Date.now();
 const fail=(status,details={})=>({status,ok:false,...details});
 if(!loaded?.record?.messages?.length)return fail('FORWARD_NO_BASELINE_HOLD');
 const tail=loaded.record.messages.at(-1).key;
 const backgroundCheck=async()=>{
  if(!requireBackground)return true;
  const r=await cdp.send('Runtime.evaluate',{expression:'document.hasFocus()===false',returnByValue:true},6000);
  return r.result?.value===true;
 };
 const evalBrowser=async e=>{
  const x=await cdp.send('Runtime.evaluate',{expression:e,returnByValue:true},9500);
  if(x.exceptionDetails)throw Error('RUNTIME_ERROR');return x.result?.value??null;
 };
 let start=null,restoreOk=false,windows=[],positions=[],attempted=0;
 async function stableSample(){
  for(let i=0;i<3;i++){
   const a=readVisible((await cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
   await sleep(settleMs);
   const b=readVisible((await cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
   if(stable(a,b))return b;
   await sleep(170);
  }
  return null;
 }
 try{
  if(!(await backgroundCheck()))return fail('FORWARD_ACTIVE_TAB_HOLD');
  start=await evalBrowser(position);
  if(!start||start.error)return fail('FORWARD_SCROLLER_MISSING_HOLD');
  // Locate a verified saved anchor by sampling the existing lazy-loaded page.
  let found=null,origin=null;
  const fractions=[0,.04,.08,.12,.16,.2,.24,.28,.32,.36,.4,.44,.48,.52,.56,.6,.64,.68,.72,.76,.8,.84,.88,.92,.96,1];
  for(const fraction of fractions){
   if(Date.now()-startAt>deadlineMs*0.3)return fail('FORWARD_LOCATE_TIMEOUT_HOLD');
   if(!(await backgroundCheck()))return fail('FORWARD_FOCUS_GAINED_HOLD',{attempted});
   const p=await evalBrowser(position);
   await evalBrowser(to(Math.round(p.max*fraction)));
   await sleep(230);
   const s=await stableSample();attempted++;
   if(!s)continue;
   if(s.some(m=>m.key===tail)){found=s;origin=await evalBrowser(position);break}
  }
  if(!found)return fail('FORWARD_TAIL_NOT_FOUND_HOLD',{attempted});
  windows.push(found);positions.push(Math.round(origin.top));
  let prev=found,reachBottom=false;
  for(let j=0;j<maxSteps;j++){
   if(Date.now()-startAt>deadlineMs)return fail('FORWARD_DEADLINE_HOLD',{attempted,windows:windows.length});
   if(!(await backgroundCheck()))return fail('FORWARD_FOCUS_GAINED_HOLD',{attempted});
   const loc=await evalBrowser(position);
   if(loc.max-loc.top<2){reachBottom=true;break}
   await evalBrowser(to(Math.min(loc.max,loc.top+stepPx)));
   await sleep(200);
   const sample=await stableSample();attempted++;
   if(!sample)return fail('FORWARD_UNSTABLE_HOLD',{attempted});
   if(!sample.some(m=>prev.some(n=>n.key===m.key)))
    return fail('FORWARD_NONOVERLAP_HOLD',{attempted,windows:windows.length,at:loc.top});
   if(sample.map(m=>m.key).join('|')!==prev.map(m=>m.key).join('|')){
    windows.push(sample);positions.push(Math.round(loc.top));
   }
   prev=sample;
  }
  if(!reachBottom)return fail('FORWARD_NOT_AT_LATEST_HOLD',{attempted,windows:windows.length});
  const result=buildScopedBridge({state:loaded.record,archivedThrough:loaded.archivedThrough,slices:windows,latest:windows.at(-1)});
  if(!result.ok)return {...result,attempted,windows:windows.length};
  return {...result,evidence:{...result.evidence,scan:'BOUNDED_FORWARD_STABLE_DOM',attempted,windows:windows.length,elapsedMs:Date.now()-startAt,
   positionsRange:[positions[0],positions.at(-1)],cloudHistory:'UNKNOWN'}};
 }catch(e){return fail('FORWARD_CDP_HOLD',{reason:String(e.message).slice(0,115),attempted})}
 finally{if(start)try{await evalBrowser(to(start.top));restoreOk=true}catch{}}
}
