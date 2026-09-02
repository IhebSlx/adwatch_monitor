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
rem  Welches Python? NICHT das erste, das existiert, sondern das erste, das
rem  AdWatch auch starten kann.
rem
rem  Die erste Fassung nahm das erste vorhandene. Im Projekt lag ein leeres
rem  .venv (Python 3.13, nur pip), also gewann das -- und der Start brach mit
rem  "No module named 'yaml'" ab, waehrend die Conda-Umgebung mit allen 92
rem  Paketen danebenstand. Vorhandensein ist eben kein Beweis fuer
rem  Brauchbarkeit; geprueft wird jetzt, ob der Kandidat die Abhaengigkeiten
rem  der App ueberhaupt importieren kann.
set "PYEXE="
call :taugt "%APPDIR%.venv\Scripts\python.exe"
if not defined PYEXE call :taugt "%LOCALAPPDATA%\miniconda3\envs\adtracker\python.exe"
if not defined PYEXE call :taugt "python"
if not defined PYEXE goto keinpython

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
rem  Taugt dieser Kandidat? Er muss existieren UND die Pakete haben, ohne die
rem  AdWatch gar nicht erst hochkommt. Ein Interpreter, der bloss da ist,
rem  reicht nicht -- genau daran ist der Start einmal gescheitert.
rem ---------------------------------------------------------------------------
:taugt
set "KANDIDAT=%~1"
if /i not "%KANDIDAT%"=="python" if not exist "%KANDIDAT%" exit /b 1
"%KANDIDAT%" -c "import yaml, fastapi, sqlalchemy, uvicorn" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYEXE=%KANDIDAT%"
exit /b 0

rem ---------------------------------------------------------------------------
rem  Kein brauchbares Python. Die Meldung sagt, wo gesucht wurde und was fehlt,
rem  statt den Nutzer mit einem Traceback allein zu lassen.
rem ---------------------------------------------------------------------------
:keinpython
echo.
echo Kein Python gefunden, das AdWatch starten kann.
echo.
echo Gesucht wurde in dieser Reihenfolge:
echo    1. %APPDIR%.venv\Scripts\python.exe
echo    2. %LOCALAPPDATA%\miniconda3\envs\adtracker\python.exe
echo    3. python (aus dem PATH)
echo.
echo Jeder Kandidat muss "import yaml, fastapi, sqlalchemy, uvicorn" koennen.
echo Fehlen Pakete, hilft in der richtigen Umgebung:  pip install -r requirements.txt
echo.
pause
exit /b 1

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
