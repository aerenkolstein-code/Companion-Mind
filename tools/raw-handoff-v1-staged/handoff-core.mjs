import {checkId} from './raw-auto-store.mjs';
export const MAX_TARGETS=8;
export const TRIGGER='接续旧窗口';
export const STATUSES=new Set(['ACTIVE','LEGACY_RECOVERY_PENDING','BOOTSTRAP_PENDING','OVERLAP_ACTIVE','SEALED','HOLD']);
export function parseChatUrl(value){
 try{
  const u=new URL(value);
  if(u.protocol!=='https:'||u.hostname!=='chatgpt.com'||u.username||u.password)return null;
  const m=u.pathname.match(/^\/g\/(g-p-[a-z0-9-]+)\/c\/([a-f0-9-]{36})\/?$/i);
  if(!m)return null;
  checkId(m[2]);
  return {projectKey:m[1],conversationId:m[2].toLowerCase(),url:u.origin+u.pathname};
 }catch{return null}
}
export function isHandoffTrigger(value){
 return typeof value==='string' && /^接续旧窗口[。.!！]?$/.test(value.trim());
}
export function assertRegistry(r){
 if(!r||r.format!=='raw-handoff-registry-v1'||typeof r.projectKey!=='string'||
  !r.projectKey.startsWith('g-p-')||r.personaId!=='U1-P08'||
  !Number.isSafeInteger(r.revision)||r.revision<0||!Array.isArray(r.targets)||
  r.targets.length<1||r.targets.length>MAX_TARGETS||
  typeof r.activeConversationId!=='string')throw Error('REGISTRY_FORMAT_INVALID');
 checkId(r.activeConversationId);
 const ids=new Set();
 for(const entry of r.targets){
  checkId(entry.id);
  if(ids.has(entry.id)||!STATUSES.has(entry.status)||entry.authorized!==true)throw Error('REGISTRY_TARGET_INVALID');
  ids.add(entry.id);
 }
 if(!ids.has(r.activeConversationId))throw Error('REGISTRY_ACTIVE_MISSING');
 if(!r.targets.some(e=>e.id===r.activeConversationId && ['ACTIVE','LEGACY_RECOVERY_PENDING','OVERLAP_ACTIVE'].includes(e.status)))throw Error('REGISTRY_ACTIVE_STATUS_INVALID');
 if(r.currentHandoff!==null&&r.currentHandoff!==undefined){
  const h=r.currentHandoff;
  if(!['PREPARED','BOOTSTRAP_PENDING','OVERLAP_ACTIVE','NEW_RAW_VERIFIED','HOLD'].includes(h.status))throw Error('REGISTRY_HANDOFF_STATUS_INVALID');
  checkId(h.from);checkId(h.to);
  if(!ids.has(h.from)||!ids.has(h.to)||h.from===h.to)throw Error('REGISTRY_HANDOFF_PAIR_INVALID');
 }
 return r;
}
export function authorizedIds(r){
 assertRegistry(r);
 return r.targets.filter(e=>e.authorized===true&&e.status!=='SEALED').map(e=>e.id);
}
export function latestTrigger(messages){
 if(!Array.isArray(messages))return null;
 const user=messages.filter(m=>m?.role==='user'&&typeof m.text==='string');
 const last=user.at(-1);
 if(!last||!isHandoffTrigger(last.text)||typeof last.key!=='string'||!last.key.startsWith('user:'))return null;
 return {key:last.key,text:last.text};
}
export function proposeHandoff({registry,tabs,selectedNewId,messages,now='2026-10-25T00:00:00.000Z'}){
 assertRegistry(registry);checkId(selectedNewId);
 const err=(status,details={})=>({ok:false,status,...details});
 if(registry.currentHandoff)return err('HANDOFF_IN_PROGRESS_HOLD');
 if(registry.targets.some(e=>e.id===selectedNewId))return err('TARGET_ALREADY_REGISTERED_HOLD');
 if(registry.targets.length>=MAX_TARGETS)return err('TARGET_CAPACITY_HOLD',{capacity:MAX_TARGETS});
 if(!Array.isArray(tabs))return err('CHROME_TABS_UNKNOWN_HOLD');
 const mapped=tabs.map(t=>({raw:t,parsed:parseChatUrl(t.url)}));
 const matches=mapped.filter(x=>x.parsed?.conversationId===selectedNewId);
 if(matches.length!==1)return err(matches.length?'DUPLICATE_NEW_TABS_HOLD':'NEW_TAB_NOT_FOUND_HOLD');
 const tab=matches[0];
 if(tab.parsed.projectKey!==registry.projectKey)return err('CROSS_PROJECT_HOLD');
 if(tab.raw.type!=='page'||!tab.raw.webSocketDebuggerUrl)return err('NEW_TAB_NOT_CDP_READY_HOLD');
 const current=registry.activeConversationId;
 const oldTabs=mapped.filter(x=>x.parsed?.conversationId===current);
 if(oldTabs.length>1)return err('DUPLICATE_OLD_TABS_HOLD');
 const trigger=latestTrigger(messages);
 if(!trigger)return err('LATEST_USER_TRIGGER_MISSING_HOLD');
 const next=structuredClone(registry);
 next.revision++;
 next.targets=next.targets.map(t=>
  t.id===current?{...t,status:'LEGACY_RECOVERY_PENDING',retainReason:'HANDOFF_OLD_TAIL_NOT_YET_SEALED'}:t);
 next.targets.push({id:selectedNewId,status:'BOOTSTRAP_PENDING',retainReason:'EXPLICIT_USER_TRIGGER',authorized:true});
 next.currentHandoff={from:current,to:selectedNewId,status:'PREPARED',triggerKey:trigger.key,
  preparedAt:now,firstNewRawCount:0,independentAcceptance:'NOT_CLAIMED'};
 next.transitions=[...(Array.isArray(registry.transitions)?registry.transitions:[]),
  {event:'HANDOFF_PREPARED',from:current,to:selectedNewId,at:now,triggerKey:trigger.key}];
 return {ok:true,status:'HANDOFF_PREPARED',next,from:current,to:selectedNewId,
  triggerKey:trigger.key,oldTabVisible:oldTabs.length===1,oldState:'LEGACY_RECOVERY_PENDING'};
}
export function verifyNewRawAndActivate({registry,raw,now='2026-10-25T00:05:00.000Z'}){
 assertRegistry(registry);
 const fail=(status,details={})=>({ok:false,status,...details});
 const handoff=registry.currentHandoff;
 if(!handoff)return fail('NO_PREPARED_HANDOFF_HOLD');
 if(!raw||raw.conversationId!==handoff.to||!Array.isArray(raw.messages))
  return fail('NEW_RAW_MISSING_HOLD');
 const i=raw.messages.findIndex(m=>m.key===handoff.triggerKey);
 if(i<0)return fail('TRIGGER_NOT_IN_FORMAL_RAW_HOLD');
 if(raw.messages.filter(m=>m.key===handoff.triggerKey).length!==1)return fail('TRIGGER_DUPLICATE_HOLD');
 const trigger=raw.messages[i];
 if(trigger.role!=='user'||!isHandoffTrigger(trigger.text))return fail('TRIGGER_TEXT_NOT_PRESERVED_HOLD');
 const reply=raw.messages[i+1];
 if(!reply||reply.role!=='assistant'||typeof reply.text!=='string'||!reply.text)
  return fail('NEW_ASSISTANT_REPLY_NOT_COMMITTED_HOLD');
 if(raw.messages.some((m,j)=>j>0&&m.key===raw.messages[j-1].key))return fail('RAW_DUPLICATE_NEIGHBOR_HOLD');
 const ids=raw.messages.map(m=>m.key);
 if(new Set(ids).size!==ids.length)return fail('RAW_DUPLICATE_KEYS_HOLD');
 const next=structuredClone(registry);
 next.revision++;
 next.activeConversationId=handoff.to;
 next.targets=next.targets.map(x=>x.id===handoff.to?{...x,status:'ACTIVE',retainReason:'VERIFIED_NEW_RAW'}:x);
 next.currentHandoff=null;
 next.transitions=[...(next.transitions||[]),{event:'HANDOFF_NEW_RAW_VERIFIED',at:now,
  from:handoff.from,to:handoff.to,triggerKey:handoff.triggerKey,assistantKey:reply.key}];
 return {ok:true,status:'NEW_ACTIVE_OLD_RETAINED',next,active:handoff.to,
  retainedOld:handoff.from,verifiedPair:[trigger.key,reply.key],
  oldSealed:false,cloudCompleteness:'UNKNOWN'};
}
export function canSealOld({registry,oldId,pendingCount,continuityVerified,officialAcceptance}){
 assertRegistry(registry);checkId(oldId);
 if(!registry.targets.some(x=>x.id===oldId))return {ok:false,status:'OLD_NOT_REGISTERED'};
 if(oldId===registry.activeConversationId)return {ok:false,status:'ACTIVE_CANNOT_SEAL'};
 if(pendingCount!==0||continuityVerified!==true||officialAcceptance!==true)
  return {ok:false,status:'OLD_RECOVERY_PENDING_HOLD'};
 return {ok:true,status:'OLD_SEAL_ELIGIBLE_NOT_EXECUTED'};
}
