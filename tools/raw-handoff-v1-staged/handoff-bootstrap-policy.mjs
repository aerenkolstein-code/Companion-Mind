import {sha256} from './raw-auto-core.mjs';
import {isHandoffTrigger} from './handoff-core.mjs';
export function checkBootstrapRounds({first,second,triggerKey,minimumSeparationMs=30000,stream='COMPLETE'}){
 const fail=(status,more={})=>({ok:false,status,...more});
 if(stream!=='COMPLETE')return fail('HANDOFF_BOOTSTRAP_STREAM_INCOMPLETE_HOLD');
 if(!first||!second||!Array.isArray(first.messages)||!Array.isArray(second.messages))
  return fail('HANDOFF_BOOTSTRAP_MISSING_ROUNDS_HOLD');
 if(!first.topReached||!second.topReached||!first.bottomReached||!second.bottomReached)
  return fail('HANDOFF_BOOTSTRAP_BOUNDS_UNVERIFIED_HOLD');
 if(Date.parse(second.observedAt)-Date.parse(first.observedAt)<minimumSeparationMs)
  return fail('HANDOFF_BOOTSTRAP_SETTLE_TIME_HOLD');
 const a=first.messages,b=second.messages;
 if(a.length!==b.length||!a.length)return fail('HANDOFF_BOOTSTRAP_LENGTH_MISMATCH_HOLD');
 const d=x=>sha256(JSON.stringify(x.map(m=>[m.key,m.role,m.text,m.final])));
 if(d(a)!==d(b))return fail('HANDOFF_BOOTSTRAP_ROUNDS_DIFFER_HOLD');
 const keys=new Set();
 for(const [i,m] of a.entries()){
  if(typeof m.key!=='string'||!m.key.includes(':')||!['user','assistant'].includes(m.role)||
   typeof m.text!=='string'||!m.text||!m.final)
   return fail('HANDOFF_BOOTSTRAP_INCOMPLETE_OR_INVALID_HOLD',{index:i});
  if(keys.has(m.key))return fail('HANDOFF_BOOTSTRAP_DUPLICATE_KEY_HOLD');
  keys.add(m.key);
  if(m.role!==(i%2===0?'user':'assistant'))return fail('HANDOFF_BOOTSTRAP_ROLE_ORDER_HOLD',{index:i});
 }
 if(a.length%2!==0)return fail('HANDOFF_BOOTSTRAP_UNPAIRED_USER_HOLD');
 const triggerAt=a.findIndex(m=>m.key===triggerKey);
 if(triggerAt<0)return fail('HANDOFF_BOOTSTRAP_TRIGGER_MISSING_HOLD');
 if(triggerAt%2!==0||!isHandoffTrigger(a[triggerAt].text))return fail('HANDOFF_BOOTSTRAP_TRIGGER_WRONG_HOLD');
 if(a[triggerAt+1]?.role!=='assistant')return fail('HANDOFF_BOOTSTRAP_REPLY_MISSING_HOLD');
 return {ok:true,status:'HANDOFF_BOOTSTRAP_READY',messages:a,added:a.length,
  evidence:{scoped:true,cloudHistoryCompleteness:'UNKNOWN',twoRoundDigest:d(a),
   firstObservedAt:first.observedAt,secondObservedAt:second.observedAt,
   triggerKey,assistantKey:a[triggerAt+1].key,windowCount:[first.windowCount,second.windowCount]}};
}
