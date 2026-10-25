import {sha256} from './raw-auto-core.mjs';

// P1 DOM status alone is insufficient to certify an assistant's final answer.
// The caller MUST additionally require stream_status === COMPLETE.
export function p1TemporalGate({tracker,candidate,now=Date.now(),minimumMs=30000,minimumObservations=3}){
 if(!Array.isArray(candidate)||!candidate.length)return {ok:false,status:'P1_EMPTY_CANDIDATE_HOLD',tracker:null};
 if(!candidate.every(m=>typeof m.key==='string'&&typeof m.text==='string'&&m.text.length>0))
  return {ok:false,status:'P1_INVALID_CANDIDATE_HOLD',tracker:null};
 const fingerprint=sha256(JSON.stringify(candidate.map(m=>[m.key,m.text])));
 if(!tracker||tracker.fingerprint!==fingerprint){
  return {ok:false,status:'P1_SETTLING_HOLD',tracker:{fingerprint,firstSeenAt:now,seen:1},elapsedMs:0};
 }
 const next={...tracker,seen:tracker.seen+1};
 const elapsedMs=now-tracker.firstSeenAt;
 if(elapsedMs<minimumMs||next.seen<minimumObservations)
  return {ok:false,status:'P1_SETTLING_HOLD',tracker:next,elapsedMs};
 return {ok:true,status:'P1_TIME_STABLE',tracker:next,elapsedMs};
}
