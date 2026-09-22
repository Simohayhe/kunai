; VALORANT Account Manager のインストーラー定義。
; 単体では叩かない。tools/build_installer.py が dist\ValorantAccountManager.exe
; を確認した上で、バージョンを /DMyAppVersion=x.y.z として渡して ISCC を呼ぶ。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "VALORANT Account Manager"
#define MyAppPublisher "Simohaya"
#define MyAppExeName "ValorantAccountManager.exe"
#define MyAppURL "https://github.com/Simohayhe/valorant-account-manager"

[Setup]
AppId={{528FAFEE-97A8-40E7-8A4C-3467F42BF689}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=ValorantAccountManagerSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; 管理者権限なしでも入れられるようにしつつ、要る場合は選べるようにする
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; アンインストール時、保管庫 (アカウント/セッション) は残す。
; ユーザーデータを黙って消すインストーラーにはしない。
