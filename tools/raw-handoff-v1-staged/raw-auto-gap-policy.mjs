/**
 * L0-LITE gap policy v2 (2026-10-24).
 * OPEN = known unresolved capture defect => hard hold.
 * RESOLVED_SCOPED = known defect addressed with a scoped evidence receipt.
 * General non-exhaustiveness of visible DOM is a source limitation, not a capture defect.
 * Unknown gap statuses fail closed.
 */
export function classifyCaptureGaps(record){
 const gaps=record.captureGaps===undefined?[]:record.captureGaps;
 if(!Array.isArray(gaps))return {blocked:true,status:'CAPTURE_GAP_INVALID',openGaps:0,invalid:1};
 const allowed=new Set(['OPEN','RESOLVED_SCOPED']);
 let unresolved=0,invalid=0,resolved=0;
 for(const g of gaps){
  if(!g||!allowed.has(g.status)){invalid++;continue}
  if(g.status==='OPEN'){unresolved++;continue}
  if(typeof g.resolvedAt!=='string'||!g.resolutionEvidence||!g.resolutionScope){invalid++;continue}
  resolved++;
 }
 if(invalid)return {blocked:true,status:'CAPTURE_GAP_INVALID',openGaps:unresolved,invalid,resolved};
 if(unresolved)return {blocked:true,status:'CAPTURE_GAP_HOLD',openGaps:unresolved,invalid:0,resolved};
 return {blocked:false,status:'ELIGIBLE',openGaps:0,invalid:0,resolved};
}

