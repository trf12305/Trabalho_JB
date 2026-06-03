@echo off
REM ============================================================
REM  JB Protecao - Instalador do Servico do Windows (NSSM)
REM  CLIQUE COM BOTAO DIREITO -> "Executar como administrador"
REM ============================================================
setlocal

set NSSM=%~dp0nssm.exe
set PYTHON=C:\Users\paulo\AppData\Local\Programs\Python\Python312\python.exe
set APPDIR=C:\Users\paulo\Downloads\Projeto
set SERVICO=JBProtecaoDashboard

echo.
echo ============================================================
echo   Instalando servico: %SERVICO%
echo ============================================================
echo.

REM Verifica admin
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Este script PRECISA ser executado como ADMINISTRADOR.
    echo Clique com o botao direito neste arquivo e escolha
    echo "Executar como administrador".
    echo.
    pause
    exit /b 1
)

REM Remove servico antigo se existir
"%NSSM%" stop %SERVICO% >nul 2>&1
"%NSSM%" remove %SERVICO% confirm >nul 2>&1

REM Libera a porta 5000 (mata app.py manual que esteja rodando)
echo Liberando a porta 5000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 >nul

REM Cria o servico apontando para run.py (Waitress - producao)
"%NSSM%" install %SERVICO% "%PYTHON%" "%APPDIR%\run.py"
"%NSSM%" set %SERVICO% AppDirectory "%APPDIR%"
"%NSSM%" set %SERVICO% DisplayName "JB Protecao - Dashboard Financeiro"
"%NSSM%" set %SERVICO% Description "Dashboard JB Protecao + sync Siprov 09h/18h. Sobe sozinho no boot."

REM Inicio automatico no boot do Windows
"%NSSM%" set %SERVICO% Start SERVICE_AUTO_START

REM Reinicia automaticamente se cair (throttle 5s, delay 10s)
"%NSSM%" set %SERVICO% AppThrottle 5000
"%NSSM%" set %SERVICO% AppExit Default Restart
"%NSSM%" set %SERVICO% AppRestartDelay 10000

REM Logs do servico
"%NSSM%" set %SERVICO% AppStdout "%APPDIR%\_servico\servico_out.log"
"%NSSM%" set %SERVICO% AppStderr "%APPDIR%\_servico\servico_err.log"
REM Rotaciona log ao chegar em ~5MB
"%NSSM%" set %SERVICO% AppRotateFiles 1
"%NSSM%" set %SERVICO% AppRotateBytes 5000000

REM Inicia o servico agora
"%NSSM%" start %SERVICO%

echo.
echo ============================================================
echo   PRONTO! Servico instalado e iniciado.
echo.
echo   - Sobe sozinho toda vez que o PC ligar
echo   - Roda invisivel em background (nao precisa abrir nada)
echo   - Reinicia sozinho se cair
echo   - Sync Siprov continua nas 09h e 18h
echo.
echo   Dashboard: http://127.0.0.1:5000
echo   Logs:      %APPDIR%\_servico\servico_*.log
echo ============================================================
echo.
pause
