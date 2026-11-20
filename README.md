# 任务栏资源监控（TaskbarMonitor）

把 CPU / GPU / 内存 / 显存 / 磁盘读写 / 网速 压成两行数字，**直接长在 Windows 任务栏里**，
不占桌面空间，不挡窗口，点击别处也不会消失。

- 数据条：310×48 两行八格，贴着任务栏走，可拖动到任务栏任意位置
- 详情面板：CPU/GPU 趋势图 + 每核心热力格 + 内存/磁盘分区/网络卡片
- 托盘菜单：开关数据条与面板、跟随系统深浅色、开机自启

> 仅支持 **Windows 10 / 11 64 位**。依赖任务栏句柄、GDI 与 PDH 计数器的实现方式，
> macOS / Linux 无法运行。

## 下载即用（推荐，不需要 Python）

1. 打开右侧 [Releases](../../releases)，下载最新版 `TaskbarMonitor-win64.zip`
2. 解压到**有写权限的目录**（例如 `D:\Tools\TaskbarMonitor`，不要放 `C:\Program Files`）
3. 双击 `TaskbarMonitor.exe`

首次运行 Windows 可能提示「Windows 已保护你的电脑」，因为 exe 没有代码签名：
点 **更多信息 → 仍要运行**。程序不联网、不写注册表以外的地方，可自行核对源码。

开机自启：右键托盘图标 → 勾选「开机自启」。它写的是当前用户注册表
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员权限。

**检查更新**：启动 5 秒后自动查一次 GitHub 最新 Release，之后每 6 小时一次。
有新版本会在托盘弹提示、右键菜单顶层出现「有新版本 vX.Y.Z ↗」，点它打开下载页。
网络不通时静默跳过，不会打扰你；也可以随时右键 → 「检查更新」手动查。

## 使用

| 操作 | 效果 |
|---|---|
| 拖动数据条 | 沿任务栏移动，位置会被记住；超出任务栏自动归位 |
| 双击托盘图标 | 打开/关闭详情面板 |
| 右键托盘图标 | 菜单：数据条 / 详情面板 / 主题 / 开机自启 / 退出 |

数据条固定浅色（深色任务栏下标签会看不清，这是已知限制）；
详情面板与托盘菜单跟随系统深浅色，也可以在菜单里手动锁定。

## 从源码运行

需要 Python 3.10+。

```bash
pip install -r requirements.txt
python main.py
```

## 自己打包 exe

```bash
pip install -r requirements.txt pyinstaller
python tools/make_icon.py          # 生成 app.ico
pyinstaller --noconfirm --clean TaskbarMonitor.spec
```

产物在 `dist/TaskbarMonitor/`，整个文件夹一起分发（不要只拷 exe）。
用 onedir 而不是 onefile：PySide6 打 onefile 启动要 3~5 秒，且更容易被杀软误报。

推送到 `main` 后 GitHub Actions 会自动打包并上传 artifact；
在 Actions 页手动 Run workflow 并填版本号（如 `v1.0.0`），会自动创建 Release 并附上 zip。

## 已知限制

- 温度读不到（消费级主板无统一接口，需要管理员权限的驱动级访问）
- 深色任务栏下数据条标签与图标偏暗，只剩彩色数字可见
- explorer.exe 重启后数据条需要重新绑定，程序会在下次刷新时自动补绑
- 同时运行多个实例会出现多条重叠（新版会加单实例限制）

## 许可

MIT
