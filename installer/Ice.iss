; Inno Setup script: makes Ice-Setup.exe (installs per-user, no admin needed).
#define AppVersion GetEnv("ICE_VERSION")
#if AppVersion == ""
  #define AppVersion "1.1.0"
#endif

[Setup]
; Same AppId as before the rename, so this upgrades the old IcePlex install.
AppId={{6B0E8F57-6C1A-4F7B-9E1C-3A6C1C2B5D10}
AppName=Ice
AppVersion={#AppVersion}
AppPublisher=Iceman
DefaultDirName={localappdata}\Programs\Ice
UsePreviousAppDir=no
DefaultGroupName=Ice
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Ice-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Ice.exe
CloseApplications=yes
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Clean up the old IcePlex install and shortcuts.
Type: filesandordirs; Name: "{localappdata}\Programs\IcePlex"
Type: filesandordirs; Name: "{userprograms}\IcePlex"
Type: files; Name: "{userdesktop}\IcePlex.lnk"

[Files]
Source: "..\dist\Ice\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Ice"; Filename: "{app}\Ice.exe"
Name: "{userdesktop}\Ice"; Filename: "{app}\Ice.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Ice.exe"; Description: "Open Ice"; Flags: nowait postinstall skipifsilent
