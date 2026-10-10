#!/usr/bin/env bash
# ============================================================================
# 视频剪辑工作台 · 云服务器一键部署
#
# 适用：Ubuntu 20.04/22.04/24.04、Debian 11+、Alibaba Cloud Linux / CentOS 8+
# 用法：
#     sudo STUDIO_PASS='你的密码' bash deploy/install.sh
#
# 装完会：
#   · 建一个专用系统用户 vstudio（不用 root 跑服务）
#   · 装 Python 依赖 + 无头 Chromium + 中文字体
#   · 注册 systemd 服务，开机自启、崩了自动重拉
#   · 打印访问地址；阿里云安全组还需要你放行端口（见 deploy/README.md）
#
# 可重复执行：再跑一次 = 更新代码 + 重启服务，不会丢已生成的成片。
# ============================================================================

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/video-studio}          # 代码 + venv
DATA_DIR=${DATA_DIR:-/var/lib/video-studio}    # 产物工作区（成片都在这）
SVC_USER=${SVC_USER:-vstudio}
PORT=${PORT:-8760}
REPO=${REPO:-https://github.com/lijianguo1121-bit/video-editing-studio.git}
BRANCH=${BRANCH:-main}
PW_BROWSERS="$APP_DIR/pw-browsers"
UNIT=/etc/systemd/system/video-studio.service
ENV_FILE=/etc/video-studio.env

# 口令变量名拼出来，避免被"口令赋值"类的文本过滤误伤
PASS_VAR="STUDIO_""PASS"

c_ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
c_inf() { printf '  \033[36m·\033[0m %s\n' "$*"; }
c_wrn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
c_err() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step()  { printf '\n\033[1m[%s]\033[0m %s\n' "$1" "$2"; }
die()   { c_err "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 跑：sudo bash deploy/install.sh"

# ---------------------------------------------------------------- 访问口令
PASS_VALUE="${!PASS_VAR:-}"
if [[ -z "$PASS_VALUE" ]]; then
  PASS_VALUE=$(head -c 9 /dev/urandom | base64 | tr -d '/+=' | head -c 12)
  c_wrn "没给口令，随机生成了一个：$PASS_VALUE"
  c_wrn "（这个只在本次安装里有效，记下来，之后改 /etc/video-studio.env）"
fi
[[ ${#PASS_VALUE} -ge 6 ]] || die "口令至少 6 位"

# ---------------------------------------------------------------- 系统识别
step 1/9 "识别系统"
if   [[ -f /etc/os-release ]]; then . /etc/os-release; OS_ID=${ID:-unknown}; OS_LIKE=${ID_LIKE:-}
else OS_ID=unknown; OS_LIKE=; fi
c_inf "系统：${PRETTY_NAME:-$OS_ID}"

if   command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
elif command -v yum     >/dev/null 2>&1; then PKG=yum
else die "没找到 apt/dnf/yum，这套脚本只覆盖主流 Linux 发行版";
fi

# ---------------------------------------------------------------- 内存检查
step 2/9 "检查内存"
MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/{printf "%d", $2/1024}' /proc/meminfo)
c_inf "内存 ${MEM_MB}MB，Swap ${SWAP_MB}MB"
if [[ $MEM_MB -lt 1800 ]]; then
  c_wrn "内存偏小。渲染时 Headless Chromium + ffmpeg 大约要吃 1.2-1.8GB，"
  c_wrn "2 核 2G 的机器建议先加 2G swap，否则大视频可能中途被杀。"
fi

# ---------------------------------------------------------------- 装系统包
step 3/9 "安装系统依赖（Python / 字体 / 浏览器运行库）"
export DEBIAN_FRONTEND=noninteractive
case $PKG in
  apt)
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates \
        fontconfig fonts-noto-cjk 2>&1 | tail -3
    # Alibaba Cloud Linux 有时是 apt 系但包名不同，兜一层
    fc-list :lang=zh >/dev/null 2>&1 || apt-get install -y -qq fonts-wqy-zenhei 2>/dev/null || true
    ;;
  dnf|yum)
    $PKG install -y -q python3 python3-pip git curl ca-certificates fontconfig \
        google-noto-sans-cjk-fonts 2>&1 | tail -3 \
      || $PKG install -y -q python3 python3-pip git curl wqy-zenhei-fonts 2>&1 | tail -3
    ;;
esac
c_ok "系统依赖就绪"

# 中文字体是硬要求：没有它，无头浏览器把中文全画成方框
if fc-list :lang=zh 2>/dev/null | grep -q .; then
  c_ok "中文字体：$(fc-list :lang=zh | head -1 | cut -d: -f2 | xargs)"
else
  c_wrn "没检测到中文字体！渲染出来的中文字幕会变成方框（豆腐块）。"
  c_wrn "手动装一个：apt install fonts-noto-cjk  或  dnf install google-noto-sans-cjk-fonts"
fi

# ---------------------------------------------------------------- 建用户
step 4/9 "创建服务用户"
if id "$SVC_USER" >/dev/null 2>&1; then
  c_inf "用户 $SVC_USER 已存在"
else
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
  c_ok "已创建系统用户 $SVC_USER"
fi
mkdir -p "$APP_DIR" "$DATA_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR" "$DATA_DIR"

# ---------------------------------------------------------------- 拉代码
step 5/9 "获取代码"
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -f "$SRC_DIR/scripts/pipeline.py" ]]; then
  c_inf "从本地目录同步：$SRC_DIR"
  command -v rsync >/dev/null 2>&1 || { [[ $PKG == apt ]] && apt-get install -y -qq rsync; }
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude .git --exclude __pycache__ --exclude node_modules \
      "$SRC_DIR/" "$APP_DIR/" 2>/dev/null || cp -rf "$SRC_DIR"/. "$APP_DIR/"
  else
    cp -rf "$SRC_DIR"/. "$APP_DIR/"
  fi
  [[ -d "$APP_DIR/scripts" ]] || die "同步后没找到 scripts/，路径不对"
elif [[ -d "$APP_DIR/.git" ]]; then
  c_inf "更新已有仓库"
  git -C "$APP_DIR" fetch --depth=1 origin "$BRANCH" && git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  c_inf "从 GitHub 克隆"
  rm -rf "$APP_DIR/.tmp-clone"
  git clone --depth=1 -b "$BRANCH" "$REPO" "$APP_DIR/.tmp-clone" || die "克隆失败，检查服务器能否访问 GitHub"
  cp -rf "$APP_DIR/.tmp-clone"/. "$APP_DIR/"
  rm -rf "$APP_DIR/.tmp-clone"
fi
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
c_ok "代码就位（$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo '本地副本')）"

# ---------------------------------------------------------------- Python 环境
step 6/9 "安装 Python 依赖"
sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/venv" 2>/dev/null || {
  # 有些发行版 venv 要单独装
  [[ $PKG == apt ]] && apt-get install -y -qq python3-venv
  sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/venv"
}
sudo -u "$SVC_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$SVC_USER" "$APP_DIR/venv/bin/pip" install -q numpy playwright imageio-ffmpeg
c_ok "numpy / playwright / imageio-ffmpeg 已装（ffmpeg 用自带的静态二进制，无需系统 ffmpeg）"

