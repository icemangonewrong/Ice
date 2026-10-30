; Inno Setup script: makes IcePlex-Setup.exe (installs per-user, no admin needed).
#define AppVersion GetEnv("ICEPLEX_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B0E8F57-6C1A-4F7B-9E1C-3A6C1C2B5D10}
AppName=IcePlex
AppVersion={#AppVersion}
AppPublisher=Iceman
DefaultDirName={localappdata}\Programs\IcePlex
DefaultGroupName=IcePlex
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=IcePlex-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\IcePlex.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\IcePlex\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\IcePlex"; Filename: "{app}\IcePlex.exe"
Name: "{userdesktop}\IcePlex"; Filename: "{app}\IcePlex.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\IcePlex.exe"; Description: "Open IcePlex"; Flags: nowait postinstall skipifsilent
