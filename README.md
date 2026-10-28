# WeChat-Out

面向 Codex 的 Windows 微信聊天读取 Skill，内置免第三方 DLL 的自动密钥获取、DPAPI 凭据保存和聊天导出。

```text
安装依赖 → 发现本人账户和进程 → 只读扫描密钥并做 HMAC 验证
→ DPAPI 保存 → 用户正常退出微信 → 批量快照/解密 → JSONL
```

## 安装与使用

需要 Windows 10/11 AMD64、64 位 Python 3.11+、本人已登录的微信。无需安装 WinDbg、寻找 DLL 或输入数据库密钥。

```powershell
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git "$env:USERPROFILE\.codex\skills\wechat-chat-reader"
cd "$env:USERPROFILE\.codex\skills\wechat-chat-reader"
python scripts/setup_skill.py
```

依赖安装到 Skill 自己的 `.venv`，不改全局 Python 配置。Codex 首次调用本 Skill 时也会按指令自动准备依赖。安装依赖需要网络；取密钥和导出流程不联网。

在新会话中说：

```text
用 $wechat-chat-reader 读取我自己的微信聊天。我同意只读扫描本人的微信进程来取得所需数据库密钥，不使用外部 DLL。
```

Skill 会完成依赖检查、账户发现和内置扫描。多账户或多主进程时仍需选定目标。成功取密钥后，用户正常退出微信，再继续导出，避免漏掉活动 WAL。**目前不是“客户端保持运行也能全量一致导出”的一键工具。**

## 不通过 Codex 的命令行用法

在仓库目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/wechat.py discover
.\.venv\Scripts\python.exe scripts/wechat.py capture --account '<本人db_storage或账户目录>' --output 'private/keyring.dpapi' --accept-own-account-memory-read
# 取密钥成功后，自己正常退出微信；不要直接结束进程或删 WAL
.\.venv\Scripts\python.exe scripts/wechat.py export --account '<同一账户目录>' --keyring 'private/keyring.dpapi' --output-dir 'exports/run-001' --source-closed
```

单账户、单主进程时可省略 `--account` 和 `--pid`；发现多个候选会要求选择，不按目录顺序或最大内存猜测。输出目录必须是新目录，最终聊天文件为 `exports/run-001/chats.jsonl`。

详细用法、错误码与高级入口见 [Windows 操作指南](references/windows-export.md)。

## 内置能力

| 组件 | 能力 |
| --- | --- |
| setup_skill.py | 创建专用虚拟环境并安装依赖 |
| wechat.py discover | 当前用户的账户候选与本人微信主 PID |
| wechat.py capture | 内置 Config.Cipher/旧式 literal 只读扫描，逐库 HMAC 认证，DPAPI 密钥环 |
| wechat.py export | 同一账户全部聊天库快照、原始密钥解密、完整性检查和 JSONL |
| inspect_databases.py / snapshot_database.py | 结构检查和单库安全副本 |
| decrypt_sqlcipher.py / read_chats.py | 单库解密和多分片消息读取 |
| capture_key.py | 旧的外部 DLL 高级接入；默认流程不使用 |

每个数据库可以使用不同密钥，不再假定一个全局密钥适用于所有库。只有所选账户的 HMAC 认证结果会保存；部分捕获不生成看似成功的密钥环。

## 安全边界

- 只处理本人或明确授权的数据，且默认扫描器仅打开当前 Windows 用户的微信主进程。
- 通过 Windows `VirtualQueryEx` 和 `ReadProcessMemory` 查询/读取，不注入 DLL、不写目标进程、不设置断点、不转储完整内存。
- 不自动提权、不关闭防护、不自动登录/退出微信。安装 Skill 不等于授权读取内存。
- 默认 60 秒、累计 4096 MiB 读取预算，可显式调整；找不到密钥会报告失败，不保证所有版本。
- 密钥只保存为当前 Windows 用户 DPAPI 加密文件，不在命令行、聊天、日志中回显。密钥环绑定所选账户目录，不能跨账户复用。
- 原始数据库只读，已有输出不覆盖。WAL 按头校验、generation salt 与累计 checksum 分类，不能只凭文件非空判定活跃；仅 reset/合法空代可使用主库副本。存在当前有效帧、损坏日志或非空 rollback journal 时仍拒绝导出，不删除日志。复制/解密前后还会核对源文件和日志指纹。
- 工具没有遥测/上传代码；导出文件、数据库副本和诊断路径仍属隐私。将正文交给云端模型分析不属于纯本地处理。
- Python 内存擦除尽力而为，不承诺清除解释器的所有副本。失败输出目录可能保留已经验证的数据库副本，不会擅自删除。

## 兼容性与验证

内置扫描器针对 Windows AMD64 WCDB `Config.Cipher` 运行时布局和旧式 SQLCipher key literal；解密器针对 SQLCipher 4 CBC/SHA512 页面布局。客户端版本、权限和加载状态会影响效果。未知布局不会通过猜密钥或绕过权限处理。

**合成端到端测试不等于真实微信版本验收，微信 4.1.15.13 的真实取密钥和全量导出仍未现场验证。** 不要求用户自行找 DLL，但也不能承诺装完后所有版本都成功。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试仅使用合成内存和数据库，覆盖无 DLL 的捕获 → DPAPI → 多库解密 → 聊天读取，以及多账户冲突、错误密钥、WAL、截断压缩和防覆盖。未导出图片/视频文件、未转写语音，JSONL 不承诺全局时间排序。

## 来源与许可

运行时布局/扫描逻辑参考并改编自 MIT 许可的 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)，固定审阅版本及完整许可见 [third_party/NOTICE.md](third_party/NOTICE.md) 和 [MIT 许可](third_party/wcdb-key-tool.LICENSE)。未捆绑或运行上游的调试器路径、明文密钥缓存和第三方 DLL。Windows API 依据：[ReadProcessMemory](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory)、[VirtualQueryEx](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex)。

本仓库自身未指定统一许可证前，不应推定获得任意再分发授权；上述第三方部分保留其原许可。
