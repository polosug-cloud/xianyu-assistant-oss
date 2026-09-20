# 闲鱼助手 - 任务栏托盘控制（多账号版，Windows）
# 用法: powershell -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File xianyu_tray.ps1
# 菜单：账号管理(新增/勾选登录/删除) / 启动全部助手 / 打开管理页面 / 暂停全部助手 /
#       提示音开关(按账号) / 运行状态(按账号) / 营收状态(按账号) / 关于 / 退出托盘
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AccountsRoot = Join-Path $Root 'accounts'
$RegPath = Join-Path $AccountsRoot 'registry.json'
$BaseDataDir = Join-Path $Root 'data'          # 主账号(acc_main)数据目录=项目根 data（兼容现有）

# ---------- Python 定位 ----------
function Get-PythonExe {
    foreach ($cand in @((Join-Path $Root 'python\pythonw.exe'), (Join-Path $Root 'python\python.exe'))) {
        if (Test-Path $cand) { return $cand }
    }
    try {
        $pc = Get-Command 'python' -ErrorAction Stop
        $pw = Join-Path (Split-Path $pc.Source) 'pythonw.exe'
        if (Test-Path $pw) { return $pw }
        return $pc.Source
    } catch {}
    return 'python'
}
$Py = Get-PythonExe
$BaseEnv = @{
    PYTHONPATH            = Join-Path $Root '.pylib'
    PLAYWRIGHT_BROWSERS_PATH = Join-Path $Root '.browsers'
    XY_BROWSERS_DIR       = Join-Path $Root '.browsers'
    XY_SOUND_SYS          = Join-Path $Root 'sounds\Spring.ogg'
    XY_SOUND_MSG          = Join-Path $Root 'sounds\Bubble.ogg'
    XY_PYTHONIOENCODING   = 'utf-8'
}
foreach ($k in $BaseEnv.Keys) { if ($BaseEnv[$k]) { Set-Item -Path "env:$k" -Value $BaseEnv[$k] } }

# ---------- 账号注册表 ----------
function Load-Registry {
    if (-not (Test-Path $AccountsRoot)) { New-Item -ItemType Directory -Force -Path $AccountsRoot | Out-Null }
    if (Test-Path $RegPath) {
        try { return (Get-Content $RegPath -Raw -Encoding UTF8 | ConvertFrom-Json) } catch {}
    }
    # 首次：注册主账号（acc_main 使用现有根 data）
    $reg = [pscustomobject]@{ version = 1; accounts = @(
        [pscustomobject]@{ id = 'acc_main'; name = '主账号'; port = 8080; data_dir = $BaseDataDir; checked = $true }
    ) }
    Save-Registry $reg
    return $reg
}
function Save-Registry($reg) {
    # 过滤 null/非法条目，避免注册表出现空账号导致菜单出错或卡顿
    $reg.accounts = @(@($reg.accounts) | Where-Object { $null -ne $_ -and $_.id })
    $reg | ConvertTo-Json -Depth 5 | Set-Content -Path $RegPath -Encoding UTF8
}
function Get-Accounts($reg) {
    # 同样过滤 null/非法条目（历史文件可能残留）
    return @(@($reg.accounts) | Where-Object { $null -ne $_ -and $_.id })
}
function Get-Account($reg, $id) {
    foreach ($a in @($reg.accounts)) { if ($null -ne $a -and $a.id -eq $id) { return $a } }
    return $null
}
function Save-Account($reg, $acc) {
    if ($null -eq $acc -or -not $acc.id) { return }
    $list = @(@($reg.accounts) | Where-Object { $null -ne $_ -and $_.id })
    $found = $false
    for ($i = 0; $i -lt $list.Count; $i++) { if ($list[$i].id -eq $acc.id) { $list[$i] = $acc; $found = $true } }
    if (-not $found) { $list = @($list) + $acc }
    $reg.accounts = $list
    Save-Registry $reg
}
function Remove-Account($reg, $id) {
    $list = @($reg.accounts) | Where-Object { $null -ne $_ -and $_.id -and $_.id -ne $id }
    $reg.accounts = $list
    Save-Registry $reg
}

