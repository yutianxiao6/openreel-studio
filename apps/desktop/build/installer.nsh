!include LogicLib.nsh

Var openReelPreserveDir

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
  Delete "$INSTDIR\*.bin"
  Delete "$INSTDIR\*.dat"
  Delete "$INSTDIR\*.dll"
  Delete "$INSTDIR\*.json"
  Delete "$INSTDIR\*.pak"
  Delete "$INSTDIR\*.xml"
  Delete "$INSTDIR\LICENSE*"

  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir /r "$INSTDIR\swiftshader"
!macroend

!macro OpenReelBypassLegacyUninstaller
  ${If} ${isUpdated}
    DetailPrint "Bypassing legacy uninstaller; installer will overwrite application files in place."
    DeleteRegKey SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}"
    !ifdef UNINSTALL_REGISTRY_KEY_2
      DeleteRegKey SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY_2}"
    !endif
    DeleteRegKey SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}"
  ${EndIf}
!macroend

!macro OpenReelMoveRuntimeDir DIR_NAME
  ${If} ${FileExists} "$INSTDIR\${DIR_NAME}\*.*"
    DetailPrint "Preserving ${DIR_NAME}"
    ClearErrors
    Rename "$INSTDIR\${DIR_NAME}" "$openReelPreserveDir\${DIR_NAME}"
    ${If} ${Errors}
      DetailPrint "Could not preserve $INSTDIR\${DIR_NAME}."
      Abort "Could not preserve OpenReel Studio runtime data. Close the app and run the installer again."
    ${EndIf}
  ${EndIf}
!macroend

!macro OpenReelRestoreRuntimeDir DIR_NAME
  ${If} ${FileExists} "$openReelPreserveDir\${DIR_NAME}\*.*"
    DetailPrint "Restoring ${DIR_NAME}"
    ClearErrors
    Rename "$openReelPreserveDir\${DIR_NAME}" "$INSTDIR\${DIR_NAME}"
    ${If} ${Errors}
      DetailPrint "Could not restore $openReelPreserveDir\${DIR_NAME}; preserved copy remains beside the install directory."
      ClearErrors
    ${EndIf}
  ${EndIf}
!macroend

!macro OpenReelPreserveRuntimeDataBeforeUpgrade
  ${If} ${isUpdated}
    StrCpy $openReelPreserveDir "$INSTDIR.openreel-upgrade-backup"
    ${If} ${FileExists} "$openReelPreserveDir\*.*"
      StrCpy $openReelPreserveDir "$PLUGINSDIR\openreel-upgrade-backup"
    ${EndIf}

    CreateDirectory "$openReelPreserveDir"
    !insertmacro OpenReelMoveRuntimeDir "data"
    !insertmacro OpenReelMoveRuntimeDir "storage"
    !insertmacro OpenReelMoveRuntimeDir "config"
    !insertmacro OpenReelMoveRuntimeDir "logs"
    !insertmacro OpenReelMoveRuntimeDir "skills"
  ${EndIf}
!macroend

!macro OpenReelRestoreRuntimeDataAfterUpgrade
  ${If} "$openReelPreserveDir" != ""
  ${AndIf} ${FileExists} "$openReelPreserveDir\*.*"
    !insertmacro OpenReelRestoreRuntimeDir "data"
    !insertmacro OpenReelRestoreRuntimeDir "storage"
    !insertmacro OpenReelRestoreRuntimeDir "config"
    !insertmacro OpenReelRestoreRuntimeDir "logs"
    !insertmacro OpenReelRestoreRuntimeDir "skills"
    RMDir "$openReelPreserveDir"
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
  !insertmacro OpenReelPreserveRuntimeDataBeforeUpgrade
  !insertmacro OpenReelRemoveApplicationFiles
  !insertmacro OpenReelBypassLegacyUninstaller
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

!macro customInstall
  !insertmacro OpenReelRestoreRuntimeDataAfterUpgrade
!macroend
