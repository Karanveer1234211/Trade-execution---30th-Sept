@echo off
REM ===================================================================
REM  daily_run.bat v5.3 - scheduled on weekdays after 18:00 IST (Task Scheduler)
REM  v5.3: step 1 sets aside stocks with old data gaps (--allow-partial, as every research build did);
REM        the watchlist step refuses to record a day whose entry session already opened.
REM  v5.2: step 1 gives the cache its window (--days 600, the cache's own incremental window;
REM        existing history is kept and only the tail is refetched). v5.1: progress lines.
REM
REM   1  DATA        run_daily.py: Kite cache -> quality gate -> leak gate
REM                  -> (old) panel -> daily report            [unchanged from v4]
REM   2  PANEL LIVE  open_close\panel_build.py (frozen v32) updates panel_live
REM   3  WATCHLIST   open_close\paper_pipeline.py watchlist: frozen D and the
REM                  ensemble score every stock; top 10 of each + Soul cards,
REM                  recorded in the hash-chained paper ledger BEFORE the open
REM   4  TRACK       open_close\paper_pipeline.py settle: settles finished paper
REM                  trades; rebuilds the tracker, the daily log and the report
REM
REM  v5 retires v4's steps 2-4 (Soul v3, signal_engine watchlists and resolve):
REM  they belong to the abandoned bracket target. Nothing is ever retrained
REM  during the paper test - the monthly_retrain task must stay disabled.
REM  Each step returns Python's own exit code; a failed step stops the rest.
REM  Output goes to the screen AND to <root>\logs\daily_YYYY-MM-DD.log
REM ===================================================================
setlocal
cd /d "%~dp0"
call "%~dp0env_daily.bat"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if "%CACHE_DAILY_ROOT%"=="" (
    echo.
    echo   CACHE_DAILY_ROOT is not set - env_daily.bat did not run.
    echo   Refusing to continue: there is no default cache path.
    exit /b 1
)
if not exist "%CACHE_DAILY_ROOT%" (
    echo   Cache root %CACHE_DAILY_ROOT% does not exist. Refusing to create one silently.
    exit /b 1
)
if not exist "%CACHE_DAILY_ROOT%\logs" mkdir "%CACHE_DAILY_ROOT%\logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%i"
set "LOG=%CACHE_DAILY_ROOT%\logs\daily_%TODAY%.log"
set "OC=%~dp0open_close"
set "ISINARG="
if exist "%CACHE_DAILY_ROOT%\paper\isin_bhavcopy.csv" set "ISINARG=--isin %CACHE_DAILY_ROOT%\paper\isin_bhavcopy.csv"
set "STARTED=%TIME%"
echo   Root: %CACHE_DAILY_ROOT%
echo   Log:  %LOG%

echo.
echo   [....................]   0%%  step 1 of 4: DATA - Kite cache, quality gate, report   (started %STARTED%, now %TIME%)
set "STEP=1/4 DATA (cache + quality gate + report)"
set "CMD=python run_daily.py --symbols-file watchlist.txt --only-stale --days 600 --allow-partial %*"
call :run
if errorlevel 1 goto :fail

if exist "%CACHE_DAILY_ROOT%\panel_live\panel.parquet" goto :live
echo.
echo   panel_live does not exist yet. One-time setup (see RUNBOOK.md):
echo   cd open_close  then  python paper_pipeline.py init-live --root %CACHE_DAILY_ROOT%
set "STEP=2/4 PANEL LIVE (missing - one-time setup not done)"
goto :fail

:live
pushd "%OC%"
set "PYTHONPATH=%~dp0"
echo.
echo   [#######.............]  35%%  step 2 of 4: PANEL LIVE - adding today to panel_live   (started %STARTED%, now %TIME%)
set "STEP=2/4 PANEL LIVE (incremental update)"
set "CMD=python panel_build.py --root %CACHE_DAILY_ROOT% --panel %CACHE_DAILY_ROOT%\panel_live --min-turnover 1e7 --allow-partial"
call :run
if errorlevel 1 goto :failp

echo.
echo   [##########..........]  50%%  step 3 of 4: WATCHLIST + SOUL CARDS - the longest step   (started %STARTED%, now %TIME%)
set "STEP=3/4 WATCHLIST + SOUL CARDS"
set "CMD=python paper_pipeline.py watchlist --root %CACHE_DAILY_ROOT% --skip-if-recorded %ISINARG%"
call :run
if errorlevel 1 goto :failp

echo.
echo   [##################..]  90%%  step 4 of 4: TRACK - settle, tracker, daily log, report   (started %STARTED%, now %TIME%)
set "STEP=4/4 TRACK (settle + tracker + daily log + report)"
set "CMD=python paper_pipeline.py settle --root %CACHE_DAILY_ROOT%"
call :run
if errorlevel 1 goto :failp
popd

echo.
echo   [####################] 100%%  DONE %TODAY%   (started %STARTED%, finished %TIME%)
echo   watchlist:  %CACHE_DAILY_ROOT%\paper\watchlists\  (latest WL_*.md)
echo   tracker:    %CACHE_DAILY_ROOT%\paper\tracker.md
echo   daily log:  %CACHE_DAILY_ROOT%\paper\daily_log.md
echo   report:     %CACHE_DAILY_ROOT%\paper\paper_report.md
echo   log:        %LOG%
endlocal
exit /b 0

:failp
popd
:fail
echo.
echo   STEP FAILED: %STEP% - the steps after it were skipped.
echo   See %LOG%
>>"%LOG%" echo STEP FAILED: %STEP%
endlocal
exit /b 1

:run
echo.
echo ===== %STEP%  [%TODAY% %TIME%] =====
>>"%LOG%" echo ===== %STEP%  [%TODAY% %TIME%] =====
where powershell >nul 2>&1
if %ERRORLEVEL%==0 (
    powershell -NoProfile -Command "$ErrorActionPreference='Continue'; %CMD% 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
) else (
    %CMD% >> "%LOG%" 2>&1
)
exit /b %ERRORLEVEL%
