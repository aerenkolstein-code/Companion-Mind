import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {extractMessages,validateCommit} from './raw-auto-core.mjs';
import {evaluateRecovery} from './raw-auto-p1.mjs';
import {createStore} from './raw-auto-store.mjs';
import {stable,readVisible} from './raw-auto-dom.mjs';
const stage=await fs.mkdtemp(path.join(os.tmpdir(),'cm-c2-raw-auto-'));
const work=path.join(stage,'unit-fixture-'+process.pid);
await fs.mkdir(path.join(work,'sessions'),{recursive:true});
await fs.mkdir(path.join(work,'l0-checkpoints'),{recursive:true});
const tests=[],ensure=(name,assert)=>{if(!assert)throw Error('FAIL_'+name);tests.push(name)};
const ids=['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333'];
const convo='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const msg=(id,role,text,finished=true)=>({id,author:{role},content:{content_type:'text',parts:[text]},
 status:finished?'finished_successfully':'in_progress',end_turn:finished&&role==='assistant'});
const user=msg(ids[0],'user','USER_MESSAGE');
const assistant=msg(ids[1],'assistant','ASSISTANT_MESSAGE');
const parsed=extractMessages(JSON.stringify({messages:[user,assistant]}));
ensure('P2_PARSE_TWO_FINISHED',parsed.candidates.length===2&&parsed.candidates.every(m=>m.final));
ensure('P2_USER_COMPLETION_NO_END_TURN',parsed.candidates[0].final&&user.end_turn===false);
ensure('P2_INCOMPLETE_ASSISTANT_NOT_FINAL',extractMessages(JSON.stringify(msg(ids[1],'assistant','IN_PROGRESS',false))).candidates[0].final===false);
ensure('P2_SCHEMA_UNKNOWN_HOLD',extractMessages('not valid json').candidates.length===0);
const obs=parsed.candidates.map(x=>({...x}));
const store=createStore(work,{write:true}),loaded=await store.get(convo,{url:'https://chatgpt.com/c/'+convo,title:'Synthetic'});
const gate=validateCommit({state:loaded.record,network:parsed.candidates,observed:obs,archivedThrough:0,allowBootstrap:true});
ensure('P0_BOOTSTRAP_GATE',gate.ok&&gate.proposed.length===2);
const stored=await store.save(convo,{loaded,decision:gate,mode:'P2'});
ensure('P0_ATOMIC_READBACK',stored.readbackVerified&&stored.added===2);
const again=await store.get(convo,{url:'irrelevant'});
ensure('P0_RELOAD_COUNT',again.record.messages.length===2);
let blockedConcurrent=false;
try{await store.save(convo,{loaded,decision:gate,mode:'P2'})}catch(e){blockedConcurrent=/CONCURRENT_RAW_UPDATE/.test(e.message)}
ensure('P0_NO_STALE_WRITES',blockedConcurrent);
let blockedOverwrite=false;
const bad={...gate,proposed:[{...gate.proposed[0],text:'TAMPER'},...gate.proposed.slice(1)]};
try{await store.save(convo,{loaded:again,decision:bad,mode:'P2'})}catch(e){blockedOverwrite=/PREFIX_REWRITE/.test(e.message)||/NO_NEW_MESSAGES/.test(e.message)}
ensure('P0_NO_HISTORY_REWRITE',blockedOverwrite);
const next={key:'user:'+ids[2],role:'user',text:'NEW_AFTER_OUTAGE',final:true};
const dom=[...obs,next];
const recovery=evaluateRecovery({state:again.record,archivedThrough:0,first:dom,second:dom});
ensure('P1_SCOPED_RECOVERY',recovery.ok&&recovery.added===1);
const saved=await store.save(convo,{loaded:again,decision:recovery,mode:'P1'});
ensure('P1_SINGLE_WRITER',saved.added===1&&saved.count===3);
const rescan=await store.get(convo);
const noop=evaluateRecovery({state:rescan.record,archivedThrough:0,first:dom,second:dom});
ensure('P1_IDEMPOTENT_NOOP',noop.status==='P1_NO_CHANGE');
ensure('P1_UNANCHORED_HOLD',evaluateRecovery({state:again.record,archivedThrough:0,first:[next],second:[next]}).status==='P1_TAIL_ANCHOR_MISSING_HOLD');
ensure('P1_IN_PROGRESS_HOLD',evaluateRecovery({state:again.record,archivedThrough:0,first:[...obs,{...next,final:false}],second:[...obs,{...next,final:false}]}).status==='P1_FINALITY_UNKNOWN_HOLD');
ensure('P1_ARCHIVED_REVISION_HOLD',evaluateRecovery({state:again.record,archivedThrough:1,first:[{...obs[0],text:'DIFFERENT'},obs[1],next],second:[{...obs[0],text:'DIFFERENT'},obs[1],next]}).status==='P1_ARCHIVED_REVISION_HOLD');
ensure('P1_CAPTURE_GAP_HOLD',evaluateRecovery({state:{...again.record,captureGaps:[{status:'OPEN'}]},archivedThrough:0,first:dom,second:dom}).status==='CAPTURE_GAP_HOLD');
ensure('P1_UNKNOWN_GAP_HOLD',evaluateRecovery({state:{...again.record,captureGaps:[{status:'WHATEVER'}]},archivedThrough:0,first:dom,second:dom}).status==='CAPTURE_GAP_INVALID');
ensure('P1_UNSTABLE_SNAPSHOT_HOLD',evaluateRecovery({state:again.record,archivedThrough:0,first:obs,second:dom}).status==='P1_UNSTABLE_DOM_HOLD');
ensure('P0_MARKDOWN_VIEW_EXISTS',(await fs.readFile(path.join(work,'sessions',convo+'.md'),'utf8')).includes('NEW_AFTER_OUTAGE'));
let readOnlyBlocked=false;
try{await createStore(work,{write:false}).save(convo,{loaded:again,decision:recovery,mode:'P1'})}catch(e){readOnlyBlocked=e.message==='WRITE_NOT_ENABLED'}
ensure('P0_EXPLICIT_WRITE_REQUIRED',readOnlyBlocked);
const checkpoint={conversationId:convo,nextIndex:100,nextVolume:3};
await fs.writeFile(path.join(work,'l0-checkpoints',convo+'.json'),JSON.stringify(checkpoint));
let badCheckpoint=false;
try{await store.get(convo)}catch(e){badCheckpoint=/INVALID_L0_CHECKPOINT/.test(e.message)}
ensure('P0_INVALID_CHECKPOINT_HOLD',badCheckpoint);
const result={at:new Date().toISOString(),testCount:tests.length,status:'PASS',tests,source:'ISOLATED_SYNTHETIC_MESSAGES',fixtureRoot:'EPHEMERAL_TEMP_DIRECTORY',productionModified:false};
// No private receipt or real chat data is written by this public regression.
console.log(JSON.stringify(result));

