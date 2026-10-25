// Read-only DOM extractor. No L0, no filesystem, no page interaction.
const blocks=new Set(['P','H1','H2','H3','H4','H5','H6','LI','PRE','BLOCKQUOTE','TR']);
const skip=new Set(['SCRIPT','STYLE','BUTTON','SVG','CANVAS','NOSCRIPT','TEMPLATE']);
const attrs=n=>{const a=n?.attributes??[],o={};for(let i=0;i<a.length;i+=2)o[a[i]]=a[i+1];return o};
function visit(node,pred,out=[],parents=[]){
 if(pred(node,parents))out.push({node,parents});
 for(const c of node.children||[])visit(c,pred,out,[...parents,node]);
 return out;
}
function userText(n){
 if(n.nodeType===3)return n.nodeValue||'';
 if(skip.has(n.nodeName))return '';
 if(n.nodeName==='BR')return '\n';
 return (n.children||[]).map(userText).join('');
}
function assistantText(n){
 if(n.nodeType===3)return n.nodeValue||'';
 if(skip.has(n.nodeName))return '';
 const a=attrs(n);
 if(a['aria-hidden']==='true'||(' '+(a.class||'')+' ').includes(' sr-only '))return '';
 if(n.nodeName==='BR')return '\n';
 const body=(n.children||[]).map(assistantText).join('');
 if(n.nodeName==='LI')return '- '+body+'\n';
 if(blocks.has(n.nodeName)||a['data-d-component']==='text'||a['data-d-component']==='title')return body+'\n';
 return body;
}
export function readVisible(root){
 const found=visit(root,n=>/:(user|assistant)$/.test(attrs(n)['data-content-search-unit-key']||''));
 const result=[];
 for(const {node,parents} of found){
  const a=attrs(node),role=a['data-content-search-unit-key'].split(':').at(-1);
  if(role!=='user'&&role!=='assistant')continue;
  const parts=visit(node,x=>{
   const b=attrs(x);
   return role==='user'?b['data-user-message-bubble']==='true':b['data-markdown-text-style']==='assistant-message';
  }).map(x=>x.node);
  if(!parts.length)continue;
  let text=parts.map(x=>role==='user'?userText(x):assistantText(x)).join('\n\n').replace(/\r\n/g,'\n').replace(/\u00a0/g,' ');
  if(role==='assistant')text=text.replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n');
  text=text.trim();
  if(!text)continue;
  const lineage=[node,...parents.slice().reverse()];
  const ids=lineage.map(n=>attrs(n)['data-chatgpt-search-message-ids']).find(Boolean);
  const turn=lineage.map(n=>attrs(n)['data-turn-key']).find(Boolean);
  const unique=ids?.split(/\s+/)[0]||turn||a['data-content-search-unit-key'];
  const finality=lineage.map(n=>attrs(n)['data-talvt-turn-state']).find(Boolean);
  result.push({key:role+':'+unique,role,text,final:finality==='complete'});
 }
 return result;
}
export const normalized=items=>JSON.stringify(items.map(m=>({key:m.key,role:m.role,text:m.text,final:m.final})));
export function stable(a,b){return a.length>0&&normalized(a)===normalized(b)}


