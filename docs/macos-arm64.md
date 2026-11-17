# Apple 芯片（arm64）开发方案

状态：方案。本分支还没有 Mac 可执行文件，也还没有平台分支代码。

目标：让同一份 Python 程序在 Apple 芯片的 Mac 上直连 hf-mirror。代理隔离、分片、总带宽上限和断点续传保持现在的行为。Windows x64 继续使用已经钉死的 curl 8.23.0_9。

执行者：在 Mac 上拉取本分支 `dev/macos-arm64`，按本文实现、测试，再推回这个分支。不要合并到 `master`。

## 范围

只做 Apple 芯片，`platform.machine() == "arm64"`，并且 Python 本身也是 arm64。Intel Mac、通用二进制（universal）、Rosetta 下的 x86_64 Python、App Store 上架和公证，都不做。

在 Rosetta 里跑的 Intel Python 会看到 `x86_64`。这种情况直接拒绝启动，并说明需要本机 arm64 的 Python。不要去调用 `/usr/bin/curl`，也不要调用 Homebrew、MacPorts 或 conda 里的 curl。

## 不要改的行为

这些已经在 Windows 上验证过，Mac 移植只换「启动哪一个 curl、给它什么环境」：

- 只允许初始地址 `https://hf-mirror.com` 和 `*.hf-mirror.com`。测试里的 `http://127.0.0.1` 仍然只在 `allow_loopback_http=True` 时放开。
- 参数顺序不变：可执行文件、`-q`、`--noproxy`、`*`、`--proxy`、空字符串、`--proto`、`--proto-redir`。正式下载的协议限制是 `=https`。
- `--next` 后面的每一段都要重复 `--noproxy *`、`--proxy ""`、`--proto`、`--proto-redir`、`--location`、`--fail-with-body` 和该段自己的 `-D`。分片传输不使用 `--limit-rate`，也不使用 `--retry`。
- 带宽仍然是一个共享令牌桶，单位 Mbps。默认 300。并发上限仍是 1 到 128。分片、慢连接换线、按偏移写入和断点续传的规则不动。
- 网络只允许出现在 `hfmd/curlsafe.py`。其它模块不增加 `subprocess`、`urllib`、`requests`、`httpx` 或 `aiohttp`。
- 不要把用户名、主目录、密钥路径、代理地址、带签名的 CDN 地址写进仓库、提交说明或日志。

`hfmd/limiter.py` 不改。`hfmd/download.py` 只改下面这一句提示，调度和写入逻辑不动。`third_party/curl/` 根目录里现有的 `curl.exe`、`libcurl-x64.dll`、`PIN.txt` 和它们的 SHA256 不改、不搬。

## 分支和提交

- 在 `dev/macos-arm64` 上工作。远端已经有这个分支时，从它拉取，不要另开分支名。
- 不要合并、变基或强推 `master`。本分支也不要强推。
- 提交身份用一次性的 `git -c user.name="hfmd" -c user.email="hfmd@local"`。不要改 git config。
- 远端保持现有的 SSH 地址。不要改成 HTTPS。
- 推送只在本文的任务做完并且测试通过之后进行：`git push -u origin HEAD`。
- 若 Git 自带的 SSH 认证失败，只为这一次命令设置 `GIT_SSH_COMMAND=/usr/bin/ssh`。不要把密钥路径写进仓库或 git config。

## 目录

Windows 文件留在原地。Mac 只新增：

```text
third_party/curl/macos-arm64/curl
third_party/curl/macos-arm64/PIN.txt
third_party/curl/macos-arm64/BUILD.txt
hfmd.command
hfmd-gui.command
```

`curl` 是单个 arm64 可执行文件，模式 `0755`。构建目录、目标文件、压缩包不要提交。`.gitignore` 已忽略 `__pycache__/`、`.venv/`、`downloads/`、`*.part` 和测试临时目录，保持这样。

`.gitattributes` 增加一行，把这个可执行文件标成二进制：

```text
third_party/curl/macos-arm64/curl binary
```

## 钉死的 curl

版本与 Windows 相同：curl **8.23.0**（上游发布日 2026-10-14）。源码包：

