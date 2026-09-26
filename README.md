# 115SavePro

**115 网盘自动转存系统 · 离线下载 · QMediaSync 联动**

[![CI](https://github.com/xiaoming677/115savepro/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaoming677/115savepro/actions/workflows/ci.yml)
[![Build and Push Docker Image](https://github.com/xiaoming677/115savepro/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/xiaoming677/115savepro/actions/workflows/docker-publish.yml)
![Image Size](https://img.shields.io/badge/image-ghcr.io-blue)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

把 [xinyuLo/bdsavepro](https://github.com/xinyuLo/bdsavepro)（百度网盘版）的思路搬到 115 网盘上：  
定时把 115 分享链接里的文件转存到自己的目录，转存到新文件后**自动通知 QMediaSync 刮削生成 strm 并整理分类**；  
同时把 115 的**离线下载**能力（磁力 / ed2k / HTTP）也接了进来。

## 快速开始（一行命令）

```bash
mkdir -p config log && docker compose up -d
```

访问 `http://你的NAS IP:5000`，默认账号 **admin** / 密码 **zxcvbnm**。
更新：`docker compose pull && docker compose up -d`

- 后端：Flask + APScheduler + SQLite
- 网盘 SDK：[p115client](https://github.com/ChenyangGao/p115client)（ChenyangGao，MIT），  
  `requirements.txt` 里**锁定到实测通过的版本** `0.0.9.6.5.1`，保证构建可复现
- 前端：**零构建单页**（一个 HTML，不需要 npm / Node 构建）
- 登录 115：**扫码登录**（也可手动粘贴 Cookie）
- 需要 **Python ≥ 3.12**（`p115client` 硬性要求）

---

## 目录

1. [功能一览](#功能一览)
2. [快速开始](#快速开始)
3. [使用指南](#使用指南)
4. [QMediaSync 联动怎么配](#qmediasync-联动怎么配)
5. [数据与备份](#数据与备份)
6. [常见问题](#常见问题)
7. [与上游项目的差异](#与上游项目的差异)
8. [发布与更新（维护者）](#发布与更新维护者看这里)
9. [更新日志](#更新日志)
10. [License](#license)

---

## 功能一览

| 模块                | 能力                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------ |
| **账号**            | 扫码登录（多设备身份可选）、手动粘贴 Cookie、多账号切换、Cookie 有效性校验、空间用量显示                                                          |
| **转存任务**          | 多任务、启用/停用、单任务 cron 或跟随全局、正则过滤、正则替换                                                                           |
| **转存范围**          | 可只转存分享里的指定文件夹（逐层下钻 + 跨层级多选），不选则整条转存                                                                          |
| **去重**            | 按文件名 / 文件名+大小 / SHA1（115 秒传 SHA1 一致，改名也能拦住）；支持独立的「对比路径」                                                      |
| **排除清单**          | 按文件名排除，命中即跳过                                                                                                 |
| **离线下载**          | 提交磁力 / ed2k / HTTP / FTP / 种子链接到 115 云端；列出进度；删除 / 重试 / 清空                                                    |
| **文件管理**          | 浏览目录、新建文件夹、重命名、移动、复制、删除、搜索、生成分享链接                                                                            |
| **转存日志**          | 每次执行留档（SQLite），含配置快照、分级日志、去重跳过清单、实际新增清单，每任务保留最近 10 次                                                         |
| **通知**            | Bark / PushPlus / 钉钉 / 飞书 / 企业微信（机器人+应用）/ Telegram / Server酱 / ntfy / Gotify / PushDeer / SMTP / 自定义 Webhook |
| **空间告警**          | 定时巡检，超过阈值推送                                                                                                  |
| **QMediaSync 联动** | 手动绑定「转存任务 / 离线下载」↔「QMS 刮削目录」，有新文件时自动触发，支持手动触发与触发日志                                                           |

---

## 快速开始

### 方式一：docker compose（推荐）

镜像已发布，**匿名可拉取**，不需要登录：

```bash
mkdir -p config log
# 把 docker-compose.yml 放到当前目录，直接启动
docker compose up -d
```

访问 `http://你的NAS IP:5000`，默认账号 **admin** / 密码 **zxcvbnm**（登录后请立刻在「系统设置」改掉）。

### 方式二：docker-compose.yml 完整内容

没有现成文件的话，新建 `docker-compose.yml`，把下面这段**完整**粘进去：

```yaml
services:
  save115:
    image: ghcr.io/xiaoming677/115savepro:latest
    container_name: 115savepro
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config
      - ./log:/app/log
    environment:
      - TZ=Asia/Shanghai
```

然后启动：

```bash
mkdir -p config log
docker compose up -d
```

> **两个最容易踩的坑**
>
> 1. **缩进必须用空格**，YAML 不认 Tab。上面每一层是 2 个空格。
>    用 Tab 缩进会报 `top-level object must be a mapping`。
> 2. 服务名是 `save115` 而**不是** `115savepro` —— 以数字开头的键会被部分
>    compose 实现和网页版编辑器误解析。容器名仍然是 `115savepro`，使用上无感知。

想改用 Docker Hub 的镜像（国内通常更快），把 `image:` 那行替换成：

```yaml
    image: xiaoming677/115savepro:latest
```

想让 QMS 同机访问更省事，把注释打开：

```yaml
    network_mode: host
```

### 方式三：不用 compose，直接 docker run

```bash
mkdir -p config log
docker run -d --name 115savepro --restart unless-stopped \
  -p 5000:5000 \
  -v "$PWD/config:/app/config" \
  -v "$PWD/log:/app/log" \
  -e TZ=Asia/Shanghai \
  ghcr.io/xiaoming677/115savepro:latest
```

> `-v` 的写法是 `宿主机路径:容器内路径`，中间的 `:` 是分隔符。
> `$PWD` 是当前目录，所以**要先 `cd` 到数据目录所在位置再执行**。
> Windows CMD 用 `%cd%`，PowerShell 用 `${PWD}`。

### 方式四：本地构建镜像（改过代码才需要）

```bash
mkdir -p config log
docker build -t 115savepro:latest .
docker run -d --name 115savepro --restart unless-stopped \
  -p 5000:5000 \
  -v "$PWD/config:/app/config" \
  -v "$PWD/log:/app/log" \
  -e TZ=Asia/Shanghai \
  115savepro:latest
```

### 方式五：本机直接运行（开发调试）

```bash
pip install -r requirements.txt
python web_app.py          # 默认 0.0.0.0:5000，可用 PORT / HOST 环境变量覆盖
```

> **需要 Python ≥ 3.12**（`p115client` 的硬性要求，Docker 镜像用的是 `python:3.12-slim`）。

### 可用的镜像地址

| 来源 | 地址 | 需要登录 |
|---|---|---|
| **GHCR**（默认，零配置） | `ghcr.io/xiaoming677/115savepro:latest` | 否，公开可拉 |
| **Docker Hub** | `xiaoming677/115savepro:latest` | 否（国内拉取通常更快） |
| 本地构建 | `115savepro:latest` | 否 |

### 更新到最新版

在 `docker-compose.yml` 所在目录执行：

```bash
docker compose pull && docker compose up -d
```

**以后更新就重复这一条命令。** 就这样，不用改任何配置。

#### 怎么确认更新生效了

三种方式，任选其一：

| 方式 | 操作 |
|---|---|
| **看页面** | 打开面板，**左下角**会显示当前版本号（如 `v1.1.1`） |
| **看接口** | 浏览器访问 `http://你的IP:5000/api/version`，返回 `{"version":"1.1.1", ...}` |
| **看容器** | `docker inspect 115savepro --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'` |

#### 常见问题

**`docker compose pull` 提示 `up to date`，但版本没变？**

先跑 `docker compose up -d --force-recreate` 强制重建容器。
如果还不行，说明拉到的确实就是最新版，去 [Releases](https://github.com/xiaoming677/115savepro/releases) 看最新版本号对不对。

**为什么 `docker compose up -d` 说容器没重建？**

compose 只在**镜像摘要变化**时才重建。如果镜像确实更新了但仍没重建，用 `--force-recreate`。

**我是用 `docker build` 自己构建的镜像，怎么办？**

那 `pull` 对你没用，要重新构建：

```bash
docker compose up -d --build        # compose 项目里带 build: . 时
# 或
docker build -t 115savepro:latest . && docker compose up -d --force-recreate
```

建议直接改用已发布的镜像（把 `image:` 指向 `ghcr.io/...` 或 `xiaoming677/...`），
这样以后更新只要 `pull` 一条命令。

**数据会不会丢？**

不会。账号 Cookie、转存任务、历史、QMS 绑定全部在 `./config` 挂载卷里，
升级镜像只换程序不碰数据。稳妥起见可以先备份：

```bash
tar czf 115savepro-config-$(date +%Y%m%d).tar.gz config/
```

**旧镜像占空间怎么办？**

```bash
docker image prune -f            # 清理悬空镜像（安全）
# 想更彻底地清掉未被使用的镜像：
docker image prune -a -f
```

> `-a` 会删掉所有没有被容器使用的镜像，执行前确认没有别的镜像在用。

### 飞牛 NAS 上要注意的

- 如果你已经用 **CloudDrive2** 把 115 挂载到本机，QMediaSync 的刮削目录要指向**挂载后的本地路径**，  
  而 115SavePro 里的「保存路径」填的是 **115 网盘里的路径**（例如 `/我的影视/剧集`）。两者是同一个位置的两个视角。
- 如果 QMediaSync 也在这台 NAS 的 Docker 里，把 `docker-compose.yml` 里的 `network_mode: host` 打开，  
  然后在 QMS 连接设置里填 `127.0.0.1` + QMS 端口（默认 `12333`）即可，最省事。

---

## 使用指南

### 第 1 步：登录 115

左侧「**账号管理**」→「**扫码登录 115**」：

1. 选登录设备（不确定就保持默认「115生活_支付宝小程序」）
2. 页面上会出现二维码，用 **115 手机 App** 扫码
3. 手机上点「确认登录」，页面会提示登录成功
4. 点「**保存账号**」

> 扫码轮询会把状态实时显示出来：等待扫码 → 已扫码待确认 → 登录成功。  
> 也支持「手动粘贴 Cookie」：浏览器登录 115 网页版 → F12 → 应用/存储 → Cookie，复制 `UID`、`CID`、`SEID`、`KID` 四项。

### 第 2 步：创建转存任务

「**转存任务**」→「**新建任务**」：

| 字段      | 说明                                                            |
| ------- | ------------------------------------------------------------- |
| 分享链接    | **必填**。支持 `https://115.com/s/xxxxx?password=abcd` 这种带提取码的完整链接 |
| 提取码     | 链接里没带时才需要填                                                    |
| 任务名称    | 留空会自动抓取分享标题                                                   |
| 保存路径    | **必填**。115 网盘里的目录，不存在会自动创建。可点「选择」用目录选择器                       |
| 对比路径    | 可选。去重时跟这个目录比；留空就与保存路径比                                        |
| 去重模式    | 按文件名 / 文件名+大小 / SHA1                                          |
| 定时规则    | cron 表达式，留空则跟随「系统设置」里的全局定时                                    |
| 文件过滤正则  | 只转存文件名匹配的项，例如 `.*\.(mp4\|mkv)$`                               |
| 排除文件清单  | 每行一个文件名，命中即跳过                                                 |
| 指定转存文件夹 | 不选 = 整条分享全部转存；选了就只转存选中的项                                      |

**cron 示例**

| 表达式               | 含义               |
| ----------------- | ---------------- |
| `*/30 * * * *`    | 每 30 分钟          |
| `0 */6 * * *`     | 每 6 小时           |
| `0 10 * * *`      | 每天 10:00         |
| `0 8,12,20 * * *` | 每天 8 点、12 点、20 点 |

创建后点任务行里的「**执行**」可以立刻跑一次，会弹出**实时日志**。

### 第 3 步：离线下载（可选）

「**离线下载**」→「**新建下载**」，把磁力 / ed2k / HTTP 链接每行一个贴进去，选个保存目录后提交。  
115 会在云端下载，下载完成后落到你的网盘目录里。

> 系统默认每 5 分钟巡检一次已完成的离线任务，完成时会触发绑定到「离线下载」的 QMS 刮削，并推送一条通知。  
> 巡检频率在「系统设置 → 离线下载巡检」里改。

### 第 4 步：文件管理

「**文件管理**」里可以像网盘客户端一样浏览目录，勾选后批量移动 / 复制 / 删除 / 生成分享链接。

---

## QMediaSync 联动怎么配

思路和上游项目一致：**手动把「转存任务」或「离线下载」绑定到「QMS 的某个刮削目录」**，  
之后只有绑定了的刮削任务会被触发，避免整库空跑。

### 1. 在 QMS 侧准备 API Key

登录 QMediaSync → **系统设置 → API 密钥 → 创建**，记下 `qms_` 开头的那串字符。

### 2. 在本项目填连接信息

「**连接 QMediaSync**」：

| 字段          | 说明                                                                           |
| ----------- | ---------------------------------------------------------------------------- |
| 地址 / 端口     | QMS 的地址。默认端口 `12333`。填**实际能访问到的地址**（同机 host 网络就填 `127.0.0.1`，跨容器填 NAS 内网 IP） |
| 鉴权方式        | API Key（推荐）或 账号密码                                                            |
| 启用 QMS 联动   | 总开关                                                                          |
| 转存到新文件后自动触发 | 关掉就只保留手动触发                                                                   |
| 触发前延时       | 挂载盘刷新有延迟时，可以设几秒再触发                                                           |

点「**测试连接**」确认通，再点「**保存配置**」。点「**查看刮削任务**」可以看 QMS 里现有的刮削目录。

### 3. 创建连接（绑定关系）

点「**创建连接**」：

- **绑定对象**：
  - `某个转存任务` → 该任务转存到**新文件**时触发
  - `离线下载完成` → 任意离线任务下载完成时触发
- **选择 QMS 刮削任务** → 从 QMS 拉到的刮削目录列表里选

连接列表里可以：**触发**（手动跑一次）、**日志**（最近 30 条）、**停用/启用**、**删除**。  
页面顶部还有「**立即触发全部**」。

### 触发规则（什么时候会触发）

| 情况                   | 是否触发             |
| -------------------- | ---------------- |
| 转存到新文件 + 有绑定         | ✅                |
| 转存了但全是已存在（去重跳过）      | ❌ 不触发，避免空跑       |
| 任务没有绑定任何连接           | ❌                |
| 总开关或「自动触发」关掉         | ❌                |
| 该条连接被停用              | ❌                |
| 离线任务完成 + 绑定了「离线下载完成」 | ✅（去重，同一个任务只触发一次） |

> QMS 配置和绑定关系存在 `config/history.db`（SQLite）里，不会被转存过程中的 `config.json` 回写覆盖。

---

## 数据与备份

| 路径                            | 内容                      | 是否需要备份 |
| ----------------------------- | ----------------------- | ------ |
| `config/config.json`          | 账号 Cookie、转存任务、通知配置     | ✅ 必须   |
| `config/history.db`           | 转存历史、QMS 配置与绑定、离线完成去重记录 | ✅ 必须   |
| `config/config.template.json` | 配置模板                    | 不用     |
| `log/`                        | 运行日志（按天切割，保留 14 天）      | 可选     |

**备份就是打包 `config/` 目录。** 换机器恢复时把 `config/` 放回去即可。

---

## 常见问题

**Q：扫码后一直停在「等待扫码」？**  
大概率是容器访问不了 `qrcodeapi.115.com`。检查容器的 DNS / 代理设置（注意：如果这台机器上跑了 Clash 之类的代理，容器内的 115 接口也走代理，可能反而更慢或失败）。

**Q：执行任务报「登录状态失效」？**  
115 的 Cookie 会过期。去「账号管理」点「校验」确认，失效了就重新扫码登录。

**Q：转存明明有文件，却提示「没有新文件需要转存」？**  
这是去重生效了。去重是拿**分享里的顶层项**和**保存路径下的直接子项**比名字。  
如果你希望在新目录里再存一份，把「对比路径」改成一个空目录，或改个保存路径。

**Q：115 转存有没有次数/风控限制？**  
有。115 对**分享接收**有频率限制，短时间大量接收会失败。  
建议把定时间隔拉开一些（比如 30 分钟以上），不要多个任务同分钟同时跑。

**Q：任务跑失败了想看细节？**  
任务行点「日志」看实时输出；跑完的记录在「转存日志」里，点「查看」能看到当时的配置快照、分级日志、去重跳过清单、实际新增清单。

**Q：QMS 连接测试失败？**

1. 确认地址端口能 ping 通 / telnet 通；
2. 确认 API Key 是在 QMS「系统设置 → API 密钥」里创建的；
3. 同机 Docker 场景，填 `127.0.0.1` 往往不行（容器内的 127.0.0.1 是自己），要用 NAS 内网 IP 或改用 host 网络。

**Q：为什么 QMS 触发了但没生成 strm？**  
本项目只负责**通知 QMS 去跑某个刮削任务**，strm 的生成规则、源目录、媒体类型都在 QMS 那边配置。  
要确认 QMS 里那个刮削任务的「源目录」指向的是能读到 115 文件的路径（挂载盘）。

**Q：改密码？**  
「系统设置 → 登录密码」。默认 `admin` / `zxcvbnm` 务必改掉。

---

## 与上游项目的差异

| 项      | bdsavepro（上游）                    | 115SavePro（本项目）                       |
| ------ | -------------------------------- | ------------------------------------- |
| 网盘     | 百度网盘（`baidupcs-py`，BDUSS/STOKEN） | 115 网盘（`p115client`，UID/CID/SEID/KID） |
| 登录     | 只能手填 Cookie                      | **扫码登录**为主，Cookie 为辅                  |
| 去重键    | MD5                              | **SHA1**（115 秒传机制，改名也能去重）/ 文件名+大小     |
| 转存范围   | 逐层下钻多选                           | 逐层下钻多选 + 跨层级混选                        |
| 离线下载   | 无                                | **有**（磁力 / ed2k / HTTP / 种子）          |
| 文件管理   | 无                                | **有**（浏览 / 重命名 / 移动 / 复制 / 删除 / 分享）   |
| 前端     | Vue 3 + Element Plus（需 Node 构建）  | **零构建单页 HTML**，镜像更小、部署更简单             |
| QMS 联动 | 手动绑定任务 ↔ 刮削目录                    | 手动绑定任务/离线下载 ↔ 刮削目录 + 离线完成自动触发         |
| 通知渠道   | 25+                              | 13 个主流渠道（含自定义 Webhook）                |

### 技术要点：为什么能直接复用上游的调度层

上游项目里「调度 / 通知 / 历史 / QMS 客户端」这几层是不依赖网盘类型的，  
它们只认一个约定：**存储层暴露 `transfer_share(task)`，返回 `{success, message, file_count, new_items, ...}`**。  
所以移植时只需要把存储层整个换掉（本项目 `storage_115.py`），上层几乎原样沿用即可。

---

## 发布与更新（维护者看这里）

### 自动构建是怎么工作的

仓库里带两个 GitHub Actions（`.github/workflows/`）：

| 工作流 | 触发 | 作用 |
|---|---|---|
| `docker-publish.yml` | 推送到 `main` / 推 `v*` tag / 手动 | 构建 Docker 镜像，推到 **Docker Hub** 和 **GHCR** |
| `ci.yml` | 每次 push / PR | Python 语法 + 前端 JS 语法检查 + 防敏感文件误提交 |

镜像标签规则：

| 触发方式 | 产生的标签 |
|---|---|
| 推送到 `main` | `latest`、`sha-xxxxxx` |
| 推 `v1.2.0` 这个 tag | `1.2.0`、`1.2`、`latest` |

### 镜像推到哪

| 目标 | 是否需要配置 | 说明 |
|---|---|---|
| **GHCR**<br>`ghcr.io/<owner>/115savepro` | **不需要** | 用仓库自带的 `GITHUB_TOKEN`，开箱即用。首次发布后包默认是**私有**的，想公开给别人拉需要手动改包可见性（见下） |
| **Docker Hub**<br>`<用户名>/115savepro` | 需要两个 Secret | 见下方「配置 Docker Hub」 |

工作流会先探测 Secrets：**没配就自动只推 GHCR，不会报错**（会在日志里给出提示）。

### 把 GHCR 包设为公开（可选）

如果你的仓库是公开的，但别人拉不到镜像，多半是因为 GHCR 包默认私有：

1. 打开 `https://github.com/users/xiaoming677/packages/container/115savepro/settings`
2. 页面底部 **Danger Zone** → **Change visibility** → 选 **Public** → 输入包名确认

> 自己用（`docker login ghcr.io` 或本机已登录）不需要这步。

### 配置 Docker Hub（可选，只做一次）

只需要 **1 个 Secret + 1 个 Variable**（用户名不是机密，所以放 Variable 更合适）：

**① 在 Docker Hub 建 Access Token**
Docker Hub → 右上角头像 → Account settings → Personal access tokens → New Access Token，
权限选 **Read & Write**，建好**立刻复制**（只显示一次）。

**② 在 GitHub 仓库里配置**

仓库 → Settings → Secrets and variables → Actions：

| 标签页 | 类型 | 名称 | 值 |
|---|---|---|---|
| **Variables** | New repository variable | `DOCKERHUB_USERNAME` | 你的 Docker Hub 用户名 |
| **Secrets** | New repository secret | `DOCKERHUB_TOKEN` | 上一步复制的 Token |

> ⚠️ 两个容易踩的坑：
> 1. **别建到 Environments 标签页里** —— 环境级 Secrets 只有工作流显式声明 `environment:` 才读得到，本工作流没声明。要用 **Actions** 标签页。
> 2. **名称必须一字不差**（全大写、下划线），值才填实际内容。

**③ 重新触发构建**
Actions → `Build and Push Docker Image` → Run workflow。
成功后推送目标会变成 **Docker Hub + GHCR**，日志里 `登录 Docker Hub` 会从 `skipped` 变成 `success`。

### 用命令行配置（更不容易出错）

```bash
# 用户名（值直接用参数给，不会填错输入框）
gh secret set DOCKERHUB_USERNAME -R <owner>/<repo>        # 或 gh variable set，两者都支持

# Token（交互式隐藏输入，不会留在命令历史里）
gh secret set DOCKERHUB_TOKEN -R <owner>/<repo>

# 核对
gh secret list -R <owner>/<repo>
gh variable list -R <owner>/<repo>
```

### 排查：构建成功但没推 Docker Hub

说明工作流没读到配置。逐项检查：

```bash
gh api repos/<owner>/<repo>/actions/secrets      --jq '.total_count'   # 仓库级 Secrets
gh api repos/<owner>/<repo>/actions/variables    --jq '.total_count'   # 仓库级 Variables
gh api repos/<owner>/<repo>/environments                               # 是不是建到环境里了
```

**仓库级为 0 但环境里有内容** → 就是建错标签页了，见上面 ⚠️。

### 以后怎么发新版

```bash
# 改完代码
git add -A
git commit -m "feat: 新增 xxx 功能"
git push

# 要发正式版就打个 tag
git tag v1.1.0
git push origin v1.1.0
```

- 只 `git push` → 更新 `latest`，NAS 上执行 `docker compose pull && docker compose up -d` 即完成升级
- 打 tag → 额外产出带版本号的镜像，方便回滚到指定版本

> 用户数据都在 `config/` 挂载卷里，**升级镜像不会丢数据**。

---

## 更新日志

### v1.1.1

- 新增**版本号显示**：面板左下角显示当前版本，`GET /api/version` 也可查，
  `docker inspect` 能看到构建时间和 commit —— 更新后一眼确认有没有生效
- 镜像补充 OCI 标准 LABEL（版本、源码地址、构建时间、commit）
- README 补充「更新到最新版」完整说明：如何确认更新生效、常见问题、
  数据备份、清理旧镜像

### v1.1.0

**修复 115 扫码登录「参数错误」**

- 修正扫码登录的**登录设备（app）不一致**问题：原先二维码是用 `web` 身份生成的，
  但换取 Cookie 时用的是页面上选的设备（如 `alipaymini`），115 会把这种
  不匹配视为参数错误而拒绝。现在 token / 二维码 / 换取凭据三步统一使用同一个 app。
- 扫码失败时不再只给一句笼统提示，而是**透出 115 返回的原始 message 与 errno**，
  并针对「参数错误」「IP登录异常」等情况给出对应建议。
- 新增**扫码登录自检**（「扫码登录 115」弹窗里的「接口自检」按钮）：
  不需要真的扫码，直接验证 token / 二维码 / 状态轮询三个接口是否都通，
  方便快速判断是代码问题还是 115 风控。
- 手动粘贴 Cookie 的入口保留，作为扫码被风控拦截时的兜底方案。

> 说明：如果自检三步全通过、但扫码确认后仍拿不到凭据，通常是 115 对本机 IP 的
> 风控（表现为「老乡验证失败」`errno=40101017` 或「IP登录异常」）。
> 换网络/挂代理，或直接用「手动粘贴 Cookie」。另外建议把登录设备换成
> `115生活_支付宝小程序`（默认值，比 `网页端` 更不容易触发风控）。

### v1.0.0（首个版本）

- 从 0 实现 115 网盘存储层（基于 `p115client`），支持**扫码登录**与 Cookie 导入、多账号切换
- 转存任务：定时执行、启用/停用、正则过滤与替换、排除清单、指定转存文件夹（逐层下钻 + 跨层级多选）
- 去重三档：文件名 / 文件名+大小 / **SHA1**（115 秒传机制下改了文件名也能拦住），支持独立的对比路径
- **离线下载**：磁力 / ed2k / HTTP / FTP / 种子提交到 115 云端，进度查看、删除、重试、清空
- **文件管理**：浏览、新建文件夹、重命名、移动、复制、删除、搜索、生成分享链接
- **QMediaSync 联动**：手动绑定「转存任务 / 离线下载」↔「QMS 刮削目录」，
  仅在转存到新文件时触发（避免空跑），离线下载完成自动触发，带触发日志
- 转存历史落 SQLite（配置快照 + 分级日志 + 去重跳过清单 + 实际新增清单），每任务保留最近 10 次
- 13 个通知渠道、空间用量告警、零构建单页前端（免 npm 构建）、Docker 化 + GitHub Actions 自动构建

---

## License

本项目为二次开发作品，沿用上游的 **AGPL-3.0**（见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)）。
`p115client` 为 MIT。115 网盘接口为非官方公开接口，请合理使用，注意账号安全与风控。
