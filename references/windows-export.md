# Windows 自动取密钥与聊天导出

在 Skill 目录运行以下命令。自动扫描不需要外部 DLL。密钥不能发到聊天、写入命令行或提交 Git。

## 首次准备

```powershell
python scripts/setup_skill.py
```

该脚本只创建本目录 `.venv` 并安装 `requirements.txt`；不访问微信。已有非正常虚拟环境会保留并报错，不自动删除或重建。以下简写的 `python` 应替换为本技能 `.venv/Scripts/python.exe`。

## 1. 发现账户与进程

```powershell
python scripts/wechat.py discover
```

仅从当前用户 Documents、微信配置指定的数据根目录发现 `xwechat_files/*/db_storage`，不扫描其他用户目录或全盘。进程候选必须属于当前 Windows 用户、名称为 `Weixin.exe` 且不是 `--type=` 子进程。结果可能包含识别账户的路径，不公开贴出未经脱敏的报告。

单候选可自动使用。多账户或多主进程时核对当前登录账户，分别通过 `--account`、`--pid` 指定，不能按第一个或内存大小选。自动发现失败时只需指定本人数据目录，不需要用户寻找 DLL。

## 2. 内置自动获取密钥

微信保持本人登录状态。用户明确同意只读扫描本人进程后：

```powershell
python scripts/wechat.py capture --account '<本人db_storage或其父账户目录>' --output 'private/keyring.dpapi' --accept-own-account-memory-read
```

必要时添加 `--pid <本人主进程PID>`。默认扫描最多 60 秒、累计读取 4096 MiB（包括多轮读取），参数为 `--timeout-seconds` 和 `--max-memory-mib`，上限分别 300 秒、32768 MiB。扫描会尝试：

1. 旧式 `x'<key><salt>'` 内存 literal；
2. 根据 `com.Tencent.WCDB.Config.Cipher` 字符串引用追踪有界对象和解码候选；
3. 对所选账户每个库的首页执行 HMAC-SHA512 验证。

不搜索任意32字节片段暴力猜测，不用固定代码地址、不注入、不设断点、不创建远程线程、不写入目标进程或转储内存，不请求 DebugPrivilege，不自动提权。布局/掩码可能随微信版本变化；候选只有通过认证才会采用。

所有所选聊天数据库均取得有效原始密钥时才保存 DPAPI 密钥环。它可能包含按不同 salt 区分的多个原始密钥，绑定当前账户目录。未全部取得时报 `KEYS_INCOMPLETE`，不会保存不完整密钥环，也不将密钥写成明文 JSON。

`capture` 只做首页密钥认证。**完整性、全页 HMAC 和聊天可读性要等 `export` 成功后才算完成。** 既有输出不会被覆盖，重新捕获应使用新的凭据文件名。

## 3. 正常退出微信后统一导出

成功取密钥后，请用户正常退出微信并确认，不擅自结束进程。只有退出后才能声明 `--source-closed`：

```powershell
python scripts/wechat.py export --account '<同一账户目录>' --keyring 'private/keyring.dpapi' --output-dir 'exports/run-001' --source-closed
```

此命令不接触内存，依次：

- 核对密钥环的账户绑定，并认证当前数据库首页；
- 认证 WAL 头、generation salt 和累计 checksum，拒绝当前有效帧/损坏日志或非空 rollback journal；允许已 reset 的旧代预分配空间或合法无帧日志，不删除源日志；
- 拒绝账户范围外路径以及已有输出目录，复制/解密前后核对源与 sidecar 指纹；
- 为联系人库和所有 `message_数字.db` 分片制作稳定副本；
- 对副本逐页 HMAC 验证、解密并执行 SQLite `quick_check`；
- 读取全部分片、关联联系人/发送者、解码文本与去重，生成 `chats.jsonl`。

输出示例：

```text
exports/run-001/
  working-copy/contact/contact.db
  working-copy/message/message_0.db
  decrypted/contact/contact.db
  decrypted/message/message_0.db
  chats.jsonl
```

可加 `--self-id '<本人username>'` 标记 sent/received。未提供或发送者无法解析时方向为 unknown，不猜成对方。失败目录可能保留已验证的副本，但没有最终聊天文件，用户应知晓其中仍有隐私；不要默认删除或覆盖重跑。

### 为什么暂时需要退出？

当前页面式解密器不重放加密 WAL。即使内存读取成功，也不能只读主文件后宣称包含全部最新消息。正常退出后的稳定副本优先保证完整性，不自动关闭客户端，也不删 sidecar 绕过检查。未来提供正确的 SQLCipher-aware 在线备份前，不宣传全在线一键导出。

