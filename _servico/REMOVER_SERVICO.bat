@echo off
REM ============================================================
REM  JB Protecao - Remove o Servico do Windows
REM  CLIQUE COM BOTAO DIREITO -> "Executar como administrador"
REM ============================================================
setlocal

set NSSM=%~dp0nssm.exe
set SERVICO=JBProtecaoDashboard

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Execute como ADMINISTRADOR.
    pause
    exit /b 1
)

echo Parando e removendo o servico %SERVICO%...
"%NSSM%" stop %SERVICO%
"%NSSM%" remove %SERVICO% confirm

echo.
echo Servico removido. O dashboard nao sobe mais sozinho.
echo (Voce ainda pode rodar manual com: python run.py)
echo.
pause
