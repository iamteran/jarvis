$path = 'C:\Users\Brian\jarvis\.env'
$c = Get-Content $path -Raw
$c = $c -replace '(?m)^JARVIS_WHISPER_MODEL\s*=.*$', 'JARVIS_WHISPER_MODEL = base.en'
$c = $c -replace '(?m)^JARVIS_WHISPER_BEAM\s*=.*$', 'JARVIS_WHISPER_BEAM = 2'
$c = $c -replace '(?m)^JARVIS_SPEECH_RMS\s*=.*$', 'JARVIS_SPEECH_RMS = 0.008'
$c = $c -replace '(?m)^JARVIS_NOISE_MULT\s*=.*$', 'JARVIS_NOISE_MULT = 1.6'
$c = $c -replace '(?m)^JARVIS_NOISE_CEILING\s*=.*$', 'JARVIS_NOISE_CEILING = 0.006'
$c = $c -replace '(?m)^JARVIS_MIN_SPEECH_MS\s*=.*$', 'JARVIS_MIN_SPEECH_MS = 160'
$c = $c -replace '(?m)^JARVIS_SALVAGE_PEAK\s*=.*$', 'JARVIS_SALVAGE_PEAK = 0.006'
$c = $c -replace '(?m)^JARVIS_END_SILENCE_MS\s*=.*$', 'JARVIS_END_SILENCE_MS = 650'
$c = $c -replace '(?m)^JARVIS_MAX_UTTERANCE_S\s*=.*$', 'JARVIS_MAX_UTTERANCE_S = 8'
if ($c -notmatch 'JARVIS_LLM_TIMEOUT') {
  $c += "`r`n`r`n# One short attempt per model`r`nJARVIS_LLM_TIMEOUT = 12`r`nJARVIS_LLM_COOLDOWN = 60`r`n"
}
Set-Content -Path $path -Value $c -NoNewline
Get-Content $path | Select-String -Pattern 'WHISPER_MODEL|SPEECH_RMS|NOISE_MULT|END_SILENCE|MAX_UTTERANCE|LLM_TIMEOUT'
