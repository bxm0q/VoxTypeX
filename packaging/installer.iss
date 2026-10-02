#ifndef AppVersion
  #error AppVersion must be supplied by scripts/build-installer.ps1
#endif

[Setup]
AppId={{A1C96F62-35B1-4D73-8AC9-216FE73D26A8}
AppName=VoxTypeX
AppVersion={#AppVersion}
AppPublisher=bxm0q
AppPublisherURL=https://github.com/bxm0q/VoxTypeX
AppSupportURL=https://github.com/bxm0q/VoxTypeX/issues
DefaultDirName={localappdata}\Programs\VoxTypeX
DefaultGroupName=VoxTypeX
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
DisableDirPage=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousTasks=yes
CloseApplications=yes
RestartApplications=no
WizardStyle=modern dynamic windows11
WizardSizePercent=120,120
SetupIconFile={#AssetsDir}\VoxTypeX.ico
WizardImageFile={#AssetsDir}\wizard.png
WizardSmallImageFile={#AssetsDir}\small.png
WizardImageFileDynamicDark={#AssetsDir}\wizard.png
WizardSmallImageFileDynamicDark={#AssetsDir}\small.png
UninstallDisplayIcon={app}\VoxTypeX.ico
OutputDir={#OutputDir}
OutputBaseFilename=VoxTypeX-{#AppVersion}-setup-x64
Compression=lzma2
SolidCompression=yes
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
WelcomeLabel1=Установка VoxTypeX
WelcomeLabel2=VoxTypeX позволяет диктовать текст в других программах.%n%nВыберите папку установки и нужные параметры запуска.%n%nМодель распознавания скачивается при первой диктовке. Затем она используется локально.
SelectDirLabel3=Выберите папку для VoxTypeX. Настройки и модели хранятся отдельно в вашем профиле Windows.
SelectTasksLabel2=Выберите, как запускать VoxTypeX:
FinishedHeadingLabel=Установка завершена
FinishedLabel=После запуска значок VoxTypeX появится в трее рядом с часами.%n%nЧерез него можно открыть настройки, выбрать микрофон и назначить клавишу диктовки.

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"
Name: "startup"; Description: "Запускать VoxTypeX при входе в Windows"; Flags: unchecked

[Files]
Source: "{#SourceDir}\VoxTypeX.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\docs\MANUAL_SMOKE_TEST.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#SourceDir}\docs\SETTINGS.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#AssetsDir}\VoxTypeX.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VoxTypeX"; Filename: "{app}\VoxTypeX.exe"; WorkingDir: "{app}"; IconFilename: "{app}\VoxTypeX.ico"
Name: "{userdesktop}\VoxTypeX"; Filename: "{app}\VoxTypeX.exe"; WorkingDir: "{app}"; IconFilename: "{app}\VoxTypeX.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VoxTypeX"; ValueData: """{app}\VoxTypeX.exe"""; Flags: uninsdeletevalue; Tasks: startup
; Deselecting startup during a reinstall must also remove the previous entry.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "VoxTypeX"; Flags: deletevalue; Tasks: not startup

[InstallDelete]
Type: files; Name: "{userdesktop}\VoxTypeX.lnk"; Tasks: not desktopicon

[Run]
Filename: "{app}\VoxTypeX.exe"; Description: "Запустить VoxTypeX"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
