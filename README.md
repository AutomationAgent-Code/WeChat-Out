# WeChat-Out

一个面向 Codex 的 Windows 微信本地聊天记录读取 Skill。

它的核心用途是组织并约束这条读取链路：

```text
定位本人微信数据 → 创建一致性副本 → 打开或解密数据库
→ 验证 SQLite 完整性 → 发现实际 Schema → 读取联系人与消息分片 → 导出
```

标签筛选、格式转换和聊天风格分析只是读取完成后的可选后处理，不是本项目的核心功能。

> 本仓库不包含任何聊天记录、账号、密钥、wxid 或个人数据库，也不提供绕过微信登录和设备保护的能力。

## 安装

### Windows PowerShell

```powershell
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git `
  "$env:USERPROFILE\.codex\skills\wechat-chat-reader"
```

### macOS / Linux

```bash
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git \
  ~/.codex/skills/wechat-chat-reader
```

重新启动或刷新 Codex 后，通过 `$wechat-chat-reader` 调用。

## 使用示例

```text
用 $wechat-chat-reader 检查我这台 Windows 电脑上的微信数据，并导出可读聊天记录
```

```text
用 $wechat-chat-reader 读取这份微信数据库副本，先验证完整性再导出 JSONL
```

## 读取原则

- 只处理用户本人账户或明确授权的数据。
- 原始微信数据库保持只读，所有解密和导出写入独立目录。
- 微信运行时必须创建一致性副本，不能遗漏 WAL/SHM。
- 密钥不打印、不上传、不写入 Git；仅驻留内存或保存为当前 Windows 用户保护的数据。
- 不假定固定微信版本、数据库路径、账户目录、表名或加密参数。
- 解密后必须验证 SQLite 头、`PRAGMA quick_check`、表结构和消息时间范围。
- 多个 `message_*.db` 分片需要全部枚举并去重。
- 无法解析的消息保留类型和标识，不能猜测正文。

## 输入

- 用户本人微信数据的一致性副本
- 已解密的 SQLite 数据库
- 加密数据库副本以及用户授权提供的本人账户密钥

## 输出

建议输出到独立目录：

```text
working-copy/<日期>/   # 数据库一致性副本
decrypted/<日期>/      # 解密后的只读数据库
exports/<日期>/        # JSONL、CSV 或 HTML 聊天记录
```

## 当前边界

- 当前仓库提供 Codex Skill、读取流程和安全约束。
- 数据库密钥获取高度依赖微信版本和用户选择的兼容工具；仓库不捆绑第三方 DLL 或未获授权的二进制。
- 不同微信版本的数据库 Schema 和 SQLCipher 参数需要实际验证。
- 通用读取脚本仍在整理中；Skill 不会把未提供的脚本描述成已经可用。

## 隐私保护

`.gitignore` 默认排除数据库、WAL/SHM、聊天导出、密钥、解密目录、日志和常见第三方工具目录。提交前仍应进行独立隐私扫描。

详细执行规则见 [SKILL.md](SKILL.md)，数据库结构发现和一致性检查见 [references/windows-export.md](references/windows-export.md)。
