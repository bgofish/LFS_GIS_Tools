@echo off

REM Activate Anaconda base environment
call %USERPROFILE%\anaconda3\Scripts\activate.bat

python "C:\Users\%username%\.lichtfeld\plugins\GIS_Tools\scripts\coord_converter.py"