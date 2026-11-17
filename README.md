# hf-mirror-dl

直连 [hf-mirror](https://hf-mirror.com/) 下载 Hugging Face 模型。程序自带一份固定的 curl，不使用系统代理，也不继承 `HTTP_PROXY` 这类环境变量。

模型会保存到：

```text
download_home/provider/model_name/
```

## 为什么自带 curl

系统里的 curl 版本不固定。有的会读取环境变量，有的会读取 `%USERPROFILE%\_curlrc`，PowerShell 里的 `curl` 还可能是 `Invoke-WebRequest` 的别名，那个别名会走系统代理。hf-mirror 会拒绝代理流量，走代理也会消耗代理额度。

因此这里只调用仓库里的这一份：

- 版本：curl **8.23.0**，构建 **8.23.0_9**（2026-10-14）
- 来源：<https://curl.se/windows/dl-8.23.0_9/curl-8.23.0_9-win64-mingw.zip>
- 压缩包 SHA256：`89cb71930f4242d884e8e349463455932ab1d089b20f4f99318261a3cbff9167`
- 文件：`third_party/curl/curl.exe` 和同目录的 `libcurl-x64.dll`

启动时会核对这两个文件的 SHA256，不一致就拒绝运行。不要改成系统 curl，也不要删掉旁边的 DLL。

Apple 芯片的 Mac 使用另一份同样版本的 curl，放在 `third_party/curl/macos-arm64/curl`。它在 MacOSX 15.5 SDK 上静态编译。curl 8.15 起已经去掉 Secure Transport，所以这份构建静态链接 LibreSSL 4.4.1（与 Windows 上那份官方 curl 相同），证书校验使用系统钥匙串（Apple SecTrust）。启动时同样核对 SHA256。Intel Mac 和 Rosetta 下的 Python 会直接拒绝运行。不要改用系统里的 curl。

每次调用都同时做这几件事：

1. 第一个参数是 `-q`，不读 curlrc。
2. `--noproxy *`，所有主机都不走代理。
3. `--proxy ""`，显式关掉代理。
4. 子进程环境是白名单，不包含任何名字里带 proxy 的变量，也不包含 `CURL_HOME`、`USERPROFILE`。
5. `--proto =https`，正式下载只允许 HTTPS。CDN 跳转也限制为 HTTPS。
6. 初始地址只能是 `https://hf-mirror.com`。大文件会由镜像 302 到它的 CDN，这一跳仍由同一个无代理的 curl 完成。

程序不会调用 Python 的 `urllib`、`requests` 或系统里的 curl。Git 自己的 `http.proxy` 也不会被用到，因为这里不用 git 下载。

## 带宽

`--bandwidth-mbps` 的单位是 **Mbps（兆比特/秒）**。默认 **300**，也就是大约 37.5 MB/s。

这是所有连接加在一起的上限，由一个共享的令牌桶执行。单条连接不再单独设限，避免慢连接占着额度、快连接却不能用。验收时允许短时间波动。

hf-mirror 的 CDN 对单条连接很慢，所以要靠很多条连接叠速度。大文件按 Range 切开，每段最大 128 MiB。同一个 curl 进程会连续拉取最多 4 段，好让 TCP 连接复用，而不是每段都重新握手。程序先打开最多 8 条连接；如果总速度还低于上限，并且不是令牌桶在限速，就继续加连接，直到 `-c` 给出的上限。明显慢于其它连接的那一条会被关掉，这段改由新连接重下，同一段最多这样换 2 次。

## 图形界面

Windows：

```bat
hfmd-gui.bat
```

Apple 芯片的 Mac：

```sh
./hfmd-gui.command
```

或：

```bat
python -m hfmd gui
```

Mac 上的图形界面需要带 Tk 的 Python 3.11 或更新版本。python.org 提供的 macOS 安装包带 Tk。没有 Tk 时，命令行仍然可用。

填写模型名（`provider/model_name`）、并发数、总带宽和保存目录。Token 和修订版本可以留空。

## 命令行

给其它 agent 用：

```bat
python -m hfmd download Qwen/Qwen2.5-7B-Instruct -c 32 -b 300 -d D:\models
```

Apple 芯片的 Mac：

```sh
python3 -m hfmd download Qwen/Qwen2.5-7B-Instruct -c 32 -b 300 -d ~/models
```

参数：

| 参数 | 含义 |
| --- | --- |
| `model` | `provider/model_name` |
| `-c` / `--concurrency` | 并发上限，1 到 128。实际连接数从少量加到这个值 |
| `-b` / `--bandwidth-mbps` | 总带宽，默认 300 Mbps |
| `-d` / `--download-home` | 保存根目录 |
| `--revision` | 默认 `main` |
| `--token` | 可选。gated 模型要先在 Hugging Face 官网申请许可 |
| `--json` | 每行输出一个 JSON 进度 |

成功时最后一行是：

```text
done path=D:\models\Qwen\Qwen2.5-7B-Instruct
```

退出码：`0` 成功，`1` 下载失败，`2` 参数错误，`130` 取消。

中断后重新执行同一条命令会续传。已完成且大小正确的文件会跳过。带 LFS sha256 的文件会在写完后校验。

## 测试

```bat
python -m unittest discover -s tests
```

Apple 芯片的 Mac 上把 `python` 换成 `python3`。

测试在本机起一个假的镜像和一个会记录连接的代理，确认代理连接数是 0，并检查总带宽没有明显超过设定值。

## 许可证

本仓库的代码使用 [MIT License](LICENSE)。

`third_party/curl/` 里的 curl 8.23.0 不是本仓库的代码，它保留上游的 curl 许可，见 `third_party/curl/COPYING.txt`。
