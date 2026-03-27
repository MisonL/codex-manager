# UI Refinement CSE Validation - 2026-03-25

## 范围

- 账号页按钮文案与计数态统一为 Scheme B。
- 设置页表单语义修复：显式 `label for`、`id`、`aria-label`、`autocomplete`。
- 设置页性能优化：非当前标签懒加载，代理模态框加入防抖校验。
- Docker 重建并验证健康状态。

## 执行记录

1. 命令：`git diff --check`
   - 退出码：`0`
   - 结果：无空白错误。

2. 浏览器验证：`http://127.0.0.1:18001/settings`
   - 结果：设置页重载后控制台无报错。
   - 结果：首屏仅请求 `settings` 页面、静态资源、`/api/settings`、`/api/settings/proxies`。
   - 结果：切换到上传标签后，才额外请求 `/api/cpa-services`、`/api/sub2api-services`、`/api/tm-services`。
   - 结果：打开“添加代理”模态框时，字段具备显式名称；保存按钮默认禁用，填写 `名称 + 主机 + 端口` 后自动启用。

3. 浏览器验证：`http://127.0.0.1:18001/accounts`
   - 结果：按钮文案显示为 `刷新列表`、`刷新 Token`、`验证 Token`、`检测订阅`。
   - 结果：勾选 2 个账号后，按钮文案显示为 `刷新 Token (2)`、`验证 Token (2)`、`检测订阅 (2)`。
   - 结果：行内“更多”菜单中的刷新文案同步为 `刷新 Token`。

4. 命令：`docker compose up -d --build`
   - 退出码：`0`
   - 结果：镜像 `codex-manager-webui:latest` 重建完成并启动容器。

5. 命令：`docker compose ps`
   - 退出码：`0`
   - 结果：`codex-manager-webui-1` 状态为 `healthy`。

6. 命令：`curl -s -o /tmp/codex-manager-15555.html -w '%{http_code}' http://127.0.0.1:15555/login`
   - 退出码：`0`
   - 结果：HTTP 状态码 `200`。

## 变更文件

- `templates/accounts.html`
- `static/js/accounts.js`
- `templates/settings.html`
- `static/js/settings.js`
