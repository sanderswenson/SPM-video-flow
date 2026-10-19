; Inno Setup script: per-user install (no admin prompt), so silent self-updates just work.
; Build: iscc /DAppVersion=0.2.0 packaging\installer.iss   (after PyInstaller has produced dist\vflow)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C3D2B61-5F0E-4E4A-9B5C-7A1D2E3F4B5C}
AppName=vflow
AppVersion={#AppVersion}
AppVerName=vflow {#AppVersion}
AppPublisher=SPM
DefaultDirName={localappdata}\Programs\vflow
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=vflow-{#AppVersion}-setup
SetupIconFile=vflow.ico
UninstallDisplayIcon={app}\vflow.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; Clear the previous version's bundle so stale libraries never linger after an update.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\vflow\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\vflow"; Filename: "{app}\vflow.exe"
Name: "{autodesktop}\vflow"; Filename: "{app}\vflow.exe"; Tasks: desktopicon

[Run]
; Not skipifsilent: after a silent self-update this relaunches the app.
Filename: "{app}\vflow.exe"; Description: "Launch vflow"; Flags: nowait postinstall
