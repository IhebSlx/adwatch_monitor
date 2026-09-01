# Legt "AdWatch" auf den Desktop -- ein Doppelklick startet die App.
#
# Die Verknuepfung zeigt auf "AdWatch starten.cmd" im Projektordner. Sie
# kopiert nichts: wird das Skript spaeter geaendert, gilt die Aenderung sofort.
#
# Aufruf:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\desktop_verknuepfung.ps1

$projekt = Split-Path -Parent $PSScriptRoot
$ziel    = Join-Path $projekt 'AdWatch starten.cmd'
$icon    = Join-Path $projekt 'static\adwatch.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk     = Join-Path $desktop 'AdWatch.lnk'

if (-not (Test-Path $ziel)) { throw "Nicht gefunden: $ziel" }

$sh = New-Object -ComObject WScript.Shell
$v  = $sh.CreateShortcut($lnk)
$v.TargetPath       = $ziel
$v.WorkingDirectory = $projekt
$v.Description      = 'AdWatch im Browser oeffnen (startet den Server, falls noetig)'
# 7 = minimiert. Das Startfenster soll nur kurz blinken; das eigentliche
# Serverfenster oeffnet das Skript separat und in normaler Groesse.
$v.WindowStyle      = 7
if (Test-Path $icon) { $v.IconLocation = "$icon,0" }
$v.Save()

Write-Host "Verknuepfung angelegt: $lnk"
