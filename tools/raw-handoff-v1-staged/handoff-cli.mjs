import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {readVisible,stable} from './raw-auto-dom.mjs';
import {createStore,checkId} from './raw-auto-store.mjs';
import {sha256} from './raw-auto-core.mjs';
import {assertRegistry,parseChatUrl,latestTrigger,proposeHandoff,verifyNewRawAndActivate} from './handoff-core.mjs';

const root=path.dirname(fileURLToPath(import.meta.url));
const registryPath=path.join(root,'handoff-registry.json');
const args=process.argv.slice(2),action=args[0]||'inspect';
const option=name=>args.find(x=>x.startsWith('--'+name+'='))?.slice(name.length+3)||null;
if(!['inspect','prepare','activate','status'].includes(action))throw Error('HANDOFF_UNSUPPORTED_COMMAND');
const givenNew=option('new');
if(givenNew)checkId(givenNew);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function tabsRead(){
 const reply=await fetch('http://127.0.0.1:9333/json/list',{signal:AbortSignal.timeout(5500)});
 if(!reply.ok)throw Error('HANDOFF_CDP_UNAVAILABLE_'+reply.status);
 const rows=await reply.json();
 if(!Array.isArray(rows))throw Error('HANDOFF_TABS_MALFORMED');
 return rows;
}
async function withCDP(tab,callback){
 const ws=new WebSocket(tab.webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(Error('CDP_OPEN_TIMEOUT')),7000);
  ws.addEventListener('open',()=>{clearTimeout(timer);resolve()},{once:true});
  ws.addEventListener('error',()=>{clearTimeout(timer);reject(Error('CDP_CONNECT_FAILED'))},{once:true});
 });
 let seq=0;const pending=new Map();
 const listener=e=>{
  let m;try{m=JSON.parse(e.data)}catch{return}
  const p=pending.get(m.id);
  if(!p)return;pending.delete(m.id);clearTimeout(p.timer);
  m.error?p.reject(Error('CDP_REQUEST_FAILED_'+m.error.code)):p.resolve(m.result);
 };
 ws.addEventListener('message',listener);
 const cdp={send(method,params={},time=12000){
  return new Promise((resolve,reject)=>{
   const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP_TIMEOUT_'+method))},time);
   pending.set(id,{resolve,reject,timer});
   ws.send(JSON.stringify({id,method,params}));
  });
 }};
 try{return await callback(cdp)}
 finally{ws.close()}
}
async function observe(tab){
 return await withCDP(tab,async cdp=>{
  const read=async()=>readVisible((await cdp.send('DOM.getDocument',{depth:-1,pierce:false})).root);
  const first=await read();await sleep(450);const second=await read();
  if(!stable(first,second))return {ok:false,status:'HANDOFF_DOM_UNSTABLE_HOLD'};
  const expression="(async()=>{try{const r=await fetch('/backend-api/conversation/"+parseChatUrl(tab.url).conversationId+"/stream_status',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return 'HTTP_'+r.status;const j=await r.json();return typeof j.status==='string'?j.status:'UNKNOWN'}catch{return 'UNAVAILABLE'}})()";
  let stream='UNKNOWN';
  try{stream=(await cdp.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},11000)).result?.value||'UNKNOWN'}
  catch{}
  return {ok:true,status:'HANDOFF_DOM_STABLE',messages:second,
   latestUser:latestTrigger(second),streamStatus:stream,
   fullDomMessages:second.length,observedFinal:second.filter(m=>m.final).length};
 });
}
async function readRegistry(){
 const bytes=await fs.readFile(registryPath,'utf8');
 return {bytes,registry:assertRegistry(JSON.parse(bytes)),hash:sha256(bytes)};
}
async function storeRegistry({before,next,event,details={}}){
 const lock=registryPath+'.handoff.lock';
 const owner=await fs.open(lock,'wx');
 let temp=null;
 try{
  if((await fs.readFile(registryPath,'utf8'))!==before)throw Error('HANDOFF_REGISTRY_CAS_MISMATCH');
  assertRegistry(next);
  const payload=JSON.stringify(next,null,2)+'\n';
  temp=registryPath+'.tmp.'+crypto.randomBytes(6).toString('hex');
  const fd=await fs.open(temp,'wx');
  try{await fd.writeFile(payload,'utf8');await fd.sync()}finally{await fd.close()}
  if((await fs.readFile(registryPath,'utf8'))!==before)throw Error('HANDOFF_REGISTRY_CONCURRENT_MODIFICATION');
  await fs.rename(temp,registryPath);temp=null;
  const check=await fs.readFile(registryPath,'utf8');
  if(check!==payload)throw Error('HANDOFF_REGISTRY_READBACK_FAILED');
  const receipt={format:'RAW-HANDOFF-V1',event,at:new Date().toISOString(),
   revision:next.revision,registrySha256:sha256(payload),active:next.activeConversationId,
   targets:next.targets.map(x=>({id:x.id,status:x.status})),authority:'LOCAL_RUNTIME_ONLY',
   independentAcceptance:'NOT_CLAIMED',...details};
  const folder=path.join(root,'evidence');
  await fs.mkdir(folder,{recursive:true});
  const rec=path.join(folder,'handoff-'+String(next.revision).padStart(3,'0')+'-'+event+'.json');
  await fs.writeFile(rec,JSON.stringify(receipt,null,2)+'\n',{flag:'wx'});
  return {receipt:rec,...receipt};
 }finally{
  if(temp)await fs.unlink(temp).catch(()=>{});
  await owner.close();await fs.unlink(lock).catch(()=>{});
 }
}
async function choose(tabs,registry){
 if(givenNew){
  const matches=tabs.filter(t=>parseChatUrl(t.url)?.conversationId===givenNew);
  if(matches.length!==1)throw Error('NEW_CONVERSATION_NOT_UNIQUE_OR_NOT_OPEN');
  return matches[0];
 }
 const candidates=[];
 for(const tab of tabs){
  const p=parseChatUrl(tab.url);
  if(!p||p.projectKey!==registry.projectKey||registry.targets.some(x=>x.id===p.conversationId))continue;
  const result=await observe(tab);
  if(result.ok&&result.latestUser)candidates.push(tab);
 }
 if(candidates.length!==1)throw Error(candidates.length?'MULTIPLE_HANDOFF_TRIGGER_CANDIDATES_HOLD':'HANDOFF_TRIGGER_NOT_VISIBLE_IN_PROJECT_HOLD');
 return candidates[0];
}
async function main(){
 const {bytes,registry}=await readRegistry();
 if(action==='status'){
  const c=registry.currentHandoff;
  console.log(JSON.stringify({status:'HANDOFF_REGISTRY_STATUS',revision:registry.revision,
   projectKey:registry.projectKey,active:registry.activeConversationId,
   targets:registry.targets,currentHandoff:c,authority:'LOCAL_RUNTIME_NOT_CURRENT'},null,2));
  return;
 }
 const tabs=await tabsRead();
 let newId=givenNew||null;
 if(action!=='activate'){
  const tab=await choose(tabs,registry);
  const parsed=parseChatUrl(tab.url);
  if(!parsed||parsed.projectKey!==registry.projectKey)throw Error('CROSS_PROJECT_OR_UNKNOWN_TAB');
  newId=parsed.conversationId;
  const observation=await observe(tab);
  if(!observation.ok){console.log(JSON.stringify(observation));return}
  const decision=proposeHandoff({registry,tabs,selectedNewId:newId,messages:observation.messages,now:new Date().toISOString()});
  const result={...decision};
  delete result.next;
  result.newLastTriggerKey=observation.latestUser?.key||null;
  result.streamStatus=observation.streamStatus;
  result.observedMessageCount=observation.fullDomMessages;
  result.currentGenerationIsStreaming=observation.streamStatus!=='COMPLETE';
  if(action==='inspect'||!decision.ok){
   console.log(JSON.stringify({...result,write:false},null,2));return
  }
  const saved=await storeRegistry({before:bytes,next:decision.next,event:'HANDOFF_PREPARED',
   details:{from:decision.from,to:decision.to,triggerKey:decision.triggerKey,
    newStreamAtPrepare:observation.streamStatus}});
  console.log(JSON.stringify({status:'HANDOFF_PREPARED_PERSISTED',from:decision.from,
   to:decision.to,triggerKey:decision.triggerKey,revision:saved.revision,
   activeUnchanged:true,oldRetained:true,nextAction:'BOOTSTRAP_AND_VERIFY_NEW_RAW_AFTER_FINALITY',
   receipt:saved.receipt},null,2));
  return;
 }
 // Activation always reads and compares the actual on-disk formal RAW.
 const h=registry.currentHandoff;
 if(!h)throw Error('HANDOFF_NOT_PREPARED');
 if(newId && h.to!==newId)throw Error('HANDOFF_NEW_ID_MISMATCH');
 const tab=tabs.find(t=>parseChatUrl(t.url)?.conversationId===h.to);
 if(!tab)throw Error('NEW_TAB_NOT_OPEN_FOR_FINALITY_CHECK');
 const observed=await observe(tab);
 if(!observed.ok){console.log(JSON.stringify(observed));return}
 if(observed.streamStatus!=='COMPLETE'){
  console.log(JSON.stringify({status:'NEW_STREAM_INCOMPLETE_HOLD',stream:observed.streamStatus}));return;
 }
 const loaded=await createStore(root,{write:false}).get(h.to,{});
 if(loaded.gap.blocked){
  console.log(JSON.stringify({status:loaded.gap.status,scope:'NEW_RAW'}));return
 }
 const decision=verifyNewRawAndActivate({registry,raw:loaded.record,now:new Date().toISOString()});
 if(!decision.ok){console.log(JSON.stringify(decision));return}
 const replyKey=decision.verifiedPair[1];
 const reply=loaded.record.messages.find(m=>m.key===replyKey);
 const seenReply=observed.messages.find(m=>m.key===replyKey);
 if(!seenReply?.final||seenReply.text!==reply.text){
  console.log(JSON.stringify({status:'NEW_REPLY_STABLE_DOM_MISMATCH_OR_NOT_VISIBLE_HOLD'}));return
 }
 const stat=await fs.stat(loaded.filename);
 if(Date.now()-stat.mtimeMs<30000){
  console.log(JSON.stringify({status:'NEW_RAW_SETTLE_30_SECONDS_HOLD'}));return
 }
 const saved=await storeRegistry({before:bytes,next:decision.next,event:'NEW_RAW_VERIFIED',
  details:{from:decision.retainedOld,to:decision.active,verifiedKeys:decision.verifiedPair,
   rawSha256:sha256(loaded.baselineBytes||''),rawCount:loaded.record.messages.length,
   oldSealed:false,cloudCompleteness:'UNKNOWN'}});
 console.log(JSON.stringify({status:'NEW_ACTIVE_OLD_RETAINED',active:decision.active,
  old:decision.retainedOld,receipt:saved.receipt,revision:saved.revision,
  oldSealed:false,independentAcceptance:'NOT_CLAIMED'},null,2));
}
main().catch(e=>{console.error('HANDOFF_HOLD '+String(e.message));process.exitCode=2});
