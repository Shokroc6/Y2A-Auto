# LOCAL REVIEW CANDIDATE ONLY. No download, launch, remote execution or publication.
# Use only after independent review and Windows acceptance. Keep stage for diagnosis.
param(
 [Parameter(Mandatory=$true)][string]$root,
 [Parameter(Mandatory=$true)][string]$stage,
 [Parameter(Mandatory=$true)][string]$zip,
 [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$zipSHA256
)
$ErrorActionPreference='Stop'
$allowed=@('Y2A-Auto.exe','_internal','static','templates')
$root=[IO.Path]::GetFullPath($root).TrimEnd('\')
$stage=[IO.Path]::GetFullPath($stage).TrimEnd('\')
if($root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\') -or
   $stage -eq [IO.Path]::GetPathRoot($stage).TrimEnd('\') -or
   $stage -eq $root -or $stage.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase) -or
   $root.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe root/stage boundary'}
function AssertSafeTree([string]$path){
 # Reject junctions/symlinks in both ancestors and descendants before any writes.
 $parent=$path
 while($parent){
  if(Test-Path -LiteralPath $parent){
   if((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse path blocked'}
  }
  $parent=Split-Path -Parent $parent
 }
 if(Test-Path -LiteralPath $path){
  foreach($item in Get-ChildItem -LiteralPath $path -Force -Recurse){
   if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse tree blocked'}
  }
 }
}
function ProgramPath([string]$base,[string]$name){
 if($name -notin $allowed){throw 'Not an allowed program target'}
 $path=[IO.Path]::GetFullPath((Join-Path $base $name))
 if((Split-Path -Parent $path) -ne $base){throw 'Program path escaped boundary'}
 return $path
}
function AssertStopped {
 $p=@(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'Y2A-Auto.exe' -or ($_.ExecutablePath -and
  $_.ExecutablePath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase))
 })
 if($p.Count){throw 'Application running; close it without killing tasks'}
}
function Manifest([string]$base,[string[]]$names){
 $out=@{}
 foreach($name in $names){
  $path=ProgramPath $base $name
  if(-not(Test-Path -LiteralPath $path)){continue}
  $items=@(Get-Item -LiteralPath $path -Force)
  if($items[0].PSIsContainer){$items+=@(Get-ChildItem -LiteralPath $path -Force -Recurse)}
  foreach($item in $items){
   $rel=$item.FullName.Substring($base.Length+1)
   if($item.PSIsContainer){$out[$rel]='DIRECTORY'}
   else{$out[$rel]=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash}
  }
 }
 return $out
}
function EqualManifest($actual,$expected){
 if($actual.Count -ne $expected.Count){throw 'Manifest count mismatch'}
 foreach($key in $expected.Keys){
  if(-not $actual.ContainsKey($key) -or $actual[$key] -ne $expected[$key]){throw 'Manifest hash/path mismatch'}
 }
}
function AssertManifest([string]$base,$expected){
 AssertSafeTree $base
 EqualManifest (Manifest $base $allowed) $expected
}
function ProtectedManifest {
 # Snapshot all non-program paths, including config/cookies/db/downloads/custom fonts.
 $out=@{}
 foreach($item in Get-ChildItem -LiteralPath $root -Force -Recurse){
  $rel=$item.FullName.Substring($root.Length+1)
  if(($rel -split '\\')[0] -in $allowed){continue}
  if($item.PSIsContainer){$out[$rel]='DIRECTORY'}
  else{$out[$rel]=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash}
 }
 return $out
}
AssertSafeTree $root
AssertSafeTree $stage
AssertSafeTree $zip
if(-not(Test-Path -LiteralPath $root -PathType Container)){throw 'Installation missing'}
if(Test-Path -LiteralPath $stage){throw 'Stage exists; preserve previous backup'}
AssertStopped
if((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $zipSHA256){throw 'ZIP SHA256 mismatch'}
New-Item -ItemType Directory -Path $stage | Out-Null
$bundle=Join-Path $stage 'bundle'
$backup=Join-Path $stage 'backup-program'
$quarantine=Join-Path $stage 'quarantine'
Expand-Archive -LiteralPath $zip -DestinationPath $bundle
AssertSafeTree $bundle
# PyInstaller onedir stores static/templates under _internal; never synthesize root copies.
foreach($name in @('Y2A-Auto.exe','_internal')){
 if(-not(Test-Path -LiteralPath (ProgramPath $bundle $name))){throw 'Incomplete incoming program set'}
}
$allowed=@($allowed | Where-Object {Test-Path -LiteralPath (ProgramPath $bundle $_)})
if(-not(Test-Path -LiteralPath (ProgramPath $bundle 'Y2A-Auto.exe') -PathType Leaf)){throw 'Invalid EXE'}
foreach($name in @($allowed | Where-Object {$_ -ne 'Y2A-Auto.exe'})){
 if(-not(Test-Path -LiteralPath (ProgramPath $bundle $name) -PathType Container)){throw 'Invalid program directory'}
}
# Distribution-only resources are not installed; existing ffmpeg/acfunid remain protected.
foreach($item in Get-ChildItem -LiteralPath $bundle -Force){
 if($item.Name -notin $allowed -and $item.Name -notin @('ffmpeg','acfunid','start.bat') -and $item.Name -notmatch '^(README|LICENSE)(\..*)?$'){throw 'Unexpected package root path'}
}
$incomingManifest=Manifest $bundle $allowed
$originalManifest=Manifest $root $allowed
$protectedBefore=ProtectedManifest
# Bound required space before copying full program backup/replacement.
$required=0L
foreach($base in @($root,$bundle)){
 foreach($name in $allowed){
  $path=ProgramPath $base $name
  if(Test-Path -LiteralPath $path){
   $item=Get-Item -LiteralPath $path
   if($item.PSIsContainer){foreach($file in Get-ChildItem -LiteralPath $path -Force -Recurse -File){$required+=$file.Length}}
   else{$required+=$item.Length}
  }
 }
}
$drive=New-Object IO.DriveInfo([IO.Path]::GetPathRoot($stage))
$available=$drive.AvailableFreeSpace
if($available -isnot [long] -or $available -lt 0){throw 'Invalid available disk space'}
if($available -lt ($required+100MB)){throw 'Insufficient backup/replacement space'}
New-Item -ItemType Directory -Path $backup,$quarantine | Out-Null
foreach($name in $allowed){
 $dest=ProgramPath $root $name
 if(Test-Path -LiteralPath $dest){Copy-Item -LiteralPath $dest -Destination (ProgramPath $backup $name) -Recurse -Force}
}
$backupManifest=Manifest $backup $allowed
EqualManifest $backupManifest $originalManifest
AssertManifest $backup $backupManifest
AssertManifest $bundle $incomingManifest
@{incoming=$incomingManifest;original=$originalManifest;protected=$protectedBefore} | ConvertTo-Json -Depth 5 |
 Set-Content -LiteralPath (Join-Path $stage 'manifests.json') -Encoding UTF8
AssertStopped
$touched=@()
try {
 # BEGIN REPLACE: every replacement and post-write validation shares this transaction.
 foreach($name in $allowed){
  AssertSafeTree $root
  AssertSafeTree $bundle
  $dest=ProgramPath $root $name
  $touched+=$name  # Record before any operation that can partly succeed.
  if(Test-Path -LiteralPath $dest){
   Move-Item -LiteralPath $dest -Destination (Join-Path $quarantine ('original-'+$name))
  }
  Copy-Item -LiteralPath (ProgramPath $bundle $name) -Destination $dest -Recurse -Force
 }
 AssertManifest $root $incomingManifest
 EqualManifest (ProtectedManifest) $protectedBefore
 $result=@{installed=$true;rollback=$false;target=$root;backup=$backup;desktop_retested=$false;app_started=$false}
 $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'status.json') -Encoding UTF8
 $result | ConvertTo-Json
} catch {
 $failure=$_.Exception.GetType().Name
 $rollbackErrors=@()
 for($i=$touched.Count-1;$i -ge 0;$i--){
  $name=$touched[$i]
  try {
   AssertSafeTree $root
   AssertSafeTree $backup
   $dest=ProgramPath $root $name
   if(Test-Path -LiteralPath $dest){
    # Never restore into an existing directory (would nest). Retain failed incoming.
    Move-Item -LiteralPath $dest -Destination (Join-Path $quarantine ('failed-'+$name))
   }
   $saved=ProgramPath $backup $name
   if(Test-Path -LiteralPath $saved){Copy-Item -LiteralPath $saved -Destination $dest -Recurse -Force}
  } catch {$rollbackErrors+=$name}
 }
 try {
  AssertManifest $root $originalManifest
  EqualManifest (ProtectedManifest) $protectedBefore
 } catch {$rollbackErrors+='restore-verification'}
 $result=@{installed=$false;rollback=($rollbackErrors.Count -eq 0);rollback_errors=$rollbackErrors;failure=$failure;backup=$backup;desktop_retested=$false;app_started=$false}
 try {$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'status.json') -Encoding UTF8} catch {}
 $result | ConvertTo-Json
 throw 'Update failed; inspect rollback status and retained backup before any retry'
}
