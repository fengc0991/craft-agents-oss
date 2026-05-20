# PaiWork 飞书发布

## 默认语义

飞书发布用 `paiobs_lark.py`。默认使用本 skill 自带的 `bin/lark-cli`、`.local-lark-cli/` 和 `.lark-cli-home/`，不依赖 `fastgpt-docker`、外部虚拟机或旧 `feishu-sync`。如需临时覆盖，可用 `PAI_OBS_LARK_CLI` 指向其他 `lark-cli`，或用 `PAI_OBS_LARK_HOME` 指向其他已授权配置目录；只有显式设置 `PAI_OBS_LARK_ALLOW_DOCKER=1` 时才允许退回 Docker 容器。

用户说“发送到飞书文档”“发到飞书文档”“把结果发飞书”时，默认含义不是只创建一个飞书文档，而是创建飞书文档，并通过飞书机器人把文档链接发送给指定接收人。因此聚合统计报告优先使用 `paiobs_quick_stats_report.py --publish-lark --send --send-as bot`；完整日报使用 `paiobs_daily_report.py` 统一编排，或在已有 Markdown/CSV 产物时使用 `paiobs_lark.py publish --send --send-as bot`。只有用户明确说“只创建文档/不要发送消息”时，才省略 `--send`。

## 自检

```bash
python3.11 scripts/paiobs_lark.py doctor --format json
```

`paiobs_lark.py doctor` 会展示 `missing_send_scopes` 和 `missing_optional_contact_scopes`。若联系人搜索缺少飞书 scope，可传 `--recipient-user-id ou_xxx` 跳过联系人搜索。若 user 身份缺少 `im:message` 或 `im:message.send_as_user`，不要停在“已创建文档”；改用 `--send-as bot` 通过机器人发送。

## 发布完整日报产物

```bash
python3.11 scripts/paiobs_lark.py publish \
  --report-md /tmp/paiobs_daily_reports/20260513_000000_to_20260514_000000/paiwork_daily_20260513_000000_to_20260514_000000.md \
  --focus-task-file /tmp/paiobs_daily_reports/20260513_000000_to_20260514_000000/paiwork_daily_20260513_000000_to_20260514_000000_focus_tasks.xlsx \
  --doc-path "paiwork_reports/2026-05-13_2026-05-14_paiwork_完整日报.md" \
  --bitable-path "paiwork_reports/2026-05-13_2026-05-14_paiwork_关注任务.bitable.xlsx" \
  --send \
  --send-as bot \
  --recipient fengchao \
  --format json
```

调试时加 `--dry-run`，不会创建飞书对象，也不会发送消息。

## 发布 quickstat 统计报告

```bash
python3.11 scripts/paiobs_quick_stats_report.py \
  --last-hours 1 \
  --publish-lark \
  --send \
  --send-as bot \
  --recipient fengchao \
  --doc-path "paiwork_reports/2026-05-18_210000_paiwork_过去1h任务统计报告.md" \
  --format json
```

## Profile、Scope 和 Fallback

支持 `--lark-profile`/`PAI_OBS_LARK_PROFILE` 切换飞书 app，默认 profile 为 `enterprise-fengchao`。发送通道可用 `--send-as` 或 `PAI_OBS_LARK_SEND_AS` 控制，默认 `auto`，可设为 `user` 或 `bot`。发布脚本会直接用 skill-local `lark-cli` 创建文档和导入关注任务表格，不再走旧的 `feishu-sync` profile。

关注任务 xlsx 导入多维表后，发布脚本会把关注类型、关注原因、queryid、session_id、query 内容、answer 内容、错误信息、低分原因、改进建议、证据、skills、责任人、用户/机构/产品等文本字段强制更新为普通文本字段，避免飞书根据逗号/分号自动识别成多选标签；`query链接` 和 `最终文件产物` 字段会更新为 URL 字段；`查询一级分类`、`查询二级分类`、`问题一级分类`、`问题二级分类` 会更新为单选标签字段，渲染方式与入口列类似。责任人列保留普通文本，以支持 `@王宝涵 @王意荃` 这类多个 @ 人写在同一单元格。由于飞书在文本列改为单选列时可能清空原单元格文本，发布脚本会在字段类型更新后按 xlsx 原值回写这些分类字段；回写按相同字段 patch 分组后调用 `record-batch-update`，避免逐行 `record-upsert` 造成分钟级等待。回执记录在 `publish.bitable.select_value_updates`。导入多 sheet xlsx 时，发布脚本会对导入后的所有 table 执行字段类型更新。`query链接` 单元格格式为 `http://192.168.15.57:30100/?queryid=<queryid>`，点击可直达 30100 的 query 查看页；`最终文件产物` 只保留文件产物的有效内容链接，并在表格列顺序中紧跟 `answer内容`。manifest 的 `publish.bitable.field_updates` 会记录字段类型更新回执；字段已是目标类型时按 no-op 成功处理，不作为发布错误。

若切换企业版 app，先新增 `lark-cli` profile，并通过 `--lark-profile` 或 `PAI_OBS_LARK_PROFILE` 指定。若 `--send-as auto` 下 user 消息 scope 缺失，默认会尝试 bot 通道。

如果飞书创建文档/表格接口触发 quota 或目录同步错误，日报入口默认降级发送 Markdown 和关注任务 xlsx 文件附件，并在 manifest 里记录失败原因。若不希望飞书发布失败时降级发送文件附件，可直接调用发布脚本时加 `--no-fallback-files`。

若飞书返回 `This month's API call quota has been exceeded`，只能等额度恢复或更换应用/授权后再发送。
