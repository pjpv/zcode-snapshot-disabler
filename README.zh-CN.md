# zcode-snapshot-disabler

一键禁用 [ZCode](https://zcode.ai) 桌面版的代码快照上传:单点等长补丁,
行为等同「未登录」,可随时完整还原。支持 Windows / macOS / Linux。

[English readme](README.md)

## 它做什么

ZCode 桌面版(Electron 应用)在部分操作前会扫描你的工作区、打包代码快照并上传
(先请求上传凭证,再执行上传)。如果你不希望自己的源代码离开本机,本工具只在
`app.asar` 里翻转**一个布尔闸门**,让这条管线提前返回 —— 与应用自带的
「**未登录**」代码路径完全一致:

- 不打包、不上传任何快照
- 不发出上传凭证请求
- 文件大小与结构逐字节不变

应用其余功能不受影响,一条命令即可完整还原。

## 兼容性

补丁基于**结构模式匹配**:按代码结构定位闸门(而非硬编码偏移),并容忍
压缩变量改名,常规应用更新不影响识别。运行时自动检测应用版本并与上次
patch 记录比对;未来版本若重构导致匹配失败,工具会报告 `no-gate` 并
拒绝改动文件。

已验证:

| ZCode 版本 | 平台                          | 结果                              |
| ---------- | ----------------------------- | --------------------------------- |
| 3.12.3     | Windows x64 全体安装          | patch 成功;`--selftest` 往返通过 |

macOS / Linux 分支已实现并通过代码审查,但尚未在真机上验证 —— 请先运行
`--scan` 并反馈结果。

## 原理

快照的所有触发路径(prompt、terminal、repo-wiki)都汇入 `app.asar` 中的
同一份实现:

```js
async captureBeforePromptUnsafe(t) {
    t.signal?.throwIfAborted();
    let r = await this.tokenProvider();          // 本地读 token,无网络
    if (t.signal?.throwIfAborted(), !r) return;  // ← 原生「未登录」守卫
    ... 扫描 / 打包 / 取上传凭证 / 上传 ...
}
```

补丁把闸门操作数 `!r` **等长**替换为恒真的 `!0`,函数从此像未登录时一样
静默返回。要点:

- **等长就地改写**:文件大小与 asar 头部所有偏移保持有效,归档结构完好;
  端点字符串永不触碰。
- **抗压缩改名**:正则匹配任意压缩变量名(`!r`、`!ab`、…),并从紧邻的
  赋值语句重新推导变量名,常规重构不影响识别。n 字符变量的替换串是
  `!`×n 加 `0`(n 为奇)或 `1`(n 为偶)—— 恒真且恰为 n+1 字节。
- **双向幂等**:已打补丁形态(`!0`、`!!1`、…)绝不可能再匹配未打补丁的
  模式,重复运行不会二次改写。
- **自校验**:写入前后精确偏移比对 + 运行后独立复查,任何异常都不动文件。
- **状态与日志**:每笔编辑记入 `patch-state.json`(偏移、原字节);
  即使状态文件丢失,还原也能从上下文正则精确推导。
- **版本感知**:从归档内的 `package.json` 解析应用自身版本(正确的
  asar 头部解析,绝非宽松 grep,不会误取打包依赖的版本)。版本显示在
  日志与 `--scan` 中、记入 `patch-state.json`,并与上次 patch 的版本
  比对 —— 应用更新替换过 `app.asar` 会被显式提示。

## 快速开始

仅需 Python 3.6+,零第三方依赖。

```bash
python3 patch_asar.py --scan      # 列出发现的安装与闸门状态(只读)
python3 patch_asar.py             # 发现 + patch 所有副本(幂等)
python3 patch_asar.py --dry-run   # 显示将要做的编辑,零改动
python3 patch_asar.py --selftest  # 每个目标 patch -> restore -> patch 往返验证
python3 restore_asar.py           # 全部还原
```

完成后**重启 ZCode** —— 运行中的进程仍持有旧代码,重启后才生效。
Windows 上若 `python` 是 2.x,脚本会自动经 `py -3` 重启自身。

## 平台与安装方式覆盖

| 平台 / 安装方式              | 探测位置                                                | 提权方式        |
| ---------------------------- | ------------------------------------------------------- | --------------- |
| Windows 全体安装             | `%ProgramFiles%\*zcode*\resources\app.asar`             | UAC(自动)     |
| Windows 用户模式 NSIS        | `%LOCALAPPDATA%\Programs\*zcode*\...`                   | 通常免提权      |
| Windows 旧式 Squirrel        | `%LOCALAPPDATA%\*zcode*\app-*\...`                      | 免提权          |
| Windows scoop                | `~/scoop/apps/*zcode*/current/...`                      | 免提权          |
| Windows / 便攜版任意目录     | —                                                       | `--asar <path>` |
| macOS                        | `/Applications/ZCode.app`、`~/Applications/...`         | osascript 授权  |
| Linux deb/rpm                | `/opt`、`/usr/lib`、`which zcode` 反查                  | sudo / pkexec   |
| Linux AppImage               | PATH 与常见目录                                         | 提取 → patch    |

自动发现的所有副本都会被 patch(不漏网);`--asar` 可钉住目标(可重复;
接受 `app.asar` 文件、安装目录或 AppImage)。

不支持(自动跳过并说明):Windows MSIX/商店版(WindowsApps ACL)、
snap 包(只读 squashfs)。

## 提权模型

父进程在**你的**上下文里发现目标,并亲自处理用户可写的目标;需要 root 的
目标以精确的绝对路径交给提权子进程 —— 子进程绝不重新发现(root 的
HOME/PATH 看到的比你少)—— 并通过随机临时路径上的 JSON marker 回报结果。
状态与日志只由非特权父进程落盘,工具目录里永远不会出现 root 属主的文件。
每次调用至多一次提权提示。

## 注意事项

- **macOS**:修改 `.app` 内容会使代码签名失效,脚本在每次编辑后自动
  ad-hoc 重签(`codesign --force --deep -s -`),无需开发者账号。若首次
  启动报「已损坏」,执行
  `xattr -dr com.apple.quarantine /Applications/ZCode.app`。
- **AppImage**:内部 squashfs 只读;脚本提取到旁边的 `<AppImage>.patched/`
  目录并在其中 patch。之后用该目录的 `AppRun` 启动;原 AppImage 文件不动。
- **应用更新会替换 `app.asar`** —— 更新后重跑 `patch_asar.py`(想先看
  变化可先 `--scan`)。`no-gate` 表示该版本已匹配不到此模式:工具拒绝动
  文件,闸门位置需要重新定位。
- 应用运行中打补丁是安全的(等长改写),但需重启后生效。
- 退出码:`0` 成功 · `1` 至少一个目标失败 · `2` 未发现目标。

## 还原

```bash
python3 restore_asar.py           # 所有副本
python3 restore_asar.py --asar "C:\Program Files\ZCode\resources\app.asar"
```

原始压缩变量名从闸门紧邻的赋值语句重新推导,即使 `patch-state.json`
丢失或过期也能精确还原。没有可还原内容时干净退出。

## 免责声明

这是一个个人隐私工具:修改你自己机器上安装的应用,使其停止上传你的代码。
本工具与应用厂商无关、未获其认可;修改应用可能与其服务条款冲突,请自行
斟酌并自担风险。请随手保留 `restore_asar.py`。

## 许可证

[MIT](LICENSE)
