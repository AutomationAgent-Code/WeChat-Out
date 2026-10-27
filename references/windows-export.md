# Windows 微信导出路径

## 路由

按风险从低到高选择：

1. 用户已提供 CSV/HTML/JSON：直接解析，不接触微信数据库。
2. 工作目录中存在本次同步后的可读数据库：校验 SQLite 表和最新消息时间，再按标签导出。
3. 用户选择的导出工具能识别当前已登录账户：让用户在本机完成必要登录，使用其导出能力，随后只读验证产物。
4. 第三方解析工具仅在其明确支持当前微信版本和数据库 Schema 时使用。
5. 逆向适配属于开发任务：先收集版本、模块哈希、数据库头和失败日志；不得直接在主账户数据上反复试写。

## 常见数据库形态

- 联系人库：`db_storage\contact\contact.db`
- 消息库：`db_storage\message\message_0.db`（账户可能有多个分片）
- 标签成员可能位于联系人 `extra_buffer` 的 `label_list` 中。
- 单聊消息表可能以 `Msg_<联系人标识的 MD5>` 命名。
- 文本可能使用 zstd 压缩；解析失败时保留类型信息，不把二进制猜成文本。

这些名称只用于结构发现，不保证适用于所有微信版本。导出前检查 `sqlite_master`、实际字段、分片数量和内容编码。

## 一致性检查

- 可读打开数据库并检查 `PRAGMA quick_check`。
- 确认联系人表、发送者映射表和消息表存在。
- 对所有 `message_*.db` 建立清单，避免只导出第一个分片。
- 比较数据库最大 `create_time` 与用户期望的最新聊天日期。
- CSV 使用 UTF-8 BOM，避免 Excel 打开中文乱码。

## 可移植脚本调用示例

```powershell
python '<项目目录>\scripts\export_label_chats.py' `
  --contact-db '<contact.db>' `
  --message-db '<message数据库或目录>' `
  --output-dir '<exports\YYYY-MM-DD>' `
  --self-id '<本人标识>' `
  --label '<标签名称或ID>'
```

不得把个人标签名称、标签编号或本人标识硬编码进公开脚本。消息有多个分片时，应全部枚举、去重并验证时间范围。

```powershell
python '<项目目录>\scripts\generate_style_profile.py' `
  '<export1.csv>' '<export2.csv>' `
  --output-dir '<style\YYYY-MM-DD>'
```

示例中的路径和标识均为占位符。公开仓库应提供相应脚本，或在技能中明确要求使用用户项目自己的实现；不得依赖作者电脑上的绝对路径。
