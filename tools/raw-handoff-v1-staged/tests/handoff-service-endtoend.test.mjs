import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createStore} from '../raw-auto-store.mjs';
import {proposeHandoff} from '../handoff-core.mjs';
import {attemptHandoffBootstrap} from '../handoff-bootstrap-service.mjs';
import {createTargetLoader} from '../handoff-target-loader.mjs';
const seed=JSON.parse(await fs.readFile(new URL('../handoff-registry.json',import.meta.url),'utf8'));
const newId='11111111-2222-4333-8444-555555555555';
const trigger='user:aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const reply='assistant:bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb';
const url='https://chatgpt.com/g/'+seed.projectKey+'/c/'+newId;
const plan=proposeHandoff({
 registry:seed,tabs:[{type:'page',url,webSocketDebuggerUrl:'ws://offline'}],
 selectedNewId:newId,messages:[{key:trigger,text:'接续旧窗口',role:'user',final:false}],
 now:'2026-10-25T00:00:00Z'
});
if(!plan.ok)throw Error('FIXTURE_HANDOFF_NOT_VALID');
const rA={ok:true,topReached:true,bottomReached:true,observedAt:'2026-10-25T00:01:00Z',windowCount:1,
 messages:[{key:trigger,role:'user',text:'接续旧窗口',final:true},
 {key:reply,role:'assistant',text:'继续之前的工作，旧 RAW 继续保留。',final:true}]};
const rB={...rA,observedAt:'2026-10-25T00:01:35Z'};
let pass=0;
async function t(name,fn){try{await fn();pass++;console.log('PASS '+name)}catch(e){process.exitCode=1;console.error('FAIL '+name+': '+e.stack)}}
const fixture=path.join(path.dirname(fileURLToPath(import.meta.url)),'handoff-service-fixture-'+process.pid);
await fs.mkdir(fixture,{recursive:false});
try{
 await fs.writeFile(path.join(fixture,'handoff-registry.json'),JSON.stringify(plan.next,null,2)+'\n');
 const store=createStore(fixture,{write:true});
 const loader=createTargetLoader(path.join(fixture,'handoff-registry.json'));
 await loader.refresh();
 const session={conversationId:newId,tabUrl:url,title:'贝贝-7',cdp:{open:true}};
 const events=[],rounds=[rA,rB];
 const readStreamCompletion=async()=> 'COMPLETE';
 let called=0;
 const outcome=await attemptHandoffBootstrap({session,store,loader,readStreamCompletion,
  root:fixture,settleMs:0,collect:async()=>rounds[called++],report:async(s,id,d)=>events.push({s,id,d})});
 console.log('DEBUG_BOOTSTRAP_OUTCOME='+JSON.stringify(outcome));
 await t('automatic bootstrap returns success',async()=>assert.equal(outcome.ok,true));
 await t('round scanner called twice',async()=>assert.equal(called,2));
 const disk=JSON.parse(await fs.readFile(path.join(fixture,'sessions',newId+'.json')));
 await t('atomic new RAW has exact two messages',async()=>{assert.equal(disk.messages.length,2);assert.equal(disk.messages[0].text,'接续旧窗口');assert.equal(disk.messages[1].key,reply)});
 await t('bootstrapped JSON has scoped provenance',async()=>assert.equal(disk.messages[0].sourceEvidence.kind,'P1_TWO_ROUND_HANDOFF_DOM'));
 await t('markdown derived file exists',async()=>assert.equal((await fs.stat(path.join(fixture,'sessions',newId+'.md'))).size>10,true));
 const active=JSON.parse(await fs.readFile(path.join(fixture,'handoff-registry.json')));
 await t('new target promoted only after formal RAW readback',async()=>assert.equal(active.activeConversationId,newId));
 await t('old target still registered and unsealed',async()=>assert.equal(active.targets.find(x=>x.id===seed.activeConversationId).status,'LEGACY_RECOVERY_PENDING'));
 await t('preexisting older target also retained',async()=>assert.ok(active.targets.some(x=>x.id===seed.targets[0].id)));
 await t('prepare and complete receipts recorded',async()=>{const names=await fs.readdir(path.join(fixture,'evidence'));assert.ok(names.includes('handoff-002-NEW_RAW_VERIFIED.json'))});
 await t('L0 not run',async()=>assert.equal(await fs.stat(path.join(fixture,'l0-checkpoints')).then(()=>true).catch(()=>false),false));
 await t('service logs commit and activation',async()=>assert.deepEqual(events.map(x=>x.s),['RAW_HANDOFF_BOOTSTRAP_COMMITTED','RAW_HANDOFF_NEW_ACTIVE_VERIFIED']));
 const savedHash=(await fs.readFile(path.join(fixture,'sessions',newId+'.json'),'utf8'));
 await loader.refresh();
 const again=await attemptHandoffBootstrap({session,store,loader,readStreamCompletion,
  root:fixture,settleMs:0,collect:async()=>rA,report:async()=>{throw Error('UNEXPECTED_EVENT')}});
 await t('idempotent second attempt returns hold',async()=>assert.equal(again.status,'HANDOFF_NOT_PREPARED_HOLD'));
 await t('idempotent attempt preserves formal RAW',async()=>assert.equal((await fs.readFile(path.join(fixture,'sessions',newId+'.json'),'utf8')),savedHash));
}finally{
 await fs.rm(fixture,{recursive:true,force:true});
}
console.log(JSON.stringify({passed:pass,expected:13,allPassed:pass===13}));
if(pass!==13)process.exitCode=1;