SQLite 的 WAL reset 不一定截断文件，旧帧盐值已失效时，文件可以非空但没有当前有效帧。`wal_state.py` 依照 [SQLite WAL 格式](https://sqlite.org/fileformat2.html#walformat) 判断，头损坏、当前帧校验失败或截断均保守拒绝，不把损坏当作空日志。当前代即使只有未提交的有效帧也暂不放行。

盐不同本身不足以证明 reset：工具还会检测“校验和属于当前头但帧盐损坏”，并通过旧首帧校验链重建旧代头，核对 salt-1 递增、salt-2 改变与头/帧一致性。无法证明旧代链时仍阻断；这是一种保守的离线判定，不是对任意损坏日志的恢复器。

## 高级单库入口（不是默认流程）

- `inspect_databases.py --root <目录>`：数据库头、表/列及 WAL 清单。
- `snapshot_database.py --source <输入> --output <新副本> --source-closed`：单库快照。明文使用 SQLite backup，包含已提交 WAL，但多库在线备份不等于跨库同一时点快照。
- `decrypt_sqlcipher.py --source <副本> --output <新解密库> --prompt-key`：本地隐藏输入单一32字节密钥；也可使用旧的单密钥 DPAPI `--protected-key`。
- `read_chats.py --contact-db <库> --message-dir <目录> --output <新.jsonl>`：直接读已解密副本；可用 `--conversation <精确username>` 筛选单会话。
- 旧 `capture_key.py --help`：保留外部 ABI 接入，必须自备可信 DLL、核对来源/许可/哈希/位数和 PID 并单独授权 Hook，不由 Skill 默认使用。

**新 keyring.dpapi 不等于旧单密钥 DPAPI 文件。** 不要把新密钥环传给 `decrypt_sqlcipher.py --protected-key`，统一 `wechat.py export --keyring` 会按库选择原始密钥并启用 raw-key 解密。

默认解密布局：4096 页面、80字节保留区、AES-256-CBC、HMAC-SHA512。手工 passphrase 使用 PBKDF2-HMAC-SHA512 256000次；自动扫描获得原始 AES key，使用 raw-key 模式，不能再做一次 PBKDF2。算法依据见 [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)。

## 消息字段契约与范围

- `contact(username, remark?, nick_name?)`
- `Name2Id(user_name)`，通过 rowid 关联发送者
- `Msg_<username MD5>`，含 `local_id, server_id, local_type, create_time, real_sender_id, message_content`

不支持的 Schema 明确失败，不按近似列名猜测。联系人缺失的消息也保留来源表。二进制或无法完整解压的数据保留原始 base64，不冒充成功正文。消息按分片/会话/时间组织，非全局排序；无 server_id 不强行跨分片去重。

图片/视频附件导出、语音转写、风格生成不属于默认范围。密钥和数据库不联网，依赖安装需要包源；将消息交给云端模型分析时另适用提供方数据处理规则。

## 故障处理

| 错误 | 含义与下一步 |
| --- | --- |
| ACCOUNT_SELECTION_REQUIRED / PID_SELECTION_REQUIRED | 多候选，核对本人账户与进程后显式指定 |
| ACCOUNT_NOT_FOUND | 指定当前用户的正确数据目录；不要求找 DLL |
| PROCESS_READ_DENIED | 权限不足，核对微信与终端权限级别；不自动提权 |
| WINDOWS_X64_PYTHON_REQUIRED / WEIXIN_AMD64_REQUIRED | 当前自动扫描仅支持 Windows AMD64 64位Python和微信 |
| KEYS_INCOMPLETE | 布局变化、选错进程/账户或相关库尚未加载；不能宣称版本必然不支持 |
| SCAN_TIMEOUT / SCAN_BYTE_LIMIT | 有界扫描未完成，可依据新状态显式调整预算 |
| KEYRING_ACCOUNT_MISMATCH | 账户目录不同，不跨账户复用；移动目录后应重新绑定捕获 |
| KEYRING_MISSING_OR_STALE | 所需库缺密钥或凭据过期；核对并重新捕获 |
| WEIXIN_STILL_RUNNING | 用户应正常退出；不自动终止客户端 |
| UNMERGED_JOURNAL | 未合并日志，正常退出后重做；不能删 sidecar |
| OUTPUT_EXISTS / OUTPUT_SIDECAR_EXISTS | 使用新的干净输出位置，不覆盖 |
| UNSUPPORTED_MESSAGE_SCHEMA | 提供脱敏表/列信息用于适配，不发送原始聊天 |

上游声明支持的版本不等于本项目已实测。内置运行时布局来源及MIT许可见 [NOTICE](../third_party/NOTICE.md)；当前合成测试不证明微信4.1.15.13现场可用。
