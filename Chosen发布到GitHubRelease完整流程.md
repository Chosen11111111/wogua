# Chosen 发布到 GitHub Release 完整流程

本文记录 Chosen 软件包从本地 ZIP 到 R2、GitHub Release 和 manifest 的完整流程，明确 ZIP、R2 对象、Git 仓库和 Release 附件之间的边界。

## 本次 2.10 软件包 R2 发布补充

软件包对象和地址：

- R2 对象：wogua/Chosen2.10.zip
- R2 公网地址：https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip
- 主下载地址：R2 公网地址。
- 备用下载地址：v4、cdn、v6 GitHub Release 代理，均指向 v2.10 的同一个附件。

发布顺序：双击 publish_software_release.bat，选择 ZIP 后由窗口后台执行 publish()：校验 ZIP、检查或上传 R2 对象，确认 CDN Range bytes=0-1048575 返回 206 并检查 ZIP 头；再检查 GitHub Release 和 tag，不存在则创建 Release、上传同一个 ZIP 并核对资产状态、大小和 digest；随后测试 GitHub v4 代理，最后才写入并推送 R2-first software-manifest.json，再核对远端 raw manifest。脚本只打印包元数据，不打印 R2 密钥或其他凭据。

从 wogua 目录双击：

~~~text
publish_software_release.bat
~~~

窗口操作顺序：

1. 点击“选择 ZIP”，工具立即校验 ZIP 并显示版本、大小和 SHA-256。
2. 点击“开始发布”，后台线程按完整流程执行并在日志窗口显示进度。
3. 发布成功后窗口提示完成；失败时停止当前流程，不自动覆盖已有 Release 或 tag。

命令行模式仍可使用：

~~~powershell
..\Chosen\.venv\Scripts\python.exe publish_software_release.py Chosen2.10.zip
~~~

ZIP 不加入 Git。客户端最终先访问 R2，R2 不可用时按 manifest 中的 GitHub 代理备用地址下载。最终安装、退出、重启和升级结果由发布人手工验证。

## 1. 发布对象

一次软件发布包含两个云端对象：

1. GitHub Release 附件：真正供用户下载的 ZIP。
2. 软件 manifest：告诉客户端当前版本、下载地址、文件大小和 SHA-256。

本次仓库与文件：

- GitHub 仓库：https://github.com/Chosen11111111/wogua
- 本地仓库：E:/ChosenSkin2.0/wogua
- 发布目录：E:/ChosenSkin2.0/ChosenreleaseNew/Chosen2.10
- 本地 ZIP：E:/ChosenSkin2.0/wogua/Chosen2.10.zip
- manifest：E:/ChosenSkin2.0/wogua/software-manifest.json
- Release tag：v2.10

> 重要：ZIP 不加入 Git 历史。GitHub 普通仓库单个文件上限是 100 MB；大于 100 MB 的软件包必须作为 GitHub Release 附件上传。manifest 加入 Git，ZIP 上传 Release。

## 2. 前置条件

发布前确认：

- ZIP 已由正确版本的发布目录生成，不是临时测试包。
- ZIP 文件名包含正确版本号，例如 Chosen2.10.zip。
- ZIP 内部版本文件是 _internal/version.json。
- 发布包已经完成既定裁剪和隐私清理。
- 包内没有账号、token、密码、用户配置、开发机路径、日志、.git 或额外 ZIP。
- Git Credential Manager 已保存 GitHub token，并有目标仓库的推送和 Release 权限。
- 发布版本、Release tag、manifest 和 ZIP 内容使用同一个版本号。

检查 GitHub 登录、仓库、分支：

```powershell
cd E:/ChosenSkin2.0/wogua
git remote -v
git branch --show-current
git status --short
```

推荐在 main 分支发布，并确认 remote 指向正确的仓库。

## 3. 发布目录和 ZIP

发布目录根部必须直接包含：

```text
Chosen.exe
ChosenUpdater.exe
_internal/
Chosendata/
```

