@echo off
REM ============================================================
REM  JB Protecao - Reinicia o Servico (aplica mudancas no codigo)
REM  CLIQUE COM BOTAO DIREITO -> "Executar como administrador"
REM ============================================================
setlocal
set SERVICO=JBProtecaoDashboard

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Execute como ADMINISTRADOR.
    pause
    exit /b 1
)

echo Reiniciando o servico %SERVICO%...
net stop %SERVICO%
timeout /t 3 >nul
net start %SERVICO%

echo.
echo Servico reiniciado. Codigo novo carregado.
echo Dashboard: http://127.0.0.1:5000
echo.
pause
