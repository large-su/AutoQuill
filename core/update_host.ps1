$ErrorActionPreference = 'Stop'

# This file is a template.  updater.powershell_host_script prepends $config,
# decoded from UTF-8 JSON, before starting this script in a detached process.

function Get-ConfigValue([string]$Name, $Default = '') {
    $property = $config.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

$installer = [string](Get-ConfigValue 'installer')
$installDir = [string](Get-ConfigValue 'install_dir')
$logPath = [string](Get-ConfigValue 'log_path')
$relaunchExe = [string](Get-ConfigValue 'relaunch_exe')
$stagePath = [string](Get-ConfigValue 'stage_path')
$expectedHash = ([string](Get-ConfigValue 'expected_sha256')).Trim()
$expectedVersion = ([string](Get-ConfigValue 'expected_version')).Trim()
$runtimeUrl = ([string](Get-ConfigValue 'runtime_url')).Trim()
$dryRun = [bool](Get-ConfigValue 'dry_run' $false)
$restartOnly = [bool](Get-ConfigValue 'restart_only' $false)
$waitSeconds = [Math]::Max(0, [int](Get-ConfigValue 'wait_seconds' 60))
$oldPids = @()
foreach ($candidate in @((Get-ConfigValue 'pid' 0)) + @(Get-ConfigValue 'extra_pids' @())) {
    try { if ([int]$candidate -gt 0) { $oldPids += [int]$candidate } } catch { }
}
$oldPids = @($oldPids | Select-Object -Unique)

function Write-Log([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($logPath)) { return }
    try {
        $parent = [IO.Path]::GetDirectoryName($logPath)
        if ($parent) { [IO.Directory]::CreateDirectory($parent) | Out-Null }
        $line = ('{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Message)
        [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch { }
}

function ConvertTo-StateTable($Value) {
    $table = [ordered]@{}
    if ($null -ne $Value) {
        foreach ($property in $Value.PSObject.Properties) { $table[$property.Name] = $property.Value }
    }
    return $table
}

function Write-State([hashtable]$Changes) {
    if ([string]::IsNullOrWhiteSpace($stagePath)) { return $false }
    $directory = [IO.Path]::GetDirectoryName($stagePath)
    if ($directory) { [IO.Directory]::CreateDirectory($directory) | Out-Null }
    $lastError = $null
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $tmp = $stagePath + '.' + $PID + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
        try {
            $existing = $null
            if (Test-Path -LiteralPath $stagePath) {
                try { $existing = Get-Content -LiteralPath $stagePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $existing = $null }
            }
            $state = ConvertTo-StateTable $existing
            foreach ($key in $Changes.Keys) { $state[$key] = $Changes[$key] }
            $state['updated_at'] = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
            $json = $state | ConvertTo-Json -Depth 12
            [IO.File]::WriteAllText($tmp, $json, [Text.UTF8Encoding]::new($false))
            if (Test-Path -LiteralPath $stagePath) {
                try { [IO.File]::Replace($tmp, $stagePath, $null) }
                catch { Move-Item -LiteralPath $tmp -Destination $stagePath -Force }
            } else {
                Move-Item -LiteralPath $tmp -Destination $stagePath -Force
            }
            return $true
        } catch {
            $lastError = $_
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 2) { Start-Sleep -Milliseconds (200 * ($attempt + 1)) }
        }
    }
    Write-Log ('无法写入更新状态：' + $lastError.Exception.Message)
    return $false
}

function Mark-Failed([string]$Message) {
    Write-Log ('更新失败：' + $Message)
    # The original failure must not be hidden if a concurrent process holds stage.json.
    try { [void](Write-State @{ stage = 'failed'; phase = 'failed'; error = $Message; host_pid = $PID }) } catch { }
}

function Write-Ready {
    $readyFile = [string]$env:AQ_UPDATE_READY_FILE
    if ([string]::IsNullOrWhiteSpace($readyFile)) { return }
    try {
        $readyDir = [IO.Path]::GetDirectoryName($readyFile)
        if ($readyDir) { [IO.Directory]::CreateDirectory($readyDir) | Out-Null }
        $payload = @{ pid = $PID; token = [string]$env:AQ_UPDATE_TOKEN } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($readyFile, $payload, [Text.UTF8Encoding]::new($false))
    } catch { throw ('无法写入更新宿主就绪标记：' + $_.Exception.Message) }
}

function New-InstallerTempDirectory {
    if ([string]::IsNullOrWhiteSpace($stagePath)) { throw '缺少更新状态路径，无法创建安装程序临时目录' }
    $stageDirectory = [IO.Path]::GetDirectoryName($stagePath)
    if ([string]::IsNullOrWhiteSpace($stageDirectory)) { throw '更新状态路径没有父目录，无法创建安装程序临时目录' }
    $directory = Join-Path -Path $stageDirectory -ChildPath 'installer-temp'
    try {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        $probe = Join-Path -Path $directory -ChildPath ('.temp-probe-' + [Guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::WriteAllText($probe, '', [Text.UTF8Encoding]::new($false))
        } finally {
            if (Test-Path -LiteralPath $probe -PathType Leaf) {
                [IO.File]::Delete($probe)
            }
        }
    } catch {
        throw ('无法准备安装程序临时目录：' + $_.Exception.Message)
    }
    return $directory
}

function Wait-For-OldProcesses {
    Test-UpdateCancelled
    if ($oldPids.Count -eq 0) { return }
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    do {
        Test-UpdateCancelled
        $alive = @($oldPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    $alive = @($oldPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($alive.Count -gt 0) { throw ('等待主程序退出超时（进程 ' + ($alive -join ', ') + '）') }
}

function Test-UpdateCancelled {
    if ([string]::IsNullOrWhiteSpace($stagePath) -or -not (Test-Path -LiteralPath $stagePath -PathType Leaf)) { return }
    try {
        $current = Get-Content -LiteralPath $stagePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$current.stage -eq 'failed') { throw '更新已被主程序取消' }
    } catch [System.Management.Automation.RuntimeException] {
        # Preserve the deliberate cancellation exception; malformed transient state is retried.
        if ($_.Exception.Message -eq '更新已被主程序取消') { throw }
    } catch { }
}

function Test-SameInstallPath([string]$Left, [string]$Right) {
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    try {
        $normalLeft = [IO.Path]::GetFullPath($Left).TrimEnd('\\')
        $normalRight = [IO.Path]::GetFullPath($Right).TrimEnd('\\')
        return $normalLeft.Equals($normalRight, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Test-InstalledVersion {
    if ([string]::IsNullOrWhiteSpace($expectedVersion)) { return }
    $manifest = Join-Path -Path $installDir -ChildPath '_internal\\build_info.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw ('安装后未找到版本信息：' + $manifest) }
    try { $info = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw ('无法读取安装后的版本信息：' + $_.Exception.Message) }
    if ([string]$info.version -ne $expectedVersion) { throw ('安装版本不匹配：期望 ' + $expectedVersion + '，实际 ' + [string]$info.version) }
}

function Wait-For-Runtime([int]$RestartedPid) {
    if ([string]::IsNullOrWhiteSpace($expectedVersion)) { return }
    if ([string]::IsNullOrWhiteSpace($runtimeUrl)) { throw '缺少更新后运行状态地址，无法验证版本' }
    $deadline = (Get-Date).AddSeconds(60)
    $lastError = 'runtime did not become ready'
    do {
        try {
            # Windows PowerShell 5.1 misdecodes JSON UTF-8 without a charset header.
            # FastAPI correctly uses application/json; decode the original bytes explicitly.
            $response = Invoke-WebRequest -Uri $runtimeUrl -Method Get -UseBasicParsing -TimeoutSec 3
            $status = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
            $runningVersion = [string]$status.running_version
            $runningPid = 0
            try { $runningPid = [int]$status.running_pid } catch { }
            $runningInstallDir = [string]$status.running_install_dir
            if ($runningVersion -eq $expectedVersion -and $runningPid -gt 0 -and ($oldPids -notcontains $runningPid) -and (Test-SameInstallPath $runningInstallDir $installDir)) { return }
            $lastError = ('runtime reported version={0}, pid={1}, install_dir={2}' -f $runningVersion, $runningPid, $runningInstallDir)
        } catch { $lastError = $_.Exception.Message }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw ('重启后的版本验证失败：' + $lastError)
}

try {
    Write-Log '=== update host started ==='
    if (-not (Write-State @{ stage = 'applying'; phase = 'validating'; error = ''; host_pid = $PID })) {
        throw '无法保存更新状态，已取消安装'
    }

    if (-not $restartOnly) {
        if ([string]::IsNullOrWhiteSpace($installer) -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw ('未找到更新安装包：' + $installer) }
        if (-not $dryRun -and ([string]::IsNullOrWhiteSpace($installDir) -or -not [IO.Path]::IsPathRooted($installDir))) { throw '安装目录必须是绝对路径' }
        if (-not [string]::IsNullOrWhiteSpace($expectedHash)) {
            $actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
            if (-not $actualHash.Equals($expectedHash, [StringComparison]::OrdinalIgnoreCase)) { throw '更新安装包校验失败（SHA-256 不一致）' }
        }
    }

    # This is deliberately after validation: it proves the detached, real host owns the update.
    if (-not (Write-State @{ stage = 'applying'; phase = 'validated'; error = ''; host_pid = $PID })) {
        throw '无法保存已校验的更新状态，已取消安装'
    }
    $installerTempDirectory = ''
    if (-not $restartOnly -and -not $dryRun) {
        $installerTempDirectory = New-InstallerTempDirectory
        Write-Log ('installer temporary directory: ' + $installerTempDirectory)
    }
    Write-Ready
    if ($dryRun) {
        Write-Log 'dry run validated staged update; no install, restart, or cleanup'
        if (-not (Write-State @{ stage = 'staged'; phase = 'validated'; error = ''; host_pid = $PID })) { throw '无法保存验证完成状态' }
        return
    }

    Test-UpdateCancelled
    if (-not (Write-State @{ stage = 'applying'; phase = 'waiting'; error = ''; host_pid = $PID })) { throw '无法保存等待状态，已取消安装' }
    Wait-For-OldProcesses

    if (-not $restartOnly) {
        if (-not (Write-State @{ stage = 'applying'; phase = 'installing'; host_pid = $PID })) { throw '无法保存安装状态，已取消安装' }
        $installerLog = $logPath + '.installer.log'
        $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /LOG="{0}" /DIR="{1}"' -f $installerLog.Replace('"', ''), $installDir.Replace('"', '')
        Write-Log ('starting installer: ' + $installer + '; installer log: ' + $installerLog + '; temporary directory: ' + $installerTempDirectory)
        $originalTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
        $originalTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
        try {
            [Environment]::SetEnvironmentVariable('TEMP', $installerTempDirectory, 'Process')
            [Environment]::SetEnvironmentVariable('TMP', $installerTempDirectory, 'Process')
            $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
        } finally {
            [Environment]::SetEnvironmentVariable('TEMP', $originalTemp, 'Process')
            [Environment]::SetEnvironmentVariable('TMP', $originalTmp, 'Process')
        }
        Write-Log ('installer exit code: ' + $process.ExitCode)
        if ($process.ExitCode -ne 0) { throw ('安装程序退出码为 ' + $process.ExitCode) }
        Test-InstalledVersion
    }

    if ([string]::IsNullOrWhiteSpace($relaunchExe) -or -not (Test-Path -LiteralPath $relaunchExe -PathType Leaf)) { throw ('未找到重启程序：' + $relaunchExe) }
    if (-not (Write-State @{ stage = 'applying'; phase = 'relaunching'; host_pid = $PID })) { throw '无法保存重启状态，已取消重启' }
    $restartProcess = Start-Process -FilePath $relaunchExe -WorkingDirectory ([IO.Path]::GetDirectoryName($relaunchExe)) -PassThru -WindowStyle Normal
    Wait-For-Runtime $restartProcess.Id

    $done = @{ stage = 'done'; phase = 'complete'; error = ''; host_pid = $PID; restarted_pid = $restartProcess.Id }
    if (-not [string]::IsNullOrWhiteSpace($expectedVersion)) { $done['installed_version'] = $expectedVersion }
    if (-not (Write-State $done)) { throw '更新已完成，但无法保存完成状态' }
    if (-not $restartOnly) {
        try {
            Remove-Item -LiteralPath $installer -Force -ErrorAction Stop
            Write-Log '已删除完成安装后的安装包'
        } catch { Write-Log ('更新已完成，但无法删除安装包：' + $_.Exception.Message) }
    }
    Write-Log '=== update host completed ==='
} catch {
    Mark-Failed $_.Exception.Message
}
