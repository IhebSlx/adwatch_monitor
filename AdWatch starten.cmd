@echo off
rem ===========================================================================
rem  AdWatch starten -- Doppelklick-Starter fuer den lokalen Server.
rem
rem  Die wichtigste Regel steckt gleich im ersten Schritt: laeuft der Server
rem  schon, wird er NICHT neu gestartet. Ein Neustart mitten in einem Import
rem  sperrt die Datenbank und bricht den Lauf ab. Dann oeffnet dieses Skript
rem  nur das Browserfenster -- und sagt dazu, ob gerade ein Job laeuft.
rem
rem  Zweimal Doppelklick ist damit harmlos.
rem ===========================================================================
setlocal
set "APPDIR=%~dp0"
set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%/"
rem  Welches Python? Erst ein .venv im Projekt, dann die Conda-Umgebung,
rem  zuletzt das, was im PATH steht. So laeuft die Datei auch auf einem
rem  anderen Rechner, ohne dass jemand einen Pfad anpassen muss.
set "PYEXE=%APPDIR%.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%LOCALAPPDATA%\miniconda3\envs\adtracker\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

rem  Zweiter Aufruf dieser Datei mit --server: das ist das Serverfenster selbst.
if /i "%~1"=="--server" goto server

title AdWatch
call :pruefen
if errorlevel 2 goto laeuft_job
if not errorlevel 1 goto laeuft

echo Starte AdWatch ...
start "AdWatch" /d "%APPDIR%" "%~f0" --server

set /a versuche=0
:warten
set /a versuche+=1
call :pruefen
if errorlevel 2 goto bereit
if not errorlevel 1 goto bereit
if %versuche% geq 45 goto fehler
rem  ping statt timeout: timeout bricht ab, sobald die Eingabe

rem  umgeleitet ist (Aufruf aus einem Skript heraus).

ping -n 2 127.0.0.1 >nul 2>&1
goto warten

:bereit
start "" "%URL%"
exit /b 0

:laeuft
echo AdWatch laeuft bereits -- es wird nur das Browserfenster geoeffnet.
start "" "%URL%"
exit /b 0

:laeuft_job
echo AdWatch laeuft bereits, und gerade laeuft ein Job (Import oder Anreicherung).
echo Der Server wird deshalb NICHT neu gestartet.
start "" "%URL%"
exit /b 0

:fehler
echo.
echo AdWatch antwortet nach 45 Sekunden nicht.
echo Im Fenster "AdWatch" steht, woran es liegt.
echo.
pause
exit /b 1

rem ---------------------------------------------------------------------------
rem  Antwortet AdWatch auf diesem Port?  0 = ja  2 = ja, mit laufendem Job
rem  1 = nein.  Gefragt wird /health, nicht nur der Port: der Endpunkt
rem  antwortet auch im Zustand "degraded" mit 503, deshalb wird die Antwort
rem  gelesen statt auf den Statuscode geschaut.
rem ---------------------------------------------------------------------------
:pruefen
powershell -NoProfile -Command "try{$r=[Net.WebRequest]::Create('%URL%health');$r.Timeout=2000;try{$s=$r.GetResponse()}catch [Net.WebException]{$s=$_.Exception.Response};if(-not $s){exit 1};$t=(New-Object IO.StreamReader($s.GetResponseStream())).ReadToEnd();$s.Close();if($t -match '\"job_running\"\s*:\s*true'){exit 2};if($t -match '\"db\"'){exit 0};exit 1}catch{exit 1}"
exit /b %errorlevel%

rem ---------------------------------------------------------------------------
rem  Das Serverfenster. Es bleibt nach dem Ende offen, damit eine Fehlermeldung
rem  lesbar ist statt in einem zuklappenden Fenster zu verschwinden.
rem ---------------------------------------------------------------------------
:server
title AdWatch - Server  (dieses Fenster offen lassen)
cd /d "%APPDIR%"
"%PYEXE%" run.py serve
echo.
echo Der Server wurde beendet.
pause
exit /b 0
