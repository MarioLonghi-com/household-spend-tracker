# Sign a test MSIX with a certificate from test-certificate.ps1 (#61).
#
#   powershell -File desktop\scripts\sign-msix.ps1 -Package "desktop\dist\Spend Tracker 0.10.1.appx" -Pfx C:\st-test\test-signing.pfx
#
# With the Windows SDK's own signtool, not the copy electron-builder carries:
# that one fails on current Windows 11 builds with "A required function is not
# present" (found on the lab machine, #59). The SDK's is found under Windows
# Kits, or named with -SignTool. The password is read from the .pass file
# beside the .pfx, which test-certificate.ps1 writes.
#
# The Store signs the real package itself (#63).
param(
  [Parameter(Mandatory = $true)][string]$Package,
  [Parameter(Mandatory = $true)][string]$Pfx,
  [string]$SignTool
)
$ErrorActionPreference = "Stop"
if (-not $SignTool) {
  $SignTool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $SignTool -or -not (Test-Path $SignTool)) { throw "no Windows SDK signtool.exe found: install the SDK, or pass -SignTool" }
$password = (Get-Content ([IO.Path]::ChangeExtension($Pfx, ".pass")) -Raw).Trim()
& $SignTool sign /fd SHA256 /f $Pfx /p $password $Package
if ($LASTEXITCODE -ne 0) { throw "signtool failed ($LASTEXITCODE)" }
$said = Get-AuthenticodeSignature $Package
"$($said.Status): $($said.SignerCertificate.Subject) on $Package"
