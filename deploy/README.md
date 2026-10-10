# 部署到云服务器（阿里云 ECS）

把工作台装到一台公网服务器上，之后手机、公司电脑打开浏览器就能用，不用带着 Mac 跑渲染。

---

## 0. 先选机器（很重要）

渲染是 **CPU 密集** 的，不是普通网页服务：

| 用途 | 最低配置 | 建议 |
|---|---|---|
| 偶尔出片（20-60s 视频） | 2 核 2G + 2G swap | **2 核 4G** 起步，更稳 |
| 经常出片 / 多套模板 | — | 4 核 8G |
| 磁盘 | 40G 系统盘 | 每条 60s 视频约占 0.5–1.5 GB，**按每月产量估** |

计算型实例（如 `ecs.c7`、`ecs.g7`）比通用型划算，因为吃的是单核性能。
**不需要 GPU**——整条链路是 CPU 渲染 + ffmpeg 编码。

> 2 核机器上，20 秒视频一套模板大约 3–6 分钟，60 秒约 10–18 分钟。
> 慢是正常的，任务会排队，页面能看到进度。

---

## 1. 一键安装

SSH 登进服务器（阿里云控制台 → 实例 → 远程连接，或本地终端 `ssh root@你的IP`），然后：

```bash
# 装上 git 和 curl 之后克隆（阿里云自带 curl）
dnf install -y git || apt-get install -y git
git clone https://github.com/lijianguo1121-bit/video-editing-studio.git /tmp/vs
cd /tmp/vs

# 跑安装脚本（口令自己换一个）
sudo STUDIO_PASS='你的访问密码' bash deploy/install.sh
```

脚本会做 9 步：识别系统 → 检查内存 → 装依赖（含**中文字体**，没有它中文全是方框）
→ 建专用用户 → 拉代码 → 装 Python 依赖 → 装无头 Chromium → 写配置 + 注册 systemd
→ 放行本机防火墙 → 自检并打印访问地址。

**可以重复执行**：再跑一次 = 更新代码 + 重启服务，已生成的成片不会丢。

---

## 2. 放行阿里云安全组（必须做，否则外网打不开）

阿里云在**本机防火墙之外**还有一层安全组，默认只开 22/3389。

控制台路径：

> **云服务器 ECS → 实例 → 点进你的实例 → 安全组 → 配置规则 → 入方向 → 手动添加**

填：

| 项 | 值 |
|---|---|
| 授权策略 | 允许 |
| 优先级 | 1 |
| 协议类型 | 自定义 TCP |
| 端口范围 | `8760/8760` |
| 授权对象 | `0.0.0.0/0`（图省事）或 **你自己的公网 IP/32**（推荐） |
| 描述 | 视频剪辑工作台 |

保存后立刻生效，不用重启服务器。

> 授权对象填 `0.0.0.0/0` 就是全网可达，靠访问口令挡人。
> 想要更安全：只填你办公室和家里的 IP；或干脆不开公网，走内网/跳板机。

---

## 3. 用起来

浏览器打开 `http://你的公网IP:8760/`，会弹一个账号密码框：

- 用户名：`admin`
- 密码：你装的时候设的那个

**手机上也能用**（Safari/Chrome 直接输网址），拖视频、选模板、看成片、下载。
手机浏览器能直接保存成片到相册。

出片落在服务器上：`/var/lib/video-studio/jobs/<任务名>/out/`，
页面上也能直接预览和下载。

---

## 4. 日常运维

```bash
systemctl status video-studio        # 看状态
systemctl restart video-studio       # 重启
systemctl stop video-studio          # 停
journalctl -u video-studio -f        # 实时日志（Ctrl+C 退出）
```

**改访问密码**：

```bash
nano /etc/video-studio.env           # 改 STUDIO_PASS=...
systemctl restart video-studio
```

**更新到最新代码**：

```bash
cd /opt/video-studio && /tmp/vs/deploy/install.sh    # 或直接重跑安装脚本
```

**磁盘满了**：成片和中间文件都在 `/var/lib/video-studio`，看哪个任务占地方：

```bash
du -sh /var/lib/video-studio/jobs/* | sort -h
rm -rf /var/lib/video-studio/jobs/<不要的任务>      # 一条 60s 视频约 0.5-1.5G
```

---

## 5. 关于安全

- **服务自己带口令门**：设了 `STUDIO_PASS` 之后，所有请求都要 HTTP Basic 认证，
  没口令连首页都打不开。口令用常数时间比较，防逐位试探。
- **没设口令时不允许监听公网**：`server.py --host=0.0.0.0` 而没设口令会**直接拒绝启动**，
  避免"以为在本地、其实全网可访问"。
- **服务以专用用户 `vstudio` 运行**，不是 root；代码在 `/opt/video-studio`，
  产物在 `/var/lib/video-studio`，互不干扰。
- **口令文件权限 600（仅 root 可读）**，`/etc/video-studio.env`。
- Basic Auth 是明文传输（HTTP）。要真正安全就套一层 HTTPS：
  最省事的是在前面挂个 Nginx + Let's Encrypt 证书，或者用阿里云的 SLB/CDN 做 HTTPS 卸载。
  **只是自己用、不涉密的话，Basic Auth + 安全组白名单已经够。**

---

## 6. 不想用脚本手动装？

<details>
<summary>展开看手动步骤</summary>

```bash
# 1) 依赖
apt-get update && apt-get install -y python3 python3-venv python3-pip git fonts-noto-cjk
#   Alibaba Cloud Linux: dnf install -y python3 python3-pip git google-noto-sans-cjk-fonts

# 2) 代码 + 虚拟环境
git clone https://github.com/lijianguo1121-bit/video-editing-studio.git /opt/video-studio
cd /opt/video-studio
python3 -m venv venv
./venv/bin/pip install -U pip numpy playwright imageio-ffmpeg

# 3) 浏览器（约 200MB）
export PLAYWRIGHT_BROWSERS_PATH=/opt/video-studio/pw-browsers
./venv/bin/python -m playwright install chromium
./venv/bin/python -m playwright install-deps chromium

# 4) 前台试跑，确认没问题
STUDIO_PASS='你的密码' VIDEO_STUDIO_WORKSPACE=/var/lib/video-studio \
  ./venv/bin/python scripts/server.py --host=0.0.0.0 --port=8760

# 5) 没问题再交给 systemd（照抄 deploy/install.sh 里第 8 步的 unit 文件）
```

</details>

---

## 7. 排错

| 现象 | 原因与处理 |
|---|---|
| 外网打不开，服务器上 `curl 127.0.0.1:8760` 正常 | 阿里云安全组没放行端口（见第 2 节） |
| 中文全是方框/豆腐块 | 没装中文字体。`apt install fonts-noto-cjk` 或 `dnf install google-noto-sans-cjk-fonts`，然后 `fc-cache -f` |
| 渲染到一半被杀 | 内存不够（Chromium 很吃）。加 swap 或升级到 4G |
| `Missing X server or $DISPLAY` | Chromium 没装全。`./venv/bin/python -m playwright install-deps chromium` |
| 报缺 `libnss3.so` / `libatk-1.0.so` 之类 | 同上，装浏览器运行库 |
| 页面一直转圈、任务排队不动 | 上一单还在渲染，`journalctl -u video-studio -f` 看进度；串行是故意的 |
| 磁盘满 | 见第 4 节清理旧任务 |
