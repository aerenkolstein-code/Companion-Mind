import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {readVisible,stable} from './raw-auto-dom.mjs';
import {extractMessages,validateCommit,sha256} from './raw-auto-core.mjs';
import {evaluateRecovery} from './raw-auto-p1.mjs';
import {evaluateConflictProbe} from './raw-auto-conflict.mjs';
import {createStore,checkId} from './raw-auto-store.mjs';

const installedRoot=path.dirname(fileURLToPath(import.meta.url));
const args=process.argv.slice(2);
const option=(name)=>args.find(x=>x.startsWith('--'+name+'='))?.slice(name.length+3);
const has=name=>args.includes('--'+name);
const root=installedRoot; // Public mirror: no private local trial-root support.
const write=has('write');
const once=has('once');
const target=option('target')||null;
const trialId=null; // Private temporary-chat test adapter omitted.
const all=has('all-authorized');
if(target)checkId(target);
if(trialId)checkId(trialId);
// No private temporary-chat write mode in this public-safe mirror.
if(trialId&&target)throw Error('AMBIGUOUS_TRIAL_AND_TARGET');
if(write&&!target&&!trialId&&!all)throw Error('EXPLICIT_TARGET_REQUIRED_FOR_WRITE');
if(!once&&!write)throw Error('CONTINUOUS_OBSERVE_REQUIRES_EXPLICIT_ONCE_OR_WRITE');
const rawPoll=Number(option('poll-ms')||3000);
const pollMs=Math.max(2000,Math.min(30000,Number.isFinite(rawPoll)?rawPoll:3000));
const durationSec=once?Math.max(8,Math.min(90,Number(option('duration-sec')||24))):0;
const MAX_RESPONSE=3_000_000;
const CDP='http://127.0.0.1:9333';
const store=createStore(root,{write});
const sessions=new Map();
const seenStatuses=new Map();
const began=Date.now();
let stopping=false,scanCount=0,commits=0,holds=0;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function report(status,id='',details={}){
 const sanitized={at:new Date().toISOString(),status,conversationId:id||null,...details};
 if(sanitized.details)delete sanitized.details;
 const s=JSON.stringify(sanitized);
 await fs.mkdir(root,{recursive:true});
 await fs.appendFile(path.join(root,'raw-auto-status.log'),s+'\n','utf8');
 if(has('stdout'))console.log(s);
}
async function statusOnce(status,id,details={}){
 const key=id+'|'+status+'|'+JSON.stringify(details);
 if(seenStatuses.get(id)===key)return;
 seenStatuses.set(id,key);
 await report(status,id,details);
}
function chatId(value){
 try{const u=new URL(value);if(u.hostname!=='chatgpt.com'||u.protocol!=='https:')return null;
 return u.pathname.match(/\/c\/([a-f0-9-]{36})(?:\/)?$/i)?.[1]??null}catch{return null}
}
function eligible(tab){
 if(tab.type!=='page'||!tab.webSocketDebuggerUrl)return false;
 try{const u=new URL(tab.url);if(u.hostname!=='chatgpt.com'||u.protocol!=='https:')return false;
  if(trialId)return u.searchParams.get('temporary-chat')==='true';
  const id=chatId(tab.url);
  if(target)return id===target;
  return Boolean(id)||all && u.pathname==='/';
 }catch{return false}
}
function relevantRequest(url){
 try{const u=new URL(url);return u.hostname==='chatgpt.com'&&u.pathname.includes('conversation')}catch{return false}
}
class Socket{
 constructor(url){this.url=url;this.id=0;this.pending=new Map();this.handlers=new Set();this.open=false;}
 async connect(){
  const ws=new WebSocket(this.url);this.ws=ws;
  await new Promise((yes,no)=>{
   const tm=setTimeout(()=>{try{ws.close()}catch{}no(Error('CDP_CONNECT_TIMEOUT'))},5500);
   ws.addEventListener('open',()=>{clearTimeout(tm);yes()},{once:true});
   ws.addEventListener('error',()=>{clearTimeout(tm);no(Error('CDP_CONNECTION_ERROR'))},{once:true});
  });
  this.open=true;
  ws.addEventListener('message',e=>{
   let obj;try{obj=JSON.parse(e.data)}catch{return}
   if(obj.id){
    const p=this.pending.get(obj.id);if(!p)return;
    this.pending.delete(obj.id);clearTimeout(p.tm);
    obj.error?p.no(Error('CDP_'+obj.error.code)):p.yes(obj.result);
   }else if(obj.method){for(const handler of this.handlers)try{handler(obj)}catch{}}
  });
  ws.addEventListener('close',()=>{
   this.open=false;
   for(const p of this.pending.values()){clearTimeout(p.tm);p.no(Error('CDP_CLOSED'))}
   this.pending.clear();
  });
  return this;
 }
 on(handler){this.handlers.add(handler);return ()=>this.handlers.delete(handler)}
 send(method,params={},timeout=8000){
  if(!this.open)return Promise.reject(Error('CDP_NOT_CONNECTED'));
  return new Promise((yes,no)=>{
   const id=++this.id;
   const tm=setTimeout(()=>{this.pending.delete(id);no(Error('CDP_TIMEOUT_'+method))},timeout);
   this.pending.set(id,{yes,no,tm});
   this.ws.send(JSON.stringify({id,method,params}));
  });
 }
 async close(){if(this.open)try{await this.send('Network.disable',{},1300)}catch{}
  try{this.ws?.close()}catch{}this.open=false}
}
function markGap(session,reason){
 if(!session.gapSince){session.gapSince=new Date().toISOString();session.gapReason=reason}
}
async function attach(tab){
 const conn=new Socket(tab.webSocketDebuggerUrl);
 await conn.connect();
 const session={id:tab.id,tabUrl:tab.url,conversationId:trialId||chatId(tab.url),
  title:tab.title,cdp:conn,candidates:new Map(),conflicts:[],conflictProbe:null,
  requests:new Set(),pendingBodies:new Set(),pendingReads:0,lastEval:0,
  dirtyAt:0,lastTraffic:0,lastError:'',rechecking:false,gapSince:new Date().toISOString(),
  gapReason:'PRE_ATTACH_HISTORY_UNVERIFIED',connectedAt:new Date().toISOString()};
 conn.on(e=>{
  const m=e.method,p=e.params??{};
  if(m==='Network.requestWillBeSent'&&relevantRequest(p.request?.url||''))session.requests.add(p.requestId);
  if(m==='Network.loadingFinished'&&session.requests.has(p.requestId)){
   session.requests.delete(p.requestId);
   const promise=readBody(session,p.requestId);
   session.pendingBodies.add(promise);
   promise.finally(()=>session.pendingBodies.delete(promise));
  }
  if(m==='Network.loadingFailed'&&session.requests.has(p.requestId)){
   session.requests.delete(p.requestId);
   markGap(session,'NETWORK_REQUEST_FAILED');
  }
 });
 await conn.send('Network.enable',{maxTotalBufferSize:7_000_000,maxResourceBufferSize:2_700_000},6500);
 sessions.set(tab.id,session);
 await report('P2_ATTACHED',session.conversationId,{mode:write?'WRITE':'OBSERVE',
  coverage:'PRE_ATTACH_HISTORY_UNKNOWN'});
}
async function readBody(session,requestId){
 try{
  const raw=await session.cdp.send('Network.getResponseBody',{requestId},4000);
  const bytes=raw.base64Encoded?Buffer.from(raw.body,'base64'):Buffer.from(raw.body,'utf8');
  if(bytes.length>MAX_RESPONSE){markGap(session,'RESPONSE_OVERSIZE');return}
  const result=extractMessages(bytes.toString('utf8'));
  if(result.status==='CONFLICT_HOLD'||result.kind==='REJECTED'){
   session.conflicts.push(result.status);markGap(session,'NETWORK_PARSE_CONFLICT');return;
  }
  let changed=false;
  for(const m of result.candidates){
   const old=session.candidates.get(m.key);
   if(old&&old.final&&m.final&&old.text!==m.text){
    session.conflicts.push('FINAL_MESSAGE_TEXT_CONFLICT');markGap(session,'FINAL_MESSAGE_CONFLICT');continue;
   }
   if(!old || m.final&&!old.final || (!old.final&&m.text.length>old.text.length)){
    session.candidates.set(m.key,m);changed=true;
   }
  }
  if(changed){session.conflictProbe=null;session.dirtyAt=Date.now();session.lastTraffic=Date.now();}
 }catch(e){
  markGap(session,'NETWORK_BODY_READ_FAILED');
  session.lastError=String(e.message).slice(0,80);
 }
}
async function captureDOM(session){
 const first=readVisible((await session.cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
 await sleep(400);
 const second=readVisible((await session.cdp.send('DOM.getDocument',{depth:-1,pierce:false},11000)).root);
 return {first,second,isStable:stable(first,second)};
}
async function evaluate(session){
 if(session.rechecking||!session.cdp.open||!session.conversationId)return;
 session.rechecking=true;
 try{
  await Promise.allSettled([...session.pendingBodies]);
  const loaded=await store.get(session.conversationId,{url:session.tabUrl,title:session.title});
  if(loaded.gap.blocked){session.conflictProbe=null;await statusOnce(loaded.gap.status,session.conversationId);return}
  const shots=await captureDOM(session);
  if(!shots.isStable){session.conflictProbe=null;await statusOnce('P1_DOM_UNSTABLE_HOLD',session.conversationId);return}
  const dom=shots.second,old=loaded.record.messages;
  const tail=old.at(-1)?.key;
  const anchor=tail?dom.findIndex(x=>x.key===tail):-1;
  const suffix=tail?anchor>=0?dom.slice(anchor+1):[]:dom;
  if(tail&&anchor<0){
   session.conflictProbe=null;
   if(session.conflicts.length){await statusOnce('P2_NETWORK_CONFLICT_HOLD',session.conversationId,{conflicts:session.conflicts.length});return}
   const verified=dom.filter(m=>{
    const net=session.candidates.get(m.key);
    return m.final===true&&net?.final===true&&net.role===m.role&&net.text===m.text&&
      !old.some(item=>item.key===m.key);
   });
   if(verified.length&&write){
    const saved=await store.savePending(session.conversationId,{verified,reason:'HISTORICAL_TAIL_NOT_VISIBLE'});
    if(saved.added)await report('RAW_PENDING_VERIFIED',session.conversationId,{added:saved.added,
     pendingTotal:saved.total,nonAuthoritative:true,eligibleForL0:false});
   }
   await statusOnce('P1_TAIL_NOT_VISIBLE_HOLD',session.conversationId,{rawCount:old.length,visible:dom.length,
    networkVerifiedPending:verified.length});
   return;
  }
  if(suffix.length===0){
   session.conflictProbe=null;
   if(session.dirtyAt)session.dirtyAt=0;
   await statusOnce('OBSERVED_NO_CHANGE',session.conversationId,{rawCount:old.length,visible:dom.length});
   return;
  }
  if(session.conflicts.length){
   session.conflictProbe=null;
   await statusOnce('P2_NETWORK_CONFLICT_HOLD',session.conversationId,{conflicts:session.conflicts.length});
   return;
  }
  const network=suffix.map(row=>session.candidates.get(row.key)).filter(m=>m?.final);
  let decision,mode='P2',resolvedConflict=null;
  if(network.length===suffix.length){
   decision=validateCommit({
    state:loaded.record,network,observed:dom,archivedThrough:loaded.archivedThrough,
    allowBootstrap:old.length===0
   });
   if(!decision.ok){
    if(decision.status!=='DOM_NETWORK_CONFLICT_HOLD'||old.length===0){
     session.conflictProbe=null;
     await statusOnce(decision.status,session.conversationId,{network:network.length,visible:suffix.length});
     return;
    }
    const probe=evaluateConflictProbe({
     state:loaded.record,archivedThrough:loaded.archivedThrough,
     first:shots.first,second:dom,networkByKey:session.candidates,
     previous:session.conflictProbe,now:Date.now(),
     networkInternallyConflicted:session.conflicts.length>0
    });
    session.conflictProbe=probe.next;
    if(!probe.ok){
     await statusOnce(probe.status,session.conversationId,{network:network.length,visible:suffix.length,
      mismatched:probe.evidence?.mismatchedMessages??null});
     return;
    }
    decision=probe.decision;
    mode='P1_CONFLICT';
    resolvedConflict=probe.evidence;
   }else session.conflictProbe=null;
  }else{
   session.conflictProbe=null;
   if(old.length===0){
    await statusOnce('P2_BOOTSTRAP_INCOMPLETE_HOLD',session.conversationId,{network:network.length,visible:suffix.length});
    return;
   }
   decision=evaluateRecovery({state:loaded.record,archivedThrough:loaded.archivedThrough,first:shots.first,second:dom});
   mode='P1';
   if(!decision.ok){await statusOnce(decision.status,session.conversationId,{network:network.length,visible:suffix.length});return}
  }
  if(decision.status==='NO_CHANGE'||decision.status==='P1_NO_CHANGE')return;
  if(!write){
   await statusOnce('DRYRUN_APPEND_ELIGIBLE',session.conversationId,{source:mode,added:decision.proposed.length-old.length});
   return;
  }
  const saved=await store.save(session.conversationId,{loaded,decision,mode});
  commits++;
  if(resolvedConflict){
   await report('RAW_CONFLICT_RECONCILED',session.conversationId,{method:'P1_TWO_ROUND_DOM',
    disagreements:resolvedConflict.mismatchedMessages,settleMs:resolvedConflict.elapsedMs,
    added:saved.added,rawSha256:saved.sha256,cloudCompleteness:'UNKNOWN'});
  }
  await report('RAW_APPEND_VERIFIED',session.conversationId,{source:mode,added:saved.added,total:saved.count,sha256:saved.sha256,
   historicalCompleteness:'UNKNOWN'});
  for(const m of suffix)session.candidates.delete(m.key);
  session.dirtyAt=0;
  session.conflictProbe=null;
  seenStatuses.delete(session.conversationId);
 }catch(e){
  holds++;
  await statusOnce('RAW_AUTO_HOLD',session.conversationId,{reason:String(e.message).slice(0,90)});
 }finally{
  session.lastEval=Date.now();session.rechecking=false;
 }
}
async function scan(){
 scanCount++;
 let tabs;
 try{
  const signal=AbortSignal.timeout(4500);
  const res=await fetch(CDP+'/json/list',{signal});
  if(!res.ok)throw Error('CDP_HTTP_'+res.status);
  tabs=await res.json();
 }catch(e){await statusOnce('CDP_TABS_UNAVAILABLE','',{reason:String(e.message).slice(0,50)});return}
 const eligibleTabs=tabs.filter(eligible);
 const duplicate=new Set();
 const ids=new Map();
 for(const t of eligibleTabs){
  const cid=trialId||chatId(t.url);
  if(!cid)continue;
  ids.set(cid,(ids.get(cid)||0)+1);
 }
 for(const [cid,count] of ids){if(count>1)duplicate.add(cid)}
 for(const [key,s] of sessions){
  const tab=eligibleTabs.find(x=>x.id===key&&x.webSocketDebuggerUrl);
  if(!tab||!s.cdp.open){
   markGap(s,'CDP_DISCONNECTED_OR_TAB_CLOSED');
   try{await s.cdp.close()}catch{}
   sessions.delete(key);
   await report('P2_DETACHED',s.conversationId,{reason:s.gapReason||'UNKNOWN',coverage:'INTERRUPTED'});
  }else{
   s.tabUrl=tab.url;s.title=tab.title;
   const cid=trialId||chatId(tab.url);
   if(cid)s.conversationId=cid;
  }
 }
 for(const t of eligibleTabs){
  if(sessions.has(t.id))continue;
  const cid=trialId||chatId(t.url);
  if(cid&&duplicate.has(cid))continue;
  try{await attach(t)}catch(e){await statusOnce('P2_ATTACH_HOLD',cid||'',{reason:String(e.message).slice(0,75)})}
 }
 for(const s of sessions.values()){
  if(!s.conversationId||duplicate.has(s.conversationId)){
   if(duplicate.has(s.conversationId))await statusOnce('DUPLICATE_TABS_HOLD',s.conversationId);
   continue;
  }
  const since=Date.now()-s.lastEval;
  if(s.rechecking||s.dirtyAt&&Date.now()-s.dirtyAt<1000)continue;
  if(s.dirtyAt||since>=12000)await evaluate(s);
 }
}
async function main(){
 await report('AUTO_RAW_START','',{writeEnabled:write,
  target:target||trialId|| (all?'ALL_EXPLICIT':'OBSERVE_OPEN_SESSIONS'),L0:'DISABLED',Journal:'DISABLED',
  durationSeconds:durationSec||null});
 process.on('SIGINT',()=>{stopping=true});
 process.on('SIGTERM',()=>{stopping=true});
 try{
  do{
   await scan();
   if(stopping)break;
   if(once&&Date.now()-began>=durationSec*1000)break;
   await sleep(pollMs);
  }while(!stopping);
 }finally{
  for(const s of sessions.values())try{await s.cdp.close()}catch{}
  await report('AUTO_RAW_STOP','',{scanCount,commits,holds,mode:write?'WRITE':'OBSERVE'});
 }
}
main().catch(e=>{console.error('RAW_AUTO_FATAL '+e.message);process.exitCode=1});

