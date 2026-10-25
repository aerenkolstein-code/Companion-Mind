import fs from 'node:fs/promises';
import path from 'node:path';
import {checkBootstrapRounds} from './handoff-bootstrap-policy.mjs';
import {collectScopedHistory} from './handoff-history-collector.mjs';
import {verifyNewRawAndActivate} from './handoff-core.mjs';
import {writeHandoffRegistry} from './handoff-registry-store.mjs';
import {sha256} from './raw-auto-core.mjs';

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const failure=(status,extra={})=>({ok:false,status,...extra});
async function verifySavedAndPromote({session,store,loader,readStreamCompletion,collectSnapshot=null,report,root}){
 const id=session.conversationId;
 const {current:registry}=loader;
 const h=registry?.currentHandoff;
 if(!h||h.to!==id)return failure('HANDOFF_NOT_PREPARED_HOLD');
 if(await readStreamCompletion(session)!=='COMPLETE')return failure('HANDOFF_FINAL_STREAM_INCOMPLETE_HOLD');
 const loaded=await store.get(id,{url:session.tabUrl,title:session.title});
 if(loaded.gap.blocked)return failure('HANDOFF_NEW_RAW_GAP_HOLD');
 const candidate=verifyNewRawAndActivate({registry,raw:loaded.record,now:new Date().toISOString()});
 if(!candidate.ok)return failure(candidate.status);
 const key=candidate.verifiedPair[1];
 const reply=loaded.record.messages.find(x=>x.key===key);
 // First successful bootstrap has two already stable scans. On later retries, collect one verification snapshot.
 if(collectSnapshot){
  const observed=await collectSnapshot(session);
  const visible=observed?.messages?.find(x=>x.key===key);
  if(!visible||!visible.final||visible.text!==reply.text)return failure('HANDOFF_NEW_REPLY_NOT_VERIFIED_IN_UI_HOLD');
 }
 const baseline=await fs.readFile(path.join(root,'handoff-registry.json'),'utf8');
 if(JSON.parse(baseline).revision!==registry.revision)return failure('HANDOFF_REGISTRY_REFRESH_REQUIRED_HOLD');
 const receipt=await writeHandoffRegistry({root,baseline,next:candidate.next,
  event:'NEW_RAW_VERIFIED',details:{from:candidate.retainedOld,to:candidate.active,
   assistantKey:key,triggerKey:h.triggerKey,readbackSha256:sha256(loaded.baselineBytes||''),
   rawCount:loaded.record.messages.length,oldSealed:false,cloudCompleteness:'UNKNOWN'}});
 await report('RAW_HANDOFF_NEW_ACTIVE_VERIFIED',id,{
  revision:candidate.next.revision,oldConversationId:candidate.retainedOld,
  rawCount:loaded.record.messages.length,oldSealed:false,
  authority:'LOCAL_RUNTIME_ONLY',independentAcceptance:'NOT_CLAIMED'});
 return {ok:true,status:'HANDOFF_ACTIVE_OLD_RETAINED',added:0,
  total:loaded.record.messages.length,registryRevision:candidate.next.revision,receipt:receipt.receiptPath};
}
export async function attemptHandoffBootstrap({
 session,store,loader,readStreamCompletion,report,root,
 collect=collectScopedHistory,settleMs=31000
}){
 const id=session?.conversationId;
 if(!id||!session?.cdp?.open)return failure('HANDOFF_CDP_NOT_READY_HOLD');
 const registry=loader.current;
 if(!registry?.currentHandoff||registry.currentHandoff.to!==id)
  return failure('HANDOFF_NOT_PREPARED_HOLD');
 const h=registry.currentHandoff;
 const loaded=await store.get(id,{url:session.tabUrl,title:session.title});
 if(loaded.gap.blocked)return failure('HANDOFF_SOURCE_GAP_HOLD');
 if(loaded.record.messages.length){
  // If the first reply was already committed by P2, this path recovers the registry transition.
  return await verifySavedAndPromote({session,store,loader,readStreamCompletion,
   report,root,collectSnapshot:async()=>await collect({cdp:session.cdp,deadlineMs:65000})});
 }
 if(await readStreamCompletion(session)!=='COMPLETE')
  return failure('HANDOFF_STREAM_STILL_GENERATING_HOLD');
 const first=await collect({cdp:session.cdp,deadlineMs:70000});
 if(!first.ok)return first;
 await sleep(settleMs);
 if(await readStreamCompletion(session)!=='COMPLETE')
  return failure('HANDOFF_STREAM_CHANGED_BETWEEN_ROUNDS_HOLD');
 const second=await collect({cdp:session.cdp,deadlineMs:70000});
 if(!second.ok)return second;
 if(await readStreamCompletion(session)!=='COMPLETE')
  return failure('HANDOFF_STREAM_CHANGED_AFTER_SCAN_HOLD');
 const result=checkBootstrapRounds({first,second,triggerKey:h.triggerKey,
  minimumSeparationMs:30000,stream:'COMPLETE'});
 if(!result.ok)return result;
 const refreshed=await loader.refresh();
 if(loader.current?.currentHandoff?.to!==id)
  return failure('HANDOFF_REGISTRY_CHANGED_DURING_SWEEP_HOLD');
 const latest=await store.get(id,{url:session.tabUrl,title:session.title});
 if(latest.baselineBytes!==loaded.baselineBytes||latest.archivedThrough!==loaded.archivedThrough)
  return failure('HANDOFF_RAW_CHANGED_DURING_SWEEP_HOLD');
 const now=new Date().toISOString();
 const proposal=result.messages.map(m=>({
  key:m.key,role:m.role,text:m.text,firstSeenAt:first.observedAt,
  lastUpdatedAt:second.observedAt,observationStatus:'P1_TWO_ROUND_HANDOFF_SCOPED',
  sourceEvidence:{kind:'P1_TWO_ROUND_HANDOFF_DOM',twoRoundDigest:result.evidence.twoRoundDigest,
   textSha256:sha256(m.text),observedStart:first.observedAt,observedEnd:second.observedAt,
   cloudHistoryComprehensiveness:'UNKNOWN',handoffTriggerKey:h.triggerKey}
 }));
 const saved=await store.save(id,{loaded:latest,
  decision:{ok:true,status:'SCOPED_COMMIT_READY',proposed:proposal},mode:'P1_TWO_ROUND_DOM_BOOTSTRAP'});
 await report('RAW_HANDOFF_BOOTSTRAP_COMMITTED',id,{
  count:saved.count,added:saved.added,sha256:saved.sha256,
  scope:'TWO_ROUND_SCOPED_DOM',historyCompleteness:'UNKNOWN',
  triggerKey:h.triggerKey,independentAcceptance:'NOT_CLAIMED'});
 const activated=await verifySavedAndPromote({session,store,loader,readStreamCompletion,
  report,root,collectSnapshot:null});
 return {...activated,bootstrapCount:saved.count,bootstrapSha256:saved.sha256,
  bootstrapDigest:result.evidence.twoRoundDigest,bootstrapAt:now};
}
