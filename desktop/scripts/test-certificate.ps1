# A throwaway certificate to sign a test MSIX with (#61), on this Windows machine.
#
#   powershell -File desktop\scripts\test-certificate.ps1 -Out C:\somewhere
#
# Windows refuses to install an unsigned MSIX that starts an executable
# (0x80073D2B), so even a local test needs a signature. This makes one for
# CN=SpendTrackerTest -- the publisher electron-builder.yml names for test
# builds -- valid for 30 days, and writes:
#
#   test-signing.pfx   the key, for sign-msix.ps1
#   test-signing.pass  its password, random
#   test-signing.cer   the public half, to trust
#
# It does NOT trust the certificate. Trusting one is a security decision for
# the machine's owner, run as administrator:
#
#   Import-Certificate -FilePath test-signing.cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople
#
# and undone, when the test is over, with:
#
#   Get-ChildItem Cert:\LocalMachine\TrustedPeople | ? Subject -eq "CN=SpendTrackerTest" | Remove-Item
#
# The Store signs the real package itself (#63); nothing made here ships.
param([Parameter(Mandatory = $true)][string]$Out)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Out | Out-Null
$password = [Convert]::ToBase64String((1..24 | ForEach-Object { Get-Random -Maximum 256 }))
$cert = New-SelfSignedCertificate -Type Custom -Subject "CN=SpendTrackerTest" -KeyUsage DigitalSignature `
  -FriendlyName "Spend Tracker test package signing" -CertStoreLocation "Cert:\CurrentUser\My" `
  -NotAfter (Get-Date).AddDays(30) `
  -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
Export-PfxCertificate -Cert $cert -FilePath (Join-Path $Out "test-signing.pfx") -Password (ConvertTo-SecureString $password -AsPlainText -Force) | Out-Null
Export-Certificate -Cert $cert -FilePath (Join-Path $Out "test-signing.cer") | Out-Null
Set-Content -NoNewline -Path (Join-Path $Out "test-signing.pass") -Value $password
# Only the files are wanted: the copy in the user's store is removed again.
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
"made $Out\test-signing.pfx for CN=SpendTrackerTest, thumbprint $($cert.Thumbprint), until $($cert.NotAfter)"