```text
https://curl.se/download/curl-8.23.0.tar.xz
```

curl.se 不提供和 Windows 那份对应的官方 Mac 二进制。Mac 上的这一份必须在 Apple 芯片的 Mac 上从上述源码编出来。不要拿 Homebrew、MacPorts、conda-forge 或 `/usr/bin/curl` 来充数，即使它们的版本号也是 8.23.0。

### 校验源码

下载 `curl-8.23.0.tar.xz`，再取得 curl.se 或 curl 官方 GitHub 发布页 `curl-8_23_0` 上公布的 SHA256。两边一致才解压。公布的校验值找不到时停止，不要编未校验的包，也不要在文档里写一个猜出来的哈希。

校验通过后，把源码包的 SHA256 写入 `BUILD.txt`。这个值是源码包的，不是可执行文件的。

### 配置

编译时的 `PATH` 只用 `/usr/bin:/bin:/usr/sbin`，避免 `pkg-config` 或 Homebrew 把头文件和库指到 `/opt/homebrew`。

TLS 使用 Apple Secure Transport，这样信任系统钥匙串，作用对应 Windows 构建上的 NativeCA。不要改用 LibreSSL 或 OpenSSL，也不要在仓库里再放一份 `cacert.pem`。

在源码树里配置（不要把本机的绝对路径写进 `BUILD.txt`，只记录这些选项）：

```sh
./configure \
  --disable-shared \
  --enable-static \
  --with-secure-transport \
  --without-openssl \
  --without-gnutls \
  --without-mbedtls \
  --without-wolfssl \
  --without-rustls \
  --without-amissl \
  --without-zlib \
  --without-brotli \
  --without-zstd \
  --without-libpsl \
  --without-libssh2 \
  --without-nghttp2 \
  --without-nghttp3 \
  --without-ngtcp2 \
  --without-libidn2 \
  --without-ca-bundle \
  --without-ca-path \
  --disable-ldap \
  --disable-ldaps \
  --disable-manual
```

不要加 `--without-ssl`。那个开关会把 TLS 整个关掉，和 `--with-secure-transport` 冲突。

当前下载不发送 `--compressed`，所以可以不链 zlib。HTTP/2 和 HTTP/3 也不要打开。连接复用靠 HTTP/1.1 的 `--next`，单元测试里的本地服务只说 HTTP/1.1。

`config.log` 里 TLS 后端应是 Secure Transport。若配置阶段改去链接 `/opt/homebrew` 或 `/usr/local` 里的库，停下来查原因，不要继续安装那些动态库。

### 产物

`make` 之后对 `src/curl` 做临时签名，再计算哈希。签名会改文件字节，哈希必须取签名之后的文件：

```sh
codesign -s - --force src/curl
shasum -a 256 src/curl
file src/curl
otool -L src/curl
src/curl -q -V
```

验收：

- `file` 显示 Mach-O 64-bit executable arm64。
- `otool -L` 只出现系统库：`/usr/lib/libSystem.B.dylib`，以及 `Security`、`CoreFoundation`、`CFNetwork` 这类系统框架。不能出现 `/opt/homebrew`、`/usr/local`、`@rpath` 下的第三方 dylib，也不能出现另一份 `libcurl`。
- `curl -q -V` 的第一行以 `curl 8.23.0 ` 开头，输出里能看到 SecureTransport。
- 若做不到「除系统库外不再依赖动态库」，停止并在提交说明里写明卡在哪一个库。不要把 Homebrew 的 dylib 放进仓库。

把签名后的文件复制为 `third_party/curl/macos-arm64/curl`，并 `chmod 755`。用 `git update-index --chmod=+x` 保证文件模式进库。

`PIN.txt` 写成：

```text
version=8.23.0
arch=arm64
tls=secure-transport
source=https://curl.se/download/curl-8.23.0.tar.xz
source_sha256=<源码包的 SHA256>
curl_sha256=<签名之后的可执行文件 SHA256>
```

`BUILD.txt` 只写源码 URL、源码包 SHA256、上面的 configure 选项，以及「临时签名 `codesign -s -`，哈希取签名后的文件」。不要写主目录、主机名、编译者用户名。