# ---------- 工具 ----------
# 端口探测：缓存监听端口表（枚举一次约 3ms，远快于逐个 Get-NetTCPConnection 的 ~150ms），
# 缓存 1 秒，避免菜单构建/轮询时反复查询造成卡顿。
$script:ListenCache = @{ ts = [datetime]::MinValue; ports = @() }
function Get-ListeningPortSet {
    $now = Get-Date
    if (($now - $script:ListenCache.ts).TotalSeconds -ge 1 -or $script:ListenCache.ports.Count -eq 0) {
        try {
            $set = New-Object 'System.Collections.Generic.HashSet[int]'
            foreach ($ep in [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()) {
                $null = $set.Add([int]$ep.Port)
            }
            $script:ListenCache = @{ ts = $now; ports = $set }
        } catch {
            # 枚举失败时退回逐个查询（慢但可用）
            $script:ListenCache = @{ ts = $now; ports = $null }
        }
    }
    return $script:ListenCache.ports
}
function Test-PortListening($port) {
    $set = Get-ListeningPortSet
    if ($null -ne $set) { return $set.Contains([int]$port) }
    try { return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) } catch { return $false }
}
function Is-PortFree($port) { return -not (Test-PortListening $port) }
function Get-FreePort($from) {
    $p = $from
    while (-not (Is-PortFree $p)) { $p++ }
    return $p
}
function New-FreePort($reg) {
    # 从 8080 起找第一个既未被占用、也未被注册表占用的端口（每账号一个固定端口）
    $used = @($reg.accounts | ForEach-Object { [int]$_.port })
    $p = 8080
    while ($p -lt 65535) {
        if ((Is-PortFree $p) -and ($used -notcontains $p)) { return $p }
        $p++
    }
    return 8080
}
function Account-Url($acc) { return "http://127.0.0.1:$($acc.port)" }
function Test-AccountOwnedByMe($acc) {
    # 判断端口上的实例是否**确属本账号**，避免误认别的副本（误停别人进程、或把别人账号名写进注册表）。
    # 判定顺序（全部为纯 ASCII 比较，规避中文路径经 HTTP/JSON 到 PowerShell 5.1 时的编码问题）：
    #   1) /api/status.account_id  == 本账号 id（托盘启动时会注入 XY_ACCOUNT_ID 环境变量）
    #   2) /api/status.db_b64      == 本地按 UTF-8 计算并 base64 后的数据库绝对路径
    #   3) 兜底：直接比较路径（两条记录都来自同一接口，仅作最后尝试）
    try {
        $base = Account-Url $acc
        $tok = (Invoke-RestMethod -Uri ($base + '/api/auth/token') -TimeoutSec 2).token
        if (-not $tok) { return $false }
        $st = Invoke-RestMethod -Uri ($base + '/api/status') -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 3
        if ($null -eq $st) { return $false }
        if ($st.account_id -and ([string]$st.account_id).Trim() -eq ([string]$acc.id).Trim()) { return $true }
        $dataDir = $acc.data_dir
        if (-not $dataDir) { $dataDir = Join-Path $AccountsRoot "$($acc.id)\data" }
        $expectPath = [System.IO.Path]::GetFullPath((Join-Path $dataDir 'xianyu.db'))
        if ($st.db_b64) {
            $expectB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($expectPath))
            return ([string]$st.db_b64 -eq $expectB64)
        }
        if ($st.db) {
            $actual = [System.IO.Path]::GetFullPath([string]$st.db)
            return ($actual.TrimEnd('\') -ieq $expectPath.TrimEnd('\'))
        }
        return $false
    } catch { return $false }
}
function Is-AccountRunning($acc) {
    # 端口监听 **且** 实例确属本账号 → 视为运行中；
    # 仅被其它程序/其它副本占用时返回 false（启动时会弹端口选择窗，绝不会误认）
    try {
        if (-not (Test-PortListening $acc.port)) { return $false }
        return (Test-AccountOwnedByMe $acc)
    } catch { return $false }
}
function Invoke-Api($acc, $Method, $Path, $Body = $null) {
    try {
        $base = Account-Url $acc
        $tok = (Invoke-RestMethod -Uri ($base + '/api/auth/token') -TimeoutSec 3).token
        $h = @{ Authorization = "Bearer $tok" }
        $url = $base + $Path
        if ($null -eq $Body) { return Invoke-RestMethod -Uri $url -Method $Method -Headers $h -TimeoutSec 6 }
        return Invoke-RestMethod -Uri $url -Method $Method -Headers $h -ContentType 'application/json' -Body ($Body | ConvertTo-Json) -TimeoutSec 6
    } catch { return $null }
}
function Show-Balloon($Title, $Text) {
    try { $script:NotifyIcon.ShowBalloonTip(3000, $Title, $Text, [System.Windows.Forms.ToolTipIcon]::Info) } catch {}
}
function Get-AccountDisplayName($acc, $runningKnown = $null) {
    # $runningKnown 传入已知运行状态时不再重复探测（菜单构建时避免重复请求造成卡顿）
    $running = if ($null -ne $runningKnown) { [bool]$runningKnown } else { Is-AccountRunning $acc }
    if ($running) {
        $a = Invoke-Api $acc 'GET' '/api/account'
        if ($null -ne $a -and $a.name) {
            if ($acc.name -ne $a.name) {
                # 把真实账户名写回注册表，暂停/未运行时也显示账户名
                $regN = Load-Registry
                $entry = Get-Account $regN $acc.id
                if ($entry) { $entry.name = $a.name; Save-Registry $regN }
            }
            return $a.name
        }
    }
    return $acc.name
}

# ---------- 操作队列：启动/暂停后台轮询，避免卡住托盘 UI ----------
$script:PendingOps = @{}
$script:OpTimer = $null
$script:SuppressClose = $false
$script:RowItems = @{}   # 账号行对象（id -> AccountRowItem），用于即时勾选反馈

function Init-OpTimer {
    if ($null -ne $script:OpTimer) { return }
    $t = New-Object System.Windows.Forms.Timer
    $t.Interval = 1000
    $t.Add_Tick({
        $now = Get-Date
        foreach ($id in @($script:PendingOps.Keys)) {
            $op = $script:PendingOps[$id]
            $a = Get-AccById $id
            if ($null -eq $a) { $script:PendingOps.Remove($id); continue }
            $running = Is-AccountRunning $a
            $elapsed = ($now - $op.started).TotalSeconds
            $wantRun = $op.action -eq 'start'
            $done = $false
            $listening = Test-PortListening $a.port
            if ($running -eq $wantRun) { $done = $true }
            elseif ($op.action -eq 'start' -and -not $running -and $listening -and $elapsed -gt 5) {
                # 端口已在监听但身份校验尚未通过（例如实例刚起、接口还没准备好）：
                # 也算启动成功，确保新账号一定能把管理页弹出来
                $done = $true
                $running = $true
            }
            elseif ($elapsed -gt 60) { $done = $true }
            elseif ($op.action -eq 'stop' -and $running -and $elapsed -gt 8) {
                Stop-PortOwner $a.port    # 退出接口卡住时按端口属主强杀一次
            }
            if ($done) {
                $script:PendingOps.Remove($id)
                $a.checked = $running
                Save-Account (Load-Registry) $a
                # 打开管理页：账号在运行，或端口已监听（后者保证新账号能弹出扫码页）
                if ($op.openPage -and ($running -or $listening)) { Start-Process (Account-Url $a) }
                $st = if ($running) { '已启动' } else { '已停止' }
                Show-Balloon '闲鱼助手' "账号「$(Get-AccountDisplayName $a $running)」（端口 $($a.port)）$st"
                Refresh-Menu
            }
        }
        if (@($script:PendingOps.Keys).Count -eq 0) { $script:OpTimer.Stop() }
    })
    $script:OpTimer = $t
}
function Enqueue-Op($id, $action, [bool]$openPage = $false) {
    Init-OpTimer
    if (-not $script:PendingOps.ContainsKey($id)) {
        $script:PendingOps[$id] = @{ action = $action; started = Get-Date; openPage = $openPage }
    }
    $script:OpTimer.Start()
}
function Stop-PortOwner($port) {
    try {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | ForEach-Object {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
            if ($proc -and $proc.Name -match '^python(w)?\.exe$' -and $proc.CommandLine -match 'app\.main') {
                Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
}
function Close-Menu {
    try { if ($NotifyIcon.ContextMenuStrip) { $NotifyIcon.ContextMenuStrip.Close() } } catch {}
}

# ---------- 实例启动/停止（立即返回，状态由后台队列确认） ----------
function Start-AccountInstance($acc, [bool]$OpenPage = $false) {
    if (Is-AccountRunning $acc) { if ($OpenPage) { Start-Process (Account-Url $acc) }; return $true }
    # 端口被占用：弹窗让用户选择
    if (-not (Is-PortFree $acc.port)) {
        $choice = Show-PortBusyForm $acc
        if ($choice -eq 'cancel') { return $false }
        if ($choice -eq 'auto') {
            $regNow = Load-Registry
            $acc.port = New-FreePort $regNow
            Save-Account (Load-Registry) $acc
        } elseif ($choice -eq 'specify') {
            $np = Show-PortInputForm $acc
            if (-not $np) { return $false }
            $acc.port = $np
            Save-Account (Load-Registry) $acc
        }
        if (-not (Is-PortFree $acc.port)) { Show-Balloon '闲鱼助手' '该端口仍被占用，请重试'; return $false }
    }
    # 准备账号数据目录
    $dataDir = $acc.data_dir
    if (-not $dataDir) { $dataDir = Join-Path $AccountsRoot "$($acc.id)\data" }
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $logDir = Join-Path (Split-Path $dataDir -Parent) 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    # 环境（临时设置后恢复）
    $saved = @{}
    @('PYTHONPATH','PLAYWRIGHT_BROWSERS_PATH','XY_BROWSERS_DIR','XY_DATA_DIR','XY_LOG_DIR','XY_SOUND_SYS','XY_SOUND_MSG','XY_PORT','XY_ACCOUNT_ID','PYTHONIOENCODING') | ForEach-Object { $saved[$_] = [Environment]::GetEnvironmentVariable($_) }
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $BaseEnv.PYTHONPATH)
    [Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $BaseEnv.PLAYWRIGHT_BROWSERS_PATH)
    [Environment]::SetEnvironmentVariable('XY_BROWSERS_DIR', $BaseEnv.XY_BROWSERS_DIR)
    [Environment]::SetEnvironmentVariable('XY_DATA_DIR', $dataDir)
    [Environment]::SetEnvironmentVariable('XY_LOG_DIR', $logDir)
    [Environment]::SetEnvironmentVariable('XY_SOUND_SYS', $BaseEnv.XY_SOUND_SYS)
    [Environment]::SetEnvironmentVariable('XY_SOUND_MSG', $BaseEnv.XY_SOUND_MSG)
    [Environment]::SetEnvironmentVariable('XY_PORT', "$($acc.port)")
    # 账号身份标识（纯 ASCII）：实例会在 /api/status 里回报，供托盘校验"这实例是不是我的账号"
    [Environment]::SetEnvironmentVariable('XY_ACCOUNT_ID', "$($acc.id)")
    [Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8')
    try {
        Start-Process -FilePath $Py -ArgumentList '-m','app.main' -WorkingDirectory $Root -WindowStyle Hidden
        Start-Sleep -Milliseconds 600
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
    Enqueue-Op $acc.id 'start' $OpenPage
    return $true
}
function Stop-AccountInstance($acc) {
    if (-not (Is-AccountRunning $acc)) {
        $acc.checked = $false
        Save-Account (Load-Registry) $acc
        return
    }
    Invoke-Api $acc 'POST' '/api/app/exit' | Out-Null
    Enqueue-Op $acc.id 'stop'
}

# ---------- 端口占用/输入弹窗 ----------
function Show-PortBusyForm($acc) {
    $result = 'cancel'
    $f = New-Object System.Windows.Forms.Form
    $f.Text = '端口被占用'
    $f.Size = New-Object System.Drawing.Size(420, 180)
    $f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Location = New-Object System.Drawing.Point(16, 14)
    $lbl.Size = New-Object System.Drawing.Size(380, 60)
    $lbl.Text = "账号「$($acc.name)」的端口 $($acc.port) 已被占用。`n请选择处理方式："
    $btn1 = New-Object System.Windows.Forms.Button
    $btn1.Text = '自动重新分配端口'; $btn1.Location = New-Object System.Drawing.Point(16, 88); $btn1.Size = New-Object System.Drawing.Size(150, 30)
    $btn1.Add_Click({ $script:portChoice = 'auto'; $f.Close() })
    $btn2 = New-Object System.Windows.Forms.Button
    $btn2.Text = '指定端口…'; $btn2.Location = New-Object System.Drawing.Point(176, 88); $btn2.Size = New-Object System.Drawing.Size(100, 30)
    $btn2.Add_Click({ $script:portChoice = 'specify'; $f.Close() })
    $btn3 = New-Object System.Windows.Forms.Button
    $btn3.Text = '取消'; $btn3.Location = New-Object System.Drawing.Point(286, 88); $btn3.Size = New-Object System.Drawing.Size(90, 30)
    $btn3.Add_Click({ $script:portChoice = 'cancel'; $f.Close() })
    $f.Controls.Add($lbl); $f.Controls.Add($btn1); $f.Controls.Add($btn2); $f.Controls.Add($btn3)
    $script:portChoice = 'cancel'
    $f.ShowDialog() | Out-Null
    return $script:portChoice
}
function Show-PortInputForm($acc) {
    Add-Type -AssemblyName Microsoft.VisualBasic
    $ans = [Microsoft.VisualBasic.Interaction]::InputBox("为「$($acc.name)」指定新端口（1024-65535）：", '指定端口', '8081')
    $n = 0
    if ([int]::TryParse($ans, [ref]$n) -and $n -ge 1024 -and $n -le 65535) { return $n }
    return 0
}

# ---------- 账号数据目录（新增） ----------
function New-AccountDir($id) {
    $d = Join-Path $AccountsRoot $id
    New-Item -ItemType Directory -Force -Path "$d\data" | Out-Null
    New-Item -ItemType Directory -Force -Path "$d\logs" | Out-Null
    return (Join-Path $d 'data')
}

# ---------- 托盘 UI ----------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 自绘账号行：一行内 勾选(运行中)/账号名/右侧删除，左击切换启动暂停，点“删除”弹确认
try {
    Add-Type -TypeDefinition @'
using System;
using System.Drawing;
using System.Windows.Forms;

public class AccountRowItem : ToolStripItem
{
    public string AccountLabel = "";
    public bool IsRunning = false;
    public event EventHandler ToggleClicked;
    public event EventHandler DeleteClicked;

    private Rectangle DelRect;
    private bool hoverDel;
    private bool mouseOver;
    private const int DelZone = 34;

    public AccountRowItem() : base()
    {
        // 固定尺寸：勾选框 + 标签 + 删除图标
        this.AutoSize = false;
        this.Size = new Size(176, 24);
        this.Padding = new Padding(0);
    }
    public override Size GetPreferredSize(Size constrainingSize) { return new Size(176, 24); }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        Font f = this.Font;
        if (f == null && this.Owner != null) f = this.Owner.Font;
        if (f == null) f = SystemFonts.MenuFont;
        Rectangle rect = new Rectangle(Point.Empty, this.Size);
        bool sel = this.Selected || mouseOver;
        using (SolidBrush bg = new SolidBrush(sel ? SystemColors.Highlight : SystemColors.Menu))
            g.FillRectangle(bg, rect);
        Color tc = sel ? SystemColors.HighlightText : SystemColors.MenuText;
        // 勾选框：始终画出方框；运行中打勾
        int box = 14;
        int bx = 8;
        int by = (this.Height - box) / 2;
        Color borderCol = sel ? tc : Color.FromArgb(120, 120, 120);
        using (Pen pen = new Pen(borderCol, 1f))
        {
            g.DrawRectangle(pen, bx, by, box, box);
        }
        if (IsRunning)
        {
            Color ck = sel ? tc : Color.FromArgb(0, 120, 215);
            using (Pen pen = new Pen(ck, 1.8f))
            {
                g.DrawLine(pen, bx + 3, by + box / 2, bx + 5, by + box - 3);
                g.DrawLine(pen, bx + 5, by + box - 3, bx + box - 2, by + 2);
            }
        }
        // 标签
        int lx = bx + box + 9;
        Rectangle lr = new Rectangle(lx, 0, Math.Max(8, this.Width - lx - DelZone - 8), this.Height);
        TextRenderer.DrawText(g, AccountLabel, f, lr, tc,
            TextFormatFlags.VerticalCenter | TextFormatFlags.Left | TextFormatFlags.EndEllipsis);
        // 右侧删除图标（X）：命中区 + 图形
        DelRect = new Rectangle(this.Width - DelZone - 3, 0, DelZone + 3, this.Height);
        int cx = this.Width - DelZone / 2 - 4;
        int cy = this.Height / 2;
        int r = 5;
        Color xc = hoverDel
            ? (sel ? Color.White : Color.FromArgb(210, 40, 40))
            : (sel ? Color.FromArgb(255, 210, 210) : Color.FromArgb(160, 40, 40));
        using (Pen pen = new Pen(xc, hoverDel ? 2.2f : 1.7f))
        {
            g.DrawLine(pen, cx - r, cy - r, cx + r, cy + r);
            g.DrawLine(pen, cx - r, cy + r, cx + r, cy - r);
        }
    }

    protected override void OnMouseEnter(EventArgs e) { mouseOver = true; base.OnMouseEnter(e); this.Invalidate(); }
    protected override void OnMouseLeave(EventArgs e) { mouseOver = false; hoverDel = false; base.OnMouseLeave(e); this.Invalidate(); }
    protected override void OnMouseMove(MouseEventArgs e)
    {
        bool h = DelRect.Contains(e.Location);
        if (h != hoverDel) { hoverDel = h; this.Invalidate(); }
        base.OnMouseMove(e);
    }
    protected override void OnMouseUp(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left)
        {
            if (DelRect.Contains(e.Location)) { if (DeleteClicked != null) DeleteClicked(this, EventArgs.Empty); }
            else { if (ToggleClicked != null) ToggleClicked(this, EventArgs.Empty); }
        }
        base.OnMouseUp(e);
    }
}
'@ -ReferencedAssemblies @('System.dll','System.Drawing.dll','System.Windows.Forms.dll') -ErrorAction Stop
} catch {
    # 编译失败（极端环境）时降级：Build-Menu 用普通行兜底，不影响其它功能
    Write-Output ("AccountRowItem 编译失败，使用普通行： " + $_.Exception.Message)
}

$NotifyIcon = New-Object System.Windows.Forms.NotifyIcon
$AppIco = Join-Path $Root '闲鱼助手.ico'
if (Test-Path $AppIco) {
    try { $NotifyIcon.Icon = New-Object System.Drawing.Icon($AppIco) }
    catch { $NotifyIcon.Icon = [System.Drawing.SystemIcons]::Application }
} else { $NotifyIcon.Icon = [System.Drawing.SystemIcons]::Application }
$NotifyIcon.Text = '闲鱼助手'
$NotifyIcon.Visible = $true

# 图片资源解码：编译型实现（毫秒级）
try {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

public static class XyDonationQr
{
    static readonly byte[] Seed = Encoding.ASCII.GetBytes("xianyu-assistant-asset-v1");
    const int Parts = 5;
    const int Header = 4 + 1 + 16 + 32 + 16;
    const int Iter = 20000;

    static byte[] ShaKs(byte[] seed, int n)
    {
        var outb = new byte[n];
        int off = 0; uint c = 0;
        using (var sha = SHA256.Create())
        {
            while (off < n)
            {
                var buf = new byte[seed.Length + 4];
                Buffer.BlockCopy(seed, 0, buf, 0, seed.Length);
                buf[seed.Length]     = (byte)(c >> 24);
                buf[seed.Length + 1] = (byte)(c >> 16);
                buf[seed.Length + 2] = (byte)(c >> 8);
                buf[seed.Length + 3] = (byte)c;
                var h = sha.ComputeHash(buf);
                int take = Math.Min(h.Length, n - off);
                Buffer.BlockCopy(h, 0, outb, off, take);
                off += take; c++;
            }
        }
        return outb;
    }

    static void XorInPlace(byte[] a, byte[] b)
    {
        for (int i = 0; i < a.Length; i++) a[i] ^= b[i];
    }

    /// <summary>从内置资源还原图片；失败返回 null</summary>
    public static byte[] Decode(string dir)
    {
        try
        {
            var raws = new byte[Parts][];
            for (int i = 0; i < Parts; i++)
            {
                string p = Path.Combine(dir, "qr.part" + i + ".dat");
                if (!File.Exists(p)) return null;
                raws[i] = File.ReadAllBytes(p);
                if (raws[i].Length <= Header) return null;
                if (raws[i][0] != (byte)'X' || raws[i][1] != (byte)'Y' || raws[i][2] != (byte)'Q') return null;
                if (raws[i][4] != (byte)i) return null;
            }
            bool v2 = raws[0][3] == (byte)'2';
            bool v1 = raws[0][3] == (byte)'1';
            if (!v2 && !v1) return null;

            var key = new byte[32];
            for (int i = 0; i < Parts; i++)
                for (int j = 0; j < 32; j++) key[j] ^= raws[i][21 + j];
            var iv = new byte[16];
            Buffer.BlockCopy(raws[0], 5, iv, 0, 16);

            var slices = new byte[Parts][];
            byte[] mac0 = null;
            for (int i = 0; i < Parts; i++)
            {
                var r = raws[i];
                int plen = r.Length - 69;
                var share = new byte[32]; Buffer.BlockCopy(r, 21, share, 0, 32);
                var mac = new byte[16]; Buffer.BlockCopy(r, 53, mac, 0, 16);
                var payload = new byte[plen]; Buffer.BlockCopy(r, 69, payload, 0, plen);
                var seed2 = new byte[Seed.Length + 1 + 32];
                Buffer.BlockCopy(Seed, 0, seed2, 0, Seed.Length);
                seed2[Seed.Length] = r[4];
                Buffer.BlockCopy(share, 0, seed2, Seed.Length + 1, 32);
                XorInPlace(payload, ShaKs(seed2, plen));
                if (i == 0) mac0 = mac;
                else
                {
                    using (var hm = new HMACSHA256(key))
                    {
                        var msg = new byte[1 + plen];
                        msg[0] = r[4];
                        Buffer.BlockCopy(payload, 0, msg, 1, plen);
                        var exp = hm.ComputeHash(msg);
                        for (int j = 0; j < 16; j++) if (exp[j] != mac[j]) return null;
                    }
                }
                slices[i] = payload;
            }

            int total = 0;
            for (int i = 0; i < Parts; i++) total += slices[i].Length;
            var ct = new byte[total];
            int o = 0;
            for (int i = 0; i < Parts; i++) { Buffer.BlockCopy(slices[i], 0, ct, o, slices[i].Length); o += slices[i].Length; }

            using (var hm = new HMACSHA256(key))
            {
                var msg = new byte[(v2 ? 16 : 17) + ct.Length];
                int mo = 0;
                if (!v2) msg[mo++] = 0;
                Buffer.BlockCopy(iv, 0, msg, mo, 16); mo += 16;
                Buffer.BlockCopy(ct, 0, msg, mo, ct.Length);
                var exp = hm.ComputeHash(msg);
                for (int j = 0; j < 16; j++) if (exp[j] != mac0[j]) return null;
            }

            var kdf = new Rfc2898DeriveBytes(key, iv, Iter, HashAlgorithmName.SHA256);
            if (v2)
            {
                var dk = kdf.GetBytes(32);
                var ksSeed = new byte[33];
                Buffer.BlockCopy(dk, 0, ksSeed, 0, 32);
                ksSeed[32] = 1;
                XorInPlace(ct, ShaKs(ksSeed, ct.Length));
            }
            else
            {
                XorInPlace(ct, kdf.GetBytes(ct.Length));
            }
            return ct;
        }
        catch { return null; }
    }
}
'@ -ReferencedAssemblies @('System.dll','System.Core.dll') -ErrorAction Stop
} catch {
    Write-Output ("图片资源解码类编译失败： " + $_.Exception.Message)
}
function Get-DonationQrBytes {
    # 优先编译型实现（毫秒级）；编译不可用时回退纯 PowerShell 实现
    try {
        if ('XyDonationQr' -as [type]) {
            $b = [XyDonationQr]::Decode((Join-Path $Root 'assets\donation'))
            if ($b -and $b.Length -gt 100) { return $b }
            return $null
        }
    } catch {}
    return (Get-DonationQrBytesPs)
}
function Get-DonationQrBytesPs {
    # 纯 PowerShell 回退实现（较慢，仅在编译不可用时使用）。
    try {
        $dir = Join-Path $Root 'assets\donation'
        $n = 5
        $raws = @()
        for ($i = 0; $i -lt $n; $i++) {
            $p = Join-Path $dir ('qr.part{0}.dat' -f $i)
            if (-not (Test-Path $p)) { return $null }
            $raws += ,([System.IO.File]::ReadAllBytes($p))
        }
        foreach ($r in $raws) {
            if ($r.Length -le 69 -or $r[0] -ne 0x58 -or $r[1] -ne 0x59 -or $r[2] -ne 0x51 -or ($r[3] -ne 0x31 -and $r[3] -ne 0x32)) { return $null }
        }
        if ((($raws | ForEach-Object { [int]$_[4] } | Sort-Object) -join ',') -ne '0,1,2,3,4') { return $null }

        # 1) XOR 分片合并出主密钥
        $key = New-Object byte[] 32
        foreach ($r in $raws) { for ($j = 0; $j -lt 32; $j++) { $key[$j] = $key[$j] -bxor $r[21 + $j] } }
        $iv = New-Object byte[] 16; [Array]::Copy($raws[0], 5, $iv, 0, 16)

        $seed = [System.Text.Encoding]::ASCII.GetBytes('xianyu-assistant-asset-v1')
        $slices = @{}; $mac0 = $null
        foreach ($r in $raws) {
            $idx = [int]$r[4]
            $share = New-Object byte[] 32; [Array]::Copy($r, 21, $share, 0, 32)
            $mac = New-Object byte[] 16;   [Array]::Copy($r, 53, $mac, 0, 16)
            $plen = $r.Length - 69
            $payload = New-Object byte[] $plen; [Array]::Copy($r, 69, $payload, 0, $plen)

            # keystream = SHA256(seed || index || share || big-endian counter) 连续拼接
            $ks = New-Object byte[] $plen
            $sha = [System.Security.Cryptography.SHA256]::Create()
            $off = 0; $c = 0
            while ($off -lt $plen) {
                $ms = New-Object System.IO.MemoryStream
                $ms.Write($seed, 0, $seed.Length)
                $ms.WriteByte([byte]$idx)
                $ms.Write($share, 0, 32)
                $cb = [BitConverter]::GetBytes([uint32]$c); [Array]::Reverse($cb)
                $ms.Write($cb, 0, 4)
                $h = $sha.ComputeHash($ms.ToArray()); $ms.Dispose()
                $take = [Math]::Min($h.Length, $plen - $off)
                [Array]::Copy($h, 0, $ks, $off, $take)
                $off += $take; $c++
            }
            $sha.Dispose()

            $sl = New-Object byte[] $plen
            for ($j = 0; $j -lt $plen; $j++) { $sl[$j] = $payload[$j] -bxor $ks[$j] }

            if ($idx -eq 0) { $mac0 = $mac }
            else {
                $hm = [System.Security.Cryptography.HMACSHA256]::new($key)
                $ms2 = New-Object System.IO.MemoryStream
                $ms2.WriteByte([byte]$idx); $ms2.Write($sl, 0, $sl.Length)
                $exp = $hm.ComputeHash($ms2.ToArray()); $hm.Dispose(); $ms2.Dispose()
                for ($j = 0; $j -lt 16; $j++) { if ($exp[$j] -ne $mac[$j]) { return $null } }
            }
            $slices[$idx] = $sl
        }

        # 2) 拼接密文并校验整体 HMAC
        $msAll = New-Object System.IO.MemoryStream
        for ($i = 0; $i -lt $n; $i++) { $msAll.Write($slices[$i], 0, $slices[$i].Length) }
        $ct = $msAll.ToArray(); $msAll.Dispose()
        $hm0 = [System.Security.Cryptography.HMACSHA256]::new($key)
        $ms3 = New-Object System.IO.MemoryStream
        $ms3.WriteByte(0); $ms3.Write($iv, 0, 16); $ms3.Write($ct, 0, $ct.Length)
        $exp0 = $hm0.ComputeHash($ms3.ToArray()); $hm0.Dispose(); $ms3.Dispose()
        for ($j = 0; $j -lt 16; $j++) { if ($exp0[$j] -ne $mac0[$j]) { return $null } }

        # 3) 校验通过后解密（v2：PBKDF2 仅派生 32 字节密钥 + SHA256 计数器密钥流；v1：派生整段）
        $kdf = [System.Security.Cryptography.Rfc2898DeriveBytes]::new($key, $iv, 20000, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
        $plain = New-Object byte[] $ct.Length
        if ($raws[0][3] -eq 0x32) {
            $dk = $kdf.GetBytes(32); $kdf.Dispose()
            $ksSeed = New-Object byte[] 33
            [Array]::Copy($dk, 0, $ksSeed, 0, 32); $ksSeed[32] = 1
            # keystream = SHA256(ksSeed || BE32(counter))
            $ks = New-Object byte[] $ct.Length
            $sha2 = [System.Security.Cryptography.SHA256]::Create()
            $off2 = 0; $c2 = 0
            while ($off2 -lt $ct.Length) {
                $ms5 = New-Object System.IO.MemoryStream
                $ms5.Write($ksSeed, 0, 33)
                $cb2 = [BitConverter]::GetBytes([uint32]$c2); [Array]::Reverse($cb2)
                $ms5.Write($cb2, 0, 4)
                $h2 = $sha2.ComputeHash($ms5.ToArray()); $ms5.Dispose()
                $take2 = [Math]::Min($h2.Length, $ct.Length - $off2)
                [Array]::Copy($h2, 0, $ks, $off2, $take2)
                $off2 += $take2; $c2++
            }
            $sha2.Dispose()
            for ($j = 0; $j -lt $ct.Length; $j++) { $plain[$j] = $ct[$j] -bxor $ks[$j] }
        } else {
            $dk = $kdf.GetBytes($ct.Length); $kdf.Dispose()
            for ($j = 0; $j -lt $ct.Length; $j++) { $plain[$j] = $ct[$j] -bxor $dk[$j] }
        }
        return $plain
    } catch { return $null }
}
function Show-DonateForm {
    $f = New-Object System.Windows.Forms.Form
    $f.Text = '捐赠与支持'; $f.Size = New-Object System.Drawing.Size(420, 452)
    $f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'
    $f.MaximizeBox = $false; $f.MinimizeBox = $false; $f.ShowInTaskbar = $false; $f.BackColor = [System.Drawing.Color]::White
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Location = New-Object System.Drawing.Point(20, 12); $lbl.Size = New-Object System.Drawing.Size(370, 40)
    $lbl.Text = "如果该软件对你有帮助，请帮忙点亮 Star，或者对作者进行捐赠，感谢。"
    $lbl.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9.5)
    # 开源地址：单独一行可点击链接（短写法 + 点击直接打开，避免长 URL 被截断）
    $lnk = New-Object System.Windows.Forms.LinkLabel
    $lnk.Location = New-Object System.Drawing.Point(20, 56); $lnk.Size = New-Object System.Drawing.Size(370, 18)
    $lnk.Text = 'github.com/polosug-cloud/xianyu-assistant-oss'
    $lnk.Font = New-Object System.Drawing.Font('Microsoft YaHei', 8)
    $lnk.LinkColor = [System.Drawing.Color]::FromArgb(0, 102, 204)
    $lnk.Add_Click({ try { Start-Process 'https://github.com/polosug-cloud/xianyu-assistant-oss' } catch {} })
    $lblNote = New-Object System.Windows.Forms.Label
    $lblNote.Location = New-Object System.Drawing.Point(20, 78); $lblNote.Size = New-Object System.Drawing.Size(370, 32)
    $lblNote.Text = "注意：捐赠仅表达支持，不提供任何额外服务，不要大额捐赠，`n不要相信本副本以外的其他副本，谢谢。"
    $lblNote.Font = New-Object System.Drawing.Font('Microsoft YaHei', 8)
    $lblNote.ForeColor = [System.Drawing.Color]::Gray
    $lblQr = New-Object System.Windows.Forms.Label
    $lblQr.Location = New-Object System.Drawing.Point(20, 112); $lblQr.Size = New-Object System.Drawing.Size(370, 20)
    $lblQr.Text = '扫码支持作者:'; $lblQr.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9)
    $pic = New-Object System.Windows.Forms.PictureBox
    $pic.Location = New-Object System.Drawing.Point(110, 134); $pic.Size = New-Object System.Drawing.Size(190, 190)
    $pic.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom; $pic.BackColor = [System.Drawing.Color]::White
    $pic.BorderStyle = 'FixedSingle'
    $btn = New-Object System.Windows.Forms.Button
    $btn.Location = New-Object System.Drawing.Point(165, 334); $btn.Size = New-Object System.Drawing.Size(80, 28)
    $btn.Text = '关闭'; $btn.Add_Click({ $f.Close() })
    $f.Controls.Add($lbl); $f.Controls.Add($lnk); $f.Controls.Add($lblNote); $f.Controls.Add($lblQr); $f.Controls.Add($pic); $f.Controls.Add($btn)

    $tmpQr = Join-Path $env:TEMP ('xy_qr_' + [guid]::NewGuid().ToString('N') + '.png')
    $ok = $false
    # 方式一（首选）：直接从包内资源本地还原 —— 无需任何账号在运行
    $bytes = Get-DonationQrBytes
    if ($bytes -and $bytes.Length -gt 100) {
        try {
            [System.IO.File]::WriteAllBytes($tmpQr, $bytes)
            $pic.Image = [System.Drawing.Image]::FromFile($tmpQr)
            $ok = $true
        } catch { $ok = $false }
    }
    # 方式二（兜底）：从正在运行的账号接口取
    if (-not $ok) {
        try {
            $acc = Get-Account (Load-Registry) 'acc_main'
            if ($acc -and (Is-AccountRunning $acc)) {
                $dBase = Account-Url $acc
                $tok = (Invoke-RestMethod -Uri ($dBase + '/api/auth/token') -TimeoutSec 3).token
                Invoke-WebRequest -Uri ($dBase + '/api/support/qr') -Headers @{ Authorization = "Bearer $tok" } -OutFile $tmpQr -TimeoutSec 8 -ErrorAction Stop
                $pic.Image = [System.Drawing.Image]::FromFile($tmpQr)
                $ok = $true
            }
        } catch { $ok = $false }
    }
    if (-not $ok) {
        $lblQr.Text = '资源暂不可用'
        $pic.Visible = $false; $f.Height = 220; $btn.Location = New-Object System.Drawing.Point(162, 140)
    }
    $f.Add_FormClosed({ if ($pic.Image) { try { $pic.Image.Dispose() } catch {} }; if (Test-Path $tmpQr) { Remove-Item $tmpQr -Force -ErrorAction SilentlyContinue } })
    $f.ShowDialog() | Out-Null
    try { $f.Dispose() } catch {}
}

# ---------- 菜单构建（每次账号变化后重建） ----------
$Menu = New-Object System.Windows.Forms.ContextMenuStrip

function Show-AccountStatus($acc) {
    $st = Invoke-Api $acc 'GET' '/api/status'
    if ($null -eq $st) { [System.Windows.Forms.MessageBox]::Show("助手未运行（端口 $($acc.port)）", "运行状态 - $(Get-AccountDisplayName $acc)", 'OK', 'Information') | Out-Null; return }
    $lines = @()
    if (-not $st.logged_in) { $lines += '⚠ 未登录 / 会话过期（请扫码）' }
    if ($st.agent_paused) { $lines += '⏸ 已暂停' }
    $sc = $st.selfcheck
    if ($sc -and $sc.ts) { $lines += $(if ($sc.ok) { "自检正常 $($sc.ts.Substring(5,11))" } else { "❌ 自检异常 $($sc.ts.Substring(5,11))" }) }
    if ($lines.Count -eq 0) { $lines += "运行正常（版本 $($st.version)）" }
    $lines += "浏览器: $($st.browser.status) ｜ 端口: $($acc.port)"
    [System.Windows.Forms.MessageBox]::Show(($lines -join '  '), "运行状态 - $(Get-AccountDisplayName $acc)", 'OK', 'Information') | Out-Null
}
function Show-AccountRevenue($acc) {
    $today = Get-Date -Format 'yyyy-MM-dd'
    $r = Invoke-Api $acc 'GET' "/api/revenue?start=$today&end=$today"
    if ($null -eq $r) { [System.Windows.Forms.MessageBox]::Show("助手未运行（端口 $($acc.port)）", "营收状态 - $(Get-AccountDisplayName $acc)", 'OK', 'Information') | Out-Null; return }
    $all = Invoke-Api $acc 'GET' '/api/revenue'
    [System.Windows.Forms.MessageBox]::Show("今日营收 ¥$($r.total_amount)（$($r.total_orders) 单）`n累计营收 ¥$($all.total_amount)（$($all.total_orders) 单）", "营收状态 - $(Get-AccountDisplayName $acc)", 'OK', 'Information') | Out-Null
}
function Refresh-Menu {
    Build-Menu   # Build-Menu 内部会更新 ContextMenuStrip
}
function Get-AccById($id) {
    $reg = Load-Registry
    return (Get-Account $reg $id)
}

# 账号动作（供菜单事件按固定 id 调用，规避闭包变量问题）
function Set-RowVisual($id, $running) {
    # 立即更新账号行的勾选显示（先打勾/去勾，后台再执行），避免延迟感与重复点击
    if ($script:RowItems.ContainsKey($id) -and $null -ne $script:RowItems[$id]) {
        $script:RowItems[$id].IsRunning = $running
        try { $script:RowItems[$id].Invalidate() } catch {}
    }
}
function Toggle-Account($id) {
    $a = Get-AccById $id
    if (-not $a) { return }
    if ($script:PendingOps.ContainsKey($id)) {
        Show-Balloon '闲鱼助手' '该账号正在启动/暂停中，请稍候'
        return
    }
    if (Is-AccountRunning $a) {
        Stop-AccountInstance $a
        Set-RowVisual $id $false
    } else {
        Start-AccountInstance $a $false
        Set-RowVisual $id $true
    }
    # 不关闭菜单：勾选即时可见（启动先打勾，就绪后气泡确认）
}
function Delete-Account($id) {
    $a = Get-AccById $id
    if (-not $a) { return }
    Close-Menu
    $delData = $a.data_dir -like "$AccountsRoot*"
    $msg = if ($delData) {
        "确定删除账号「$(Get-AccountDisplayName $a)」吗？`n该账号的助手、数据与登录态将被一并删除（不可恢复）。"
    } else {
        "确定注销主账号「$(Get-AccountDisplayName $a)」吗？`n助手将停止并从列表移除，原数据目录保留（可重新扫码启用）。"
    }
    $ans = [System.Windows.Forms.MessageBox]::Show($msg, '删除账号', 'YesNo', 'Warning')
    if ($ans -ne 'Yes') { return }
    # 停止该账号实例（接口退出 + 兜底强杀，确认进程退出后才删数据目录，避免残留）
    # 仅当端口上确实是"本账号实例"时才强杀，绝不误杀其它副本/程序
    if (Is-AccountRunning $a) { Invoke-Api $a 'POST' '/api/app/exit' | Out-Null }
    for ($i = 0; $i -lt 8; $i++) {
        if (-not (Is-AccountRunning $a)) { break }
        if (Test-AccountOwnedByMe $a) { Stop-PortOwner $a.port }
        Start-Sleep -Milliseconds 800
    }
    $script:PendingOps.Remove($id) | Out-Null
    $dataDirDel = $a.data_dir
    $reg = Load-Registry
    Remove-Account $reg $a.id
    if ($delData -and (Test-Path (Split-Path $dataDirDel -Parent))) {
        $dirDel = Split-Path $dataDirDel -Parent
        for ($i = 0; $i -lt 5 -and (Test-Path $dirDel); $i++) {
            Remove-Item -Recurse -Force $dirDel -ErrorAction SilentlyContinue
            if (Test-Path $dirDel) { Start-Sleep -Milliseconds 600 }
        }
    }
    Show-Balloon '闲鱼助手' "已删除账号「$($a.name)」（数据已清理）"
    Refresh-Menu
}
function Open-Account($id) {
    $a = Get-AccById $id
    if (-not $a) { return }
    if (Is-AccountRunning $a) { Start-Process (Account-Url $a) }
    else { Show-Balloon '闲鱼助手' "账号「$(Get-AccountDisplayName $a)」已暂停，请先在「账号管理」中启动" }
    Close-Menu
}
function Status-Account($id) {
    $a = Get-AccById $id
    if ($a) { Show-AccountStatus $a }
}
function Revenue-Account($id) {
    $a = Get-AccById $id
    if ($a) { Show-AccountRevenue $a }
}

function Build-Menu {
    $reg = Load-Registry
    $accs = Get-Accounts $reg
    $script:RowItems = @{}
    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    # 提示音开关等切换操作后保持菜单打开（不立即收起）
    $menu.add_Closing({
        param($s, $ev)
        if ($script:SuppressClose) { $script:SuppressClose = $false; $ev.Cancel = $true }
    })

    # 每账号一次探测：运行状态 + 显示名（含写回注册表）
    $nameMap = @{}; $runMap = @{}
    foreach ($a in $accs) {
        $runMap[$a.id] = Is-AccountRunning $a
        $nameMap[$a.id] = Get-AccountDisplayName $a $runMap[$a.id]
    }
    $runCount = @($runMap.Values | Where-Object { $_ }).Count
    $anyRunning = $runCount -gt 0
    $anyStopped = $runCount -lt $accs.Count

    # 1) 账号管理：每账号一行「☑ 账号名 [端口]   ✕ 删除」（左击=启动/暂停，右侧=删除）
    $mAcc = New-Object System.Windows.Forms.ToolStripMenuItem('账号管理')
    $mAdd = New-Object System.Windows.Forms.ToolStripMenuItem('＋ 新增账号')
    $mAdd.Add_Click({
        $reg2 = Load-Registry
        $newId = 'acc_' + [DateTime]::Now.ToString('HHmmss')
        $dataDir = New-AccountDir $newId
        # 分配空闲端口（8080 起，跳过已占用/已注册的端口）
        $newAcc = [pscustomobject]@{ id = $newId; name = '新账号'; port = (New-FreePort $reg2); data_dir = $dataDir; checked = $false }
        $reg2.accounts = @($reg2.accounts) + $newAcc
        Save-Registry $reg2
        Close-Menu
        if (Start-AccountInstance $newAcc $true) {
            Show-Balloon '闲鱼助手' "新账号已创建（端口 $($newAcc.port)），就绪后自动打开管理页：请扫码登录"
        }
    })
    $mAcc.DropDownItems.Add($mAdd)
    $mAcc.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    foreach ($a in $accs) {
        $nm = $nameMap[$a.id]
        $rn = $runMap[$a.id]
        if ('AccountRowItem' -as [type]) {
            $row = New-Object AccountRowItem
            $row.AccountLabel = "$nm  [:$($a.port)]"
            $row.IsRunning = $rn
            $row.add_ToggleClicked([scriptblock]::Create("Toggle-Account '$($a.id)'"))
            $row.add_DeleteClicked([scriptblock]::Create("Delete-Account '$($a.id)'"))
            $mAcc.DropDownItems.Add($row)
            $script:RowItems[$a.id] = $row
        } else {
            # 降级：自绘行不可用时用普通行（点击切换 + 行下删除）
            $row = New-Object System.Windows.Forms.ToolStripMenuItem("$nm  [:$($a.port)]")
            $row.Checked = $rn
            $row.Add_Click([scriptblock]::Create("Toggle-Account '$($a.id)'"))
            $mAcc.DropDownItems.Add($row)
            $del = New-Object System.Windows.Forms.ToolStripMenuItem("✕ 删除「$nm」")
            $del.Add_Click([scriptblock]::Create("Delete-Account '$($a.id)'"))
            $mAcc.DropDownItems.Add($del)
        }
    }
    # 2) 启动全部助手（常亮：启动全部已添加账号，不论是否勾选）
    $mStartAll = New-Object System.Windows.Forms.ToolStripMenuItem('启动全部助手')
    $mStartAll.Add_Click({
        $regS = Load-Registry
        $n = 0
        foreach ($a in @($regS.accounts)) {
            if (-not (Is-AccountRunning $a)) { if (Start-AccountInstance $a $false) { $n++ } }
        }
        Close-Menu
        if ($n -gt 0) { Show-Balloon '闲鱼助手' "正在启动 $n 个账号助手…" }
        else { Show-Balloon '闲鱼助手' '所有账号助手已在运行' }
    })
    # 3) 打开管理页面（悬停展开：打开全部管理界面 + 已勾选（运行中）账号）
    $mOpen = New-Object System.Windows.Forms.ToolStripMenuItem('打开管理页面')
    $mOpenAll = New-Object System.Windows.Forms.ToolStripMenuItem('打开全部管理界面')
    $mOpenAll.Add_Click({
        $regO = Load-Registry
        $n = 0
        foreach ($a in @($regO.accounts)) {
            if (Is-AccountRunning $a) { Start-Process (Account-Url $a); $n++ }
        }
        Close-Menu
        if ($n -eq 0) { Show-Balloon '闲鱼助手' '没有运行中的账号可打开' }
    })
    $mOpen.DropDownItems.Add($mOpenAll)
    $mOpen.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    foreach ($a in $accs) {
        if ($runMap[$a.id]) {
            $it = New-Object System.Windows.Forms.ToolStripMenuItem($nameMap[$a.id])
            $it.Add_Click([scriptblock]::Create("Open-Account '$($a.id)'"))
            $mOpen.DropDownItems.Add($it)
        }
    }
    # 4) 暂停全部助手
    $mPauseAll = New-Object System.Windows.Forms.ToolStripMenuItem('暂停全部助手')
    $mPauseAll.Enabled = $anyRunning
    $mPauseAll.Add_Click({
        $regP = Load-Registry
        $n = 0
        foreach ($a in @($regP.accounts)) { if (Is-AccountRunning $a) { Stop-AccountInstance $a; $n++ } }
        Close-Menu
        if ($n -gt 0) { Show-Balloon '闲鱼助手' "正在暂停 $n 个账号助手…" }
    })
    # 5) 提示音开关（三级：系统/消息 → 账号；切换后菜单保持打开，持久化在各账号设置）
    function Add-SoundMenu($parent, $title, $keyName) {
        $top = New-Object System.Windows.Forms.ToolStripMenuItem($title)
        foreach ($a in $accs) {
            $nm = $nameMap[$a.id]
            $rn = $runMap[$a.id]
            $label = if ($rn) { $nm } else { "$nm（未运行）" }
            $it = New-Object System.Windows.Forms.ToolStripMenuItem($label)
            $it.CheckOnClick = $false
            if ($rn) {
                $it.Tag = @{ acc = $a; key = $keyName }
                $val = Invoke-Api $a 'GET' "/api/settings/$keyName"
                $it.Checked = if ($null -eq $val -or $val.value -ne '0') { $true } else { $false }
                $it.Add_Click({
                    param($s, $e)
                    if ($null -eq $s -or $null -eq $s.Tag) { return }
                    $cfg = $s.Tag
                    $script:SuppressClose = $true
                    $nv = if ($s.Checked) { '0' } else { '1' }
                    $resp = Invoke-Api $cfg.acc 'PUT' ("/api/settings/" + $cfg.key) @{ value = $nv }
                    if ($null -ne $resp) { $s.Checked = -not $s.Checked }
                })
            } else { $it.Enabled = $false }
            $top.DropDownItems.Add($it)
        }
        $parent.DropDownItems.Add($top)
    }
    $mSound = New-Object System.Windows.Forms.ToolStripMenuItem('提示音开关')
    Add-SoundMenu $mSound '系统提示音（自检异常）' 'sound_sys_enabled'
    Add-SoundMenu $mSound '消息提示音（来消息）' 'sound_msg_enabled'
    # 6) 运行状态（按账号）
    $mStatus = New-Object System.Windows.Forms.ToolStripMenuItem('运行状态')
    foreach ($a in $accs) {
        $nm = $nameMap[$a.id]
        $rn = $runMap[$a.id]
        $it = New-Object System.Windows.Forms.ToolStripMenuItem($(if ($rn) { $nm } else { "$nm（未运行）" }))
        if ($rn) { $it.Add_Click([scriptblock]::Create("Status-Account '$($a.id)'")) } else { $it.Enabled = $false }
        $mStatus.DropDownItems.Add($it)
    }
    # 7) 营收状态（按账号）
    $mRevenue = New-Object System.Windows.Forms.ToolStripMenuItem('营收状态')
    foreach ($a in $accs) {
        $nm = $nameMap[$a.id]
        $rn = $runMap[$a.id]
        $it = New-Object System.Windows.Forms.ToolStripMenuItem($(if ($rn) { $nm } else { "$nm（未运行）" }))
        if ($rn) { $it.Add_Click([scriptblock]::Create("Revenue-Account '$($a.id)'")) } else { $it.Enabled = $false }
        $mRevenue.DropDownItems.Add($it)
    }

    $mDonate = New-Object System.Windows.Forms.ToolStripMenuItem('捐赠作者')
    $mDonate.Add_Click({ Show-DonateForm })
    # 9) 关于
    $mAbout = New-Object System.Windows.Forms.ToolStripMenuItem('关于')
    $mAbout.Add_Click({
        [System.Windows.Forms.MessageBox]::Show("闲鱼助手 v1.0.0（多账号）`n多账号多端口自动化助手`n仅限学习研究，请遵守平台规则`nGitHub: https://github.com/polosug-cloud/xianyu-assistant-oss", '关于闲鱼助手', 'OK', 'Information') | Out-Null
    })
    # 10) 退出托盘（底部；二次确认后关闭全部助手并退出）
    $mExit = New-Object System.Windows.Forms.ToolStripMenuItem('退出托盘')
    $mExit.Add_Click({
        $ans = [System.Windows.Forms.MessageBox]::Show('确定退出托盘并关闭所有账号的助手吗？', '退出托盘', 'YesNo', 'Warning')
        if ($ans -ne 'Yes') { return }
        $regX = Load-Registry
        $fired = @($regX.accounts | Where-Object { Is-AccountRunning $_ })
        foreach ($a in $fired) { Invoke-Api $a 'POST' '/api/app/exit' | Out-Null }
        Start-Sleep -Milliseconds 2000
        foreach ($a in $fired) { Stop-PortOwner $a.port }
        $NotifyIcon.Visible = $false
        try { $LockFs.Close() } catch {}
        [System.Windows.Forms.Application]::Exit()
    })

    $menu.Items.AddRange(@($mAcc, $mStartAll, $mOpen, $mPauseAll, $mSound, $mStatus, $mRevenue, $mDonate, $mAbout, $mExit))
    $script:Menu = $menu
    try { $NotifyIcon.ContextMenuStrip = $menu } catch {}
}

# ---------- 单实例锁 ----------
$TrayLock = Join-Path $Root '.tray.lock'
try {
    $LockFs = [System.IO.File]::Open($TrayLock, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch { exit 0 }

$NotifyIcon.Add_MouseUp({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
        Build-Menu
    }
})
$NotifyIcon.Add_MouseDoubleClick({
    # 双击：打开所有运行中账号的管理页
    $regD = Load-Registry
    $n = 0
    foreach ($a in @($regD.accounts)) {
        if (Is-AccountRunning $a) { Start-Process (Account-Url $a); $n++ }
    }
    if ($n -eq 0) { Show-Balloon '闲鱼助手' '没有运行中的账号（可在账号管理中启动）' }
})

Build-Menu

# 托盘启动：默认不启动任何助手，等待用户自行操作
$regBoot = Load-Registry
if ((Get-Accounts $regBoot).Count -gt 0) {
    Show-Balloon '闲鱼助手' '托盘已启动：右键可管理账号、启动/暂停助手'
} else {
    Show-Balloon '闲鱼助手' '暂无账号，请点「账号管理 → ＋ 新增账号」'
}

[System.Windows.Forms.Application]::Run()
