// Bounded P2/DOM disagreement reconciliation; no browser, file, or archive actions.
import {sha256} from './raw-auto-core.mjs';
import {evaluateRecovery} from './raw-auto-p1.mjs';

const MAX_SUFFIX=20;
export const MIN_CONFLICT_SETTLE_MS=9000;

export function evaluateConflictProbe({
 state,archivedThrough,first,second,networkByKey,previous=null,now=Date.now(),
 minSettleMs=MIN_CONFLICT_SETTLE_MS,networkInternallyConflicted=false
}){
 const hold=(status,extra={})=>({ok:false,status,next:null,decision:null,...extra});
 if(networkInternallyConflicted)return hold('P2_INTERNAL_FINAL_CONFLICT_HOLD');
 if(!(networkByKey instanceof Map))return hold('P2_NETWORK_MAP_INVALID_HOLD');
 if(!Number.isFinite(now)||!Number.isFinite(minSettleMs)||minSettleMs<1000)return hold('P1_CONFLICT_CLOCK_INVALID_HOLD');
 const decision=evaluateRecovery({state,archivedThrough,first,second});
 if(!decision.ok)return hold(decision.status);
 if(decision.status==='P1_NO_CHANGE')return hold('P1_CONFLICT_NO_SUFFIX_HOLD');
 const old=state.messages??[],tail=old.at(-1)?.key;
 if(!tail)return hold('P1_CONFLICT_BOOTSTRAP_HOLD');
 const suffix=second.slice(second.findIndex(m=>m.key===tail)+1);
 if(!suffix.length||suffix.length>MAX_SUFFIX)return hold('P1_CONFLICT_BATCH_BOUNDS_HOLD');
 const mismatches=[],network=[];
 for(const msg of suffix){
  const candidate=networkByKey.get(msg.key);
  if(!candidate?.final||candidate.role!==msg.role||candidate.key!==msg.key)return hold('P2_CONFLICT_INCOMPLETE_CANDIDATE_HOLD');
  network.push(candidate);
  if(candidate.text!==msg.text)mismatches.push({keySha256:sha256(msg.key),networkSha256:sha256(candidate.text),domSha256:sha256(msg.text)});
 }
 if(!mismatches.length)return hold('P2_DOM_NOW_AGREES_NO_CONFLICT');
 const fingerprint=sha256(JSON.stringify({
  tail,archivedThrough,oldCount:old.length,
  observed:suffix.map(m=>[m.key,m.role,m.text,m.final]),
  network:network.map(m=>[m.key,m.role,m.text,m.final])
 }));
 if(!previous||previous.fingerprint!==fingerprint||previous.tail!==tail||previous.oldCount!==old.length){
  return hold('P1_CONFLICT_VERIFYING',{
   next:{fingerprint,tail,oldCount:old.length,firstAt:now,rounds:1},
   evidence:{rounds:1,mismatchedMessages:mismatches.length,messageDigests:mismatches,historyScope:'KNOWN_TAIL_VISIBLE_ONLY'}
  });
 }
 if(previous.firstAt>now)return hold('P1_CONFLICT_CLOCK_ROLLBACK_HOLD');
 const elapsed=now-previous.firstAt;
 if(elapsed<minSettleMs){
  return hold('P1_CONFLICT_SETTLING_HOLD',{
   next:{...previous,rounds:(previous.rounds??1)+1},
   evidence:{elapsedMs:elapsed,mismatchedMessages:mismatches.length}
  });
 }
 const proposed=decision.proposed.map((m,i)=>{
  if(i<old.length)return m;
  const original=networkByKey.get(m.key);
  return {...m,observationStatus:'P1_CONFLICT_RESOLVED_SCOPED',
   sourceEvidence:{kind:'P1_CONFLICT_TWO_SEPARATE_STABLE_DOM_ROUNDS',
    oldTailHash:sha256(tail),textSha256:sha256(m.text),
    p2TextSha256:sha256(original.text),networkTextDisagreed:original.text!==m.text,
    settleMs:elapsed,globalContinuity:'UNKNOWN'}}
 });
 return {ok:true,status:'P1_SCOPED_APPEND_READY',
  decision:{ok:true,status:'P1_SCOPED_APPEND_READY',proposed,
   detail:{mode:'P1_CONFLICT_RESOLVED',added:proposed.length-old.length,
    mismatchedMessages:mismatches.length,stableRoundsAtLeast:2,elapsedMs:elapsed,cloudCompleteness:'UNKNOWN'}},
  next:null,
  evidence:{rounds:2,elapsedMs:elapsed,mismatchedMessages:mismatches.length,
   messageDigests:mismatches,oldTailSha256:sha256(tail)}};
}


