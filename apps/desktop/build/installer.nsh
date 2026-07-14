!include LogicLib.nsh

Var openReelInPlaceInstall

!macro OpenReelTaskKill IMAGE_NAME
  nsExec::ExecToLog `"$SYSDIR\cmd.exe" /C taskkill /F /T /IM "${IMAGE_NAME}"`
  Pop $0
  DetailPrint "taskkill ${IMAGE_NAME} returned $0"
!macroend

!macro OpenReelStopRuntimeProcesses
  DetailPrint "Closing OpenReel Studio runtime processes."
  !insertmacro OpenReelTaskKill "${APP_EXECUTABLE_FILENAME}"
  !insertmacro OpenReelTaskKill "openreel-api.exe"
  Sleep 800
!macroend

!macro OpenReelRemoveApplicationFiles
  SetOutPath "$TEMP"
  DetailPrint "Removing OpenReel Studio application files while keeping local data."

  Delete "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Delete "$INSTDIR\${UNINSTALL_FILENAME}"
  Delete "$INSTDIR\uninstallerIcon.ico"
  Delete "$INSTDIR\chrome_100_percent.pak"
  Delete "$INSTDIR\chrome_200_percent.pak"
  Delete "$INSTDIR\d3dcompiler_47.dll"
  Delete "$INSTDIR\dxcompiler.dll"
  Delete "$INSTDIR\dxil.dll"
  Delete "$INSTDIR\ffmpeg.dll"
  Delete "$INSTDIR\icudtl.dat"
  Delete "$INSTDIR\libEGL.dll"
  Delete "$INSTDIR\libGLESv2.dll"
  Delete "$INSTDIR\resources.pak"
  Delete "$INSTDIR\snapshot_blob.bin"
  Delete "$INSTDIR\v8_context_snapshot.bin"
  Delete "$INSTDIR\vk_swiftshader.dll"
  Delete "$INSTDIR\vk_swiftshader_icd.json"
  Delete "$INSTDIR\vulkan-1.dll"
  Delete "$INSTDIR\LICENSE.electron.txt"
  Delete "$INSTDIR\LICENSES.chromium.html"

  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir /r "$INSTDIR\swiftshader"
!macroend

!macro OpenReelDetectInPlaceInstall
  StrCpy $openReelInPlaceInstall "0"
  ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  ${OrIf} ${FileExists} "$INSTDIR\${UNINSTALL_FILENAME}"
  ${OrIf} ${FileExists} "$INSTDIR\data\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\storage\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\assets\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\config\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\logs\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\plugins\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\skills\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\workflow_templates\*.*"
    StrCpy $openReelInPlaceInstall "1"
  ${EndIf}
!macroend

!macro OpenReelBypassOldUninstallerForInPlaceInstall
  ${If} "$openReelInPlaceInstall" == "1"
    DetailPrint "Keeping runtime data in place and overwriting only application files."
    DeleteRegKey SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}"
    !ifdef UNINSTALL_REGISTRY_KEY_2
      DeleteRegKey SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY_2}"
    !endif
    DeleteRegKey SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}"
  ${EndIf}
!macroend

!macro OpenReelContinueAfterOldUninstallFailure
  ${If} ${Errors}
    DetailPrint "Previous uninstaller could not be launched; continuing with in-place overwrite."
    ClearErrors
  ${EndIf}

  ${If} $R0 != 0
    DetailPrint "Previous uninstaller returned $R0; continuing with in-place overwrite."
    StrCpy $R0 0
  ${EndIf}
!macroend

!macro customCheckAppRunning
  !insertmacro OpenReelStopRuntimeProcesses
  !insertmacro OpenReelDetectInPlaceInstall
  !insertmacro OpenReelRemoveApplicationFiles
  !insertmacro OpenReelBypassOldUninstallerForInPlaceInstall
!macroend

!macro customUnInstallCheck
  !insertmacro OpenReelContinueAfterOldUninstallFailure
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro OpenReelContinueAfterOldUninstallFailure
!macroend

!macro customRemoveFiles
  !insertmacro OpenReelStopRuntimeProcesses
  !insertmacro OpenReelRemoveApplicationFiles
  RMDir "$INSTDIR"
!macroend
