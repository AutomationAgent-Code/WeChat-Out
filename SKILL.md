---
name: wechat-chat-reader
description: Read the user's own Windows WeChat local chat databases using bundled inventory, consistent-copy, authenticated decryption and message-export scripts. Use for 直接读取微信聊天记录、解析本地微信数据库、导出消息；不用于读取他人账户或绕过登录、访问控制。
---

# Windows 微信聊天记录读取

主任务是读出聊天，而不是分类或风格分析。先读 [references/windows-export.md](references/windows-export.md)，再使用本技能目录下的真实 `scripts/`。命令路径相对技能目录，不依赖作者的个人目录。

## 执行顺序

1. 确认用户授权的数据范围。多账户时用目录与当前登录状态核对，不能选择第一个账户或只按文件大小猜测。
2. `inspect_databases.py --root <选定数据目录>` 检查数据库头、Schema 和 WAL，不输出聊天正文。识别不到 SQLite 头只意味着“不是明文”，不能据此断言微信版本不兼容。
3. 明文数据库用 `snapshot_database.py` 的 SQLite backup 取得包含已提交 WAL 的单库快照。加密库仅在用户正常退出微信后使用 `--source-closed`；此参数是用户确认，不会替用户退出。若非空 WAL/热 journal 仍在，使用支持 SQLCipher 的一致性备份工具，不能删掉 sidecar 强行继续。
4. 使用 `decrypt_sqlcipher.py` 解密副本，密钥从本地隐藏输入或当前用户 DPAPI 凭据取得，不接受聊天中的明文密钥。每页 HMAC 和 SQLite `quick_check` 全部通过才发布输出。
5. 若缺少密钥，说明这是“缺少密钥来源”，不是“读取器未实现”。检查用户指定的已有兼容工具；如它提供本文档列出的 DLL ABI，可使用可选 `capture_key.py`。先核对来源、许可、SHA256、Python/DLL/微信位数、目标 PID 和本人账户，再单独取得用户对进程 Hook 的明确许可。不自动搜索磁盘上的 DLL 后运行，不关闭防护，不擅自提权。
6. `read_chats.py` 对已解密的联系人库和所有消息分片执行结构检查，关联联系人和发送者，解压文本，去重，导出 JSONL。不要用未知 Schema 猜列含义，也不要把无法解析的二进制当文本。
7. 报告消息数、分片数、去重数、未解析数量和时间范围。JSONL 按分片/会话排序，不声称全局时间排序；附件文件、语音转写、标签和风格生成不在本次读取器默认范围。

## 恢复与证据

- 先安装 `requirements.txt` 并检查脚本 `--help`；缺依赖不能直接要求用户提供已解密数据库。
- 版本号不在历史记录里时仍可做只读结构检查；是否兼容以 HMAC、完整性、字段契约和实际消息读取结果确认，不伪造版本支持列表。
- 区分 `KEY_NOT_RECEIVED`、`HMAC_FAILED`、`UNMERGED_JOURNAL`、`UNSUPPORTED_MESSAGE_SCHEMA` 和环境/依赖失败。HMAC 失败可能是密钥、参数、损坏或格式不匹配，不单凭它下确定结论。
- 停止已证实不安全或确定失败的操作，但可继续独立的只读诊断。不得反复尝试未知偏移、猜密钥或盲目更换解密参数。
- 没有可信密钥来源时如实报告必要外部依赖。本仓库有读取实现，但不捆绑第三方密钥提取二进制，也不保证任何微信版本的 Hook 均可用。

## 隐私

不上传数据库、消息、联系人、密钥或带账户路径的诊断。脚本的诊断摘要不含正文，但导出文件本身包含私人信息；放在当前用户限制访问的目录，不得提交 Git。Python 的内存擦除是尽力而为，不保证清除所有解释器副本。不得声称使用云端模型分析聊天时数据仍完全不出本机。

源库只读，输出不覆盖已有文件。整个账户跨库的同一时点一致性需要正常关闭客户端后制作副本；多个单库在线备份不等于跨库一致性快照。
