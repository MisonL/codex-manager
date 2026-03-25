# 导出菜单层级修复验证 2026-03-25

## 变更范围

- 文件: `static/css/style.css`
- 目的: 修复账号管理页面导出下拉菜单被表格表头遮挡的问题

## 执行记录

1. 启动本地服务

```bash
./.venv/bin/python webui.py --host 127.0.0.1 --port 18000 --access-password admin123
```

- 退出码: 运行中
- 结果: 服务成功启动并可访问

2. 浏览器验证

- 访问地址: `http://127.0.0.1:18000/accounts`
- 验证方式: 登录后进入账号管理页，临时移除导出按钮禁用态并展开 `#export-menu`，检查菜单与表头重叠区域的顶层元素
- 关键结果:
  - `.toolbar-card` 计算后 `z-index = 30`
  - `.toolbar` 计算后 `z-index = 31`
  - `.toolbar-right` 计算后 `z-index = 32`
  - `.dropdown-menu` 计算后 `z-index = 2200`
  - `.data-table th` 计算后 `z-index = 5`
  - 菜单与 `状态`、`CPA`、`订阅` 表头的重叠采样点顶层元素均为 `.dropdown-item`

3. 改动范围检查

```bash
git diff --name-only
```

- 退出码: 0
- 结果: 仅包含 `static/css/style.css`

## 结论

本次修复已确认导出菜单展开后位于表格表头之上，遮挡问题已消除。
