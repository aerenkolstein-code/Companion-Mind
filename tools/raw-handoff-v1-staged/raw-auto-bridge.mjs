import {sha256} from './raw-auto-core.mjs';

// Recover only a scoped chain demonstrated by overlapping, stable DOM windows.
// A page window is NOT evidence of cloud-wide completeness.
export function buildScopedBridge({state,archivedThrough=0,slices=[],latest=[]}){
 const fail=(status,details={})=>({ok:false,status,...details});
 if(!state||!Array.isArray(state.messages)||!state.messages.length)return fail('BRIDGE_NO_BASELINE_HOLD');
 if(!Number.isInteger(archivedThrough)||archivedThrough<0||archivedThrough>state.messages.length)return fail('BRIDGE_BAD_CHECKPOINT_HOLD');
 if(!Array.isArray(slices)||!slices.length||slices.length>180)return fail('BRIDGE_NO_SLICES_HOLD');
 const old=state.messages,oldKeys=new Map(old.map((m,i)=>[m.key,{m,i}]));
 if(oldKeys.size!==old.length)return fail('BRIDGE_DUPLICATE_BASELINE_HOLD');
 const tail=old.at(-1).key, nodes=new Map(),next=new Map(),prev=new Map();
 let containsTail=false,segments=0,accepted=0,partialSeen=0;
 const normalized=[];
 for(const [sliceIndex,s] of slices.entries()){
  if(!Array.isArray(s)||!s.length||s.length>120)return fail('BRIDGE_BAD_SLICE_HOLD',{sliceIndex});
  const keys=new Set(), clean=[];
  for(const m of s){
   if(typeof m.key!=='string'||!['user','assistant'].includes(m.role)||typeof m.text!=='string'||!m.text||typeof m.final!=='boolean')
    return fail('BRIDGE_BAD_MESSAGE_HOLD',{sliceIndex});
   if(keys.has(m.key))return fail('BRIDGE_DUPLICATE_SLICE_HOLD',{sliceIndex});
   keys.add(m.key);
   const prior=nodes.get(m.key);
   if(prior&&(prior.role!==m.role||prior.text!==m.text||prior.final!==m.final))
    return fail('BRIDGE_CONFLICTING_SNAPSHOTS_HOLD',{sliceIndex,keySha:sha256(m.key)});
   nodes.set(m.key,m);
   const found=oldKeys.get(m.key);
   if(found&&(found.m.role!==m.role||found.m.text!==m.text))
    return fail(found.i<archivedThrough?'BRIDGE_ARCHIVED_REVISION_HOLD':'BRIDGE_EXISTING_REVISION_HOLD',{sliceIndex,keySha:sha256(m.key)});
   if(m.key===tail){if(m.final!==true)return fail('BRIDGE_TAIL_UNFINAL_HOLD');containsTail=true}
   if(!m.final)partialSeen++;
   clean.push(m);
  }
  normalized.push(clean);
  segments++;
 }
 if(!containsTail)return fail('BRIDGE_TAIL_NOT_FOUND_HOLD',{segments});
 // The collected windows must form one overlapping chain: never silently jump a missing viewport.
 for(let i=1;i<normalized.length;i++){
  const prevKeys=new Set(normalized[i-1].map(m=>m.key));
  const common=normalized[i].filter(m=>prevKeys.has(m.key));
  if(!common.length)return fail('BRIDGE_VIEWPORT_GAP_HOLD',{between:[i-1,i]});
  // Every overlap must have identical relative ordering.
  const order=normalized[i-1].filter(m=>prevKeys.has(m.key)&&normalized[i].some(n=>n.key===m.key)).map(m=>m.key);
  if(order.join('|')!==common.map(m=>m.key).join('|'))return fail('BRIDGE_OVERLAP_ORDER_HOLD',{between:[i-1,i]});
 }
 function add(a,b,si){
  if(a.key===b.key)return fail('BRIDGE_SELF_EDGE_HOLD',{si});
  if(next.has(a.key)&&next.get(a.key)!==b.key)return fail('BRIDGE_FORK_HOLD',{si,keySha:sha256(a.key)});
  if(prev.has(b.key)&&prev.get(b.key)!==a.key)return fail('BRIDGE_JOIN_HOLD',{si,keySha:sha256(b.key)});
  next.set(a.key,b.key);prev.set(b.key,a.key);return null;
 }
 for(let si=0;si<normalized.length;si++){
  const s=normalized[si];
  let lastCompleteIndex=s.findIndex(m=>!m.final);
  if(lastCompleteIndex<0)lastCompleteIndex=s.length;
  if(s.slice(lastCompleteIndex).some(m=>m.final))return fail('BRIDGE_INTERIOR_UNFINAL_HOLD',{si});
  for(let i=0;i<lastCompleteIndex-1;i++){
   const error=add(s[i],s[i+1],si);if(error)return error;
  }
 }
 const chain=[],seen=new Set();let key=tail;
 for(let i=0;i<=nodes.size;i++){
  if(!next.has(key))break;
  key=next.get(key);
  if(seen.has(key))return fail('BRIDGE_CYCLE_HOLD');
  if(oldKeys.has(key))return fail('BRIDGE_REENTERS_OLD_HISTORY_HOLD');
  const msg=nodes.get(key);
  if(!msg?.final)return fail('BRIDGE_UNFINAL_CHAIN_HOLD');
  chain.push(msg);seen.add(key);
 }
 if(!chain.length)return fail('BRIDGE_NO_VERIFIED_SUFFIX_HOLD',{segments});
 if(!Array.isArray(latest)||!latest.length)return fail('BRIDGE_LATEST_MISSING_HOLD');
 const latestFinal=latest.filter(m=>m.final);
 if(!latestFinal.length)return fail('BRIDGE_NO_FINAL_LATEST_HOLD');
 const reachedLatest=latestFinal.some(m=>m.key===chain.at(-1).key);
 if(!reachedLatest)return fail('BRIDGE_NOT_REACHED_LATEST_HOLD',{lastSha:sha256(chain.at(-1).key)});
 const latestNewComplete=latestFinal.filter(m=>!oldKeys.has(m.key));
 if(latestNewComplete.some(m=>!seen.has(m.key)))return fail('BRIDGE_MISSING_LATEST_HOLD');
 // A completed message encountered during the sweep may not be silently excluded.
 const unseen=[...nodes.values()].filter(m=>m.final&&!oldKeys.has(m.key)&&!seen.has(m.key));
 if(unseen.length)return fail('BRIDGE_UNLINKED_COMPLETED_HOLD',{unlinked:unseen.length});
 const now=new Date().toISOString();
 const proposal=[...old,...chain.map(m=>({
  key:m.key,role:m.role,text:m.text,firstSeenAt:now,lastUpdatedAt:now,
  observationStatus:'P1_SCOPED_SCROLL_BRIDGE',
  sourceEvidence:{kind:'P1_OVERLAPPING_STABLE_DOM_WINDOWS',windowCount:segments,historyComprehensiveness:'UNKNOWN',textSha256:sha256(m.text)}
 }))];
 accepted=chain.length;
 return {ok:true,status:'P1_SCOPED_APPEND_READY',proposed:proposal,added:accepted,
  evidence:{segments,partialSeen,anchor:tail,latestKey:chain.at(-1).key,sourceCompleteness:'UNKNOWN'}};
}
