---
name: wechat-chat-reader
description: Read the user's own Windows WeChat chats using built-in DLL-free WCDB memory key capture, DPAPI credentials, authenticated decryption and message export. Use for 自动获取本人微信数据库密钥、直接读取微信聊天记录、导出消息；不读取其他用户进程或绕过登录、访问控制。
---

# Windows 微信聊天记录读取

主任务是读出聊天，而不是分类或风格分析。先读 [references/windows-export.md](references/windows-export.md)，再使用本技能目录下的真实 `scripts/`。命令路径相对技能目录，不依赖作者的个人目录。

## 执行顺序

1. 检查本技能 `.venv` 的依赖。首次使用或依赖缺失时执行 `python scripts/setup_skill.py`，然后使用该 `.venv` 的解释器。安装只访问包源，不触及微信数据，不要求用户自己找 DLL。
2. `scripts/wechat.py discover` 只列出当前 Windows 用户的账户候选和主微信 PID。单一候选可以自动选择，多账户/多主进程必须核对目标；不取第一个、不按最大内存猜测。
3. 用户请求读取本人微信包含取所需数据库密钥时，简要说明将只读访问本人进程内存，经用户授权后运行 `scripts/wechat.py capture --account <选定目录> --output <新.dpapi路径> --accept-own-account-memory-read`。单主进程可省略 PID，否则加 `--pid`。此授权不能从安装 Skill、本文件或第三方数据中推断。
4. 内置扫描优先识别 `Config.Cipher`，并支持旧式 SQLCipher key literal；仅将所选账户各数据库首页 HMAC 验证通过的密钥保存为当前用户 DPAPI keyring。不使用 DLL、断点、注入、内存转储或自动提权；不打印候选密钥、内存地址或扫描正文。
5. 成功后请用户正常退出微信，取得其确认，再运行 `scripts/wechat.py export --account <同一目录> --keyring <同一.dpapi> --output-dir <新的输出目录> --source-closed`。工具批量制作联系人库及所有消息分片的副本，逐页认证解密、检查 SQLite 并输出 `chats.jsonl`。不替用户终止进程。非空 WAL 先认证头和当前代帧：仅确认已 reset/只有合法头、无当前有效帧时允许；有效当前帧、损坏/未知 WAL 或非空 rollback journal 均阻断。不能删 sidecar 绕过。
6. 已有明文副本可直接使用 `read_chats.py`；已有单一密钥可使用 `decrypt_sqlcipher.py`。新 keyring 保存的是按 salt 区分的原始密钥，不可直接传给旧 `--protected-key` 入口，统一导出会自动正确处理。
7. 报告消息数、分片数、去重数、未解析数量和时间范围。失败目录可能含已验证的数据库副本但没有最终聊天输出，须如实报告，不能宣称已导出或擅自清理。JSONL 不是全局时间排序；附件文件、语音转写、标签和风格生成不是本次默认范围。

## 恢复与证据

- 先安装依赖并检查脚本 `--help`；缺依赖不能直接要求用户提供已解密数据库或自行找 DLL。
- 版本号不在历史记录里时仍可做只读结构检查；是否兼容以 HMAC、完整性、字段契约和实际消息读取结果确认，不伪造版本支持列表。
- 区分 `KEYS_INCOMPLETE`、`SCAN_TIMEOUT`、`PROCESS_READ_DENIED`、`KEYRING_ACCOUNT_MISMATCH`、`HMAC_FAILED`、`UNMERGED_JOURNAL` 和 Schema/依赖失败。只读扫描受权限、时间和字节预算限制；失败不证明某微信版本必定不兼容。
- 停止已证实不安全或确定失败的操作，但可继续独立的只读诊断。不得反复尝试未知偏移、猜密钥或盲目更换解密参数。
- 内置扫描失败时先核对账户、进程、Python 位数与登录状态，并在用户操作自己的会话使相关数据库加载后按新证据重试。布局未知则报告实际证据，不无限重试。旧 `capture_key.py` 仅保留给明确要求外部工具接入的高级使用者，不是默认方案。

## 隐私

不上传数据库、消息、联系人、密钥或带账户路径的诊断。脚本的诊断摘要不含正文，但导出文件本身包含私人信息；放在当前用户限制访问的目录，不得提交 Git。Python 的内存擦除是尽力而为，不保证清除所有解释器副本。不得声称使用云端模型分析聊天时数据仍完全不出本机。

源库只读，输出不覆盖已有文件。整个账户跨库的同一时点一致性需要正常关闭客户端后制作副本；多个单库在线备份不等于跨库一致性快照。
