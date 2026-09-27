# Single Windows release entry: install, --check, --recover and --uninstall.
# Requires Windows PowerShell 5.1+; never changes machine execution policy.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[string[]]$agentArguments = @($args)
if (($agentArguments -contains '--help') -or ($agentArguments -contains '-h')) {
    Write-Output @'
Usage: .\install_release.ps1 [--mode local|docker] [options]
       .\install_release.ps1 --uninstall [--dry-run] [--purge] [--remove-image]
Options: --data-dir DIR, --bin-dir DIR, --no-path, --offline, --skip-sandbox,
         --skip-toolchains, --check, --recover, --archive ZIP, --sha256 HASH.
First install defaults to local; reinstall preserves the recorded mode.
Bundled Python and wheels are used without a system Python or administrator rights.
Existing configuration is preserved. Core install failures roll back commands and venv.
--check / --recover / --uninstall do not create or download a Python runtime.
AGENT_PYTHON can select an absolute interpreter path (or 'system').
AGENT_PYTHON_CACHE overrides the persistent managed-runtime cache location.
'@
    exit 0
}

function Assert-NoReparse([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not allowed: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Assert-FileHash([string]$Path, [string]$Hash) {
    Assert-NoReparse $Path
    if (-not [IO.File]::Exists($Path)) { throw "Missing release file: $Path" }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Hash) {
        throw "SHA256 mismatch: $Path"
    }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitOperatingSystem -or
        ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') -or
        ($env:PROCESSOR_ARCHITEW6432 -eq 'ARM64')) {
        throw 'This release requires Windows x86_64.'
    }
    $agentRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    Assert-NoReparse $agentRoot
    $manifestFile = Join-Path $agentRoot 'release.json'
    Assert-NoReparse $manifestFile
    $manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 3 -or $manifest.name -ne 'repo-agent' -or
        $manifest.target -ne 'windows-x86_64') { throw 'Invalid Windows release manifest.' }
    $maintenance = ($agentArguments -contains '--check') -or
                   ($agentArguments -contains '--recover') -or
                   ($agentArguments -contains '--uninstall')
    $runtimeFiles = @()
    $seen = @{}
    foreach ($property in $manifest.files.PSObject.Properties) {
        $name = $property.Name
        if ($name -match '[\\:\x00]' -or $name.StartsWith('/') -or
            $name -eq 'release.json' -or $seen.ContainsKey($name) -or
            [string]$property.Value -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid release path or hash.' }
        foreach ($part in $name.Split('/')) {
            if (-not $part -or $part -in @('.', '..') -or $part -match '[ .]$' -or
                $part -match '[<>"|?*\x00-\x1f]' -or
                $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                throw "Unsafe release path: $name"
            }
        }
        $seen[$name] = $true
        if ($name.StartsWith('runtime/python/')) { $runtimeFiles += $property }
        $bootstrap = $name.StartsWith('runtime/') -or $name.StartsWith('cli/') -or
                     $name.StartsWith('host_support/') -or $name -eq 'install_release.ps1'
        if (-not $maintenance -or $bootstrap) {
            Assert-FileHash (Join-Path $agentRoot $name) ([string]$property.Value)
        }
    }
    foreach ($required in @('install_release.ps1', 'runtime/python/python.exe', 'runtime/target',
                            'runtime/python.lock', 'cli/release_install.py', 'cli/uninstall.py')) {
        if (-not $seen.ContainsKey($required)) { throw "Incomplete release: $required" }
    }
    if ((Get-Content -LiteralPath (Join-Path $agentRoot 'runtime/target') -Raw).Trim() -ne 'windows-x86_64') {
        throw 'Runtime target mismatch.'
    }
    $records = @(Get-Content -LiteralPath (Join-Path $agentRoot 'runtime/python.lock') |
        Where-Object { $_ -match '^windows-x86_64\s' })
    if ($records.Count -ne 1) { throw 'Missing or duplicate Python lock record.' }
    $record = $records[0] -split '\s+'
    if ($record.Count -ne 4 -or $record[2] -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid Python lock.' }
    $agentPython = Join-Path $agentRoot 'runtime/python/python.exe'
    if ($env:AGENT_PYTHON) {
        if ($env:AGENT_PYTHON -eq 'system') {
            $agentPython = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
        } else {
            if (-not [IO.Path]::IsPathRooted($env:AGENT_PYTHON)) { throw 'AGENT_PYTHON must be absolute.' }
            $agentPython = $env:AGENT_PYTHON
        }
    } elseif (-not $maintenance) {
        $cache = $env:AGENT_PYTHON_CACHE
        if (-not $cache) {
            $data = $env:XDG_DATA_HOME
            if (-not $data) { $data = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.local/share' }
            $cache = Join-Path $data 'repo-agent/runtimes'
        }
        $cache = [IO.Path]::GetFullPath($cache)
        Assert-NoReparse $cache
        [IO.Directory]::CreateDirectory($cache) | Out-Null
        $runtimeId = $record[1] + '-windows-x86_64-' + $record[2].Substring(0, 12)
        $destination = Join-Path $cache $runtimeId
        Assert-NoReparse $destination
        Assert-NoReparse ($destination + '.lock')
        $lock = [IO.File]::Open($destination + '.lock', [IO.FileMode]::OpenOrCreate,
                                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $stage = $null
        try {
            if (-not (Test-Path -LiteralPath $destination)) {
                $stage = Join-Path $cache ('.python-' + [Guid]::NewGuid().ToString('N'))
                [IO.Directory]::CreateDirectory($stage) | Out-Null
                foreach ($property in $runtimeFiles) {
                    $relative = $property.Name.Substring('runtime/'.Length)
                    $target = Join-Path $stage $relative
                    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
                    [IO.File]::Copy((Join-Path $agentRoot $property.Name), $target, $false)
                    Assert-FileHash $target ([string]$property.Value)
                }
                [IO.File]::WriteAllText((Join-Path $stage '.verified'), $record[2])
                [IO.Directory]::Move($stage, $destination)
                $stage = $null
            }
            $verified = Join-Path $destination '.verified'
            Assert-NoReparse $verified
            if (-not [IO.File]::Exists($verified) -or [IO.File]::ReadAllText($verified) -ne $record[2]) {
                throw 'Incomplete managed runtime; move it aside and retry.'
            }
            foreach ($property in $runtimeFiles) {
                Assert-FileHash (Join-Path $destination $property.Name.Substring('runtime/'.Length)) ([string]$property.Value)
            }
            $agentPython = Join-Path $destination 'python/python.exe'
        } finally {
            if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
            $lock.Dispose()
        }
    }
    Assert-NoReparse $agentPython
    # Single quotes survive Windows PowerShell 5.1 native argument marshalling.
    $probe = 'import sys,ssl,ctypes,venv,ensurepip; assert sys.platform == ''win32'' and sys.maxsize > 2**32 and sys.version_info >= (3,11)'
    $env:PYTHONUTF8 = '1'
    & $agentPython -X utf8 -I -B -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Python runtime check failed.' }
    Write-Output "Using Python: $agentPython"
    & $agentPython -X utf8 -E -s -B (Join-Path $agentRoot 'cli/release_install.py') @agentArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Release operation failed: ' + $_.Exception.Message)
    exit 1
}
