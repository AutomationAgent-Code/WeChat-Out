# WeChat-Out

面向 Codex 的 Windows 微信本地聊天读取 Skill，附带可执行 Python 脚本。核心是读出本人聊天记录，不是分类或风格生成。

```text
选定本人账户 → 检查数据库 → 制作副本 → 本地密钥接入
→ 逐页认证解密 → SQLite 完整性检查 → 多分片读取 → JSONL
```

## 安装

```powershell
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git "$env:USERPROFILE\.codex\skills\wechat-chat-reader"
cd "$env:USERPROFILE\.codex\skills\wechat-chat-reader"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

需要 Python 3.11+。下面简写的 `python` 应使用该虚拟环境解释器。重新启动会话后调用 `$wechat-chat-reader`。数据库读取、离线解密和测试可跨平台运行；DPAPI 与可选进程适配器仅用于 Windows。

## 使用

在 Codex 中：

```text
用 $wechat-chat-reader 读取我本人的 Windows 微信聊天。先检查指定账户目录和现有工具，再制作副本并导出；缺密钥时说明本地接入步骤，不要让我把密钥发到聊天里。
```

已有解密副本时，可直接运行：

```powershell
python scripts/read_chats.py --contact-db '<contact.db>' --message-dir '<message分片目录>' --output 'exports/chats.jsonl'
```

加密数据库：

```powershell
python scripts/inspect_databases.py --root '<本人账户目录>'
# 正常退出微信后，对联系人库及每个消息分片分别制作副本
python scripts/snapshot_database.py --source '<加密源数据库>' --output 'working-copy/message_0.db' --source-closed
python scripts/decrypt_sqlcipher.py --source 'working-copy/message_0.db' --output 'decrypted/message/message_0.db' --prompt-key
```

`--prompt-key` 只在本地终端隐藏输入本人密钥，不要求用户在聊天中提供。已有 DPAPI blob 可用 `--protected-key`。

没有密钥时，提供了可选 `capture_key.py` 适配器：用户必须自备可信、合法且 ABI 匹配的 DLL，核对 SHA256/PID，并明确同意对本人微信进程安装 Hook。**仓库不捆绑第三方 DLL，也不是无需外部依赖、支持所有版本的一键密钥提取器。** 完整步骤、ABI、错误恢复及参数见 [Windows 操作指南](references/windows-export.md)。

## 已实现

| 脚本 | 功能 |
| --- | --- |
| inspect_databases.py | 显式目录内的明文/非明文检查、表字段与 WAL 清单 |
| snapshot_database.py | 明文 SQLite backup，或已关闭加密源的稳定副本 |
| decrypt_sqlcipher.py | SQLCipher 4 页面布局、逐页 HMAC、完整性校验、不覆盖输出 |
| capture_key.py | 可选外部 DLL ABI 接入，验证捕获凭据后保存为当前用户 DPAPI |
| read_chats.py | 实际字段检查、多分片读取、发送者关联、zstd、去重和 JSONL |

## 兼容性与验证

- 读取器支持指南列出的微信 4.x 形态字段契约，不凭客户端版本号推定兼容。
- 解密器实现一种 SQLCipher 4 SHA512/CBC 页面布局；不是所有 SQLCipher 版本/参数均支持。
- 测试使用合成 SQLite、合成加密页面和模拟消息；没有打包真实聊天或凭据。
- **微信 4.1.15.13 的真实 Hook、解密与全量导出尚未现场验证**。缺少 DLL/密钥不等于该版本一定不支持。
- 未导出图片/视频等附件文件、未转写语音；复杂消息保留原文或二进制 base64。

```powershell
python -m unittest discover -s tests -v
```

## 隐私与安全

只允许本人或明确授权的数据。原始数据库只读，不关闭安全防护、不自动退出微信、不提权、不绕过登录。Hook 是独立的有副作用操作，默认检查/读取不会调用它。

工具不包含上传或遥测功能；诊断摘要不打印正文/密钥。检查清单可能含账户路径，导出文件和 base64 仍含私人信息，必须存放于限制访问的目录。Python 内存擦除仅为尽力而为；将聊天交给云端模型分析不属于“纯本地处理”。

`.gitignore` 排除数据库、聊天导出、密钥、DLL、日志和工作副本，但不替代提交前审查。不要把这些文件附到 Issue 中。

## 仓库结构

```text
SKILL.md
agents/openai.yaml
references/windows-export.md
scripts/                 # 可执行实现
tests/test_reader.py     # 仅合成夹具
requirements.txt
```

工作流规则见 [SKILL.md](SKILL.md)。本仓库未指定开源许可证前，不应推定已获得任意再分发授权；第三方工具遵循其各自许可。
