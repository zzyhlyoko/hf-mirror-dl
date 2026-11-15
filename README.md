# hfmd

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

这是所有并发连接加在一起的上限。每个连接还有一份按并发数均分的 curl `--limit-rate`，共享的令牌桶再压住同时突发。验收时允许短时间波动。

hf-mirror 的 CDN 对单条连接有速度上限，所以用并发把多条连接叠起来。大文件会按 Range 切成多段，一起下载。

## 图形界面

```bat
hfmd-gui.bat
```

或：

```bat
python -m hfmd gui
```

填写模型名（`provider/model_name`）、并发数、总带宽和保存目录。Token 和修订版本可以留空。

## 命令行

给其它 agent 用：

```bat
python -m hfmd download Qwen/Qwen2.5-7B-Instruct -c 8 -b 300 -d D:\models
```

参数：

| 参数 | 含义 |
| --- | --- |
| `model` | `provider/model_name` |
| `-c` / `--concurrency` | 并发连接数，1 到 64 |
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

测试在本机起一个假的镜像和一个会记录连接的代理，确认代理连接数是 0，并检查总带宽没有明显超过设定值。
