param(
 [ValidateSet('inspect','prepare','activate','status')][string]$Mode='inspect',
 [string]$New='',
 [switch]$AllowStaging
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$node='D:\Program Files\nodejs\node.exe'
$entry=Join-Path $root 'handoff-cli.mjs'
$registry=Join-Path $root 'handoff-registry.json'
if(!(Test-Path -LiteralPath $node -PathType Leaf)){throw 'NODE_RUNTIME_NOT_FOUND'}
if(!(Test-Path -LiteralPath $entry -PathType Leaf)){throw 'HANDOFF_CLI_NOT_DEPLOYED'}
if(!(Test-Path -LiteralPath $registry -PathType Leaf)){throw 'HANDOFF_REGISTRY_NOT_FOUND'}
if($New -and $New -notmatch '^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$'){
 throw 'INVALID_NEW_CONVERSATION_ID'
}
if($root -match 'handoff-v1-stage-' -and $Mode -eq 'prepare' -and !$AllowStaging){
 throw 'STAGING_PREPARE_REQUIRES_EXPLICIT_ALLOW_STAGING'
}
if($root -notmatch 'handoff-v1-stage-' -and $Mode -eq 'prepare'){
 $pidFile=Join-Path $root 'recorder.pid'
 if(!(Test-Path -LiteralPath $pidFile)){throw 'HANDOFF_SERVICE_NOT_RUNNING'}
 $targetPid=[int]([IO.File]::ReadAllText($pidFile).Trim())
 $runner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$targetPid)
 if(!$runner -or $runner.Name -ne 'node.exe' -or
  [string]$runner.CommandLine -notlike '*chat-raw-lite*raw-auto.mjs*' -or
  [string]$runner.CommandLine -notlike '*--handoff-registry=*'){
  throw 'HANDOFF_REGISTRY_MODE_NOT_RUNNING'
 }
}
$argsList=@($entry,$Mode)
if($New){$argsList+=('--new='+$New)}
& $node @argsList
exit $LASTEXITCODE
