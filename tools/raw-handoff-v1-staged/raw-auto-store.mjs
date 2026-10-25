import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {sha256} from './raw-auto-core.mjs';
import {classifyCaptureGaps} from './raw-auto-gap-policy.mjs';
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export const digest=sha256;
export function checkId(id){if(!UUID.test(id??''))throw Error('INVALID_CONVERSATION_ID');return id}
export function createStore(root,{write=false}={}){
 const sessions=path.resolve(root,'sessions');
 const checkpoints=path.resolve(root,'l0-checkpoints');
 async function get(id,{url,title}={}){
  checkId(id);
  const filename=path.join(sessions,id+'.json');
  let bytes=null;
  try{bytes=await fs.readFile(filename,'utf8')}
  catch(e){if(e.code!=='ENOENT')throw Error('RAW_READ_FAILED_'+id)}
  const record=bytes===null?{
   format:'chat-raw-lite-v1',conversationId:id,url:url||'',title:title||'ChatGPT',
   captureScope:'P2 network verified with P1 DOM; full historical completeness UNKNOWN',
   firstCapturedAt:new Date().toISOString(),lastCapturedAt:null,messages:[],captureGaps:[]}
   :JSON.parse(bytes);
  if(record.conversationId!==id||!Array.isArray(record.messages))throw Error('RAW_RECORD_INVALID_'+id);
  const gap=classifyCaptureGaps(record);
  let checkpoint=0;
  try{
   const cp=JSON.parse(await fs.readFile(path.join(checkpoints,id+'.json'),'utf8'));
   if(cp.conversationId!==id||!Number.isInteger(cp.nextIndex)||cp.nextIndex<0||cp.nextIndex>record.messages.length)
    throw Error('INVALID_L0_CHECKPOINT');
   checkpoint=cp.nextIndex;
  }catch(e){if(e.code!=='ENOENT')throw e}
  return {record,baselineBytes:bytes,archivedThrough:checkpoint,gap,filename};
 }
 async function save(id,{loaded,decision,mode='P2'}){
  checkId(id);
  if(!write)throw Error('WRITE_NOT_ENABLED');
  if(!loaded?.record||!decision?.ok||!Array.isArray(decision.proposed))throw Error('CANDIDATE_NOT_APPROVED');
  if(!['SCOPED_COMMIT_READY','P1_SCOPED_APPEND_READY'].includes(decision.status))throw Error('DECISION_NOT_COMMITTABLE');
  if(loaded.gap?.blocked)throw Error('CAPTURE_GAP_HOLD');
  const original=loaded.record.messages,proposed=decision.proposed;
  if(proposed.length<=original.length)throw Error('NO_NEW_MESSAGES_TO_COMMIT');
  if(original.some((m,i)=>proposed[i]?.key!==m.key||proposed[i]?.role!==m.role||proposed[i]?.text!==m.text))throw Error('PREFIX_REWRITE_HOLD');
  const keys=new Set();
  for(const m of proposed){
   if(typeof m.key!=='string'||!['assistant','user'].includes(m.role)||typeof m.text!=='string'||!m.text)throw Error('INVALID_PROPOSED_MESSAGE');
   if(keys.has(m.key))throw Error('DUPLICATE_MESSAGE_KEY');keys.add(m.key);
  }
  await fs.mkdir(sessions,{recursive:true});
  const final=path.join(sessions,id+'.json'), lock=final+'.auto.lock';
  const handle=await fs.open(lock,'wx');
  let temp=null;
  try{
   const disk=await fs.readFile(final,'utf8').catch(e=>e.code==='ENOENT'?null:Promise.reject(e));
   if(disk!==loaded.baselineBytes)throw Error('CONCURRENT_RAW_UPDATE_HOLD');
   const cpPath=path.join(checkpoints,id+'.json');
   const cp=await fs.readFile(cpPath,'utf8').catch(e=>e.code==='ENOENT'?null:Promise.reject(e));
   if(cp!==null){
    const val=JSON.parse(cp);
    if(val.conversationId!==id||val.nextIndex!==loaded.archivedThrough)throw Error('CHECKPOINT_CHANGED_HOLD');
   }else if(loaded.archivedThrough!==0)throw Error('CHECKPOINT_DISAPPEARED_HOLD');
   const next={...loaded.record,
    messages:proposed,lastCapturedAt:new Date().toISOString(),
    captureScope:mode==='P1_TWO_ROUND_DOM_BOOTSTRAP'?'P1 two-round top-to-bottom DOM bootstrap of completed prior turns; current live turn and cloud-wide completeness UNKNOWN':'P2 network + P1 DOM scoped capture; history and other-device coverage UNKNOWN',
    autoRawEvidence:{writer:'RAW_ONLY_SINGLE_WRITER',mode,sourceCompleteness:'UNKNOWN',l0Triggered:false}
   };
   const payload=JSON.stringify(next,null,2)+'\n';
   temp=final+'.tmp.auto.'+crypto.randomBytes(6).toString('hex');
   const fd=await fs.open(temp,'wx');
   try{await fd.writeFile(payload,'utf8');await fd.sync()}finally{await fd.close()}
   const again=await fs.readFile(final,'utf8').catch(e=>e.code==='ENOENT'?null:Promise.reject(e));
   if(again!==loaded.baselineBytes)throw Error('CONCURRENT_RAW_UPDATE_HOLD');
   await fs.rename(temp,final);temp=null;
   const confirmed=await fs.readFile(final,'utf8');
   if(confirmed!==payload)throw Error('RAW_READBACK_FAILED');
   // JSON is authoritative. Markdown is a derived view and can be rebuilt after failure.
   const md=renderMarkdown(next);
   const mdFile=path.join(sessions,id+'.md'),mdTemp=mdFile+'.tmp.auto.'+crypto.randomBytes(5).toString('hex');
   try{await fs.writeFile(mdTemp,md,{flag:'wx'});await fs.rename(mdTemp,mdFile)}
   finally{await fs.unlink(mdTemp).catch(()=>{})}
   return {count:proposed.length,added:proposed.length-original.length,sha256:sha256(payload),mode,readbackVerified:true};
  }finally{
   if(temp)await fs.unlink(temp).catch(()=>{});
   await handle.close();await fs.unlink(lock).catch(()=>{});
  }
 }
 async function savePending(id,{verified,reason='UNANCHORED_TAIL',source='P2_NETWORK_PLUS_DOM'}){
  checkId(id);
  if(!write)throw Error('WRITE_NOT_ENABLED');
  if(!Array.isArray(verified)||!verified.length)throw Error('PENDING_EMPTY_OR_INVALID');
  if(!['P1_TWO_STABLE_DOM_SNAPSHOTS','P2_NETWORK_PLUS_DOM'].includes(source))throw Error('UNAPPROVED_PENDING_PROVENANCE');
  const seen=new Set();
  for(const m of verified){
   if(!m.key||!['user','assistant'].includes(m.role)||!m.text||m.final!==true||seen.has(m.key))
    throw Error('PENDING_MESSAGE_INVALID');
   seen.add(m.key);
  }
  const pendingDir=path.resolve(root,'raw-auto-pending');
  await fs.mkdir(pendingDir,{recursive:true});
  const file=path.join(pendingDir,id+'.json'),lock=file+'.lock';
  const lk=await fs.open(lock,'wx');
  let tmp=null;
  try{
   let current;
   try{current=JSON.parse(await fs.readFile(file,'utf8'))}
   catch(e){if(e.code!=='ENOENT')throw e;current={format:'raw-p2-pending-evidence-v1',
    conversationId:id,authoritative:false,eligibleForL0:false,historyCompleteness:'UNKNOWN',messages:[]}}
   if(current.conversationId!==id||current.authoritative!==false||!Array.isArray(current.messages))throw Error('PENDING_STATE_INVALID');
   const existing=new Map(current.messages.map(m=>[m.key,m]));
   if(existing.size!==current.messages.length)throw Error('PENDING_DUPLICATE_KEY');
   const fresh=[];
   for(const m of verified){
    const old=existing.get(m.key);
    if(old){
     if(old.role!==m.role||old.text!==m.text)throw Error('PENDING_EXISTING_CONTENT_CONFLICT');
    }else fresh.push({key:m.key,role:m.role,text:m.text,
     firstSeenAt:new Date().toISOString(),observationStatus:source==='P1_TWO_STABLE_DOM_SNAPSHOTS'?'PENDING_UNANCHORED_STABLE_DOM':'PENDING_UNANCHORED_NETWORK_AND_DOM_VERIFIED',sourceEvidence:{kind:source,authoritative:false},
     textSha256:sha256(m.text)});
   }
   if(!fresh.length)return {added:0,total:current.messages.length,status:'PENDING_ALREADY_RECORDED'};
   const next={...current,reason,updatedAt:new Date().toISOString(),messages:[...current.messages,...fresh]};
   const payload=JSON.stringify(next,null,2)+String.fromCharCode(10);
   tmp=file+'.tmp.'+crypto.randomBytes(6).toString('hex');
   const fh=await fs.open(tmp,'wx');
   try{await fh.writeFile(payload,'utf8');await fh.sync()}finally{await fh.close()}
   await fs.rename(tmp,file);tmp=null;
   const check=await fs.readFile(file,'utf8');
   if(check!==payload)throw Error('PENDING_READBACK_FAILED');
   return {added:fresh.length,total:next.messages.length,status:'PENDING_SAVED_NON_AUTHORITATIVE',sha256:sha256(payload)};
  }finally{
   if(tmp)await fs.unlink(tmp).catch(()=>{});
   await lk.close();await fs.unlink(lock).catch(()=>{});
  }
 }
 return {get,save,savePending};
}
export function renderMarkdown(state){
 const lines=['# '+(state.title||'ChatGPT')+' — 轻量 RAW','',
  '- 会话：'+state.url,
  '- 来源：自动 P2 网络采集及 P1 DOM 核验（仅可验证的局部消息）',
  '- 限制：不保证其他设备、未开启的窗口或完整历史覆盖；时间是首次观察时间。',''];
 for(const [index,m] of state.messages.entries()){
  lines.push('## '+(index+1)+' · '+(m.role==='user'?'USER':'ASSISTANT'));
  lines.push('消息标识：'+m.key+'；首次观察：'+(m.firstSeenAt||'UNKNOWN'));
  lines.push('',m.text,'');
 }
 return lines.join('\n')+'\n';
}