错误结构是多套一层版本目录：

```text
Chosen2.10/Chosen2.10/Chosen.exe
```

如果 ZIP 尚未生成，可以使用项目既有发布流程生成。示例：

```powershell
$source = 'E:/ChosenSkin2.0/ChosenreleaseNew/Chosen2.10'
$zip = 'E:/ChosenSkin2.0/wogua/Chosen2.10.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $source '*') -DestinationPath $zip -CompressionLevel Optimal
```

如果 ZIP 已经准备好，则不要在云端发布步骤中重新压缩，直接校验该 ZIP。

## 4. 校验本地 ZIP

### 4.1 文件存在、大小和 SHA-256

```powershell
cd E:/ChosenSkin2.0/wogua
if (-not (Test-Path Chosen2.10.zip)) { throw 'Chosen2.10.zip not found' }
$zip = Get-Item Chosen2.10.zip
$hash = Get-FileHash Chosen2.10.zip -Algorithm SHA256
Write-Output ('size=' + $zip.Length)
Write-Output ('sha256=' + $hash.Hash.ToLower())
```

本次 2.10 实际值：

- size：219176976 字节
- SHA-256：82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53

### 4.2 ZIP 路径安全

ZIP 内部路径不能：

- 以盘符开头，例如 C:/。
- 以 / 或反斜杠开头。
- 包含 ../ 或路径穿越。
- 包含符号链接、设备文件或不允许的特殊文件。
- 多套一层版本目录。

