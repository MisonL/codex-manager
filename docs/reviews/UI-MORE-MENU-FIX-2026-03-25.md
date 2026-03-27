# UI More Menu Fix Validation - 2026-03-25

## 范围

- 修复账号页和设置页行内"更多"按钮点击无响应。
- 显式传递 `event` 给菜单开关和关闭函数，并在函数入口阻止事件冒泡。
- 补充菜单定位脚本的错误提示和默认定位回退。
- 验证页面点击关闭逻辑不会误伤当前菜单。

## 执行记录

1. 命令: `node --check static/js/accounts.js`
   - 退出码: `0`
   - 结果: JavaScript 语法检查通过。

2. 命令: `node --check static/js/settings.js`
   - 退出码: `0`
   - 结果: JavaScript 语法检查通过。

3. 命令: `git diff --check`
   - 退出码: `0`
   - 结果: 无空白和补丁格式错误。

4. 浏览器验证: `http://127.0.0.1:18002/accounts`
   - 结果: 注入示例账号后，点击"更多"可展开菜单。
   - 结果: 再次点击同一按钮可收起菜单。
   - 结果: 点击页面其他区域后，菜单可正常关闭。

5. 浏览器验证: `http://127.0.0.1:18002/settings`
   - 结果: 注入示例代理后，点击"更多"可展开菜单。
   - 结果: 再次点击同一按钮可收起菜单。
   - 结果: 点击页面其他区域后，菜单可正常关闭。

6. 浏览器脚本验证: 菜单定位异常回退
   - 结果: `positionMoreMenu` 在缺失 `.dropdown` 容器时不抛异常，并提示 `账号菜单定位失败，已回退为默认位置`。
   - 结果: `positionSettingsMoreMenu` 在缺失 `.dropdown` 容器时不抛异常，并提示 `代理菜单定位失败，已回退为默认位置`。

## 变更文件

- `static/js/accounts.js`
- `static/js/settings.js`
- `static/css/style.css`
