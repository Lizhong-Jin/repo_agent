# Run from a source checkout; does not install Docker or change the default mode.
[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$Image = "repo-agent-sandbox:v1",
    [string]$Report = ".\artifacts\windows-docker.json",
    [switch]$Build,
    [switch]$ManualCtrlC
)
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Run this acceptance on Windows, not WSL." }
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
    Get-Command docker -ErrorAction Stop | Out-Null
    Get-Command git -ErrorAction Stop | Out-Null
    if ($Build) {
        & $Python -X utf8 -m sandbox.build --profile standard --image $Image
        if ($LASTEXITCODE -ne 0) { throw "Image build failed ($LASTEXITCODE)." }
    }
    & $Python -X utf8 -m pytest -q -ra tests/test_windows_files.py tests/test_host_file_contracts.py tests/test_docker_backend.py tests/test_windows_job.py tests/test_docker_acceptance.py tests/test_cancellation.py -k "not real_windows_docker and not real_outer_timeout and not real_docker_complete_acceptance and not real_docker_tui"
    if ($LASTEXITCODE -ne 0) { throw "Host contracts failed ($LASTEXITCODE)." }
    $acceptanceArgs = @("-X", "utf8", "-m", "sandbox.docker_acceptance", "--image", $Image, "--report", $Report)
    if ($ManualCtrlC) { $acceptanceArgs += "--manual-ctrl-c" }
    & $Python @acceptanceArgs
    if ($LASTEXITCODE -ne 0) { throw "Container acceptance failed; see $Report." }
    $oldWindowsTests = $env:RUN_WINDOWS_DOCKER_TESTS
    $oldImage = $env:SANDBOX_LSP_TEST_IMAGE
    $oldTuiReport = $env:DOCKER_TUI_REPORT
    try {
        $env:RUN_WINDOWS_DOCKER_TESTS = "1"
        $env:SANDBOX_LSP_TEST_IMAGE = $Image
        $env:DOCKER_TUI_REPORT = [IO.Path]::ChangeExtension($Report, "tui.json")
        & $Python -X utf8 -m pytest -q -ra tests/test_docker_acceptance.py -k real_docker_tui
        if ($LASTEXITCODE -ne 0) { throw "Real-container TUI acceptance failed." }
    } finally {
        $env:RUN_WINDOWS_DOCKER_TESTS = $oldWindowsTests
        $env:SANDBOX_LSP_TEST_IMAGE = $oldImage
        $env:DOCKER_TUI_REPORT = $oldTuiReport
    }
    Write-Host "Container checks passed. Report: $Report"
    Write-Host "Real terminal/TUI Ctrl+C, GPU and installation gates remain separate."
} finally {
    Pop-Location
}
