; CineHaven installer – per-user, no admin rights needed.
Unicode true
!define APPNAME "CineHaven"
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SRCDIR
  !define SRCDIR "..\..\dist\CineHaven-${VERSION}-windows"
!endif
Name "${APPNAME} ${VERSION}"
OutFile "..\..\dist\CineHaven-${VERSION}-windows-setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "${SRCDIR}\CineHaven.ico"
UninstallIcon "${SRCDIR}\CineHaven.ico"

!include "MUI2.nsh"
!define MUI_ICON "${SRCDIR}\CineHaven.ico"
!define MUI_UNICON "${SRCDIR}\CineHaven.ico"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start CineHaven"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchCineHaven
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function LaunchCineHaven
  Exec '"$INSTDIR\bin\pythonw.exe" "$INSTDIR\app\cinehaven-launcher"'
FunctionEnd

Section "Install"
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\app"
  File /r "${SRCDIR}\*"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\bin\pythonw.exe" '"$INSTDIR\app\cinehaven-launcher"' "$INSTDIR\CineHaven.ico"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\bin\pythonw.exe" '"$INSTDIR\app\cinehaven-launcher"' "$INSTDIR\CineHaven.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\CineHaven.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "CineHaven"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "NoModify" 1
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
