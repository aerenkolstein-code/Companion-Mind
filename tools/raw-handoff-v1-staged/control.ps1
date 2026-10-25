param(
 [ValidateSet('start','stop','status','once')][string]$Mode='status',
 [string]$ConversationId='',
 [string]$AdditionalConversationId='',
 [switch]$RecoverIdle,
 [switch]$UseHandoffRegistry
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$node='D:\Program Files\nodejs\node.exe'
$entry=Join-Path $root 'raw-auto.mjs'
$pidPath=Join-Path $root 'recorder.pid'
$uuid='^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$'
function Read-Pid {
 if(!(Test-Path $pidPath)){return $null}
 try{return [int]([IO.File]::ReadAllText($pidPath).Trim())}catch{return $null}
}
function Owned([int]$id){
 try{
  $p=Get-CimInstance Win32_Process -Filter ("ProcessId="+$id) -ErrorAction Stop
  if(!$p -or $p.Name -ne 'node.exe'){return $false}
  return ([string]$p.CommandLine -like '*chat-raw-lite*raw-auto.mjs*')
 }catch{return $false}
}
function Status {
 $id=Read-Pid
 $active=($null -ne $id -and (Owned $id))
 $json=@(Get-ChildItem (Join-Path $root 'sessions') -Filter '*.json' -File -ErrorAction SilentlyContinue)
 $pending=@(Get-ChildItem (Join-Path $root 'raw-auto-pending') -Filter '*.json' -File -ErrorAction SilentlyContinue)
 [pscustomobject]@{
  service=if($active){'RUNNING_RAW_ONLY'}else{'STOPPED'}
  pid=if($active){$id}else{$null}
  conversations=$json.Count
  pending_conversations=$pending.Count
  l0_auto_from_raw='DISABLED'
  journal_auto='DISABLED'
  root=$root
 }|ConvertTo-Json -Compress
}
if($RecoverIdle -and $Mode -ne 'start'){throw 'RECOVER_IDLE_ONLY_ALLOWED_FOR_START'}
if($UseHandoffRegistry -and $Mode -ne 'start'){throw 'HANDOFF_REGISTRY_ONLY_ALLOWED_FOR_START'}
if($UseHandoffRegistry -and ($ConversationId -or $AdditionalConversationId)){throw 'HANDOFF_REGISTRY_MUTUALLY_EXCLUSIVE_WITH_FIXED_TARGETS'}
if($AdditionalConversationId -and ($AdditionalConversationId -notmatch $uuid -or $AdditionalConversationId -eq $ConversationId)){throw 'INVALID_SECOND_TARGET'}
if($Mode -eq 'status'){Status;exit 0}
if(!(Test-Path $entry)){throw 'AUTO_RAW_ENTRY_MISSING'}
if($Mode -eq 'once'){
 if($AdditionalConversationId){throw 'SECOND_TARGET_ONCE_NOT_SUPPORTED'}
 $argsList=@($entry,'--once','--duration-sec=12','--stdout')
 if($ConversationId){
  if($ConversationId -notmatch $uuid){throw 'INVALID_CONVERSATION_ID'}
  $argsList+=("--target="+$ConversationId)
 }
 & $node @argsList
 exit $LASTEXITCODE
}
if($Mode -eq 'stop'){
 $id=Read-Pid
 if($null -eq $id){'NOT_RUNNING';exit 0}
 if(!(Owned $id)){throw 'OWNERSHIP_NOT_PROVEN; no process stopped'}
 Stop-Process -Id $id -Force -ErrorAction Stop
 Remove-Item $pidPath -Force
 'STOPPED_OWNED_RAW_AUTO='+$id
 exit 0
}
if($Mode -eq 'start'){
 if(!$UseHandoffRegistry -and (!$ConversationId -or $ConversationId -notmatch $uuid)){throw 'EXPLICIT_CONVERSATION_ID_REQUIRED_FOR_WRITE'}
 if($UseHandoffRegistry){
  $registryFile=Join-Path $root 'handoff-registry.json'
  if(!(Test-Path -LiteralPath $registryFile -PathType Leaf)){throw 'HANDOFF_REGISTRY_NOT_FOUND'}
  $config=Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json
  if($config.format -ne 'raw-handoff-registry-v1' -or @($config.targets).Count -lt 1){throw 'HANDOFF_REGISTRY_INVALID'}
 }
 $id=Read-Pid
 if($null -ne $id -and (Owned $id)){'ALREADY_RUNNING='+$id;Status;exit 0}
 $all=@(Get-CimInstance Win32_Process -Filter "name='node.exe'" -ErrorAction SilentlyContinue|
  Where-Object {[string]$_.CommandLine -like '*chat-raw-lite*raw-auto.mjs*' -or [string]$_.CommandLine -like '*chat-raw-lite*raw-recorder.mjs*'})
 if($all.Count -gt 0){throw 'EXISTING_RAW_WRITER_OR_LEGACY_RECORDER_PRESENT_STOP'}
 $cdp=Invoke-RestMethod -Uri 'http://127.0.0.1:9333/json/version' -TimeoutSec 4
 if(!$cdp.webSocketDebuggerUrl){throw 'DEDICATED_CHROME_UNAVAILABLE'}
 $stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
 $stdout=Join-Path $root ('raw-auto-'+$stamp+'-stdout.log')
 $stderr=Join-Path $root ('raw-auto-'+$stamp+'-stderr.log')
 $writeTarget=if($UseHandoffRegistry){'--handoff-registry='+$registryFile}elseif($AdditionalConversationId){'--targets='+$ConversationId+','+$AdditionalConversationId}else{'--target='+$ConversationId}
 $startArgs=@($entry,'--write',$writeTarget,'--poll-ms=3000')
 if($RecoverIdle){$startArgs+='--recover-idle'}
 $p=Start-Process -FilePath $node -ArgumentList $startArgs -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
 [IO.File]::WriteAllText($pidPath,([string]$p.Id),[Text.UTF8Encoding]::new($false))
 Start-Sleep -Seconds 2
 if(!(Owned $p.Id)){throw 'RAW_AUTO_DID_NOT_STAY_RUNNING'}
 'STARTED_RAW_ONLY='+$p.Id
 'TARGET='+$ConversationId
 if($AdditionalConversationId){'SECOND_TARGET='+$AdditionalConversationId}
 if($UseHandoffRegistry){'HANDOFF_TARGET_REGISTRY='+$registryFile}
 'L0_JOURNAL_NOT_ENABLED'
 Status
}


