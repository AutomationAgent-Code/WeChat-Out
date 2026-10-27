# Windows 数据库读取操作

以下命令在 Skill/仓库目录执行，所有输入路径都是示例占位符。先由用户选择自己的账户目录，不在整个磁盘搜索其他人的数据库。

## 依赖与检查

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/inspect_databases.py --root '<本人的账户目录>'
```

脚本不自动升级/降级微信、不修改登录状态。检查结果含相对路径，可能识别账户；不要公开贴出未经脱敏的完整输出。

## 1. 创建副本

```powershell
python scripts/snapshot_database.py --source '<源contact.db>' --output 'working-copy/contact.db' --source-closed
python scripts/snapshot_database.py --source '<源message_0.db>' --output 'working-copy/message_0.db' --source-closed
```

重复处理所有 `message_*.db`。加密源必须正常退出微信后才可声明 `--source-closed`；非空 WAL 或 journal 会报 `UNMERGED_JOURNAL`。不能删 sidecar 或复制主文件来绕过。明文库使用 SQLite backup，不要求退出，但多个单库备份不提供跨库同一时点保证。

## 2. 密钥来源（二选一）

### A. 已有本人密钥或 DPAPI 文件

```powershell
python scripts/decrypt_sqlcipher.py --source '<待解密副本>' --output '<新的解密输出.db>' --prompt-key
```

只在本地终端隐藏提示中输入 64 位 hex，不能将密钥写入命令行、聊天或日志。已有当前用户 DPAPI 凭据时，将 `--prompt-key` 替换为 `--protected-key '<本地凭据.dpapi>'`。不支持跨 Windows 用户/机器直接迁移该凭据。

### B. 可选外部 DLL 接入

仓库提供 `capture_key.py`，但**不提供 DLL 本身**。只支持用户合法取得并确认可信的以下 ABI：

```text
int InitializeHook(uint32_t pid)
int PollKeyData(void* buffer, size_t capacity)  # 以 NUL 结束的 64 字符 hex
int CleanupHook(void)
```

它会在明确选定的 `Weixin.exe` 进程中安装第三方 Hook，不是普通只读文件检查；来源/许可/兼容性由工具提供方说明，SHA256 只保证文件没变，不证明安全。禁止自动下载未知 DLL、关闭安全软件或自动提权。位数不匹配或导出符号不匹配时不能加载。

先在退出微信时制作加密副本。用户随后手动启动自己的微信进程，核对 PID、账户和 DLL，并明确同意 Hook 后：

```powershell
python scripts/capture_key.py --dll '<可信工具目录/wx_key.dll>' --dll-sha256 '<已核验SHA256>' --pid <本人微信PID> --database 'working-copy/contact.db' --output 'private/database.dpapi' --acknowledge-process-hook
```

捕获后会对离线副本逐页验证并校验 SQLite；成功才存为当前 Windows 用户 DPAPI blob，失败不保存。默认等待 30 秒，可设 1–120 秒；finally 尝试清理 Hook。未知 DLL 可能崩溃或挂起，此适配器不能为第三方实现提供安全保证。不调用 DLL 的诊断输出，防止其带出密钥。

此路径尚未对微信 `4.1.15.13` 实测。不允许把“缺少已确认的密钥提取工具”写成“该版本必然不支持数据库读取”。

## 3. 解密每个库

```powershell
python scripts/decrypt_sqlcipher.py --source 'working-copy/contact.db' --output 'decrypted/contact.db' --prompt-key
python scripts/decrypt_sqlcipher.py --source 'working-copy/message_0.db' --output 'decrypted/message/message_0.db' --prompt-key
```

以上使用 A 路线的本地隐藏输入。若已通过 B 路线生成 DPAPI 文件，将每条命令的 `--prompt-key` 替换为 `--protected-key 'private/database.dpapi'`。已经生成的输出不要重复解密，直接进入读取步骤；需要重做时指定新输出名。

默认实现一种明确的 SQLCipher 4 布局：AES-256-CBC、4096 字节页面、80 字节保留区、PBKDF2-HMAC-SHA512 256000 次和逐页 HMAC-SHA512。不是所有加密格式的通用解密器。参数依据见 [SQLCipher 4 发布说明](https://www.zetetic.net/blog/2018/11/30/sqlcipher-400-release/) 与 [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)。如原工具明确使用原始 AES 密钥可指定 `--raw-key`；不默认猜测参数或暴力重试。不同库的凭据/盐可能不同，每个库独立验证。

输出只在所有页通过认证且 `quick_check=ok` 后发布。已有输出一律拒绝覆盖。页面解密器不重放 WAL，不能用于正在变化的数据库或带未合并 journal 的副本。

## 4. 读取消息

```powershell
python scripts/read_chats.py --contact-db 'decrypted/contact.db' --message-dir 'decrypted/message' --output 'exports/chats.jsonl'
```

可用 `--self-id '<本人账户标识>'` 区分 sent/received；未知发送者不会猜成对方。可用 `--conversation '<联系人username>'` 读取单一会话，不需要选择标签。

当前读取器的字段契约：

- `contact(username, remark?, nick_name?)`。
- `Name2Id(user_name)` 使用 `rowid` 关联发送者。
- `Msg_<联系人标识MD5>` 至少包含 `local_id, server_id, local_type, create_time, real_sender_id, message_content`。

此契约不适用时明确报 Schema 缺口；不擅自读取另一张同名近似表。无法映射到联系人的消息表仍导出 `source_table`，不丢弃群聊/未知联系人。

JSONL 每行含会话、发送者、原始时间、UTC 时间、消息类型、正文、来源分片/表及消息 ID。压缩失败或二进制保留 base64，不宣称解码成功。server_id 非零时按会话去重；相同 ID 内容冲突会中止，不静默丢消息。无 server_id 的消息保留分片身份，不强行去重。

## 故障与限制

| 错误 | 下一步 |
| --- | --- |
| KEY_NOT_RECEIVED | 核对本人进程、第三方工具 ABI/版本和位数，不猜偏移 |
| HMAC_FAILED | 核对密钥来源、配置和副本完整性，不能确定是哪一个原因 |
| UNMERGED_JOURNAL | 正常关闭后重新取副本，或使用支持 SQLCipher 的备份工具 |
| UNSUPPORTED_MESSAGE_SCHEMA | 取得脱敏的表名/列名，开发新 adapter；不要发送原始聊天 |
| OUTPUT_EXISTS | 使用新的输出文件名；不自动覆盖 |

工具执行完全本地，但用户将消息交给云端模型分析时适用模型提供方的数据处理规则。未导出附件文件、未转写语音，JSONL 不承诺跨会话全局时间排序。
