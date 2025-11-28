@echo off
REM === Algemene config ===

REM NAO
set "NAO_IP=192.168.68.61"
set "NAO_PORT=9559"

REM Py2 (legacy NAO controller)
set "PYTHON2=C:\Python27\python.exe"
set "PY2_WEB_HOST=0.0.0.0"
set "PY2_WEB_PORT=5000"

REM Py3 (nieuwe behavior manager)
REM PYTHON3 mag ook gewoon "py" zijn als py3 de default is
set "PYTHON3=py"
set "PY3_WEB_HOST=0.0.0.0"
set "PY3_WEB_PORT=5001"
