@echo off
REM run_now.bat - run the nightly job by hand, watch its progress, and keep the window open.
REM Double-click it, or run it from cmd. It calls daily_run.bat (the same job Task Scheduler runs).
REM Use it only after 18:00 (today's data final) or before 09:00 (yesterday's list still on time).
REM Never run it while the scheduled job is running.
title bigmove_deploy - daily run
echo.
echo   Running the daily job by hand. Progress lines show the step, the percent done and the time.
echo   Close this window only after RESULT appears.
call "%~dp0daily_run.bat" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo   RESULT: OK - the watchlist is in the paper\watchlists folder of your cache root
) else (
    echo   RESULT: FAILED - read the lines above; the log is in the logs folder of your cache root
)
echo.
pause
exit /b %RC%
