# 安全审计报告

## 执行摘要

- **审计对象**：`video-editing-studio`（本仓库）
- **审计范围**：`SKILL.md`、`README.md`、`scripts/*.py`、`assets/*`、`references/*`
- **发现问题总数**：2 个，**均为 P1（无需关注阻断）**
  - P0 阻断级：**0 个**
  - P1 需关注：2 个（本次已修复）
- **安全评分**：**95 / 100**

---

## P0 阻断级风险

✅ **未发现 P0 风险。**

未出现以下任一危险组合：

- 从远程下载内容并执行（`curl | bash`、下载脚本后 `eval`）
- 自动读取敏感文件（`~/.ssh`、`.env`、`credentials`）并外发
- 自动执行破坏性命令（`rm -rf`、修改系统配置）
- 以 `2>/dev/null`、`nohup`、`--quiet` 等方式隐蔽执行上述操作
- 权限提升（`sudo` / `chmod 777`）配合危险操作

---

## P1 需关注风险（已修复）

### 1. 依赖安装指引未提示虚拟环境

- **位置**：`README.md`、`references/troubleshooting.md`、`scripts/doctor.py`
- **原内容**：`python3 -m pip install numpy playwright imageio-ffmpeg`
- **风险描述**：直接对系统 Python 装包，可能与其它项目产生版本冲突
- **是否自动执行**：**否**。`doctor.py` 只**打印**建议命令，不会自己执行安装；
  用户需手动复制运行。因此属环境风险提示，非投毒。
- **修复**：已在三处补充 `python3 -m venv .venv && source .venv/bin/activate` 前置步骤。

### 2. 本地服务的监听范围未说明

- **位置**：`scripts/server.py`
- **原内容**：默认 `--host 127.0.0.1`，但文档未说明改成 `0.0.0.0` 的后果
- **风险描述**：服务本身无鉴权。若用户主动改成 `0.0.0.0`，
  同网段任何人都可访问工作台、上传文件并下载成片
- **是否自动执行**：**否**。默认值已是仅本机，需用户显式传参才会扩大暴露面。
- **修复**：已在 `README.md` 增加「安全说明」章节明确写出该风险。

---

## 详细检查结果

### 命令执行检查

搜索关键词：`curl`、`wget`、`eval(`、`exec(`、`os.system`、`subprocess`、`Popen`

- 命中：`scripts/pipeline.py`、`scripts/preview.py`、`scripts/build_assets.py`
- 判定：全部为**调用本机二进制**（`ffmpeg`、Playwright 自带 Chromium），
  参数以**列表形式**传递、**不经 shell**、不拼接远程输入。
  无 `shell=True`，无 `os.system`，无 `eval/exec`。
- 结论：**无风险**

### 隐蔽执行检查

搜索关键词：`silently`、`nohup`、`disown`、`>/dev/null`、`--quiet`、`hidden`

- 命中：全部为 CSS 的 `overflow:hidden` / `visibility:hidden`（页面渲染用）
- 结论：**无隐蔽执行**

### 敏感路径与凭证检查

搜索关键词：`~/.ssh`、`/etc/passwd`、`.env`、`credentials`、`token`、`api_key`、`password`

- 命中：仅 `scripts/paths.py:65` 的 `os.environ.get("VIDEO_STUDIO_WORKSPACE")`
- 判定：只读取一个自定义的环境变量（工作目录路径），不涉及凭证
- 结论：**无风险**

### 网络请求检查

- 发现的 URL：
  - `http://127.0.0.1:8760`（本地服务地址，出现在文档与 `server.py` 启动提示中）
  - `http://www.w3.org/2000/svg`（SVG 命名空间标识符，不产生网络请求）
- 前端 `fetch()` 调用：`/api/templates`、`/api/upload`、`/api/jobs`，**全部同源相对路径**
- Base64 编码检测：未发现可疑长编码串
- 结论：**无外部网络请求**

### 文件操作检查

- 读取：读取自身的引擎文件、任务配置、原片
- 写入：仅写入产物工作区（默认 `~/VideoStudio`）
- 删除：
  - `pipeline.py` 删 `frames/<style>.mov`（自己刚生成的一个中间文件）
  - `preview.py` 删 `preview_tmp/`（自己在任务目录内建的临时目录）
  - `doctor.py` 删自己刚写的 `.write_test` 探针文件
- 判定：**删除范围全部限定在自身工作区/任务目录内**，不触碰工作区之外的文件
- 结论：**无风险**

### 路径穿越防护

`server.py` 的 `safe_child()` 将所有静态文件请求限制在根目录内。
实测 `/media/library/../../../../etc/passwd` 返回 **HTTP 404**。

### 依赖安装风险检查

- **全局安装检测**：未发现 `pip install`（无虚拟环境）、`npm install -g`、
  `sudo pip`、`brew install` 等自动执行的全局安装命令
- **虚拟环境说明**：**本次已补齐**
- **依赖来源**：仅 PyPI 官方源，无 `--index-url` 非官方镜像，无 `git+https://` 直装

### 元数据异常检查

| 字段 | 值 | 判定 |
|---|---|---|
| `name` | `video-editing-studio`（20 字符） | 正常，小写连字符 |
| `description` | 160 字符 | 正常（<500） |
| `license` | MIT（见 `LICENSE`） | 正常，非占位符 |

### 描述与实际行为一致性

`SKILL.md` 声明的能力（上传视频 → 选模板 → 自动分节拍/配图/配乐 → 渲染合成 → 质检）
与 `scripts/` 中的实际实现**逐项对应**，未发现描述之外的隐藏行为。

---

## 审计结论

**风险等级**：✅ **P2 - 安全**

本 skill 不下载执行远程代码、不读取敏感文件、不外发数据、不做破坏性删除，
全部处理在本机完成。子进程仅调用本地 ffmpeg 与 Chromium，服务默认仅监听 127.0.0.1。

两处 P1 环境风险提示已在本次审计中修复。

**使用建议**：可安全使用。若要将服务暴露到局域网，请自行评估鉴权需求。
