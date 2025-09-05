# leaflow-auto
leaflow automatic check-in
leaflow 自动签到 控制面板脚本
支持在线新增多账户

## 环境变量说明

### 基础配置
- `PORT`: 服务端口，默认 8181
- `ADMIN_USERNAME`: 管理员用户名，默认 admin
- `ADMIN_PASSWORD`: 管理员密码，默认 admin123
- `JWT_SECRET_KEY`: JWT密钥，留空会自动生成

### 数据库配置（MYSQL)
- `DB_TYPE`: 数据库类型（设为mysql）
-  `MYSQL_DSN`: MySQL连接字符串，格式：`mysql://user:password@host:port/database`

### 数据库配置（备选方式，当MYSQL_DSN未设置时生效）
- `DB_TYPE`: 数据库类型（设置为sqlite），默认 sqlite

### 通知配置（可选）
- `TG_BOT_TOKEN`: Telegram Bot Token
- `TG_USER_ID`: Telegram User ID
- `QYWX_KEY`: 企业微信Webhook Key

## 主要改进

1. **MySQL连接问题修复**：添加了 `cryptography` 依赖包，解决认证问题

2. **简化MySQL配置**：支持通过单个 `MYSQL_DSN` 环境变量配置，自动解析连接参数

3. **中英文切换**：完整的国际化支持，默认显示中文，可切换英文

4. **移动端适配**：
   - 响应式设计，自动适配不同屏幕尺寸
   - 移动端操作按钮垂直排列
   - 隐藏次要列信息
   - 优化触摸操作体验

5. **Cookie输入优化**：
   - 支持JSON格式输入
   - 支持分号分隔的cookie字符串直接粘贴
   - 自动解析和转换格式
   - 提供格式提示

6. **界面美化**：
   - 现代化设计风格
   - 渐变色彩主题
   - 动画效果
   - Toast通知提示
   - 加载动画
