import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {sha256} from './raw-auto-core.mjs';
import {assertRegistry} from './handoff-core.mjs';
export async function writeHandoffRegistry({root,baseline,next,event,details={}}){
 const file=path.join(root,'handoff-registry.json');
 assertRegistry(next);
 if(typeof baseline!=='string'||next.revision!==JSON.parse(baseline).revision+1)throw Error('HANDOFF_REVISION_NOT_INCREMENTAL');
 const lock=file+'.handoff.lock';
 const handle=await fs.open(lock,'wx');
 let tmp=null;
 try{
  if(await fs.readFile(file,'utf8')!==baseline)throw Error('HANDOFF_REGISTRY_CONCURRENT_CHANGE');
  const payload=JSON.stringify(next,null,2)+'\n';
  tmp=file+'.tmp.'+crypto.randomBytes(8).toString('hex');
  const fh=await fs.open(tmp,'wx');
  try{await fh.writeFile(payload,'utf8');await fh.sync()}finally{await fh.close()}
  if(await fs.readFile(file,'utf8')!==baseline)throw Error('HANDOFF_REGISTRY_CONCURRENT_CHANGE');
  await fs.rename(tmp,file);tmp=null;
  if(await fs.readFile(file,'utf8')!==payload)throw Error('HANDOFF_REGISTRY_READBACK_FAILED');
  const receipt={format:'RAW-HANDOFF-V1',at:new Date().toISOString(),event,revision:next.revision,
   registrySha256:sha256(payload),authority:'LOCAL_RUNTIME_ONLY_NOT_CURRENT',
   active:next.activeConversationId,targets:next.targets.map(t=>({id:t.id,status:t.status})),
   independentAcceptance:'NOT_CLAIMED',...details};
  await fs.mkdir(path.join(root,'evidence'),{recursive:true});
  const target=path.join(root,'evidence','handoff-'+String(next.revision).padStart(3,'0')+'-'+event+'.json');
  await fs.writeFile(target,JSON.stringify(receipt,null,2)+'\n',{flag:'wx'});
  return {...receipt,receiptPath:target};
 }finally{
  if(tmp)await fs.unlink(tmp).catch(()=>{});
  await handle.close();await fs.unlink(lock).catch(()=>{});
 }
}
