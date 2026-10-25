import {collectForwardBridge} from './raw-auto-forward.mjs';
import {sha256} from './raw-auto-core.mjs';

export async function attemptIdleRecovery({
 session,store,report,readStreamCompletion,collect=collectForwardBridge,
 deadlineMs=75000,stepPx=570,maxSteps=195,write=false
}){
 const result=(status,other={})=>({status,ok:false,...other});
 if(!session?.cdp?.open||!session.conversationId)return result('RECOVERY_DISCONNECTED_HOLD');
 if(!write)return result('RECOVERY_WRITE_NOT_AUTHORIZED_HOLD');
 const background=async()=>{
  try{
   const x=await session.cdp.send('Runtime.evaluate',
    {expression:'document.hasFocus()===false',returnByValue:true},6000);
   return x.result?.value===true;
  }catch{return false}
 };
 if(!(await background()))return result('RECOVERY_USER_ACTIVE_DEFERRED');
 if(await readStreamCompletion(session)!=='COMPLETE')return result('RECOVERY_STREAM_INCOMPLETE_HOLD');
 const baseline=await store.get(session.conversationId,{url:session.tabUrl,title:session.title});
 if(baseline.gap.blocked)return result('RECOVERY_CAPTURE_GAP_HOLD');
 if(baseline.record.messages.length===0)return result('RECOVERY_BOOTSTRAP_NOT_ALLOWED');
 const run=()=>collect({
  cdp:session.cdp,loaded:baseline,deadlineMs,maxSteps,stepPx,settleMs:120,
  requireBackground:true
 });
 const first=await run();
 if(!first.ok)return result(first.status,{round:1,details:first.reason||null});
 if(!(await background()))return result('RECOVERY_USER_ACTIVE_DEFERRED',{round:1});
 if(await readStreamCompletion(session)!=='COMPLETE')return result('RECOVERY_STREAM_CHANGED_HOLD');
 const second=await run();
 if(!second.ok)return result(second.status,{round:2,details:second.reason||null});
 const encode=x=>JSON.stringify(x.proposed.map(m=>[m.key,m.role,m.text]));
 const firstHash=sha256(encode(first)),secondHash=sha256(encode(second));
 if(firstHash!==secondHash||first.added!==second.added||
  first.proposed.length!==second.proposed.length)
  return result('RECOVERY_INDEPENDENT_ROUNDS_DIFFER_HOLD',{
   firstCount:first.proposed.length,secondCount:second.proposed.length});
 if(!(await background()))return result('RECOVERY_USER_ACTIVE_DEFERRED',{round:2});
 if(await readStreamCompletion(session)!=='COMPLETE')return result('RECOVERY_STREAM_CHANGED_HOLD');
 const latest=await store.get(session.conversationId,{url:session.tabUrl,title:session.title});
 if(latest.baselineBytes!==baseline.baselineBytes||
  latest.archivedThrough!==baseline.archivedThrough)return result('RECOVERY_CANONICAL_DRIFT_HOLD');
 if(second.added<1)return result('RECOVERY_NO_NEW_MESSAGES_HOLD');
 // Single store writer enforces immutable prefix, exclusive lock, checkpoint, atomic save and readback.
 const saved=await store.save(session.conversationId,{
  loaded:latest,decision:second,mode:'P1_SCOPED_SCROLL_BRIDGE'
 });
 await report('RAW_SCOPED_AUTO_RECOVERY_VERIFIED',session.conversationId,{
  added:saved.added,total:saved.count,sha256:saved.sha256,
  twiceCheckedDigest:secondHash,steps1:first.evidence?.attempted||null,
  steps2:second.evidence?.attempted||null,
  provenance:'BOUNDED_STABLE_DOM_ONLY',cloudHistoryCompleteness:'UNKNOWN',
  independentAcceptance:'NOT_CLAIMED'
 });
 return {ok:true,status:'RAW_SCOPED_AUTO_RECOVERY_VERIFIED',
  added:saved.added,total:saved.count,sha256:saved.sha256,twiceCheckedDigest:secondHash};
}
