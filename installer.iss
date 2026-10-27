; GoMentor Windows installer script (Inno Setup)
;
; Build:   ISCC.exe /DVersion=1.0.0 installer.iss
; Input:   dist\GoMentor\   (output of build.py --empty)
; Output:  Output\GoMentor-setup-<version>.exe
;
; The user can choose where to install. If it goes into Program Files, that
; folder is NOT writable; the app handles this itself and keeps its data under
; %LOCALAPPDATA%\GoMentor (see bundle.data_root).

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
; Same AppId as the project's former name (go-hoca): installing GoMentor
; upgrades an existing go-hoca install instead of sitting next to it.
AppId={{7F3A1C42-9B6E-4D2A-8E15-3C7A9D4E6B21}
AppName=GoMentor
AppVersion={#Version}
AppPublisher=Latif Akgun
DefaultDirName={autopf}\GoMentor
DefaultGroupName=GoMentor
OutputDir=Output
OutputBaseFilename=GoMentor-setup-{#Version}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit: the KataGo and Qt binaries are 64-bit
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Admin rights are requested, but the user can decline and install into their own folder.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; Show the uninstaller in Control Panel
UninstallDisplayName=GoMentor
UninstallDisplayIcon={app}\GoMentor.exe
; License page during setup. The app itself is MIT; the package also carries
; LGPL components (Qt, Qt's FFmpeg DLLs, edge-tts), LICENSE points to them,
; and the full texts are installed under {app}\LICENSES (they come with dist\GoMentor\*).
LicenseFile=LICENSE

; The app is bilingual, and so is the installer. English FIRST: international
; users see it as the default; Turkish users pick Turkish on the selection screen.
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

; Inno's OWN translated messages instead of hand-written text: with two
; languages, hand-written text stayed in the wrong language.
[Tasks]
Name: "desktop"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\GoMentor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\GoMentor"; Filename: "{app}\GoMentor.exe"
Name: "{group}\{cm:UninstallProgram,GoMentor}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GoMentor"; Filename: "{app}\GoMentor.exe"; Tasks: desktop

[Run]
Filename: "{app}\GoMentor.exe"; Description: "{cm:LaunchProgram,GoMentor}"; \
    Flags: nowait postinstall skipifsilent

; User data ({localappdata}\GoMentor) is deliberately NOT deleted: games,
; briefs and videos are the user's own records. The uninstaller removes the
; app, not its history.
[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
