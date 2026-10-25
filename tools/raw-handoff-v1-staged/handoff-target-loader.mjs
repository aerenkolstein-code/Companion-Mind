import fs from 'node:fs/promises';
import path from 'node:path';
import {sha256} from './raw-auto-core.mjs';
import {assertRegistry,authorizedIds} from './handoff-core.mjs';

export function createTargetLoader(registryPath){
 const file=path.resolve(registryPath);
 let previousBytes=null,revision=-1,targets=new Set(),registry=null;
 return {
  get current(){return registry},
  get ids(){return new Set(targets)},
  get version(){return revision},
  async refresh(){
   const bytes=await fs.readFile(file,'utf8');
   if(bytes===previousBytes)return {changed:false,revision,ids:[...targets]};
   const next=assertRegistry(JSON.parse(bytes));
   const wanted=new Set(authorizedIds(next));
   if(next.revision<revision)throw Error('HANDOFF_REGISTRY_ROLLBACK_HOLD');
   if(next.revision===revision)throw Error('HANDOFF_REGISTRY_CHANGED_WITHOUT_REVISION_HOLD');
   // V1 never silently drops historical targets, even if a record was manually edited.
   const lost=[...targets].filter(x=>!wanted.has(x));
   if(lost.length)throw Error('HANDOFF_UNAUTHORIZED_TARGET_REMOVAL_HOLD');
   previousBytes=bytes;
   revision=next.revision;targets=wanted;registry=next;
   return {changed:true,revision,ids:[...targets],sha256:sha256(bytes),
    projectKey:next.projectKey,active:next.activeConversationId};
  }
 };
}
export function assertRegistryPath({entryDir,provided}){
 if(!provided||typeof provided!=='string')throw Error('HANDOFF_REGISTRY_PATH_REQUIRED');
 const actual=path.resolve(provided);
 const expected=path.resolve(entryDir,'handoff-registry.json');
 if(process.platform==='win32'?actual.toLowerCase()!==expected.toLowerCase():actual!==expected)
  throw Error('HANDOFF_REGISTRY_OUTSIDE_CONTROL_ROOT');
 return expected;
}
