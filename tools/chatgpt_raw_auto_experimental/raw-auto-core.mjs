import crypto from 'node:crypto';
import {classifyCaptureGaps} from './raw-auto-gap-policy.mjs';

export const sha256=x=>crypto.createHash('sha256').update(x).digest('hex').toUpperCase();
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const COMPLETED=new Set(['finished_successfully','finished','complete','completed']);
const MAX_RESPONSE=3000000;

function parsePayload(raw){
  if(typeof raw!=='string'||Buffer.byteLength(raw)>MAX_RESPONSE)return {kind:'REJECTED',documents:[]};
  let data;
  try{data=JSON.parse(raw)}catch{}
  if(data!==undefined)return {kind:'JSON',documents:[data]};
  const documents=[];
  for(const line of raw.split(/\r?\n/)){
    const s=line.trim();
    if(!s.startsWith('data:'))continue;
    const frag=s.slice(5).trim();
    if(!frag||frag==='[DONE]')continue;
    try{documents.push(JSON.parse(frag))}catch{}
    if(documents.length>5000)return {kind:'REJECTED',documents:[]};
  }
  return {kind:documents.length?'SSE':'UNKNOWN',documents};
}
function textParts(message){
 const content=message.content;
 if(!content||!['text','multimodal_text'].includes(content.content_type)||!Array.isArray(content.parts))return null;
 if(content.parts.some(p=>typeof p!=='string'))return null;
 const text=content.parts.join('\n').trim();
 return text.length>0&&text.length<=250000?text:null;
}
function messageFromObject(obj){
 if(!obj||typeof obj!=='object'||Array.isArray(obj))return null;
 if(typeof obj.id!=='string'||!UUID.test(obj.id))return null;
 const role=obj.author?.role;
 if(!['assistant','user'].includes(role))return null;
 const text=textParts(obj);
 if(text===null)return null;
 const isFinished=COMPLETED.has(obj.status)&&(role==='user'||obj.end_turn===true);
 const safe={key:role+':'+obj.id,role,text,final:isFinished,finalityEvidence:{
  status:typeof obj.status==='string'?obj.status.slice(0,45):'MISSING',endTurn:obj.end_turn===true
 }};
 return safe;
}
export function extractMessages(raw){
 const parsed=parsePayload(raw);
 const found=[],visits=new Set();let objects=0;
 function walk(node,depth){
  if(depth>35||objects>20000||!node||typeof node!=='object'||visits.has(node))return;
  visits.add(node);objects++;
  const msg=messageFromObject(node);
  if(msg)found.push(msg);
  if(Array.isArray(node)){for(const x of node)walk(x,depth+1);return}
  for(const [k,v] of Object.entries(node)){
   if(k==='request'||k==='headers'||k==='metadata'&&(!msg)){continue}
   if(v&&typeof v==='object')walk(v,depth+1);
  }
 }
 for(const document of parsed.documents)walk(document,0);
 const map=new Map(),conflicts=[];
 for(const m of found){
  if(map.has(m.key)){
    const old=map.get(m.key);
    if(old.text!==m.text||old.role!==m.role)conflicts.push({keyHash:sha256(m.key),reason:'MULTIPLE_VERSIONS_IN_ONE_RESPONSE'});
    else if(m.final&&!old.final)map.set(m.key,m);
  }else map.set(m.key,m);
 }
 return {kind:parsed.kind,candidates:[...map.values()],conflicts,status:conflicts.length?'CONFLICT_HOLD':parsed.kind==='REJECTED'?'OVERSIZE_HOLD':'CANDIDATES_EXTRACTED'};
}
export function validateCommit({state,network,observed,archivedThrough=0,requiredTail=null,allowBootstrap=false}){
 const fail=(status,detail)=>({status,detail,ok:false,proposed:null});
 if(!Array.isArray(state?.messages)||!Array.isArray(network)||!Array.isArray(observed))return fail('INVALID_INPUT_HOLD');
 if(!Number.isInteger(archivedThrough)||archivedThrough<0||archivedThrough>state.messages.length)return fail('CHECKPOINT_INVALID_HOLD');
 const gap=classifyCaptureGaps(state);
 if(gap.blocked)return fail(gap.status);
 const ids=new Set(),unique=new Set();
 for(const m of [...state.messages,...network,...observed]){
  if(!m||typeof m.key!=='string'||typeof m.text!=='string'||!['assistant','user'].includes(m.role))return fail('BAD_MESSAGE_HOLD');
 }
 for(const m of state.messages){if(ids.has(m.key))return fail('DUPLICATE_HISTORY_HOLD');ids.add(m.key)}
 for(const m of network){if(unique.has(m.key))return fail('DUPLICATE_NETWORK_HOLD');unique.add(m.key)}
 const seen=new Set();
 for(const m of observed){if(seen.has(m.key))return fail('DUPLICATE_DOM_HOLD');seen.add(m.key)}
 const currentMap=new Map(state.messages.map((m,i)=>[m.key,{...m,index:i}]));
 const newMessages=network.filter(m=>!currentMap.has(m.key));
 for(const m of network){
  if(!m.final)return fail('NETWORK_FINALITY_UNKNOWN_HOLD',{keyHash:sha256(m.key)});
  const o=observed.find(x=>x.key===m.key);
  if(!o)return fail('DOM_CONFIRMATION_MISSING_HOLD',{keyHash:sha256(m.key)});
  if(o.text!==m.text||o.role!==m.role||o.final!==true)return fail('DOM_NETWORK_CONFLICT_HOLD',{keyHash:sha256(m.key)});
  const old=currentMap.get(m.key);
  if(old&&(old.text!==m.text||old.role!==m.role))return fail(old.index<archivedThrough?'ARCHIVED_REVISION_HOLD':'EXISTING_REVISION_HOLD',{keyHash:sha256(m.key)});
 }
 const oldTail=state.messages.at(-1)?.key||null;
 if(oldTail!==null){
  if(requiredTail&&requiredTail!==oldTail)return fail('TAIL_MISMATCH_HOLD');
  const anchor=observed.findIndex(m=>m.key===oldTail);
  if(anchor===-1)return fail('TAIL_NOT_OBSERVED_HOLD');
  const suffix=observed.slice(anchor+1);
  if(suffix.some(m=>m.final!==true))return fail('DOM_SUFFIX_FINALITY_HOLD');
  if(suffix.length!==newMessages.length)return fail('NETWORK_SUFFIX_INCOMPLETE_HOLD',{dom:suffix.length,network:newMessages.length});
  if(suffix.some((m,i)=>m.key!==newMessages[i].key))return fail('NETWORK_DOM_ORDER_CONFLICT_HOLD');
 }else{
  if(!allowBootstrap)return fail('BOOTSTRAP_NOT_AUTHORIZED_HOLD');
  if(observed.length!==network.length||observed.length===0)return fail('BOOTSTRAP_INCOMPLETE_HOLD');
  if(observed.some((m,i)=>m.key!==network[i].key||m.final!==true))return fail('BOOTSTRAP_ORDER_HOLD');
 }
 for(const m of network){
  if(!currentMap.has(m.key))continue;
  // In-range network messages must appear in the exact same historical order as the DOM.
 }
 const oldKeys=network.filter(m=>currentMap.has(m.key)).map(m=>currentMap.get(m.key).index);
 if(oldKeys.some((n,i)=>i>0&&n<=oldKeys[i-1]))return fail('OLD_KEY_ORDER_CONFLICT_HOLD');
 const nextMessages=[...state.messages,...newMessages.map(m=>({
  key:m.key,role:m.role,text:m.text,firstSeenAt:new Date().toISOString(),
  lastUpdatedAt:new Date().toISOString(),
  observationStatus:'NETWORK_FINALITY_AND_DOM_CONFIRMED',
  sourceEvidence:{kind:'P2_NETWORK_PLUS_P1_DOM_CHECK',textSha256:sha256(m.text)}
 }))];
 return {status:newMessages.length?'SCOPED_COMMIT_READY':'NO_CHANGE',detail:{added:newMessages.length,oldCount:state.messages.length,newCount:nextMessages.length,unprovenCloudHistory:'UNKNOWN'},ok:true,proposed:nextMessages};
}

