$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
# Native disk evidence and test-only PSDrive adapter (SSH PS5 Free can be null).
$disk=New-Object IO.DriveInfo('C:\')
@{phase='disk';native_free=$disk.AvailableFreeSpace;psdrive_free=(Microsoft.PowerShell.Management\Get-PSDrive -Name C).Free} | ConvertTo-Json -Compress

$source=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'update_portable_local.ps1'))
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
@{phase='parse';version=$PSVersionTable.PSVersion.ToString();errors=@($errors | ForEach-Object {$_.Message})} | ConvertTo-Json -Compress
if($errors.Count){exit 2}
$nativeTemp=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Temp'
$parent=Join-Path $nativeTemp ('y2a-independent-review-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $parent | Out-Null
$script=Join-Path $parent 'candidate.ps1'
[IO.File]::WriteAllText($script,$source,(New-Object Text.UTF8Encoding($true)))
function Snapshot([string]$base){
 $m=@{}
 foreach($f in Get-ChildItem -LiteralPath $base -Recurse -Force){
  $rel=$f.FullName.Substring($base.Length+1)
  if($f.PSIsContainer){$m[$rel]='DIRECTORY'}else{$m[$rel]=(Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash}
 }
 return $m
}
function Same($a,$b){if($a.Count -ne $b.Count){return $false};foreach($k in $a.Keys){if(-not $b.ContainsKey($k) -or $a[$k] -ne $b[$k]){return $false}};return $true}
# Test-only process fixture: no real process stopped, started or inspected for modification.
function Get-CimInstance {param($ClassName) return @()}
function Copy-Item {
 param([string]$LiteralPath,[string]$Destination,[switch]$Recurse,[switch]$Force)
 $install=$Destination.StartsWith($global:testRoot+'\',[StringComparison]::OrdinalIgnoreCase)
 $rollback=$LiteralPath.StartsWith($global:testStage+'\backup-program\',[StringComparison]::OrdinalIgnoreCase)
 if($install -and -not $rollback){$global:installs++}
 if($install -and -not $rollback -and $global:scenario -eq 'partial-second' -and $global:installs -eq 2){
  New-Item -ItemType Directory -Path $Destination | Out-Null
  [IO.File]::WriteAllText((Join-Path $Destination 'partial.bin'),'partial failure')
  throw 'Injected partial second-path copy failure'
 }
 Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Recurse:$Recurse -Force:$Force
 if($install -and -not $rollback -and $global:scenario -eq 'post-verify' -and $global:installs -eq 4){[IO.File]::WriteAllText((Join-Path $Destination 'unexpected.bin'),'post-verify corruption')}
}
$results=@()
foreach($case in @('success','partial-second','post-verify','absent-original','frozen-layout')){
 $caseDir=Join-Path $parent $case;New-Item -ItemType Directory -Path $caseDir | Out-Null
 $root=Join-Path $caseDir 'dummy-install';$stage=Join-Path $caseDir 'stage';$bundle=Join-Path $caseDir 'incoming';$zip=Join-Path $caseDir 'dummy.zip'
 New-Item -ItemType Directory -Path $root,$bundle | Out-Null
 foreach($base in @($root,$bundle)){
  [IO.File]::WriteAllText((Join-Path $base 'Y2A-Auto.exe'),('dummy-not-executable '+$base))
  foreach($name in @('_internal','static','templates')){New-Item -ItemType Directory -Path (Join-Path $base $name) | Out-Null;[IO.File]::WriteAllText((Join-Path $base ($name+'\payload.txt')),('payload '+$base))}
 }
 if($case -eq 'absent-original'){Rename-Item -LiteralPath (Join-Path $root 'static') -NewName 'custom-static-data'}
 foreach($name in @('config.json','cookies.txt','cookies.db','tasks.db')){[IO.File]::WriteAllText((Join-Path $root $name),'dummy protected data')}
 foreach($name in @('downloads','fonts','logs')){New-Item -ItemType Directory -Path (Join-Path $root $name) | Out-Null;[IO.File]::WriteAllText((Join-Path $root ($name+'\keep.txt')),'dummy protected data')}
 if($case -eq 'frozen-layout'){
  foreach($name in @('static','templates')){Move-Item -LiteralPath (Join-Path $bundle $name) -Destination (Join-Path $bundle ('_internal\'+$name))}
  foreach($name in @('ffmpeg','acfunid')){New-Item -ItemType Directory -Path (Join-Path $bundle $name) | Out-Null;[IO.File]::WriteAllText((Join-Path $bundle ($name+'\distribution.txt')),'not installed')}
  [IO.File]::WriteAllText((Join-Path $bundle 'start.bat'),'not executed')
 }
 $before=Snapshot $root;$incoming=Snapshot $bundle
 if($case -eq 'frozen-layout'){foreach($key in @($incoming.Keys)){if(($key -split '\\')[0] -notin @('Y2A-Auto.exe','_internal')){$incoming.Remove($key)}}}
 Compress-Archive -Path (Join-Path $bundle '*') -DestinationPath $zip
 $sha=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
 $global:testRoot=$root;$global:testStage=$stage;$global:installs=0;$global:scenario=$case
 if($case -eq 'absent-original'){$global:scenario='post-verify'}
 $exception=$null
 try { & $script -root $root -stage $stage -zip $zip -zipSHA256 $sha | Out-Null }catch{$exception=$_.Exception.Message}
 $after=Snapshot $root
 $statusPath=Join-Path $stage 'status.json';$status=$null
 if(Test-Path -LiteralPath $statusPath){$status=Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json}
 $protected=$true;foreach($key in $before.Keys){if(($key -split '\\')[0] -notin @('Y2A-Auto.exe','_internal','static','templates')){if(-not $after.ContainsKey($key) -or $before[$key] -ne $after[$key]){$protected=$false}}}
 $program=$true;foreach($key in $incoming.Keys){if(-not $after.ContainsKey($key) -or $after[$key] -ne $incoming[$key]){$program=$false}}
 $restored=Same $before $after
 $pass=if($case -in @('success','frozen-layout')){($null -eq $exception -and $status.installed -and $program -and $protected)}else{($null -ne $exception -and -not $status.installed -and $status.rollback -and $restored -and $protected)}
 $results+=@{case=$case;passed=[bool]$pass;exception=$exception;status=$status;all_original_hashes_restored=$restored;protected_hashes_match=$protected;incoming_program_matches=$program;install_copies=$global:installs}
}
@{phase='sandbox';directory=$parent;candidate_sha256=(Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash;results=$results} | ConvertTo-Json -Depth 8 -Compress
if(@($results | Where-Object {-not $_.passed}).Count){exit 1}
