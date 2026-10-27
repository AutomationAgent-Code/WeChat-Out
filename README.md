# WeChat-Out

一个面向 Codex 的微信聊天记录处理 Skill，用于组织以下工作流：

- 从用户本人授权的 Windows 微信数据或现成导出文件中整理聊天记录
- 按联系人标签筛选并导出结构化聊天数据
- 从“我发送”的消息中分析个人回复风格
- 生成可复用的风格画像、提示词、指标和 few-shot 样本

> 本仓库当前提供的是 **Codex Skill 工作流和数据安全约束**，不包含微信数据库解密程序，也不包含任何聊天记录、账号、密钥或个人数据。

## 安装

将仓库克隆到 Codex Skills 目录，并确保目录名为 `wechat-chat-style`。

### Windows PowerShell

```powershell
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git `
  "$env:USERPROFILE\.codex\skills\wechat-chat-style"
```

### macOS / Linux

```bash
git clone https://github.com/AutomationAgent-Code/WeChat-Out.git \
  ~/.codex/skills/wechat-chat-style
```

重新启动或刷新 Codex 后，即可通过 `$wechat-chat-style` 调用。

## 使用示例

```text
用 $wechat-chat-style 分析这批微信聊天 CSV，生成我的回复风格画像
```

```text
用 $wechat-chat-style 检查现有微信数据库副本，并导出指定标签的聊天记录
```

```text
用 $wechat-chat-style 根据新增聊天记录更新风格提示词
```

## 预期输入

优先使用已经由用户合法导出的结构化文件：

- CSV
- HTML
- JSON
- 用户本人账户的只读 SQLite 数据库副本

数据库结构会随微信版本变化。Skill 会先检查实际 Schema、消息分片和时间范围，不会把某一版本的字段或密钥方案当作通用规则。

## 预期输出

根据输入情况，可以生成：

```text
exports/<日期>/
  <标签>_聊天记录.csv

style/<日期>/
  style_metrics.json
  style_profile.md
  ai_style_prompt.md
  fewshot.md
```

风格样本仅用于模仿表达方式，不应直接继承历史聊天中的事实、技术结论、价格或承诺。

## 隐私与安全

- 只处理用户本人账户或用户明确授权的数据。
- 不绕过登录、访问控制或设备保护。
- 不向网络上传聊天数据库、密钥、wxid、联系人或聊天正文。
- 不修改或删除微信原始数据，导出和分析均写入独立目录。
- 运行中的 SQLite 数据应使用一致性副本，避免遗漏 WAL/SHM 数据。
- 数据库结构或密钥方案无法验证时停止处理，不猜测结果。

仓库的 `.gitignore` 默认排除数据库、聊天导出、解密目录、密钥和常见本地工具目录。提交前仍建议额外执行一次隐私扫描。

## 当前边界

- 仓库暂未提供数据库解密工具。
- 仓库暂未提供通用的标签导出和风格分析脚本；Skill 可以使用调用方项目中已有的兼容脚本，或直接处理用户提供的结构化导出文件。
- 不同微信版本的数据结构兼容性需要逐版验证。

## 目录结构

```text
WeChat-Out/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   └── windows-export.md
└── .gitignore
```

详细执行规则见 [SKILL.md](SKILL.md)，Windows 数据路径与一致性检查见 [references/windows-export.md](references/windows-export.md)。
