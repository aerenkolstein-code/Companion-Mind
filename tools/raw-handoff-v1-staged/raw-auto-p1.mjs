// P1 is a gated suffix repair path, not a second authority or writer.
import {classifyCaptureGaps} from './raw-auto-gap-policy.mjs';
import {stable} from './raw-auto-dom.mjs';
export function evaluateRecovery({state,archivedThrough,first,second}){
 const stop=(status)=>({ok:false,status,proposed:null});
 if(!Array.isArray(state?.messages)||!Number.isInteger(archivedThrough)||archivedThrough<0||archivedThrough>state.messages.length)return stop('P1_INVALID_BASELINE_HOLD');
 const gap=classifyCaptureGaps(state);
 if(gap.blocked)return stop(gap.status);
 if(!stable(first,second))return stop('P1_UNSTABLE_DOM_HOLD');
 if(second.some(m=>!m.key||!m.text||!['user','assistant'].includes(m.role)))return stop('P1_INVALID_VISIBLE_MESSAGE_HOLD');
 const keys=second.map(m=>m.key);
 if(new Set(keys).size!==keys.length)return stop('P1_DUPLICATE_DOM_HOLD');
 const old=state.messages;
 if(!old.length)return stop('P1_BOOTSTRAP_REQUIRES_NETWORK_HOLD');
 const byKey=new Map(old.map((x,i)=>[x.key,i]));
 if(byKey.size!==old.length)return stop('P1_DUPLICATE_HISTORY_HOLD');
 const known=second.filter(m=>byKey.has(m.key));
 const positions=known.map(m=>byKey.get(m.key));
 if(positions.some((p,i)=>i>0&&p<=positions[i-1]))return stop('P1_ORDER_HOLD');
 for(const m of known){
  const existing=old[byKey.get(m.key)];
  if(existing.role!==m.role||existing.text!==m.text)return stop(byKey.get(m.key)<archivedThrough?'P1_ARCHIVED_REVISION_HOLD':'P1_EXISTING_REVISION_HOLD');
 }
 const tail=old.at(-1).key,anchor=second.findIndex(m=>m.key===tail);
 if(anchor<0)return stop('P1_TAIL_ANCHOR_MISSING_HOLD');
 const suffix=second.slice(anchor+1);
 if(!suffix.length)return {ok:true,status:'P1_NO_CHANGE',proposed:old,added:0};
 if(suffix.some(m=>byKey.has(m.key)))return stop('P1_REORDER_HOLD');
 if(suffix.some(m=>!m.final))return stop('P1_FINALITY_UNKNOWN_HOLD');
 const proposal=[...old,...suffix.map(m=>({
  key:m.key,role:m.role,text:m.text,firstSeenAt:new Date().toISOString(),
  lastUpdatedAt:new Date().toISOString(),
  observationStatus:'P1_DOM_RECOVERY_SCOPED',
  sourceEvidence:{kind:'P1_TWO_STABLE_DOM_SNAPSHOTS',historyComprehensiveness:'UNKNOWN'}
 }))];
 return {ok:true,status:'P1_SCOPED_APPEND_READY',proposed:proposal,added:suffix.length,unknownCoverage:true};
}


