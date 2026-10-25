import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {readVisible,stable} from './raw-auto-dom.mjs';
import {extractMessages,validateCommit,sha256} from './raw-auto-core.mjs';
import {evaluateRecovery} from './raw-auto-p1.mjs';
import {evaluateConflictProbe} from './raw-auto-conflict.mjs';
import {p1TemporalGate} from './raw-auto-finality.mjs';
import {attemptIdleRecovery} from './raw-auto-automatic-recovery.mjs';
import {createStore,checkId} from './raw-auto-store.mjs';
import {createTargetLoader,assertRegistryPath} from './handoff-target-loader.mjs';
import {parseChatUrl} from './handoff-core.mjs';
import {attemptHandoffBootstrap} from './handoff-bootstrap-service.mjs';

const installedRoot=path.dirname(fileURLToPath(import.meta.url));
const args=process.argv.slice(2);
const option=(name)=>args.find(x=>x.startsWith('--'+name+'='))?.slice(name.length+3);
const has=name=>args.includes('--'+name);
const trialRoot=option('trial-root');
if(trialRoot&&!path.resolve(trialRoot).includes('RAW-AUTO-CANDIDATE-20261024'))throw Error('TRIAL_ROOT_NOT_APPROVED');
const root=trialRoot?path.resolve(trialRoot):installedRoot;
const write=has('write');
const recoverIdle=has('recover-idle');
if(recoverIdle&&!write)throw Error('IDLE_RECOVERY_REQUIRES_EXPLICIT_WRITE');
const once=has('once');
const target=option('target')||null;
const targetsRaw=option('targets')||null;
const registryOption=option('handoff-registry')||null;
const handoffRegistry=registryOption?createTargetLoader(assertRegistryPath({entryDir:installedRoot,provided:registryOption})):null;
const targets=targetsRaw?targetsRaw.split(','):[];
if(targetsRaw&&(targets.length!==2||new Set(targets).size!==2))
 throw Error('EXACTLY_TWO_UNIQUE_TARGETS_REQUIRED');
for(const id of targets)checkId(id);
const trialId=option('trial-temp-id')||null;
const all=has('all-authorized');
if(targets.length&&(target||trialId||all))throw Error('TARGET_SET_MUTUALLY_EXCLUSIVE');
if(handoffRegistry&&(target||targets.length||trialId||all))
 throw Error('HANDOFF_REGISTRY_WITH_FIXED_TARGETS_FORBIDDEN');