从浏览器或 GitHub 压缩包得到的副本可能带隔离属性。文档中说明：若系统拦截这个二进制，在仓库目录执行 `xattr -d com.apple.quarantine third_party/curl/macos-arm64/curl`。不要把这当作改用系统 curl 的理由。

## 代码

### `hfmd/curlsafe.py`

按 `sys.platform` 选择二进制。对外仍提供现在的函数：`curl_exe()`、`child_env()`、`command_prefix()`、`verify_bundled_curl()`、`system_proxy_enabled()`。

- `win32`：继续 `third_party/curl/curl.exe` 加同目录 `libcurl-x64.dll`，哈希常量保持现有两个值，`PINNED_BUILD` 保持 `8.23.0_9`。
- `darwin` 且 `arm64`：`third_party/curl/macos-arm64/curl`。只核对这一个文件，哈希常量与 Mac 的 `PIN.txt` 里 `curl_sha256` 相同。版本检查仍然只要求第一行以 `curl 8.23.0 ` 开头。
- 其它平台，包括 Intel Mac：`verify_bundled_curl()` 和 `curl_exe()` 抛出 `CurlError`，说明当前只提供 Windows x64 和 Apple 芯片。缺少文件或哈希不符也拒绝运行，提示不能改用系统 curl。

Windows 测试不要求 Mac 文件存在。Mac 哈希常量只在 darwin 分支使用。不要在 Windows 路径上打开 `macos-arm64/curl`。

`child_env()`：

- Windows 保持现在的白名单和 `System32` PATH。缺少 `SYSTEMROOT` 时仍然拒绝启动。
- Mac 的环境只放两项：`PATH=/usr/bin:/bin`，`TMPDIR=/tmp`。不从父进程复制其它变量。这样子进程看不到 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、名字里带 `proxy` 的变量、`CURL_HOME`、`HOME`、`USER`、`SSL_CERT_FILE`、`CURL_CA_BUNDLE`、`NETRC`，也看不到 `DYLD_LIBRARY_PATH`、`DYLD_FALLBACK_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`。
- 父进程的 `TMPDIR` 常常位于带用户名的目录。Mac 上固定写成 `/tmp`，不要把那个路径传进去。

`command_prefix()` 的参数顺序两边相同，只是第 0 个元素换成 `curl_exe()` 的结果。空字符串 `""` 必须作为单独的 argv 元素传给 `--proxy`。不要用 shell 拼接命令。

`_creation_flags()` 在 Mac 上返回 `0`。`creationflags=0` 在 POSIX 上可以传。不要传非 0 的 Windows 标志。

`system_proxy_enabled()`：

- Windows 继续只读注册表 `ProxyEnable`，不读 `ProxyServer`。
- Mac 调用绝对路径 `/usr/sbin/scutil --proxy`。解析时只认下面四行里值为 `1` 的开关：`HTTPEnable`、`HTTPSEnable`、`SOCKSEnable`、`ProxyAutoConfigEnable`。任一为 1 就返回 True。
- 不要读取或保存 `HTTPProxy`、`HTTPSProxy`、`SOCKSProxy`、端口和 PAC 地址。异常信息里不要带上 `scutil` 的原始输出。解析失败时返回 False。
- 把「一段文本进、一个布尔出」拆成纯函数，便于单测。单测不要真的去读这台机器的代理设置。

`download.py` 里的那句提示改成：「系统代理开着，但下载不会使用它。」没有打开时仍是「未使用系统代理。」

### 界面和启动脚本

`hfmd/gui.py`：Windows 继续用 `("Microsoft YaHei UI", 10)`。Mac 用 `("PingFang SC", 13)`。`vista` 主题的判断已经是「有才用」，Mac 上不要改成强制某个主题。

`hfmd.command` 和 `hfmd-gui.command`：

```sh
#!/bin/bash
cd "$(dirname "$0")" || exit 1
exec python3 -m hfmd "$@"
```

图形界面那个脚本把参数固定成 `gui`。两个文件都 `chmod 755`。不要写死 Python 的绝对路径。README 说明 Mac 需要带 Tk 的 Python 3.11 或更新版本（python.org 的 macOS 安装包带 Tk）。Homebrew 的 Python 经常没有 Tk；那种环境命令行仍然可用，图形界面会在 `import tkinter` 失败。无图形会话不把「窗口打不开」当成测试失败。

