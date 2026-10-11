# 内网网页录音

浏览器只在 HTTPS 或 localhost 页面开放麦克风。通过局域网 HTTP 地址访问时，即使服务器连接正常，也无法开始网页录音。

本项目提供一个独立的 Caddy HTTPS 入口，转发到已经运行的 HTTP 服务。它不加载模型，原有 HTTP API 继续使用原端口。

配置完成后，HTTP 页的录音按钮会显示「打开录音页面」，点击即可切到 HTTPS 的快速复刻 Tab；HTTPS 页面提供「开始录音」。

## 启动 HTTPS

需要 Linux 和 Docker Compose。在 `.env` 中配置访问地址和当前 HTTP 服务的地址：

```dotenv
HTTPS_PUBLIC_URL=https://voice.example.test:8443
HTTPS_UPSTREAM=127.0.0.1:8000
HTTPS_LOCAL_CA=1
```

上例仅使用示例域名。将其替换为访问设备能解析的域名或 IP，端口按自己的部署配置调整，然后执行：

```bash
bash scripts/start_https.sh
```

首次启动会生成本机独立的 CA，并导出公共根证书到 `static/local-ca.crt`。HTTPS 容器会自动重启和续期证书。CA 和私钥保存在 `.local/https/data`，需要持久保留；重新生成 CA 后，访问电脑需要重新信任证书。这些文件均不进入 Git 或 TTS 镜像。

修改 `.env` 后，重启 TTS 服务以更新页面中的 HTTPS 链接。本机通过 systemd 部署时：

```bash
sudo systemctl restart qwen3-tts-openai
```

通过 Docker 部署 TTS 时，先运行 HTTPS 启动脚本生成公共证书，再在 `docker-compose.yml` 的 `qwen3_tts.volumes` 中追加只读挂载 `./static/local-ca.crt:/app/static/local-ca.crt:ro`，执行 `docker compose up -d` 更新容器。这样 HTTP 页面才能提供证书下载。HTTPS 代理单独使用 `docker-compose.https.yml`；查看状态和日志：

```bash
docker compose -f docker-compose.https.yml ps
docker compose -f docker-compose.https.yml logs --tail 50
```

## 在访问电脑上启用录音

1. 在自己的 HTTP 工作台「快速复刻」Tab 点击「下载内网证书」，或在工作台地址后追加 `/static/local-ca.crt` 下载公共证书。
2. 在访问电脑的证书管理器中，将证书导入受信任根证书。
   - Windows：打开下载的证书，选择「安装证书」→「当前用户」→「将所有的证书都放入下列存储」→「受信任的根证书颁发机构」。
   - macOS：将证书导入「钥匙串访问」，打开该证书的「信任」，设为「始终信任」。
   - Linux：按发行版导入系统 CA；例如 Ubuntu/Debian 将证书复制到 `/usr/local/share/ca-certificates/qwen3-tts-local.crt` 后执行 `sudo update-ca-certificates`。浏览器使用独立证书库时，也需在浏览器证书设置中导入该 CA。
3. 重新打开浏览器，在工作台点击「打开录音页面」，进入 `HTTPS_PUBLIC_URL` 对应的 HTTPS 地址。证书受到信任后，点击「开始录音」，允许网站使用麦克风。

Firefox 使用独立证书库时，在「设置」→「隐私与安全」→「证书」→「查看证书」→「证书颁发机构」中导入并信任此 CA。

网页无法替访问电脑安装证书或授予麦克风权限。公共 CA 文件可下载，私钥始终留在服务器。开启接口鉴权时，HTTP 和 HTTPS 属于不同网页来源，在 HTTPS 页面重新填写 API Token。

## 使用已有证书

已有受信任域名与 HTTPS 代理时，只需设置 `HTTPS_PUBLIC_URL` 指向该工作台地址，并保持 `HTTPS_LOCAL_CA=0`。也可以同时设置 `TLS_CERTFILE`、`TLS_KEYFILE`，让 TTS 直接在 `PORT` 提供 HTTPS；此时不需要本页的独立代理。

参考：[浏览器麦克风的安全上下文要求](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)、[Caddy 内网 HTTPS 与证书信任](https://caddyserver.com/docs/automatic-https#local-https)。
