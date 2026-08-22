@echo off
setlocal
cd /d "%~dp0"
chcp 932 >nul
title VR クロマキーパネル

set "SCRIPT=vr_chroma_panel.py"
set "LOG=%~dp0error_log.txt"
set "PYTHONIOENCODING=cp932:replace"
set "PYTHONFAULTHANDLER=1"
set "PYTHONUNBUFFERED=1"

if not exist "%SCRIPT%" goto no_script

rem --- Python が「実際に動くか」で判定する ---
rem     where python は Microsoft Store のダミーにも反応してしまうため使わない
python -c "import sys" >nul 2>&1
if errorlevel 1 goto no_python

rem --- 必要なパッケージがあるか ---
python -c "import openvr, PIL" >nul 2>&1
if errorlevel 1 goto ask_setup
goto run

:ask_setup
echo.
echo ============================================
echo  初回セットアップ
echo ============================================
echo このツールには Python パッケージが 2 つ必要です。
echo.
echo     openvr  ... SteamVR を操作する
echo     pillow  ... 板の画像を作る
echo.
echo 次のコマンドを実行します:
echo     python -m pip install --user openvr pillow
echo.
set "ANSWER="
set /p ANSWER="インストールしてよければ y を入力して Enter (中止は Enter だけ): "
if /i not "%ANSWER%"=="y" goto canceled
echo.
echo インストール中です。少し待ってください...
python -m pip install --user openvr pillow
if errorlevel 1 goto pip_failed
python -c "import openvr, PIL" >nul 2>&1
if errorlevel 1 goto pip_failed

:run
echo ============================================
echo  VR クロマキーパネル v1.0.0
echo ============================================
echo  SteamVR を起動した状態で使ってください。
echo  操作ウィンドウを閉じるまで、この画面は残ります。
echo.
echo ==== %DATE% %TIME% ==== > "%LOG%"
python "%SCRIPT%" >> "%LOG%" 2>&1
set RC=%errorlevel%
echo.
type "%LOG%"
echo.
if "%RC%"=="0" goto ok
echo ============================================
echo  エラーが発生しました (終了コード %RC%)
echo  上の内容は error_log.txt にも保存されています
echo  作者に報告するときは、このファイルを添えてください
echo ============================================
goto end

:ok
echo 正常に終了しました。
goto end

:no_script
echo [エラー] %SCRIPT% が見つかりません。
echo run.bat と同じフォルダに置いてください。
echo zip を解凍せずに直接実行していないか確認してください。
echo.
echo 現在このフォルダにある .py ファイル:
dir /b *.py 2>nul
goto end

:no_python
echo [エラー] Python が使える状態になっていません。
echo.
echo  1. https://www.python.org/downloads/ から Python をインストール
echo  2. インストーラの最初の画面で
echo     「Add python.exe to PATH」に必ずチェックを入れる
echo  3. PC を再起動してから、もう一度 run.bat を実行
echo.
echo ※ Microsoft Store の画面が開く場合は、Windows の設定
echo    「アプリ実行エイリアス」で python を OFF にしてください。
goto end

:canceled
echo.
echo 中止しました。パッケージを入れないと起動できません。
goto end

:pip_failed
echo.
echo [エラー] パッケージのインストールに失敗しました。
echo ネットワーク接続、またはウイルス対策ソフトの設定を確認してください。
echo 手動で試す場合はコマンドプロンプトで:
echo     python -m pip install --user openvr pillow
goto end

:end
echo.
pause
endlocal
