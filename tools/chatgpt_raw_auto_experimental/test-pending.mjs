import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createStore} from './raw-auto-store.mjs';
const stage=await fs.mkdtemp(path.join(os.tmpdir(),'cm-c2-pending-'));
const root=path.join(stage,'pending-unit-'+process.pid);
const id='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const store=createStore(root,{write:true});
const m={key:'assistant:33333333-3333-4333-8333-333333333333',role:'assistant',text:'NETWORK_AND_DOM_VERIFIED',final:true};
const results={at:new Date().toISOString(),tests:[],scope:'SYNTHETIC_NON_AUTHORITATIVE_PENDING'};
const check=(name,ok)=>{if(!ok)throw Error('FAIL_'+name);results.tests.push(name)};
const first=await store.savePending(id,{verified:[m]});
check('SAVE_ONE_PENDING',first.added===1&&first.total===1);
const second=await store.savePending(id,{verified:[m]});
check('IDEMPOTENT_PENDING_NO_DUPLICATE',second.added===0&&second.total===1);
const file=path.join(root,'raw-auto-pending',id+'.json');
const payload=JSON.parse(await fs.readFile(file,'utf8'));
check('NOT_RAW_AUTHORITY',payload.authoritative===false&&payload.eligibleForL0===false&&payload.historyCompleteness==='UNKNOWN');
check('ONLY_PENDING_NOT_SESSION',!(await fs.access(path.join(root,'sessions',id+'.json')).then(()=>true).catch(()=>false)));
let conflict=false;
try{await store.savePending(id,{verified:[{...m,text:'MUTATED'}]})}catch(e){conflict=e.message==='PENDING_EXISTING_CONTENT_CONFLICT'}
check('TEXT_CONFLICT_HOLD',conflict);
let invalid=false;
try{await store.savePending(id,{verified:[{...m,final:false}]})}catch(e){invalid=e.message==='PENDING_MESSAGE_INVALID'}
check('INCOMPLETE_MESSAGE_HOLD',invalid);
check('PENDING_UNCHANGED_AFTER_CONFLICT',(await fs.readFile(file,'utf8')).includes('NETWORK_AND_DOM_VERIFIED'));
results.passed=results.tests.length;results.status='PASS';
// Synthetic regression only; no sensitive receipt is published.
console.log(JSON.stringify(results));