if(target)checkId(target);
if(trialId)checkId(trialId);
if(trialId&&!trialRoot)throw Error('TEMP_CANARY_PRODUCTION_FORBIDDEN');
if(trialId&&target)throw Error('AMBIGUOUS_TRIAL_AND_TARGET');
if(write&&!target&&!trialId&&!all&&!targets.length&&!handoffRegistry)throw Error('EXPLICIT_TARGET_REQUIRED_FOR_WRITE');
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
let stopping=false,scanCount=0,commits=0,holds=0,lastHeartbeatAt=0;
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
   if(targets.length)return targets.includes(id);
   if(handoffRegistry){
    const parsed=parseChatUrl(tab.url);
    const current=handoffRegistry.current;
    return Boolean(parsed&&current&&parsed.projectKey===current.projectKey&&handoffRegistry.ids.has(parsed.conversationId));
   }
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
 const now=new Date().toISOString();
 if(!session.gapSince)session.gapSince=now;
 session.gapReason=reason;
 session.lastGapAt=now;
 session.gapCount=(session.gapCount||0)+1;
}
async function attach(tab){
 const conn=new Socket(tab.webSocketDebuggerUrl);
 await conn.connect();
 const session={id:tab.id,tabUrl:tab.url,conversationId:trialId||chatId(tab.url),
  title:tab.title,cdp:conn,candidates:new Map(),conflicts:[],conflictProbe:null,
  requests:new Set(),pendingBodies:new Set(),pendingReads:0,lastEval:0,
  dirtyAt:0,lastTraffic:0,lastError:'',rechecking:false,gapSince:null,
  gapReason:'PRE_ATTACH_HISTORY_UNVERIFIED',connectedAt:new Date().toISOString(),lastGapAt:null,gapCount:0,lastP1Candidate:null,uncommittedSince:0,lastHealth:'INITIALIZING',
   tailMissingSince:0,lastRecoveryAt:0,recovering:false,
   handoffBootstrapping:false,lastHandoffAttemptAt:0};
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
async function readStreamCompletion(session){
 const id=session.conversationId;
 // Authenticated read-only stream status. Unknown results always fail closed.
 const js="(async()=>{try{const r=await fetch('/backend-api/conversation/"+id+"/stream_status',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return 'HTTP_'+r.status;const d=await r.json();return typeof d?.status==='string'?d.status:'UNKNOWN';}catch(e){return 'UNAVAILABLE'}})()";
 try{
  const out=await session.cdp.send('Runtime.evaluate',{expression:js,returnByValue:true,awaitPromise:true},10000);
  return out.result?.value==='COMPLETE'?'COMPLETE':String(out.result?.value||'UNKNOWN').slice(0,30);
 }catch{return 'UNAVAILABLE'}
}
async function heartbeat(){
 if(Date.now()-lastHeartbeatAt<60000)return;
 lastHeartbeatAt=Date.now();
 const details=[...sessions.values()].map(s=>({
  id:s.conversationId,health:s.lastHealth,secondsSinceLastEvaluation:s.lastEval?Math.round((Date.now()-s.lastEval)/1000):null,
  secondsSinceFormalCommit:s.lastOfficialCommitAt?Math.round((Date.now()-s.lastOfficialCommitAt)/1000):null,
  secondsNewContentUncommitted:s.uncommittedSince?Math.round((Date.now()-s.uncommittedSince)/1000):0,
  secondsTailNotVisible:s.tailMissingSince?Math.round((Date.now()-s.tailMissingSince)/1000):0,
  recovering:s.recovering,handoffBootstrapping:!!s.handoffBootstrapping,
  lastGapReason:s.gapReason,gapCount:s.gapCount,p2Connected:s.cdp.open
 }));
 await report('AUTO_RAW_HEARTBEAT','',{scanCount,commits,holds,targets:details});
 for(const s of sessions.values()){
  if(s.uncommittedSince&&Date.now()-s.uncommittedSince>120000)
   await statusOnce('RAW_LAG_ALERT',s.conversationId,{reason:s.lastHealth,thresholdSeconds:120});
  if(s.tailMissingSince&&Date.now()-s.tailMissingSince>120000)
   await statusOnce('RAW_CONTINUITY_ALERT',s.conversationId,
    {reason:'TAIL_NOT_VISIBLE',automaticRecoveryEnabled:recoverIdle,historyCompleteness:'UNKNOWN'});
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
   const oldKeys=new Set(old.map(m=>m.key));
   const observedNew=dom.filter(m=>m.final===true&&!oldKeys.has(m.key));
   session.lastHealth='OBSERVED';
   if(observedNew.length){if(!session.uncommittedSince)session.uncommittedSince=Date.now()}
   else session.uncommittedSince=0;
  const tail=old.at(-1)?.key;
  const anchor=tail?dom.findIndex(x=>x.key===tail):-1;
  const suffix=tail?anchor>=0?dom.slice(anchor+1):[]:dom;
   if(!tail||anchor>=0)session.tailMissingSince=0;
  if(tail&&anchor<0){
    if(!session.tailMissingSince)session.tailMissingSince=Date.now();
   session.conflictProbe=null;
   if(session.conflicts.length){await statusOnce('P2_NETWORK_CONFLICT_HOLD',session.conversationId,{conflicts:session.conflicts.length});return}
   const stream=observedNew.some(m=>m.role==='assistant')?await readStreamCompletion(session):'UNKNOWN';
    const verified=observedNew.filter(m=>m.role==='user'||stream==='COMPLETE');
    if(verified.length&&write){
     const saved=await store.savePending(session.conversationId,{
      verified,source:'P1_TWO_STABLE_DOM_SNAPSHOTS',reason:'HISTORICAL_TAIL_NOT_VISIBLE'});
     if(saved.added)await report('RAW_PENDING_OBSERVED',session.conversationId,{added:saved.added,
      pendingTotal:saved.total,nonAuthoritative:true,eligibleForL0:false});
    }
    session.lastHealth='TAIL_NOT_VISIBLE_HOLD';
    await statusOnce('P1_TAIL_NOT_VISIBLE_HOLD',session.conversationId,{
     rawCount:old.length,visible:dom.length,observedCompleteUnanchored:verified.length,serverStream:stream});
    // Opt-in bounded idle recovery. Never scroll a focused tab or skip a missing segment.
    const eligibleAfterMs=120000,cooldownMs=600000;
    if(recoverIdle&&write&&!session.recovering&&session.cdp.open&&
     Date.now()-session.tailMissingSince>eligibleAfterMs&&
     Date.now()-session.lastRecoveryAt>cooldownMs&&!session.conflicts.length){
     session.recovering=true;
     session.lastRecoveryAt=Date.now();
     session.lastHealth='IDLE_RECOVERY_STARTED';
     void attemptIdleRecovery({session,store,report,readStreamCompletion,write:true})
      .then(async r=>{
       session.lastHealth=r.status;
       if(r.ok){
        commits++;session.uncommittedSince=0;session.tailMissingSince=0;
        session.candidates.clear();seenStatuses.delete(session.conversationId);
       }else await statusOnce(r.status,session.conversationId,
        {scopedRecovery:true,automatic:true,remainsUnverified:true});
      }).catch(async e=>{
       session.lastHealth='IDLE_RECOVERY_ERROR_HOLD';
       await statusOnce('IDLE_RECOVERY_ERROR_HOLD',session.conversationId,
        {reason:String(e.message).slice(0,105)});
      }).finally(()=>{session.recovering=false;});
    }
   return;
  }
  if(tail&&anchor>=0&&old.at(-1).text!==dom[anchor].text){
    session.lastHealth='TAIL_REVISION_HOLD';
    session.lastP1Candidate=null;
    const stream=observedNew.some(m=>m.role==='assistant')?await readStreamCompletion(session):'UNKNOWN';
    const safe=observedNew.filter(m=>m.role==='user'||stream==='COMPLETE');
    if(write&&safe.length){
     const saved=await store.savePending(session.conversationId,{verified:safe,
      source:'P1_TWO_STABLE_DOM_SNAPSHOTS',reason:'AUTHORITATIVE_TAIL_REVISION'});
     if(saved.added)await report('RAW_PENDING_OBSERVED',session.conversationId,{
      added:saved.added,pendingTotal:saved.total,nonAuthoritative:true,eligibleForL0:false});
    }
    await statusOnce('RAW_TAIL_REVISION_HOLD',session.conversationId,{
     oldChars:old.at(-1).text.length,nowChars:dom[anchor].text.length,
     textExtended:dom[anchor].text.startsWith(old.at(-1).text),archivedTail:(old.length-1)<loaded.archivedThrough});
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
   if(mode==='P1'||mode==='P1_CONFLICT'){
    const candidate=decision.proposed.slice(old.length);
    const gate=p1TemporalGate({tracker:session.lastP1Candidate,candidate});
    session.lastP1Candidate=gate.tracker;
    if(!gate.ok){
     session.lastHealth=gate.status;
     await statusOnce(gate.status,session.conversationId,{added:candidate.length,minimumMs:30000});
     return;
    }
    const server=await readStreamCompletion(session);
    if(server!=='COMPLETE'){
     session.lastHealth='SERVER_FINALITY_HOLD';
     await statusOnce('P1_SERVER_FINALITY_HOLD',session.conversationId,{serverStream:server});
     return;
    }
   }else session.lastP1Candidate=null;
  if(!write){
   await statusOnce('DRYRUN_APPEND_ELIGIBLE',session.conversationId,{source:mode,added:decision.proposed.length-old.length});
   return;
  }
  const saved=await store.save(session.conversationId,{loaded,decision,mode});
  commits++;
   session.lastP1Candidate=null;
   session.uncommittedSince=0;
   session.lastOfficialCommitAt=Date.now();
   session.lastHealth='COMMIT_VERIFIED';
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
   session.lastHealth='EVALUATION_HOLD';
  await statusOnce('RAW_AUTO_HOLD',session.conversationId,{reason:String(e.message).slice(0,90)});
 }finally{
  session.lastEval=Date.now();session.rechecking=false;
 }
}
async function scan(){
 scanCount++;
 if(handoffRegistry){
  try{
   const outcome=await handoffRegistry.refresh();
   if(outcome.changed)await report('HANDOFF_TARGET_REGISTRY_APPLIED','',{
    revision:outcome.revision,targets:outcome.ids,projectKey:outcome.projectKey,
    active:outcome.active,registrySha256:outcome.sha256});
  }catch(e){
   await statusOnce('HANDOFF_REGISTRY_INVALID_HOLD','',{reason:String(e.message).slice(0,110)});
   if(!handoffRegistry.current)return;
  }
 }
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
  const pendingHandoff=handoffRegistry?.current?.currentHandoff;
  if(s.handoffBootstrapping)continue;
  if(write&&pendingHandoff?.to===s.conversationId&&
   Date.now()-s.lastHandoffAttemptAt>25000&&!s.rechecking){
   s.lastHandoffAttemptAt=Date.now();
   s.handoffBootstrapping=true;
   s.lastHealth='HANDOFF_BOOTSTRAP_ATTEMPT';
   void attemptHandoffBootstrap({
    session:s,store,loader:handoffRegistry,readStreamCompletion,report,root
   }).then(async outcome=>{
    s.lastHealth=outcome.status;
    if(outcome.ok){
     commits++;s.uncommittedSince=0;
     s.lastOfficialCommitAt=Date.now();
     seenStatuses.delete(s.conversationId);
    }else await statusOnce(outcome.status,s.conversationId,{
     handoff:true,authoritative:false,eligibleForL0:false});
   }).catch(async e=>{
    s.lastHealth='HANDOFF_BOOTSTRAP_ERROR_HOLD';
    await statusOnce('HANDOFF_BOOTSTRAP_ERROR_HOLD',s.conversationId,{
     reason:String(e.message).slice(0,90)});
   }).finally(()=>{s.handoffBootstrapping=false;});
   continue;
  }
  const since=Date.now()-s.lastEval;
  if(s.rechecking||s.recovering||s.dirtyAt&&Date.now()-s.dirtyAt<1000)continue;
  if(s.dirtyAt||since>=12000)await evaluate(s);
 }
}
async function main(){
 if(handoffRegistry){
  const opened=await handoffRegistry.refresh();
  if(!opened.changed||opened.ids.length<1)throw Error('HANDOFF_REGISTRY_EMPTY_STARTUP');
 }
 await report('AUTO_RAW_START','',{writeEnabled:write,
  target:handoffRegistry?'HANDOFF_REGISTRY':target||trialId||(targets.length?'EXACT_TARGET_SET':(all?'ALL_EXPLICIT':'OBSERVE_OPEN_SESSIONS')),
   targets:handoffRegistry?[...handoffRegistry.ids]:targets.length?[...targets]:target?[target]:[],
    handoffRegistryRevision:handoffRegistry?.version??null,L0:'DISABLED',Journal:'DISABLED',
  durationSeconds:durationSec||null,idleRecoveryEnabled:recoverIdle});
 process.on('SIGINT',()=>{stopping=true});
 process.on('SIGTERM',()=>{stopping=true});
 try{
  do{
   await scan();
   await heartbeat();
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