### README

保留现有 Windows 说明。另加一节 Apple 芯片：

- 二进制位置、版本 8.23.0、Secure Transport、启动时核对 SHA256。
- 明确不使用 `/usr/bin/curl`。
- 命令行例子使用 `~/models`，不要写真实主目录。
- 启动方式：`./hfmd-gui.command`，或 `python3 -m hfmd gui`，以及 `python3 -m hfmd download provider/model_name -c 32 -b 300 -d ~/models`。
- 测试命令：`python3 -m unittest discover -s tests`。

## 测试

现有用例继续保留，并在两边都能跑：

- `test_command_disables_proxy_before_anything_else`
- `test_piece_command_keeps_proxy_off_and_has_no_per_connection_cap`
- `test_poisoned_proxy_is_not_contacted`：代理连接数是 0。
- `test_two_ranges_share_one_connection`：两次请求，一次 TCP accept。
- `tests/test_download.py` 里的分片规划和本地镜像带宽用例。

按平台调整 `test_child_env_drops_proxy_and_curlrc_locations`：

- Windows：仍然要求环境里有 `SYSTEMROOT`，并且没有 `USERPROFILE`、`CURL_HOME` 和名字里带 proxy 的变量。
- Mac：`PATH` 等于 `/usr/bin:/bin`，`TMPDIR` 等于 `/tmp`，没有 `HOME`、`USER`、`CURL_HOME`、`DYLD_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`，也没有名字里带 proxy 的键。

源码扫描继续禁止 `urllib.request`、`http.client`、`requests`、`httpx`、`aiohttp`、`urlopen`、`System32\curl`、`System32/curl`、`shutil.which`。再禁止 `/usr/bin/curl` 和 `/opt/homebrew`。`System32` 这个词本身仍允许出现在 Windows 的 PATH 里。

为 Mac 增加、在 Windows 上跳过的检查：

- 可执行文件的 Mach-O 头是 64 位 arm64。用 Python 读文件头，不要为了这个检查再去调用系统 curl。
- `system_proxy_enabled` 的纯解析函数：输入里有 `HTTPEnable : 1` 和一行文档用地址 `HTTPProxy : 192.0.2.1` 时返回 True；四个开关都是 0 时返回 False。断言这个函数的实现不会把 `192.0.2.1` 放进返回值或异常文本。测试数据只用 `192.0.2.1` 这种文档地址。

Mac 上在二进制和哈希写好之后运行：

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests
```

全部通过才算完成。另外做一次直连探测，不作为单元测试提交进仓库：父进程设置 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 指向 `127.0.0.1` 上一个没有进程监听的端口，然后用内置 curl 访问 `https://hf-mirror.com/`。退出码是 0，且那个端口的连接数是 0。不要把响应头里的跳转地址写进提交或方案。

可选的小文件冒烟：用命令行下载一个公开小模型到仓库已忽略的 `downloads/` 或系统临时目录，确认退出码 0，然后删掉该目录。不要把模型、`state.json` 或下载目录提交进来。无图形环境跳过界面；有图形时打开窗口，确认字体和输入框在，再关掉。

Windows 上在改完平台分支之后再跑同一组单元测试，确认现有 12 个测试仍然通过。Mac 上的机器不代替这次 Windows 回归；提交说明里写明哪一边实际跑过。

## 完成标准

1. `dev/macos-arm64` 上有签名后的 arm64 curl，`PIN.txt` 与 `curlsafe.py` 中的 SHA256 一致，`otool -L` 没有第三方库。
2. Windows 仍只加载原来的 `curl.exe` 和 `libcurl-x64.dll`。
3. 代理被污染时的单元测试在 Mac 上是 0 次连接；两段 Range 仍是一次 TCP accept。
4. README 能让人在 Apple 芯片的 Mac 上用命令行启动，并说明图形界面需要带 Tk 的 Python。
5. 提交里没有用户名、主目录、密钥、代理地址和签名 CDN 地址。
6. 分支已推到现有 SSH 远端。`master` 保持不动。