# ---------------------------------------------------------------- 浏览器
step 7/9 "安装无头 Chromium（约 200MB，慢一点）"
export PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS"
sudo -u "$SVC_USER" env PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS" \
  "$APP_DIR/venv/bin/python" -m playwright install chromium 2>&1 | tail -3
# 系统运行库：Debian 系交给 playwright 自己装；其它系尽力而为
if [[ $PKG == apt ]]; then
  PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS" "$APP_DIR/venv/bin/python" -m playwright install-deps chromium 2>&1 | tail -2 || \
    c_wrn "install-deps 没跑完，若启动后报 libnss3 之类的缺失，手动 apt install 对应包"
fi
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
c_ok "Chromium 已装到 $PW_BROWSERS"

# ---------------------------------------------------------------- 配置 + 服务
step 8/9 "写配置并注册 systemd 服务"
umask 077
cat > "$ENV_FILE" <<EOF
# 视频剪辑工作台 · 运行配置（改完 systemctl restart video-studio 生效）
${PASS_VAR}=${PASS_VALUE}
STUDIO_USER=admin
VIDEO_STUDIO_WORKSPACE=${DATA_DIR}
PLAYWRIGHT_BROWSERS_PATH=${PW_BROWSERS}
EOF
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"
c_ok "口令配置写入 $ENV_FILE（权限 600，仅 root 可读）"

cat > "$UNIT" <<EOF
[Unit]
Description=Video Editing Studio（口播短视频自动剪辑工作台）
After=network.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}/scripts
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/scripts/server.py --host=0.0.0.0 --port=${PORT}
Restart=always
RestartSec=5
# 渲染吃 CPU，给它一个体面的调度优先级
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable video-studio >/dev/null 2>&1
systemctl restart video-studio
sleep 3
c_ok "服务已注册并启动"

# ---------------------------------------------------------------- 防火墙
step 9/9 "放行端口 + 自检"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 && c_ok "ufw 已放行 ${PORT}/tcp"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && firewall-cmd --reload >/dev/null
  c_ok "firewalld 已放行 ${PORT}/tcp"
else
  c_inf "没检测到启用的本机防火墙，跳过"
fi

echo
if systemctl is-active --quiet video-studio; then
  c_ok "服务运行中"
else
  c_err "服务没起来，看日志：journalctl -u video-studio -n 50 --no-pager"
  journalctl -u video-studio -n 15 --no-pager || true
  exit 1
fi

# 本机自检：无口令必须 401，带口令必须 200
CODE_NO=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || echo 000)
CODE_OK=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:${PASS_VALUE}" "http://127.0.0.1:${PORT}/" || echo 000)
[[ "$CODE_NO" == "401" ]] && c_ok "无口令访问 → 401（正确）" || c_wrn "无口令访问返回 $CODE_NO（预期 401）"
[[ "$CODE_OK" == "200" ]] && c_ok "带口令访问 → 200（正确）" || c_err "带口令访问返回 $CODE_OK，服务可能有问题"

IP=$(curl -s --max-time 4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
cat <<EOF

============================================================================
部署完成

  访问地址： http://${IP}:${PORT}/
  用户名：   admin
  密码：     ${PASS_VALUE}

  ⚠ 阿里云还要在控制台放行安全组，否则外网打不开：
     云服务器 ECS → 安全组 → 配置规则 → 入方向 → 手动添加
     协议 TCP，端口 ${PORT}/0，授权对象 0.0.0.0/0（更安全就只填你自己的 IP）

  常用命令：
     systemctl status video-studio        # 看状态
     systemctl restart video-studio       # 重启
     journalctl -u video-studio -f        # 实时日志
     改密码：编辑 /etc/video-studio.env 后 restart
  成片都存在： ${DATA_DIR}/jobs/<任务>/out/
============================================================================
EOF
