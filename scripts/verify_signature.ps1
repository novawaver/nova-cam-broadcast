# Fails unless a file has a valid, timestamped Authenticode signature, so a
# signing problem can never reach users as a silently unsigned release.
param([Parameter(Mandatory)][string]$Path)

$ErrorActionPreference = 'Stop'

$signature = Get-AuthenticodeSignature -LiteralPath $Path
if ($signature.Status -ne 'Valid') {
    throw "$Path is not validly signed: $($signature.Status). $($signature.StatusMessage)"
}
if (-not $signature.TimeStamperCertificate) {
    throw "$Path has no timestamp, so its signature would expire with the certificate."
}
Write-Host "Signed by: $($signature.SignerCertificate.Subject)"