检查示例：

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path Chosen2.10.zip))
try {
    $bad = @($archive.Entries | Where-Object {
        $name = $_.FullName.Replace('\', '/')
        $name.StartsWith('/') -or
        $name -match '^[A-Za-z]:/' -or
        $name -match '(^|/)\.\.(/|$)'
    })
    if ($bad.Count -gt 0) {
        throw ('Unsafe ZIP paths: ' + (($bad | ForEach-Object FullName) -join '; '))
    }
    Write-Output ('zip_path_safety=passed entries=' + $archive.Entries.Count)
}
finally { $archive.Dispose() }
```

### 4.3 发布包隐私

对解压后的发布目录执行扫描。禁止出现：

- auth_token、saved_password、saved_username。
- ChosenSkinCache。
- 开发机 LeaguePath 或本机绝对路径。
- logs、datastore、crash_*、*.log、*.bak*。
- .git。
- 除 PyInstaller 必需的 base_library.zip 外的额外 ZIP。

示例：

```powershell
$root = 'E:/ChosenSkin2.0/ChosenreleaseNew/Chosen2.10'
$textFiles = @(Get-ChildItem $root -Recurse -File -Force | Where-Object { $_.Extension -in @('.json','.ini','.cfg','.conf','.txt') })
$leaks = @($textFiles | Select-String -Pattern 'auth_token|saved_password|saved_username|ChosenSkinCache|LeaguePath=|E:\\WeGameApps')
if ($leaks.Count -gt 0) { throw ('Sensitive text found: ' + (($leaks | ForEach-Object Path) -join '; ')) }
Write-Output 'package_privacy=passed'
```

## 5. 生成和核对 manifest

manifest 字段：

| 字段 | 含义 |
| --- | --- |
| version | 客户端要升级到的版本 |
| download_url | 主下载地址，指向 R2 公网对象 |
| download_url_backup | 备用下载地址列表，指向同一个附件 |
| size | ZIP 实际字节数 |
| sha256 | ZIP 的小写 SHA-256 |
| release_tag | GitHub Release tag |

2.10 manifest：

```json
{
  "version": "2.10",
  "download_url": "https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip",
  "download_url_backup": [
    "https://v4.gh-proxy.org/https://github.com/Chosen11111111/wogua/releases/download/v2.10/Chosen2.10.zip",
    "https://cdn.gh-proxy.org/https://github.com/Chosen11111111/wogua/releases/download/v2.10/Chosen2.10.zip",
    "https://v6.gh-proxy.org/https://github.com/Chosen11111111/wogua/releases/download/v2.10/Chosen2.10.zip"
  ],
  "size": 219176976,
  "sha256": "82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53",
  "release_tag": "v2.10"
}
```

每次发布必须重新计算，不要复制旧版本的 size 或 sha256。核对命令：

```powershell
$manifest = Get-Content software-manifest.json -Raw | ConvertFrom-Json
$zip = Get-Item Chosen2.10.zip
$hash = (Get-FileHash Chosen2.10.zip -Algorithm SHA256).Hash.ToLower()
if ($manifest.version -ne '2.10') { throw 'Manifest version mismatch' }
if ($manifest.release_tag -ne 'v2.10') { throw 'Manifest release tag mismatch' }
if ($manifest.size -ne $zip.Length) { throw 'Manifest size mismatch' }
if ($manifest.sha256 -ne $hash) { throw 'Manifest SHA-256 mismatch' }
if ($manifest.download_url -ne 'https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip') { throw 'Manifest URL mismatch' }
if (@($manifest.download_url_backup).Count -lt 1) { throw 'Manifest backup URLs are missing' }
Write-Output 'manifest_match=passed'
```

## 6. 准备 manifest 的 Git 提交

推荐发布顺序是：先上传并核对 R2 对象，再创建并核对 GitHub Release，最后提交并推送 R2-first manifest，再核对远端 manifest。这样不会让客户端先看到一个尚未存在的下载对象。

### 6.1 ZIP 不加入 Git

不要执行 git add Chosen2.10.zip。

正确边界：

- manifest：加入 Git、提交、推送。
- ZIP：留在本地，上传到 Release。

如果误加入暂存区：

```powershell
git rm --cached Chosen2.10.zip
```

如果误进入尚未推送的本地提交：

```powershell
git rm --cached Chosen2.10.zip
git commit --amend --no-edit
```

这会保留本地 ZIP，不会删除它。

### 6.2 准备暂存内容（Release 验证后提交）

先只准备暂存区，不要在 Release 附件验证前 commit 或 push：

```powershell
cd E:/ChosenSkin2.0/wogua
git status --short
git add software-manifest.json
git diff --cached --check
git diff --cached -- software-manifest.json
# 等第 8.1 节确认 Release 附件后，再执行 commit 和 push
```

提交前看到 ?? Chosen2.10.zip 是正常的，表示 ZIP 保留在本地但未加入 Git。

本次 manifest 提交：

ac2ad44 release: publish Chosen 2.10 package

## 7. 创建 GitHub Release 并上传 ZIP

脚本会自动完成以下步骤；命令行仅作为手动排障备用：

1. 查询 Release 和 tag，任一已存在就停止，避免覆盖。
2. 创建 Release。
3. 上传同一个本地 ZIP，不重新压缩。
4. 核对 uploaded、size 和 SHA-256 digest。

手动查看 Release：

```powershell
gh release view v2.10 --repo Chosen11111111/wogua
```

如果返回 release not found，创建 Release 并上传附件：

```powershell
gh release create v2.10 Chosen2.10.zip --repo Chosen11111111/wogua --title "Chosen 2.10" --notes "Chosen 2.10 release package."
```

这个命令会：

1. 创建或发布 tag v2.10。
2. 创建 GitHub Release。
3. 上传本地 ZIP 为 Chosen2.10.zip 附件。
4. 生成下载地址：https://github.com/Chosen11111111/wogua/releases/download/v2.10/Chosen2.10.zip。

如果 Release 或同名附件已经存在，先查看第 8 节的 JSON，不要盲目重复上传。

## 8. 上传后远端核对

### 8.1 核对 Release 附件

```powershell
gh release view v2.10 --repo Chosen11111111/wogua --json tagName,name,url,assets
```

必须确认：

- tagName 是 v2.10。
- 附件名称是 Chosen2.10.zip。
- 附件状态是 uploaded。
- 附件 size 等于本地 ZIP 字节数。
- GitHub asset digest 等于本地 SHA-256。
- 附件 URL 的 tag 和文件名正确。

本次实际结果：

- Release：https://github.com/Chosen11111111/wogua/releases/tag/v2.10
- 附件大小：219176976 字节
- GitHub digest：sha256:82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53
- 状态：uploaded

### 8.2 Release 验证后提交并推送 manifest

确认第 8.1 节的 Release 附件名称、大小、digest 和 uploaded 状态全部正确后，执行：

```powershell
git add software-manifest.json
git diff --cached --check
git diff --cached -- software-manifest.json
git commit -m "release: publish Chosen 2.10 package"
git push origin main
```

如果 commit 或 push 失败，停止后续客户端发布，不要让文档记录为完成。

### 8.3 核对远端 manifest

```powershell
$url = 'https://raw.githubusercontent.com/Chosen11111111/wogua/main/software-manifest.json'
$remote = (Invoke-WebRequest -UseBasicParsing $url).Content | ConvertFrom-Json
Write-Output ('version=' + $remote.version)
Write-Output ('release_tag=' + $remote.release_tag)
Write-Output ('size=' + $remote.size)
Write-Output ('sha256=' + $remote.sha256)
Write-Output $remote.download_url
```

远端值必须与本地 ZIP 和 Release 附件一致。本次远端值：

```text
version=2.10
release_tag=v2.10
size=219176976
sha256=82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53
```

### 8.4 同步并核对业务 manifest 服务

GitHub raw manifest 和业务 manifest 服务是两个层次，不能默认认为它们会自动同步。客户端实际读取哪个地址，必须以 Chosen 发布配置为准。

本次业务 manifest 地址：

- 主地址：https://skins.52015002.xyz/updates/software-manifest.json
- 备用地址：https://160.202.238.53:18221/updates/software-manifest.json

如果业务服务是自动从 GitHub 同步，等待同步完成后分别请求两个地址；如果业务服务是手动部署，则把同一份 software-manifest.json 发布到主、备用服务，再分别请求核对。禁止手工改出与 GitHub Release 不同的 size、sha256、tag 或下载 URL。

检查命令：

```powershell
$urls = @(
  'https://skins.52015002.xyz/updates/software-manifest.json',
  'https://160.202.238.53:18221/updates/software-manifest.json'
)
foreach ($url in $urls) {
    $remote = (Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 20).Content | ConvertFrom-Json
    if ($remote.version -ne '2.10' -or
        $remote.release_tag -ne 'v2.10' -or
        $remote.size -ne 219176976 -or
        $remote.sha256 -ne '82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53') {
        throw ('Hosted manifest mismatch: ' + $url)
    }
    Write-Output ('hosted_manifest_match=passed url=' + $url)
}
```

如果某个地址连接失败、证书失败或返回旧版本，不能把它标记为已同步。先修复服务或保留旧版本 manifest，完成后再让客户端使用新版本。

本次实际结果：

- 主地址返回 HTTP 200，内容与 2.10 Release 一致。
- 备用地址当前 TLS 信任链校验失败，不能据此确认备用服务已同步；该项仍需发布人处理或验证。

### 8.5 可选下载复核

网络条件允许时，下载 Release 附件到临时目录，再计算下载文件 SHA-256。下载文件、本地 ZIP、GitHub asset digest 和 manifest.sha256 必须一致。临时文件不要加入 Git。

## 9. 客户端更新链路

客户端实际链路：

```text
客户端
  -> 软件 manifest 主地址
  -> manifest.download_url（R2 公网对象）
  -> R2 对象 Chosen2.10.zip
  -> 失败时按 download_url_backup 使用 GitHub Release 代理
  -> GitHub Release 附件 Chosen2.10.zip
  -> SHA-256 校验
  -> 安装软件文件
```

ZIP 下载顺序：R2 主地址；R2 不可用时依次使用 v4.gh-proxy.org、cdn.gh-proxy.org、v6.gh-proxy.org。三条代理地址最终都必须指向同一个 Release asset。

客户端安装前应检查版本、HTTPS、SHA-256、ZIP 路径安全、包结构和失败回滚行为。

## 10. 发布后的保留和清理

应保留：

- GitHub Release 及其 ZIP 附件。
- Git 仓库中的 software-manifest.json。
- 本地发布目录，直到人工验收完成。
- 本地 ZIP，直到远端附件和 manifest 核对完成。

不要提交：

- Chosen2.10.zip。
- 构建目录、临时目录和备份目录。
- 用户配置、token、密码和日志。

ZIP 保持未跟踪状态是正常的。不要为了清理 git status 删除它。

## 11. 失败处理

### ZIP 不存在

停止流程，生成或复制正确 ZIP；不要创建空 Release，也不要用旧包冒充新版本。

### manifest 不一致

停止提交和上传，重新计算 size 和 SHA-256，修正后再核对。

### GitHub 拒绝大文件

如果提示 exceeds GitHub's file size limit of 100.00 MB：

1. 用 git rm --cached 移出 ZIP。
2. 用 git commit --amend --no-edit 修正尚未推送的提交。
3. 只推送 manifest。
4. 用 gh release create 上传 ZIP。

### manifest 已推送但 Release 失败

客户端可能看到新版本却下载不到附件。先完成 Release 并核对附件，不要让 manifest 长时间指向不存在的 Release。

### Release 已上传但 manifest 未更新

客户端仍会看到旧版本，这是可接受的安全状态。先核对 Release，再提交 manifest。

### 远端 manifest 不是最新

检查仓库、分支、推送结果、raw URL 分支和是否有后续提交覆盖 manifest。

## 12. 发布完成检查清单

### 本地 ZIP

- [ ] 文件存在。
- [ ] 文件名和版本正确。
- [ ] ZIP 大小已计算。
- [ ] ZIP SHA-256 已计算。
- [ ] ZIP 路径安全检查通过。
- [ ] 包内容无隐私和开发机路径。
- [ ] ZIP 没有加入 Git。

### manifest

- [ ] version 正确。
- [ ] release_tag 正确。
- [ ] size 等于 ZIP 字节数。
- [ ] sha256 等于 ZIP SHA-256。
- [ ] 主 URL 指向 R2 正确对象和文件名。
- [ ] 备用 URL 指向同一附件。
- [ ] 本地一致性检查通过。

### Git

- [ ] 只提交 manifest。
- [ ] commit message 清楚。
- [ ] 推送到了正确的 main 分支。
- [ ] 远端 commit 可查询。
- [ ] ZIP 作为未跟踪本地文件保留。

### GitHub Release

- [ ] tag 正确。
- [ ] Release 标题正确。
- [ ] ZIP 附件名称正确。
- [ ] 附件状态为 uploaded。
- [ ] 附件大小正确。
- [ ] GitHub digest 与本地 SHA-256 一致。
- [ ] Release 下载 URL 正确。

### 远端一致性

- [ ] 远端 raw manifest 已更新。
- [ ] 远端 version、tag、size、sha256 正确。
- [ ] 远端 URL 与 Release 附件一致。
- [ ] 主 manifest 服务最终返回同一份数据。
- [ ] R2/CDN HEAD 和 bytes=0-1048575 返回值已核对，GitHub v4 代理 Range 也已测试。
- [ ] 发布人完成手工冒烟测试。

## 13. 本次 2.10 发布记录

- ZIP：Chosen2.10.zip
- size：219176976 字节
- SHA-256：82c79e702ec10c73ff05acd52cd61d8eb7a8db33fc3435fdd335fd17c3001b53
- Git commit：ac2ad44 release: publish Chosen 2.10 package
- Release tag：v2.10
- Release 页面：https://github.com/Chosen11111111/wogua/releases/tag/v2.10
- Release asset：https://github.com/Chosen11111111/wogua/releases/download/v2.10/Chosen2.10.zip
- 结果：Release 附件、远端 manifest 和本地 ZIP 已核对一致。
