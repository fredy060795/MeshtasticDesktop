; Inno Setup 6 Skript fuer Meshtastic Desktop
; Wird von build.bat automatisch aufgerufen (ISCC.exe installer.iss)

#define AppName "Meshtastic Desktop"
#define AppVersion "1.2.0"
#define AppExe "MeshtasticDesktop.exe"

[Setup]
AppId={{6B8E0C1A-4E5D-4C59-9C3E-6D2F1A7B9E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Meshtastic Desktop (Community)
DefaultDirName={autopf}\Meshtastic Desktop
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=MeshtasticDesktop-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\MeshtasticDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\MeshtasticFlasher.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Meshtastic Flasher (offline)"; Filename: "{app}\MeshtasticFlasher.exe"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Chats/Einstellungen in %APPDATA%\MeshtasticDesktop bleiben bei Deinstallation erhalten.
